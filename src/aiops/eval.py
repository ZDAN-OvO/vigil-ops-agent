"""评估清单：冻结场景与逐次运行记录，按固定规则汇总 MVP 门槛。

清单是人工维护的版本化记录，评分不读取注入真值来选择动作，只核对已登记的事实。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, model_validator

from aiops.actions.executor import proposal_hash
from aiops.actions.policy import decide
from aiops.investigation.snapshot import state_snapshot


class ScenarioRun(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_id: str
    valid: bool
    reason: str
    investigation_id: str = ""
    proposal_id: str = ""
    attempt_id: str = ""
    recovered: bool = False
    investigation_correct: bool = False


class Scenario(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    version: int
    card: str
    signal_classes: list[str] = Field(min_length=1)
    allowed_actions: list[str]
    core: bool = True
    runs: list[ScenarioRun]


class BaselineRun(BaseModel):
    """V09 的规则基线用例；同场景、同资源、同预算、同授权边界下的独立运行。"""

    model_config = ConfigDict(extra="forbid")

    scenario: str
    run_id: str
    investigation_id: str = ""
    rules_version: str = ""
    agent_run_ids: list[str] = Field(default_factory=list)
    compared: list[str] = Field(default_factory=list)
    limitations: list[str] | None = None


class SafetyRun(BaseModel):
    """V06 的独立异常用例；动作类要有关联提案、执行记录和待办。"""

    model_config = ConfigDict(extra="forbid")

    run_id: str
    expected: Literal["no_improvement", "worsened", "unknown", "insufficient_evidence"]
    proposal_id: str = ""
    attempt_id: str = ""
    investigation_id: str = ""
    evidence: str = ""


class StopGateRun(BaseModel):
    """失败之后的下一次提案必须转人工，且不能有执行记录。"""

    model_config = ConfigDict(extra="forbid")

    run_id: str
    failed_proposal_id: str
    next_proposal_id: str


class ApprovalRun(BaseModel):
    """V05 的审批用例；resumed 要跨命令恢复并执行，expired 不许留下执行记录。"""

    model_config = ConfigDict(extra="forbid")

    run_id: str
    expected: Literal["resumed", "expired", "rejected"]
    proposal_id: str
    attempt_id: str = ""
    evidence: str = ""
    markers: list[str] = Field(default_factory=list)


class IdempotencyRun(BaseModel):
    """V07 的重复审批／重复继续用例，按审批与执行记录条数核对。"""

    model_config = ConfigDict(extra="forbid")

    run_id: str
    kind: Literal["approval", "resume"]
    proposal_id: str
    expected_approvals: int = Field(ge=0)
    expected_attempts: int = Field(ge=0, le=1)
    evidence: str = ""
    markers: list[str] = Field(default_factory=list)


class DeduplicationRun(BaseModel):
    """V07 的告警去重用例；同一指纹在窗口内只能有一个事件。"""

    model_config = ConfigDict(extra="forbid")

    run_id: str
    incident_id: str
    investigation_id: str
    fingerprint: str
    window_minutes: int = Field(gt=0)
    evidence: str = ""
    markers: list[str] = Field(default_factory=list)


class TraceRun(BaseModel):
    """V07 的原始观测回查用例；证据文件和工具调用都要能落到实际产物。"""

    model_config = ConfigDict(extra="forbid")

    run_id: str
    investigation_id: str
    evidence: str = ""
    markers: list[str] = Field(default_factory=list)


class ContextRun(BaseModel):
    """V08 的背景核实用例；Context 中的说法要能被假设与反证记录核对。"""

    model_config = ConfigDict(extra="forbid")

    run_id: str
    investigation_id: str
    context_hint: str
    refuted_hypothesis: str
    evidence: str = ""
    markers: list[str] = Field(default_factory=list)


class RunbookRun(BaseModel):
    """V08 的 runbook 用例；引用要能回查条目，且不进入动作证据。"""

    model_config = ConfigDict(extra="forbid")

    run_id: str
    investigation_id: str
    accepted: str
    rejected: list[str] = Field(default_factory=list)
    proposal_id: str = ""
    evidence: str = ""
    markers: list[str] = Field(default_factory=list)


class ExplainRun(BaseModel):
    """V08 的只读追问用例；引用必须落在已有证据上，追问后不新增取证或动作。"""

    model_config = ConfigDict(extra="forbid")

    run_id: str
    investigation_id: str
    evidence: str = ""
    markers: list[str] = Field(default_factory=list)


class Manifest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    frozen_at: str
    scenarios: list[Scenario] = Field(min_length=1)
    baseline: list[BaselineRun] = Field(default_factory=list)
    safety_runs: list[SafetyRun] = Field(default_factory=list)
    stop_gate: list[StopGateRun] = Field(default_factory=list)
    approval_runs: list[ApprovalRun] = Field(default_factory=list)
    idempotency_runs: list[IdempotencyRun] = Field(default_factory=list)
    deduplication_runs: list[DeduplicationRun] = Field(default_factory=list)
    trace_runs: list[TraceRun] = Field(default_factory=list)
    context_runs: list[ContextRun] = Field(default_factory=list)
    runbook_runs: list[RunbookRun] = Field(default_factory=list)
    explain_runs: list[ExplainRun] = Field(default_factory=list)
    evidence_root: str = "runtime"
    demo_packs: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def scenario_ids_are_unique(self) -> Manifest:
        identifiers = [item.id for item in self.scenarios]
        if len(identifiers) != len(set(identifiers)):
            raise ValueError("场景 id 重复")
        return self


@dataclass(frozen=True)
class CriterionResult:
    id: str
    passed: bool
    detail: str


@dataclass(frozen=True)
class EvaluationReport:
    frozen_at: str
    criteria: list[CriterionResult] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return all(item.passed for item in self.criteria)


def _window_seconds(incident: Any) -> float | None:
    time_range = getattr(incident, "time_range", None)
    if time_range is None:
        return None
    return (time_range.end - time_range.start).total_seconds()


class StoreProtocol(Protocol):
    async def get_investigation(self, investigation_id: str) -> Any: ...
    async def get_proposal_disposition(self, proposal_id: str) -> str | None: ...
    async def get_proposal(self, proposal_id: str) -> Any: ...
    async def get_attempt(self, proposal_id: str) -> Any: ...
    async def get_followup_by_attempt(self, attempt_id: str) -> Any: ...
    async def count_proposals_for_investigation(self, investigation_id: str) -> int: ...
    async def get_approval(self, proposal_id: str) -> Any: ...
    async def count_approvals(self, proposal_id: str) -> int: ...
    async def list_audit(self, correlation_id: str) -> list[Any]: ...
    async def get_incident(self, incident_id: str) -> Any: ...
    async def get_incident_created_at(self, incident_id: str) -> Any: ...
    async def list_incident_receipts(self, fingerprint: str) -> list[Any]: ...
    async def list_proposals_for_investigation(self, investigation_id: str) -> list[Any]: ...
    async def list_incidents(self, limit: int = 20) -> list[Any]: ...
    async def list_hypotheses(self, investigation_id: str) -> list[Any]: ...
    async def list_evidence(self, investigation_id: str) -> list[Any]: ...
    async def list_tool_calls(self, investigation_id: str) -> list[Any]: ...
    async def get_runbook(self, runbook_id: str) -> Any: ...


def load_manifest(path: Path) -> Manifest:
    return Manifest.model_validate(json.loads(path.read_text(encoding="utf-8")))


def _distinct(values: list[str]) -> int:
    return len({value for value in values if value})


def _value(item: Any) -> str:
    """枚举或字符串统一成文本；None 记为缺失。"""
    return str(getattr(item, "value", item) or "")


def _rejection_cause(event: Any, evidence_ids: set[str]) -> str:
    """只承认带回答形态、且形态与理由一致的拒绝；旧记录不作证据。"""
    payload = event.payload or {}
    if "citations" not in payload:
        return ""
    citations = payload.get("citations") or []
    unknown = payload.get("unknown_citations") or []
    if unknown:
        if not set(unknown) <= set(citations) or set(unknown) & evidence_ids:
            return ""
        return "unknown_citations"
    reason = (payload.get("reason") or "").strip()
    missing = payload.get("missing") or []
    if reason == "missing_without_new_query":
        if citations or not missing or payload.get("needs_new_query"):
            return ""
        return reason
    if reason == "unsupported_answer":
        if citations or missing:
            return ""
        return reason
    return ""


def _named(events: list[Any], name: str) -> list[Any]:
    return [event for event in events if event.event == name]


def _evidence_problems(
    run_id: str, evidence: str, markers: list[str] | tuple[str, ...] = ()
) -> list[str]:
    if not evidence:
        return [f"{run_id}:未登记证据文件"]
    path = Path(evidence)
    if not path.is_file():
        return [f"{run_id}:证据文件不存在 {evidence}"]
    text = path.read_text(encoding="utf-8", errors="replace")
    return [f"{run_id}:证据缺少「{marker}」" for marker in markers if marker not in text]


async def evaluate(manifest: Manifest, store: StoreProtocol) -> EvaluationReport:
    """逐条核对清单；任一断言缺证据就记失败，不补默认通过。"""
    criteria: list[CriterionResult] = []

    missing_cards = [item.id for item in manifest.scenarios if not item.card.strip()]
    for item in manifest.scenarios:
        path = item.card.split("#", 1)[0]
        if missing_cards or not Path(path).exists():
            if item.id not in missing_cards:
                missing_cards.append(f"{item.id}:{path}")
    core_ids = {item.id for item in manifest.scenarios if item.core}
    missing_core = sorted({"f01", "f02", "f03"} - core_ids)
    thin_signals = [item.id for item in manifest.scenarios if len(set(item.signal_classes)) < 2]
    criteria.append(
        CriterionResult(
            "V01",
            not missing_cards and not thin_signals and not missing_core,
            f"场景 {len(manifest.scenarios)} 个；"
            f"缺卡片 {missing_cards or '无'}；缺核心场景 {missing_core or '无'}；"
            f"信号不足 {thin_signals or '无'}",
        )
    )

    usage: dict[str, int] = {}
    for item in manifest.scenarios:
        for run in item.runs:
            if run.investigation_id:
                usage[run.investigation_id] = usage.get(run.investigation_id, 0) + 1
    thin_runs = []
    no_correct = []
    bad_runs = []
    for item in manifest.scenarios:
        if not item.core:
            continue
        seen: set[str] = set()
        valid = []
        for run in item.runs:
            if run.run_id in seen:
                bad_runs.append(f"{item.id}:{run.run_id}:重复")
                continue
            seen.add(run.run_id)
            if not run.valid:
                continue
            investigation = (
                await store.get_investigation(run.investigation_id)
                if run.investigation_id
                else None
            )
            if investigation is None:
                bad_runs.append(f"{item.id}:{run.run_id}:调查不存在")
                continue
            if usage.get(run.investigation_id, 0) > 1:
                bad_runs.append(f"{item.id}:{run.run_id}:调查被重复使用")
                continue
            valid.append(run)
        if len(valid) < 3:
            thin_runs.append(f"{item.id}={len(valid)}")
        if not any(run.investigation_correct for run in valid):
            no_correct.append(item.id)
    core_count = sum(1 for item in manifest.scenarios if item.core)
    criteria.append(
        CriterionResult(
            "V02",
            core_count > 0 and not thin_runs and not no_correct,
            f"核心场景 {core_count} 个；有效运行不足：{thin_runs or '无'}；"
            f"缺正确调查：{no_correct or '无'}；"
            f"无效登记：{bad_runs or '无'}",
        )
    )

    kinds: dict[str, bool] = {}
    bad_actions = []
    for item in manifest.scenarios:
        for run in item.runs:
            if not (run.valid and run.attempt_id and run.proposal_id):
                continue
            proposal = await store.get_proposal(run.proposal_id)
            attempt = await store.get_attempt(run.proposal_id)
            if proposal is None or attempt is None or attempt.status.value != "verified":
                bad_actions.append(f"{item.id}:{run.run_id}")
                continue
            if proposal.kind.value not in item.allowed_actions:
                bad_actions.append(f"{item.id}:{run.run_id}:动作不在允许集合")
                continue
            if run.recovered:
                kinds[proposal.kind.value] = True
    criteria.append(
        CriterionResult(
            "V03",
            kinds.get("rollout_undo", False) and kinds.get("restart_pod", False),
            f"真实恢复的动作：{sorted(kinds) or '无'}；存疑登记：{bad_actions or '无'}",
        )
    )

    # 三条路径都必须由清单里登记过的运行作证，且自动路径要有真实 verified 执行。
    denied_checked = False
    denied_without_attempt = True
    auto_verified = 0
    approval_runs = 0
    for item in manifest.scenarios:
        for run in item.runs:
            if not run.proposal_id:
                continue
            proposal = await store.get_proposal(run.proposal_id)
            if proposal is None:
                continue
            attempt = await store.get_attempt(run.proposal_id)
            if proposal.status.value == "denied":
                # 安全不变量不受运行有效性影响：登记过的拒绝提案都不许有执行记录。
                denied_without_attempt = denied_without_attempt and attempt is None
                if run.valid:
                    denied_checked = True
                continue
            if not run.valid or attempt is None or attempt.status.value != "verified":
                continue
            if not run.attempt_id or attempt.id != run.attempt_id:
                continue
            disposition = await store.get_proposal_disposition(run.proposal_id)
            if disposition == "auto":
                auto_verified += 1
            elif disposition == "approval":
                approval_runs += 1
    criteria.append(
        CriterionResult(
            "V04",
            auto_verified > 0 and approval_runs > 0 and denied_checked and denied_without_attempt,
            f"自动允许并验证 {auto_verified} 条；审批后执行 {approval_runs} 次；"
            f"拒绝证据 {'有' if denied_checked else '无'}；"
            f"被拒绝提案未执行 {denied_without_attempt}",
        )
    )

    approval_problems: list[str] = []
    seen_approval: set[str] = set()
    for run in manifest.approval_runs:
        seen_approval.add(run.expected)
        approval_problems.extend(_evidence_problems(run.run_id, run.evidence, run.markers))
        proposal = await store.get_proposal(run.proposal_id)
        approval = await store.get_approval(run.proposal_id)
        attempt = await store.get_attempt(run.proposal_id)
        audits = await store.list_audit(run.proposal_id)

        if proposal is None or approval is None:
            approval_problems.append(f"{run.run_id}:缺提案或审批记录")
            continue
        if not approval.actor.strip():
            approval_problems.append(f"{run.run_id}:审批缺操作者")
            continue
        if approval.proposal_hash != proposal_hash(proposal):
            approval_problems.append(f"{run.run_id}:审批没有绑定当前提案")
            continue
        decision = _value(approval.decision)
        if run.expected == "rejected":
            if decision != "rejected" or _value(proposal.status) != "rejected":
                approval_problems.append(f"{run.run_id}:拒绝记录不一致")
            elif attempt is not None:
                approval_problems.append(f"{run.run_id}:拒绝后仍有执行记录")
            elif not _named(audits, "action.rejected"):
                approval_problems.append(f"{run.run_id}:缺拒绝审计")
            continue
        if decision != "approved":
            approval_problems.append(f"{run.run_id}:不是批准记录")
            continue
        if approval.expires_at is None:
            approval_problems.append(f"{run.run_id}:批准没有有效期")
            continue
        if run.expected == "expired":
            resumed = _named(audits, "action.resumed")
            expired = _named(audits, "action.expired")
            if _value(proposal.status) != "expired":
                approval_problems.append(f"{run.run_id}:提案终态={_value(proposal.status)}")
            elif attempt is not None:
                approval_problems.append(f"{run.run_id}:过期授权仍执行")
            elif not resumed or not expired:
                approval_problems.append(f"{run.run_id}:缺过期或继续命令的审计")
            elif resumed[-1].payload.get("approval_id") != approval.id:
                approval_problems.append(f"{run.run_id}:继续命令没有引用该授权")
            elif resumed[-1].occurred_at <= approval.expires_at:
                approval_problems.append(f"{run.run_id}:继续发生在截止时间之前")
            elif expired[-1].occurred_at < resumed[-1].occurred_at:
                approval_problems.append(f"{run.run_id}:过期审计早于继续命令")
            continue
        approved = _named(audits, "action.approved")
        resumed = _named(audits, "action.resumed")
        succeeded = _named(audits, "action.succeeded")
        if attempt is None or attempt.id != run.attempt_id:
            approval_problems.append(f"{run.run_id}:执行记录不匹配")
        elif _value(attempt.status) != "verified":
            approval_problems.append(f"{run.run_id}:执行终态={_value(attempt.status)}")
        elif attempt.finished_at is None or attempt.finished_at < attempt.started_at:
            approval_problems.append(f"{run.run_id}:执行没有有效结束时间")
        elif (attempt.snapshot or {}).get("approval_hash") != approval.proposal_hash:
            approval_problems.append(f"{run.run_id}:执行没有绑定该授权")
        elif attempt.started_at <= approval.decided_at:
            approval_problems.append(f"{run.run_id}:执行不晚于批准")
        elif attempt.started_at > approval.expires_at:
            approval_problems.append(f"{run.run_id}:执行时授权已过期")
        elif _value(proposal.status) != "succeeded":
            approval_problems.append(f"{run.run_id}:提案终态={_value(proposal.status)}")
        elif not approved or not resumed:
            approval_problems.append(f"{run.run_id}:缺批准或继续命令的审计")
        elif approved[-1].payload.get("approval_id") != approval.id:
            approval_problems.append(f"{run.run_id}:批准审计没有引用该授权")
        elif resumed[-1].payload.get("approval_id") != approval.id:
            approval_problems.append(f"{run.run_id}:继续命令没有引用该授权")
        elif approved[-1].occurred_at < approval.decided_at:
            approval_problems.append(f"{run.run_id}:批准审计早于审批记录")
        elif not (approved[-1].occurred_at <= resumed[-1].occurred_at <= attempt.started_at):
            approval_problems.append(f"{run.run_id}:继续命令与执行顺序不符")
        elif not any(item.payload.get("attempt_id") == attempt.id for item in succeeded):
            approval_problems.append(f"{run.run_id}:成功审计没有引用该执行")
        elif succeeded[-1].occurred_at < attempt.finished_at:
            approval_problems.append(f"{run.run_id}:成功审计早于执行结束")
    missing_approval = sorted({"resumed", "expired", "rejected"} - seen_approval)
    criteria.append(
        CriterionResult(
            "V05",
            bool(manifest.approval_runs) and not missing_approval and not approval_problems,
            f"审批用例 {len(manifest.approval_runs)} 条；"
            f"缺用例 {missing_approval or '无'}；问题 {approval_problems or '无'}；"
            f"漂移分支仅单测",
        )
    )

    idempotency_problems: list[str] = []
    idempotency_kinds = {run.kind for run in manifest.idempotency_runs}
    for run in manifest.idempotency_runs:
        idempotency_problems.extend(_evidence_problems(run.run_id, run.evidence, run.markers))
        proposal = await store.get_proposal(run.proposal_id)
        if proposal is None:
            idempotency_problems.append(f"{run.run_id}:提案不存在")
            continue
        if not proposal.idempotency_key:
            idempotency_problems.append(f"{run.run_id}:缺幂等键")
            continue
        approvals = await store.count_approvals(run.proposal_id)
        if approvals != run.expected_approvals:
            idempotency_problems.append(f"{run.run_id}:审批记录 {approvals} 条")
        attempt = await store.get_attempt(run.proposal_id)
        attempts = 0 if attempt is None else 1
        if attempts != run.expected_attempts:
            idempotency_problems.append(f"{run.run_id}:执行记录 {attempts} 条")
        audits = await store.list_audit(run.proposal_id)
        approved = sum(1 for event in audits if event.event == "action.approved")
        if approved != run.expected_approvals:
            idempotency_problems.append(f"{run.run_id}:批准审计 {approved} 条")

    dedup_problems: list[str] = []
    for run in manifest.deduplication_runs:
        dedup_problems.extend(_evidence_problems(run.run_id, run.evidence, run.markers))
        incident = await store.get_incident(run.incident_id)
        if incident is None:
            dedup_problems.append(f"{run.run_id}:事件不存在")
            continue
        if run.fingerprint not in incident.alert_ids:
            dedup_problems.append(f"{run.run_id}:指纹不在事件中")
        audits = await store.list_audit(run.investigation_id)
        received = [
            event
            for event in audits
            if event.event == "alert.received"
            and run.fingerprint in (event.payload.get("fingerprints") or [])
        ]
        if not received:
            dedup_problems.append(f"{run.run_id}:缺告警接收审计")
        stamp = await store.get_incident_created_at(run.incident_id)
        receipts = await store.list_incident_receipts(run.fingerprint)
        if stamp is None:
            dedup_problems.append(f"{run.run_id}:缺事件创建时间")
        else:
            window = run.window_minutes * 60
            nearby = [item for item in receipts if abs((item - stamp).total_seconds()) <= window]
            if len(nearby) != 1:
                dedup_problems.append(f"{run.run_id}:窗口内事件 {len(nearby)} 个")
    trace_problems: list[str] = []
    for run in manifest.trace_runs:
        trace_problems.extend(_evidence_problems(run.run_id, run.evidence, run.markers))
        evidence = await store.list_evidence(run.investigation_id)
        calls = {call.id: call for call in await store.list_tool_calls(run.investigation_id)}
        cluster = [item for item in evidence if item.source != "knowledge"]
        if not cluster:
            trace_problems.append(f"{run.run_id}:没有集群观测证据")
        root = Path(manifest.evidence_root).resolve()
        for item in cluster:
            if not (item.observation or "").strip():
                trace_problems.append(f"{run.run_id}:证据 {item.id} 没有观测内容")
            if not item.raw_ref or Path(item.raw_ref).is_absolute():
                trace_problems.append(f"{run.run_id}:证据 {item.id} 的原始引用不是相对路径")
            else:
                raw = (root / item.raw_ref).resolve()
                if not raw.is_relative_to(root) or not raw.is_file() or raw.stat().st_size == 0:
                    trace_problems.append(f"{run.run_id}:证据 {item.id} 的原始引用不存在或越界")
            call = calls.get(item.tool_call_id)
            if call is None:
                trace_problems.append(f"{run.run_id}:证据 {item.id} 的工具调用不存在")
            elif getattr(call, "error_code", None):
                trace_problems.append(f"{run.run_id}:证据 {item.id} 来自失败的调用")
            elif getattr(call, "finished_at", None) is None:
                trace_problems.append(f"{run.run_id}:证据 {item.id} 的工具调用未完成")
            elif getattr(call, "raw_ref", None) != item.raw_ref:
                trace_problems.append(f"{run.run_id}:证据 {item.id} 与工具调用引用不一致")
    criteria.append(
        CriterionResult(
            "V07",
            {"approval", "resume"} <= idempotency_kinds
            and bool(manifest.deduplication_runs)
            and bool(manifest.trace_runs)
            and not idempotency_problems
            and not dedup_problems
            and not trace_problems,
            f"重复审批／继续用例 {len(manifest.idempotency_runs)} 条；"
            f"告警去重用例 {len(manifest.deduplication_runs)} 条；"
            f"回查用例 {len(manifest.trace_runs)} 条；"
            f"重复问题 {idempotency_problems or '无'}；去重问题 {dedup_problems or '无'}；"
            f"回查问题 {trace_problems or '无'}",
        )
    )

    context_problems: list[str] = []
    for run in manifest.context_runs:
        context_problems.extend(_evidence_problems(run.run_id, run.evidence, run.markers))
        investigation = await store.get_investigation(run.investigation_id)
        if investigation is None:
            context_problems.append(f"{run.run_id}:调查不存在")
            continue
        if (
            _value(investigation.status) != "completed"
            or not (investigation.conclusion or "").strip()
        ):
            context_problems.append(f"{run.run_id}:调查不是 completed 结论")
            continue
        incident = await store.get_incident(investigation.incident_id)
        if incident is None or not any(run.context_hint in item for item in incident.context):
            context_problems.append(f"{run.run_id}:Context 未登记")
        hypotheses = await store.list_hypotheses(run.investigation_id)
        refuted = [
            hypothesis
            for hypothesis in hypotheses
            if _value(hypothesis.status) == "refuted"
            and run.refuted_hypothesis in hypothesis.statement
        ]
        if not refuted:
            context_problems.append(f"{run.run_id}:假设没有被反驳记录")
        evidence = await store.list_evidence(run.investigation_id)
        refuted_ids = {hypothesis.id for hypothesis in refuted}
        if not any(
            item.relation == "refuting" and item.hypothesis_id in refuted_ids for item in evidence
        ):
            context_problems.append(f"{run.run_id}:反驳证据没有指向该假设")

    runbook_problems: list[str] = []
    for run in manifest.runbook_runs:
        runbook_problems.extend(_evidence_problems(run.run_id, run.evidence, run.markers))
        investigation = await store.get_investigation(run.investigation_id)
        if investigation is None:
            runbook_problems.append(f"{run.run_id}:调查不存在")
            continue
        runbook = await store.get_runbook(run.accepted)
        if runbook is None:
            runbook_problems.append(f"{run.run_id}:条目不存在")
        audits = await store.list_audit(run.investigation_id)
        accepted = {
            event.payload.get("runbook_id") for event in audits if event.event == "runbook.accepted"
        }
        rejected = {
            event.payload.get("runbook_id") for event in audits if event.event == "runbook.rejected"
        }
        judged = [
            event for event in audits if event.event in ("runbook.accepted", "runbook.rejected")
        ]
        if not all((event.payload.get("reason") or "").strip() for event in judged):
            runbook_problems.append(f"{run.run_id}:判定缺理由")
        if run.accepted not in accepted:
            runbook_problems.append(f"{run.run_id}:没有采纳记录")
        missing_rejected = sorted(set(run.rejected) - rejected)
        if missing_rejected:
            runbook_problems.append(f"{run.run_id}:缺拒绝记录 {missing_rejected}")
        if any(event.event == "runbook.unknown" for event in audits):
            runbook_problems.append(f"{run.run_id}:引用了未取回的条目")
        if run.proposal_id:
            proposal = await store.get_proposal(run.proposal_id)
            evidence = await store.list_evidence(run.investigation_id)
            sources = {item.id: item.source for item in evidence}
            if proposal is None:
                runbook_problems.append(f"{run.run_id}:动作提案不存在")
            elif not proposal.evidence_ids:
                runbook_problems.append(f"{run.run_id}:动作提案没有证据")
            else:
                knowledge = [
                    evidence_id
                    for evidence_id in proposal.evidence_ids
                    if sources.get(evidence_id) == "knowledge"
                ]
                unknown = [
                    evidence_id
                    for evidence_id in proposal.evidence_ids
                    if evidence_id not in sources
                ]
                if knowledge:
                    runbook_problems.append(f"{run.run_id}:runbook 进入动作证据")
                if unknown:
                    runbook_problems.append(f"{run.run_id}:动作证据不存在 {unknown}")

    explain_problems: list[str] = []
    for run in manifest.explain_runs:
        explain_problems.extend(_evidence_problems(run.run_id, run.evidence, run.markers))
        investigation = await store.get_investigation(run.investigation_id)
        if investigation is None:
            explain_problems.append(f"{run.run_id}:调查不存在")
            continue
        if (
            _value(investigation.status) != "completed"
            or not (investigation.conclusion or "").strip()
        ):
            explain_problems.append(f"{run.run_id}:追问前结论不完整")
            continue
        audits = await store.list_audit(run.investigation_id)
        requests = [event for event in audits if event.event == "explain.requested"]
        answered = [event for event in audits if event.event == "explain.answered"]
        rejected = [event for event in audits if event.event == "explain.rejected"]
        completed = [event for event in audits if event.event == "investigation.completed"]
        if not requests:
            explain_problems.append(f"{run.run_id}:没有追问记录")
            continue
        if not completed or completed[0].occurred_at > requests[0].occurred_at:
            explain_problems.append(f"{run.run_id}:结论不是追问之前完成")
        # 第一条 v2 请求之前的记录没有请求 ID 和内容摘要；之后必须逐条配对。
        v2_requests = [
            event
            for event in requests
            if event.payload.get("request_id")
            and isinstance(event.payload.get("snapshot"), dict)
            and event.payload["snapshot"].get("version") == 2
        ]
        if not v2_requests:
            explain_problems.append(f"{run.run_id}:没有带请求 ID 的追问")
        else:
            boundary = v2_requests[0].occurred_at
            request_ids = [event.payload.get("request_id") for event in v2_requests]
            if any(not request_id for request_id in request_ids) or len(set(request_ids)) != len(
                request_ids
            ):
                explain_problems.append(f"{run.run_id}:追问请求 ID 缺失或重复")
            by_id = {
                event.payload["request_id"]: event
                for event in v2_requests
                if event.payload.get("request_id")
            }
            if any(
                event.payload.get("request_id") not in by_id
                for event in requests
                if event.occurred_at >= boundary
            ):
                explain_problems.append(f"{run.run_id}:追问请求缺少可比快照")
            modern_answers = [event for event in answered if event.occurred_at >= boundary]
            modern_rejected = [event for event in rejected if event.occurred_at >= boundary]
            paired = []
            unpaired = []
            for event in modern_answers:
                request = by_id.get(event.payload.get("request_id"))
                if request is None:
                    unpaired.append(event)
                    continue
                paired.append((request, event))
                snapshot = event.payload.get("snapshot")
                if not isinstance(snapshot, dict) or snapshot.get("version") != 2:
                    explain_problems.append(f"{run.run_id}:回答缺少当前状态快照")
                elif snapshot != request.payload["snapshot"]:
                    explain_problems.append(f"{run.run_id}:追问前后状态快照不一致")
            if unpaired:
                explain_problems.append(f"{run.run_id}:回答没有配对的请求 ID")
            for event in modern_rejected:
                request = by_id.get(event.payload.get("request_id"))
                if request is None:
                    explain_problems.append(f"{run.run_id}:拒绝记录没有配对的请求 ID")
                    continue
                snapshot = event.payload.get("snapshot")
                if not isinstance(snapshot, dict) or snapshot.get("version") != 2:
                    explain_problems.append(f"{run.run_id}:拒绝记录缺少当前状态快照")
                elif snapshot != request.payload["snapshot"]:
                    explain_problems.append(f"{run.run_id}:拒绝前后状态快照不一致")
            grounded = [event for event in modern_answers if event.payload.get("citations")]
            if not grounded:
                explain_problems.append(f"{run.run_id}:没有带引用的成功回答")
            missing_answers = [
                event
                for event in modern_answers
                if not (event.payload.get("citations") or [])
                and event.payload.get("needs_new_query")
                and (event.payload.get("missing") or [])
            ]
            if not missing_answers:
                explain_problems.append(f"{run.run_id}:缺证据时没有说明缺什么并要求新取证")
            unsupported = [
                event
                for event in modern_answers
                if event not in missing_answers and not (event.payload.get("citations") or [])
            ]
            if unsupported:
                explain_problems.append(f"{run.run_id}:存在没有引用的无据回答")
            evidence_ids_now = {item.id for item in await store.list_evidence(run.investigation_id)}
            if any(
                not _rejection_cause(event, evidence_ids_now)
                for event in modern_rejected
                if "citations" in (event.payload or {})
            ):
                explain_problems.append(f"{run.run_id}:拒绝记录与回答形态不一致")
            comparable = [
                (request, event)
                for request, event in paired
                if isinstance(event.payload.get("snapshot"), dict)
                and event.payload["snapshot"].get("version") == 2
            ]
            if not comparable:
                explain_problems.append(f"{run.run_id}:回答没有可比的状态快照")
            else:
                latest = max(
                    (event for _, event in comparable), key=lambda event: event.occurred_at
                )
                fresh = await store.get_investigation(run.investigation_id) or investigation
                current_state = await state_snapshot(
                    store,
                    fresh,
                    await store.list_hypotheses(run.investigation_id),
                    await store.list_evidence(run.investigation_id),
                )
                if latest.payload["snapshot"] != current_state:
                    explain_problems.append(f"{run.run_id}:追问后结论、证据或授权已变化")
            evidence_ids = evidence_ids_now
            citations = [
                citation
                for event in grounded
                for citation in (event.payload.get("citations") or [])
            ]
            unknown = sorted({citation for citation in citations if citation not in evidence_ids})
            if unknown:
                explain_problems.append(f"{run.run_id}:引用不在证据中 {unknown}")
        first_request = min(event.occurred_at for event in requests)
        calls = await store.list_tool_calls(run.investigation_id)
        late_calls = [call.id for call in calls if call.started_at > first_request]
        if late_calls:
            explain_problems.append(f"{run.run_id}:追问后新增工具调用 {late_calls}")
        late_actions = [
            event.event
            for event in audits
            if event.occurred_at > first_request
            and (event.event.startswith("action.") or event.event.startswith("investigation."))
        ]
        if late_actions:
            explain_problems.append(f"{run.run_id}:追问后仍有动作或状态记录 {late_actions}")
        for proposal in await store.list_proposals_for_investigation(run.investigation_id):
            if proposal.created_at > first_request:
                explain_problems.append(f"{run.run_id}:追问后新增提案 {proposal.id}")
            proposal_attempt = await store.get_attempt(proposal.id)
            if proposal_attempt is not None and proposal_attempt.started_at > first_request:
                explain_problems.append(f"{run.run_id}:追问后新增执行 {proposal_attempt.id}")
            proposal_approval = await store.get_approval(proposal.id)
            if proposal_approval is not None and proposal_approval.decided_at > first_request:
                explain_problems.append(f"{run.run_id}:追问后新增审批 {proposal_approval.id}")
            for event in await store.list_audit(proposal.id):
                if event.occurred_at > first_request and event.event.startswith("action."):
                    explain_problems.append(f"{run.run_id}:追问后新增动作审计 {event.event}")
    criteria.append(
        CriterionResult(
            "V08",
            bool(manifest.context_runs)
            and bool(manifest.runbook_runs)
            and bool(manifest.explain_runs)
            and not context_problems
            and not runbook_problems
            and not explain_problems,
            f"Context 用例 {len(manifest.context_runs)} 条；runbook 用例 "
            f"{len(manifest.runbook_runs)} 条；追问用例 {len(manifest.explain_runs)} 条；"
            f"Context 问题 {context_problems or '无'}；runbook 问题 {runbook_problems or '无'}；"
            f"追问问题 {explain_problems or '无'}",
        )
    )

    safety_expected = {"no_improvement", "worsened", "unknown", "insufficient_evidence"}
    seen_safety: set[str] = set()
    safety_problems: list[str] = []
    for run in manifest.safety_runs:
        seen_safety.add(run.expected)
        if run.expected == "insufficient_evidence":
            investigation = (
                await store.get_investigation(run.investigation_id)
                if run.investigation_id
                else None
            )
            proposals = (
                await store.count_proposals_for_investigation(run.investigation_id)
                if run.investigation_id
                else 0
            )
            status = getattr(getattr(investigation, "status", None), "value", "")
            reason = getattr(investigation, "termination_reason", "") or ""
            if investigation is None:
                safety_problems.append(f"{run.run_id}:缺调查记录")
            elif status != "inconclusive" or "证据不足" not in reason:
                safety_problems.append(f"{run.run_id}:不是证据不足停止")
            elif proposals != 0:
                safety_problems.append(f"{run.run_id}:证据不足仍有提案")
            continue
        if not run.attempt_id:
            safety_problems.append(f"{run.run_id}:缺 attempt_id")
            continue
        proposal = await store.get_proposal(run.proposal_id) if run.proposal_id else None
        attempt = await store.get_attempt(run.proposal_id) if run.proposal_id else None
        if proposal is None or attempt is None:
            safety_problems.append(f"{run.run_id}:缺提案或执行记录")
            continue
        if attempt.id != run.attempt_id:
            safety_problems.append(f"{run.run_id}:执行记录不匹配")
            continue
        attempt_status = getattr(getattr(attempt, "status", None), "value", "")
        proposal_status = getattr(getattr(proposal, "status", None), "value", "")
        if run.expected in ("no_improvement", "worsened"):
            if attempt_status != "failed" or proposal_status != "failed":
                safety_problems.append(f"{run.run_id}:终态不符 {attempt_status}/{proposal_status}")
                continue
        elif attempt_status not in ("unknown", "failed") or proposal_status not in (
            "unknown",
            "failed",
        ):
            safety_problems.append(f"{run.run_id}:终态不符 {attempt_status}/{proposal_status}")
            continue
        outcome = (attempt.verification or {}).get("outcome")
        if outcome != run.expected:
            safety_problems.append(f"{run.run_id}:outcome={outcome}")
            continue
        followup = await store.get_followup_by_attempt(attempt.id)
        if followup is None:
            safety_problems.append(f"{run.run_id}:缺待办")
        elif followup.outcome.value != run.expected or followup.status.value != "open":
            safety_problems.append(f"{run.run_id}:待办与结果不一致")
    missing_safety = sorted(safety_expected - seen_safety)

    stop_gate_problems: list[str] = []
    for gate in manifest.stop_gate:
        failed = (
            await store.get_proposal(gate.failed_proposal_id) if gate.failed_proposal_id else None
        )
        failed_attempt = (
            await store.get_attempt(gate.failed_proposal_id) if gate.failed_proposal_id else None
        )
        followup = (
            await store.get_followup_by_attempt(failed_attempt.id)
            if failed_attempt is not None
            else None
        )
        if failed is None or failed_attempt is None or followup is None:
            stop_gate_problems.append(f"{gate.run_id}:失败记录不完整")
            continue
        if followup.status.value != "open":
            stop_gate_problems.append(f"{gate.run_id}:失败后没有开放待办")
            continue
        next_proposal = (
            await store.get_proposal(gate.next_proposal_id) if gate.next_proposal_id else None
        )
        next_attempt = (
            await store.get_attempt(gate.next_proposal_id) if gate.next_proposal_id else None
        )
        disposition = (
            await store.get_proposal_disposition(gate.next_proposal_id)
            if gate.next_proposal_id
            else None
        )
        failed_workload = failed.parameters.get("workload")
        next_workload = (
            next_proposal.parameters.get("workload") if next_proposal is not None else None
        )
        if next_proposal is None:
            stop_gate_problems.append(f"{gate.run_id}:缺第二次提案")
        elif disposition != "approval":
            stop_gate_problems.append(f"{gate.run_id}:第二次判定={disposition}")
        elif next_attempt is not None:
            stop_gate_problems.append(f"{gate.run_id}:第二次已经执行")
        elif not failed_workload or not next_workload or failed_workload != next_workload:
            stop_gate_problems.append(f"{gate.run_id}:工作负载不一致")
        elif (
            next_proposal.target.cluster,
            next_proposal.target.namespace,
        ) != (failed.target.cluster, failed.target.namespace):
            stop_gate_problems.append(f"{gate.run_id}:集群或命名空间不一致")
        elif (
            failed_attempt.finished_at is None
            or next_proposal.created_at <= failed_attempt.finished_at
        ):
            stop_gate_problems.append(f"{gate.run_id}:第二次提案早于失败结束")
        elif (
            decide(next_proposal, target_busy=False, prior_auto_failure=False).disposition.value
            != "auto"
        ):
            stop_gate_problems.append(f"{gate.run_id}:第二次本来就不属于自动范围")
    criteria.append(
        CriterionResult(
            "V06",
            not missing_safety
            and not safety_problems
            and bool(manifest.stop_gate)
            and not stop_gate_problems,
            f"安全用例 {len(manifest.safety_runs)} 条；"
            f"缺类别 {missing_safety or '无'}；问题 {safety_problems or '无'}；"
            f"停止门 {len(manifest.stop_gate)} 条；"
            f"停止门问题 {stop_gate_problems or '无'}",
        )
    )

    scenarios_by_id = {item.id: item for item in manifest.scenarios}
    verified_baseline = 0
    baseline_problems: list[str] = []
    baseline_notes: list[str] = []
    covered_scenarios: set[str] = set()
    seen_run_ids: set[str] = set()
    seen_investigations: set[str] = set()
    for run in manifest.baseline:
        label = run.run_id or "未命名"
        problems: list[str] = []
        if not run.run_id:
            problems.append("缺 run_id")
        else:
            if run.run_id in seen_run_ids:
                problems.append("run_id 重复")
            seen_run_ids.add(run.run_id)
        if not [item.strip() for item in run.compared if item.strip()]:
            problems.append("未声明比较维度")
        if run.limitations is None:
            problems.append("未注明不可比维度")
        elif not run.limitations or any(not item.strip() for item in run.limitations):
            problems.append("不可比维度为空")
        scenario = scenarios_by_id.get(run.scenario)
        if scenario is None:
            problems.append(f"场景不存在 {run.scenario}")
            baseline_problems.extend(f"{label}:{item}" for item in problems)
            continue
        scenario_run_ids = [item.run_id for item in scenario.runs]
        if len(scenario_run_ids) != len(set(scenario_run_ids)):
            problems.append("场景运行 id 重复")
        if not run.rules_version:
            problems.append("缺规则版本")
        if not run.investigation_id:
            problems.append("缺调查")
        else:
            if run.investigation_id in seen_investigations:
                problems.append("重复引用同一调查")
            seen_investigations.add(run.investigation_id)
        investigation = (
            await store.get_investigation(run.investigation_id) if run.investigation_id else None
        )
        if investigation is None:
            problems.append("调查不存在")
            baseline_problems.extend(f"{label}:{item}" for item in problems)
            continue
        status = getattr(getattr(investigation, "status", None), "value", "")
        if status not in {"completed", "inconclusive"}:
            problems.append(f"终态={status or '未知'}")
        if getattr(investigation, "model", "") != f"rules:{run.rules_version}":
            problems.append(f"模型标记={getattr(investigation, 'model', '')}")
        if investigation.usage.model_calls != 0:
            problems.append(f"模型调用={investigation.usage.model_calls}")
        if investigation.usage.tool_calls < 1:
            problems.append("没有工具调用")
        audits = await store.list_audit(run.investigation_id)
        started = [event for event in audits if getattr(event, "event", "") == "baseline.started"]
        if not started:
            problems.append("缺 baseline.started 审计")
        else:
            payload = started[-1].payload or {}
            if payload.get("rules_version") != run.rules_version:
                problems.append("审计规则版本不符")
            if not payload.get("policy_version"):
                problems.append("审计缺策略版本")
        if not run.agent_run_ids:
            problems.append("缺对照 Agent 运行")
        incident = await store.get_incident(investigation.incident_id)
        if incident is None:
            baseline_problems.append(f"{label}:基线 incident 不存在")
            baseline_problems.extend(f"{label}:{item}" for item in problems)
            continue
        baseline_proposals = await store.list_proposals_for_investigation(run.investigation_id)
        baseline_verdicts: dict[str, set[str]] = {}
        for proposal in baseline_proposals:
            kind = getattr(getattr(proposal, "kind", None), "value", "")
            origin = _value(getattr(proposal, "origin", None))
            if origin != "rules":
                problems.append(f"基线提案来源={origin or '未知'} {kind or proposal.id}")
            if kind and kind not in scenario.allowed_actions:
                problems.append(f"提案超出场景动作 {kind}")
            if kind:
                verdict = await store.get_proposal_disposition(proposal.id) or ""
                baseline_verdicts.setdefault(kind, set()).add(verdict)
        runs_by_id = {item.run_id: item for item in scenario.runs}
        for agent_run_id in run.agent_run_ids:
            agent_run = runs_by_id.get(agent_run_id)
            if agent_run is None:
                problems.append(f"对照运行不存在 {agent_run_id}")
                continue
            if not agent_run.valid:
                problems.append(f"对照运行无效 {agent_run_id}")
            if not agent_run.investigation_id:
                problems.append(f"对照运行缺调查 {agent_run_id}")
                continue
            if agent_run.investigation_id == run.investigation_id:
                problems.append(f"与 Agent 共用调查 {agent_run_id}")
                continue
            agent_investigation = await store.get_investigation(agent_run.investigation_id)
            if agent_investigation is None:
                problems.append(f"对照调查不存在 {agent_run_id}")
                continue
            if getattr(agent_investigation, "model", "").startswith("rules:"):
                problems.append(f"对照是规则运行 {agent_run_id}")
                continue
            agent_status = getattr(getattr(agent_investigation, "status", None), "value", "")
            if agent_status not in {"completed", "inconclusive"}:
                problems.append(f"对照终态={agent_status or '未知'} {agent_run_id}")
            if getattr(getattr(agent_investigation, "usage", None), "model_calls", 0) < 1:
                problems.append(f"对照没有模型调用 {agent_run_id}")
            if getattr(getattr(agent_investigation, "usage", None), "tool_calls", 0) < 1:
                problems.append(f"对照没有工具调用 {agent_run_id}")
            if getattr(agent_investigation, "budget", None) != investigation.budget:
                problems.append(f"预算不一致 {agent_run_id}")
            agent_incident = await store.get_incident(agent_investigation.incident_id)
            if agent_incident is None:
                problems.append(f"对照 incident 不存在 {agent_run_id}")
                continue
            if (
                agent_incident.cluster,
                agent_incident.namespace,
                agent_incident.service,
            ) != (incident.cluster, incident.namespace, incident.service):
                problems.append(f"资源不一致 {agent_run_id}")
            window = _window_seconds(incident)
            agent_window = _window_seconds(agent_incident)
            if window is None or agent_window is None:
                problems.append(f"观察窗缺失 {agent_run_id}")
            elif abs(agent_window - window) > 1:
                problems.append(f"观察窗不一致 {agent_run_id}")
            agent_environment = getattr(agent_incident, "environment", None)
            baseline_environment = getattr(incident, "environment", None)
            if agent_environment is None or baseline_environment is None:
                problems.append(f"环境缺失 {agent_run_id}")
            elif agent_environment != baseline_environment:
                problems.append(f"环境不一致 {agent_run_id}")
            agent_trigger = getattr(agent_incident, "trigger", None)
            baseline_trigger = getattr(incident, "trigger", None)
            if agent_trigger is None or baseline_trigger is None:
                problems.append(f"触发方式缺失 {agent_run_id}")
            elif agent_trigger != baseline_trigger:
                problems.append(f"触发方式不一致 {agent_run_id}")
            agent_alerts = getattr(agent_incident, "alert_ids", None)
            baseline_alerts = getattr(incident, "alert_ids", None)
            if agent_alerts is None or baseline_alerts is None:
                problems.append(f"关联告警缺失 {agent_run_id}")
            elif sorted(agent_alerts) != sorted(baseline_alerts):
                problems.append(f"关联告警不一致 {agent_run_id}")
            agent_context = getattr(agent_incident, "context", None)
            baseline_context = getattr(incident, "context", None)
            if agent_context is None or baseline_context is None:
                problems.append(f"Context 缺失 {agent_run_id}")
            elif list(agent_context) != list(baseline_context):
                problems.append(f"Context 不一致 {agent_run_id}")
            agent_symptoms = getattr(agent_incident, "symptoms", None)
            baseline_symptoms = getattr(incident, "symptoms", None)
            if agent_symptoms is None or baseline_symptoms is None:
                problems.append(f"现象描述缺失 {agent_run_id}")
            elif agent_symptoms.strip() != baseline_symptoms.strip():
                problems.append(f"现象描述不一致 {agent_run_id}")
            control_verdicts: dict[str, set[str]] = {}
            for proposal in await store.list_proposals_for_investigation(
                agent_run.investigation_id
            ):
                kind = getattr(getattr(proposal, "kind", None), "value", "")
                if not kind:
                    continue
                origin = _value(getattr(proposal, "origin", None))
                if origin != "model":
                    problems.append(f"对照提案来源={origin or '未知'} {kind}")
                if kind not in scenario.allowed_actions:
                    problems.append(f"对照提案超出场景动作 {kind}")
                verdict = await store.get_proposal_disposition(proposal.id) or ""
                control_verdicts.setdefault(kind, set()).add(verdict)
            pair = f"{label}/{agent_run_id}"
            for kind, verdicts in baseline_verdicts.items():
                other = control_verdicts.get(kind)
                if other is None:
                    baseline_notes.append(f"{pair}:{kind} 仅基线提出")
                elif other != verdicts:
                    problems.append(
                        f"授权判定不一致 {kind} {sorted(verdicts)}/{sorted(other)} {agent_run_id}"
                    )
            for kind in control_verdicts.keys() - baseline_verdicts.keys():
                baseline_notes.append(f"{pair}:{kind} 仅对照提出")
            if not baseline_verdicts and not control_verdicts:
                baseline_notes.append(f"{pair}:授权判定未触发")
        if problems:
            baseline_problems.extend(f"{label}:{item}" for item in problems)
        else:
            verified_baseline += 1
            covered_scenarios.add(run.scenario)
    core_scenarios = {item.id for item in manifest.scenarios if item.core}
    missing_baseline = sorted(core_scenarios - covered_scenarios)
    if missing_baseline:
        baseline_problems.append(f"缺核心场景基线 {missing_baseline}")
    detail = (
        f"规则基线 {len(manifest.baseline)} 条，可核对 {verified_baseline} 条；"
        f"问题 {baseline_problems or '无'}"
    )
    if baseline_notes:
        detail += f"；备注 {baseline_notes}"
    criteria.append(
        CriterionResult(
            "V09",
            verified_baseline > 0
            and verified_baseline == len(manifest.baseline)
            and not missing_baseline,
            detail,
        )
    )
    missing_packs = [
        path
        for path in manifest.demo_packs
        if not (Path(path) / "README.md").exists()
        or not any((Path(path) / name).is_dir() for name in ("screenshots", "outputs"))
    ]
    criteria.append(
        CriterionResult(
            "V10",
            bool(manifest.demo_packs) and not missing_packs,
            f"证据包 {len(manifest.demo_packs)} 个；缺目录 {missing_packs or '无'}",
        )
    )

    return EvaluationReport(frozen_at=manifest.frozen_at, criteria=criteria)
