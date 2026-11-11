"""单 Agent 测试共用：脚本模型、假工具箱、假执行器与一次运行的快捷入口。"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

from db_env import DSN, TEST_REASON
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatResult

from aiops.actions.executor import ActionExecutor
from aiops.actions.restart_pod import PodPlan
from aiops.actions.rollout_undo import ActionError, RolloutPlan
from aiops.agent.engine import LangChainInvestigation
from aiops.config import Settings
from aiops.contracts import (
    ActionAttempt,
    ActionKind,
    ActionOrigin,
    ActionProposal,
    ActionPurpose,
    ActionTarget,
    Approval,
    ApprovalDecision,
    AttemptOutcome,
    AttemptStatus,
    Budget,
    Incident,
    Investigation,
    InvestigationStatus,
    ProposalStatus,
    RiskDecision,
    RiskDisposition,
    TimeRange,
    ToolError,
    ToolResult,
)
from aiops.db import Store

START = datetime.now(UTC)
WINDOW = TimeRange(start=START - timedelta(minutes=15), end=START)


def settings(**overrides: Any) -> Settings:
    """不读 .env 的测试配置；模型由脚本模型替代。"""
    base = Settings(_env_file=None, model_name="scripted")
    return base.model_copy(update=overrides)


class ScriptedChatModel(BaseChatModel):
    """按顺序返回预置消息；可传函数读取已有消息再决定下一步。"""

    responses: list[Any]
    seen: list[list[BaseMessage]] = []

    @property
    def _llm_type(self) -> str:
        return "scripted"

    def bind_tools(self, tools, **kwargs):  # noqa: ANN001 - 测试替身
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs) -> ChatResult:  # noqa: ANN001
        self.seen.append(list(messages))
        if not self.responses:
            raise RuntimeError("脚本响应已用尽；这是测试剧本的问题，不是运行时的问题")
        item = self.responses.pop(0)
        message = item(messages) if callable(item) else item
        return ChatResult(generations=[ChatGeneration(message=message)])


class FakeToolbox:
    """按工具名返回预置数据；记录收到的请求。"""

    def __init__(self, responses: dict[str, Any] | None = None) -> None:
        self.responses = responses or {}
        self.requests: list[Any] = []

    async def run(self, context, time_range, request):  # noqa: ANN001
        self.requests.append(request)
        now = datetime.now(UTC)
        spec = self.responses.get(
            request.tool, {"source": request.tool.split(".")[0], "data": default_data(request.tool)}
        )
        if isinstance(spec, Exception):
            return ToolResult(
                tool_call_id="ignored",
                source=request.tool.split(".")[0],
                query=request.model_dump(),
                time_range=TimeRange(start=now - timedelta(minutes=2), end=now),
                collected_at=now,
                error=ToolError(code="timeout", message=str(spec), retryable=True),
            )
        return ToolResult(
            tool_call_id="ignored",
            source=str(spec.get("source", request.tool.split(".")[0])),
            query=request.model_dump(),
            time_range=TimeRange(start=now - timedelta(minutes=2), end=now),
            collected_at=now,
            data=spec.get("data", default_data(request.tool)),
            raw_ref=spec.get("raw_ref", f"evidence/test/{request.tool}.json"),
            truncated=bool(spec.get("truncated", False)),
        )


def default_data(tool: str) -> Any:
    if tool == "prometheus.query_range":
        return {"series": [{"labels": {}, "points": [[1.0, 0.0], [2.0, 0.0]]}]}
    if tool == "kubernetes.list_pods":
        return {"items": [{"name": "target-pod", "ready": True, "restarts": 0}]}
    if tool == "kubernetes.list_logs":
        return {"lines": ["GET /work 500"]}
    if tool == "kubernetes.list_deployments":
        return {"items": [{"name": "target-deploy", "revisions": [1, 2]}]}
    if tool == "kubernetes.list_services":
        return {"items": [{"name": "target-service", "selector": "app=target"}]}
    if tool == "kubernetes.list_events":
        return {"items": []}
    if tool == "knowledge.list_runbooks":
        return {"items": []}
    if tool == "knowledge.get_runbook":
        return {"id": "rb-1", "title": "参考", "steps": ["ref"]}
    return {}


class FakeAgentExecutor:
    """只实现 Agent 需要的三段接口；判定与预算上界可控。"""

    def __init__(
        self,
        store: Store,
        *,
        disposition: RiskDisposition = RiskDisposition.APPROVAL,
        outcome: AttemptOutcome = AttemptOutcome.IMPROVED,
        upper_bound: float = 60.0,
        prepare_error: ActionError | None = None,
    ) -> None:
        self._store = store
        self._disposition = disposition
        self._outcome = outcome
        self._upper_bound = upper_bound
        self._prepare_error = prepare_error
        self.executed: list[str] = []
        self.prepared: list[ActionProposal] = []

    def adapter_upper_bound(self) -> float:
        return self._upper_bound

    async def prepare_restart_pod(self, **kwargs):  # noqa: ANN003
        if self._prepare_error is not None:
            raise self._prepare_error
        now = datetime.now(UTC)
        proposal = ActionProposal(
            id=f"act-{uuid4().hex[:8]}",
            kind=ActionKind.RESTART_POD,
            target=ActionTarget(
                cluster="vigil-lab",
                namespace=kwargs["namespace"],
                kind="Pod",
                name=kwargs["pod"],
                uid="uid-demo",
                resource_version="1",
            ),
            parameters={
                "pod": kwargs["pod"],
                "replica_set": "target-rs",
                "owner_kind": "ReplicaSet",
                "workload": "uid-demo",
                "workload_name": "target-deploy",
                "discardable": True,
                "template_hash": "hash",
                "restart_count": 0,
                "service": kwargs["service"],
                "path": kwargs["path"],
            },
            idempotency_key=f"restart:{uuid4().hex[:8]}",
            reason=kwargs["reason"],
            origin=ActionOrigin.MODEL,
            investigation_id=kwargs["investigation_id"],
            evidence_ids=kwargs["evidence_ids"],
            created_at=now,
            purpose=ActionPurpose(kwargs["purpose"]),
            hypothesis_id=kwargs["hypothesis_id"],
            prediction=kwargs["prediction"],
            observation_conditions={"window_seconds": 90},
            observation_window_seconds=90,
        )
        decision = RiskDecision(
            proposal_id=proposal.id,
            disposition=self._disposition,
            policy_version="test-policy",
            reasons=["测试判定"],
            requires_compensation=False,
        )
        status = {
            RiskDisposition.AUTO: ProposalStatus.APPROVED,
            RiskDisposition.APPROVAL: ProposalStatus.PENDING_APPROVAL,
            RiskDisposition.DENY: ProposalStatus.DENIED,
        }[self._disposition]
        proposal = proposal.model_copy(update={"status": status})
        await self._store.save_proposal(proposal, decision)
        approval = None
        if self._disposition is RiskDisposition.AUTO:
            approval = Approval(
                id=f"apr-{uuid4().hex[:8]}",
                proposal_id=proposal.id,
                proposal_hash="hash",
                actor="policy:test-policy",
                decision=ApprovalDecision.APPROVED,
                reason="自动允许",
                decided_at=now,
                expires_at=now + timedelta(seconds=900),
            )
            await self._store.save_approval(approval)
        self.prepared.append(proposal)
        return proposal, decision, approval

    async def execute_authorized(self, proposal_id: str, *, actor: str = "operator"):  # noqa: ANN201
        self.executed.append(proposal_id)
        now = datetime.now(UTC)
        attempt = ActionAttempt(
            id=f"att-{uuid4().hex[:8]}",
            proposal_id=proposal_id,
            status=AttemptStatus.VERIFIED
            if self._outcome is AttemptOutcome.IMPROVED
            else AttemptStatus.FAILED,
            idempotency_key=f"idem-{uuid4().hex[:6]}",
            snapshot={},
            request={},
            started_at=now,
            result={"stage": "rebuilt"},
            verification={
                "outcome": self._outcome.value,
                "outcome_reason": f"测试结果 {self._outcome.value}",
                "baseline": {"samples": 3, "failed_samples": 3},
                "post_tail": {"samples": 3, "failed_samples": 0},
                "old_uid": "uid-demo",
                "new_uid": "uid-demo-2",
                "raw_ref": "evidence/test/verify.json",
            },
            finished_at=now,
        )
        await self._store.save_attempt(attempt)
        await self._store.finish_attempt(
            attempt.id,
            proposal_id=proposal_id,
            proposal_status=ProposalStatus.SUCCEEDED
            if self._outcome is AttemptOutcome.IMPROVED
            else ProposalStatus.FAILED,
            status=attempt.status,
            result=attempt.result,
            verification=attempt.verification,
            finished_at=now,
        )
        return attempt


class FakeRolloutAdapter:
    """提供给真实 ActionExecutor 的读侧替身；只实现被调用的方法。"""

    def __init__(
        self,
        *,
        memory_limit: str | None = "256Mi",
        previous_memory_limit: str | None = "512Mi",
        current_revision: int = 2,
        target_revision: int = 1,
        fail_previous: bool = False,
    ) -> None:
        self.memory_limit = memory_limit
        self.previous_memory_limit = previous_memory_limit
        self.current_revision = current_revision
        self.target_revision = target_revision
        self.fail_previous = fail_previous
        self.applied: list[bool] = []

    def plan(self, namespace: str, name: str) -> RolloutPlan:
        if self.fail_previous:
            raise ActionError("no_previous_revision", "没有可回退的历史 revision")
        return RolloutPlan(
            namespace=namespace,
            name=name,
            current_revision=self.current_revision,
            target_revision=self.target_revision,
            current_image="app:v2",
            target_image="app:v1",
            template={"spec": {"containers": []}},
            deployment_uid="uid-deploy",
            resource_version="12",
            current_memory_limit=self.memory_limit,
            target_memory_limit=self.previous_memory_limit,
        )

    def state(self, namespace: str, name: str) -> dict[str, Any]:
        return {
            "uid": "uid-deploy",
            "memory_limit": self.memory_limit,
            "pods": [],
        }

    def apply(self, plan, *, dry_run: bool, proposal_id: str):  # noqa: ANN001, ANN201
        self.applied.append(dry_run)
        return {"dry_run": dry_run}

    def wait_rolled_out(self, plan, timeout_seconds):  # noqa: ANN001, ANN201
        return True


class FakeRestartAdapter:
    def __init__(
        self,
        *,
        last_terminated_reason: str | None = None,
        restart_count: int = 0,
        memory_limit: str | None = "256Mi",
        ready: bool = True,
        discardable: bool = True,
        service_selects_ok: bool = True,
    ) -> None:
        self.last_terminated_reason = last_terminated_reason
        self.restart_count = restart_count
        self.memory_limit = memory_limit
        self.ready = ready
        self.discardable = discardable
        self.service_selects_ok = service_selects_ok
        self.deleted: list[str] = []
        self.tag = uuid4().hex[:6]

    def plan(self, namespace: str, name: str) -> PodPlan:
        return PodPlan(
            namespace=namespace,
            name=name,
            uid=f"uid-pod-{self.tag}",
            resource_version="7",
            owner_kind="ReplicaSet",
            owner_name="target-rs",
            owner_uid="uid-rs",
            template_hash="template",
            spec_hash="spec",
            sibling_uids=(),
            restart_count=self.restart_count,
            ready=self.ready,
            image="app:v1",
            discardable=self.discardable,
            ip="10.0.0.5",
            workload_name="target-deploy",
            workload_uid="uid-deploy",
        )

    def applicability(self, namespace: str, name: str) -> dict[str, Any]:
        return {
            "uid": f"uid-pod-{self.tag}",
            "phase": "Running",
            "ready": self.ready,
            "restart_count": self.restart_count,
            "last_terminated_reason": self.last_terminated_reason,
            "last_terminated_exit_code": 137 if self.last_terminated_reason else None,
            "memory_limit": self.memory_limit,
            "memory_request": self.memory_limit,
            "image": "app:v1",
        }

    def service_selects(self, namespace, service, pod_ip, *, ready_only=True):  # noqa: ANN001
        return {"ok": self.service_selects_ok, "pod_ip": pod_ip, "addresses": [pod_ip]}

    def dry_run(self, plan):  # noqa: ANN001
        return {"dry_run": True, "uid": plan.uid, "allowed": True}

    def execute(self, plan):  # noqa: ANN001
        self.deleted.append(plan.uid)
        return {"deleted": plan.name, "uid": plan.uid}

    def wait_rebuilt(self, plan, timeout_seconds):  # noqa: ANN001
        return {"name": f"{plan.name}-new", "uid": "uid-new", "ip": "10.0.0.6", "ready": True}

    def pod_state(self, namespace, owner_uid, uid):  # noqa: ANN001
        return {
            "name": f"{namespace}-pod",
            "uid": uid,
            "ready": True,
            "restarts": 0,
            "template_hash": "spec",
            "ip": "10.0.0.6",
        }

    def pod_get(self, namespace, name, path):  # noqa: ANN001
        return {"ok": True, "status": 200, "body": "ok"}


class FakePrometheus:
    def __init__(self, statuses: list[int] | None = None) -> None:
        self.statuses = statuses or [200, 200, 200, 200, 200, 200]

    async def query_range(self, context, expression, time_range, step_seconds):  # noqa: ANN001
        return ToolResult(
            tool_call_id="probe",
            source="prometheus",
            query={"expression": expression},
            time_range=time_range,
            collected_at=datetime.now(UTC),
            data={
                "series": [
                    {
                        "labels": {},
                        "points": [
                            [float(index), float(value)]
                            for index, value in enumerate(self.statuses)
                        ],
                    }
                ]
            },
        )


def make_proposal_executor(
    store: Store,
    *,
    rollout: FakeRolloutAdapter | None = None,
    restart: FakeRestartAdapter | None = None,
    prometheus: FakePrometheus | None = None,
) -> ActionExecutor:
    return ActionExecutor(
        store=store,
        adapter=rollout or FakeRolloutAdapter(),  # type: ignore[arg-type]
        restart_adapter=restart or FakeRestartAdapter(),  # type: ignore[arg-type]
        prometheus=prometheus or FakePrometheus(),  # type: ignore[arg-type]
        runtime_path=Settings(_env_file=None).runtime_path,
        cluster="vigil-lab",
        namespace="vigil-experiment",
        rollout_timeout_seconds=5,
        verify_seconds=6,
        sample_seconds=1,
        verify_delay_seconds=0,
        verify_grace_seconds=0,
        io_timeout_seconds=1,
    )


@dataclass
class Case:
    store: Store
    runtime: LangChainInvestigation
    model: ScriptedChatModel
    toolbox: FakeToolbox
    incident: Incident
    investigation: Investigation
    tags: list[str] = field(default_factory=list)


def build(ids, *, budget: Budget | None = None, namespace: str = "vigil-experiment"):
    incident = Incident(
        id=ids["incident"],
        environment="local",
        cluster="vigil-lab",
        namespace=namespace,
        service="frontend",
        trigger="manual",
        symptoms="首页返回 500",
        time_range=WINDOW,
        context=["测试事件"],
    )
    investigation = Investigation(
        id=ids["investigation"],
        incident_id=incident.id,
        status=InvestigationStatus.RUNNING,
        model="scripted",
        policy_version="test",
        budget=budget
        or Budget(
            max_tool_calls=10,
            max_model_calls=8,
            max_wall_clock_seconds=120,
            max_action_seconds=120,
        ),
        started_at=START,
    )
    return incident, investigation


@asynccontextmanager
async def agent_case(
    ids,
    *,
    responses: list[Any],
    toolbox: FakeToolbox | None = None,
    executor: Any = None,
    budget: Budget | None = None,
    namespace: str = "vigil-experiment",
    investigation_budget: Budget | None = None,
) -> AsyncIterator[Case]:
    if not DSN:
        raise RuntimeError("未配置 VIGIL_DATABASE_URL")
    config = settings()
    async with Store.open(DSN) as store:
        await store.migrate()
        incident, investigation = build(ids, budget=budget, namespace=namespace)
        await store.save_incident(incident)
        if investigation_budget is not None:
            investigation = investigation.model_copy(update={"budget": investigation_budget})
        await store.create_investigation(investigation)
        model = ScriptedChatModel(responses=responses)
        fake_toolbox = toolbox or FakeToolbox()
        runtime = LangChainInvestigation(
            settings=config,
            store=store,
            dsn=DSN,
            incident=incident,
            investigation=investigation,
            toolbox=fake_toolbox,  # type: ignore[arg-type]
            executor=executor,
            model=model,  # type: ignore[arg-type]
        )
        yield Case(
            store=store,
            runtime=runtime,
            model=model,
            toolbox=fake_toolbox,
            incident=incident,
            investigation=investigation,
        )


# ---------------------------------------------------------------- 消息辅助


def call(name: str, args: dict[str, Any], call_id: str) -> AIMessage:
    return AIMessage(content="", tool_calls=[{"name": name, "args": args, "id": call_id}])


def report_call(
    *,
    conclusion: str = "测试结论",
    confirmed: bool = True,
    supporting: list[str] | None = None,
    call_id: str = "call-report",
    boundary: str = "测试证据边界",
) -> AIMessage:
    return call(
        "ReportDraft",
        {
            "conclusion": conclusion,
            "root_cause_confirmed": confirmed,
            "supporting_observation_ids": supporting or [],
            "contradictory_observation_ids": [],
            "evidence_boundary": boundary,
            "known_contradictions": [],
            "remaining_unknowns": [],
            "prediction_check": "",
            "needs_human": [],
        },
        call_id,
    )


def tool_payloads(messages: list[BaseMessage]) -> list[dict[str, Any]]:
    payloads = []
    for message in messages:
        if message.__class__.__name__ == "ToolMessage":
            try:
                payloads.append(json.loads(str(message.content)))
            except json.JSONDecodeError:
                payloads.append({"raw": str(message.content)})
    return payloads


def hypothesis_id(messages: list[BaseMessage]) -> str:
    for payload in tool_payloads(messages):
        for item in payload.get("hypotheses") or []:
            if item.get("id"):
                return str(item["id"])
    raise AssertionError("工具消息里没有假设 ID")


def observation_ids(messages: list[BaseMessage]) -> list[str]:
    return [
        str(payload["observation_id"])
        for payload in tool_payloads(messages)
        if payload.get("observation_id")
    ]


def create_hypothesis(call_id: str = "call-hyp", temp_id: str = "h1") -> AIMessage:
    return call(
        "manage_hypotheses",
        {
            "operations": [
                {
                    "op": "create",
                    "temp_id": temp_id,
                    "statement": "目标 Pod 的临时状态损坏",
                    "expected_observations": ["/work 持续 500 且 Pod 就绪"],
                    "refutation_criteria": ["重建后仍然 500"],
                    "reason": "日志显示 500 但 Pod 就绪",
                }
            ]
        },
        call_id,
    )


__all__ = [
    "Case",
    "FakeAgentExecutor",
    "FakePrometheus",
    "FakeRestartAdapter",
    "FakeRolloutAdapter",
    "FakeToolbox",
    "ScriptedChatModel",
    "TEST_REASON",
    "agent_case",
    "build",
    "call",
    "create_hypothesis",
    "default_data",
    "hypothesis_id",
    "make_proposal_executor",
    "observation_ids",
    "report_call",
    "settings",
    "tool_payloads",
]
