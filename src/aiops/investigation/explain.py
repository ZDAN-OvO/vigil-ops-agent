"""只读追问：只依据已存的调查记录回答，不取证、不执行、不改结论。"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field

from aiops.contracts import AuditEvent, Evidence, Hypothesis, Investigation


class ExplainError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class Explanation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    answer: str = Field(min_length=1)
    citations: list[str]
    missing: list[str]
    needs_new_query: bool


class ModelProtocol(Protocol):
    async def complete_json[T: BaseModel](
        self,
        messages: list[dict[str, str]],
        response_model: type[T],
        *,
        schema_name: str = "response",
        temperature: float | None = None,
    ) -> Any: ...


SYSTEM_PROMPT = """你只回答对已有调查记录的追问，不做新的取证。

规则：
1. 只能使用下面给出的调查记录。不要补充记录里没有的事实，也不要猜测。
2. 引用记录时在 citations 里写 evidence id；没有对应记录就不要引用。编造的 id 会被拒绝。
3. 记录不足以回答时，在 missing 里写清缺什么，并把 needs_new_query 设为 true，不要编造答案。
4. 不提出新的工具调用，也不建议执行任何动作；可以说明需要哪些证据才能回答。
5. 回答用简体中文，直接回应问题，不要复述整份记录。
"""


@dataclass(frozen=True)
class ExplainResult:
    investigation_id: str
    answer: str
    citations: list[str]
    missing: list[str] = field(default_factory=list)
    needs_new_query: bool = False
    model: str = ""


class StoreProtocol(Protocol):
    async def get_investigation(self, investigation_id: str) -> Investigation | None: ...
    async def get_incident(self, incident_id: str) -> Any: ...
    async def list_hypotheses(self, investigation_id: str) -> list[Hypothesis]: ...
    async def list_evidence(self, investigation_id: str) -> list[Evidence]: ...
    async def append_audit(self, events: list[AuditEvent]) -> None: ...


class ExplainService:
    def __init__(self, *, store: StoreProtocol, model: ModelProtocol) -> None:
        self._store = store
        self._model = model

    async def answer(self, investigation_id: str, question: str) -> ExplainResult:
        if not question.strip():
            raise ExplainError("invalid_argument", "问题不能为空")
        investigation = await self._store.get_investigation(investigation_id)
        if investigation is None:
            raise ExplainError("not_found", f"调查不存在：{investigation_id}")
        status = getattr(investigation.status, "value", str(investigation.status))
        if status != "completed":
            raise ExplainError("not_completed", f"调查状态为 {status}，只能追问已完成的调查")
        incident = await self._store.get_incident(investigation.incident_id)
        hypotheses = await self._store.list_hypotheses(investigation_id)
        evidence = await self._store.list_evidence(investigation_id)
        if not evidence:
            raise ExplainError("no_evidence", "该调查没有记录任何证据，无法追问")

        await self._audit(investigation_id, "explain.requested", {"question": question[:500]})
        payload = {
            "investigation": {
                "id": investigation.id,
                "status": investigation.status.value,
                "conclusion": investigation.conclusion,
                "termination_reason": investigation.termination_reason,
            },
            "incident": {
                "service": getattr(incident, "service", None),
                "symptoms": getattr(incident, "symptoms", None),
                "context": getattr(incident, "context", []),
            },
            "hypotheses": [
                {"id": item.id, "statement": item.statement, "status": item.status}
                for item in hypotheses
            ],
            "evidence": [
                {
                    "id": item.id,
                    "source": item.source,
                    "relation": item.relation,
                    "observation": item.observation[:1500],
                }
                for item in evidence
            ],
            "question": question,
        }
        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False, default=str)},
        ]
        try:
            call = await self._model.complete_json(messages, Explanation, schema_name="explanation")
        except Exception as exc:  # noqa: BLE001 - 模型失败按错误上报，不写结论
            raise ExplainError("model_failed", exc.__class__.__name__) from exc

        visible = {item.id for item in evidence}
        unknown = sorted({citation for citation in call.value.citations if citation not in visible})
        if unknown:
            await self._audit(investigation_id, "explain.rejected", {"unknown_citations": unknown})
            raise ExplainError("unknown_citation", f"引用了不在记录中的证据：{','.join(unknown)}")
        # 有引用的回答可以附带边界说明；没有引用又声明缺信息时，必须同时要求新取证。
        if not call.value.citations and call.value.missing and not call.value.needs_new_query:
            await self._audit(
                investigation_id, "explain.rejected", {"reason": "missing_without_new_query"}
            )
            raise ExplainError(
                "inconsistent_answer", "没有任何引用却声明缺信息时，必须要求新的取证"
            )
        if not call.value.citations and not call.value.missing:
            await self._audit(
                investigation_id, "explain.rejected", {"reason": "unsupported_answer"}
            )
            raise ExplainError("unsupported_answer", "回答既没有引用证据也没有说明缺什么")

        result = ExplainResult(
            investigation_id=investigation_id,
            answer=call.value.answer,
            citations=list(dict.fromkeys(call.value.citations)),
            missing=list(call.value.missing),
            needs_new_query=call.value.needs_new_query,
            model=getattr(call, "model", ""),
        )
        await self._audit(
            investigation_id,
            "explain.answered",
            {
                "citations": result.citations,
                "needs_new_query": result.needs_new_query,
                "model": result.model,
            },
        )
        return result

    async def _audit(self, investigation_id: str, event: str, payload: dict[str, Any]) -> None:
        await self._store.append_audit(
            [
                AuditEvent(
                    id=f"audit-{uuid.uuid4().hex[:12]}",
                    correlation_id=investigation_id,
                    actor="cli",
                    event=event,
                    occurred_at=datetime.now(UTC),
                    payload=payload,
                )
            ]
        )
