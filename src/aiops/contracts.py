"""W0 跨模块数据契约；存储、模型与集群实现留到后续阶段。"""

from enum import StrEnum
from typing import Any, Literal, Self

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid")


class TimeRange(Contract):
    start: AwareDatetime
    end: AwareDatetime

    @model_validator(mode="after")
    def validate_order(self) -> Self:
        if self.end <= self.start:
            raise ValueError("时间窗结束时间必须晚于开始时间")
        return self


class Incident(Contract):
    id: str
    environment: str
    cluster: str
    namespace: str
    service: str
    trigger: Literal["alert", "manual", "scheduled"]
    symptoms: str
    time_range: TimeRange
    alert_ids: list[str] = Field(default_factory=list)
    context: list[str] = Field(default_factory=list)


class InvestigationStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    INCONCLUSIVE = "inconclusive"
    ABORTED = "aborted"

    @property
    def terminal(self) -> bool:
        return self in {
            InvestigationStatus.COMPLETED,
            InvestigationStatus.INCONCLUSIVE,
            InvestigationStatus.ABORTED,
        }


class Budget(Contract):
    max_tool_calls: int = Field(gt=0)
    max_model_calls: int = Field(gt=0)
    max_wall_clock_seconds: int = Field(gt=0)


class BudgetUsage(Contract):
    tool_calls: int = Field(default=0, ge=0)
    model_calls: int = Field(default=0, ge=0)
    elapsed_seconds: float = Field(default=0, ge=0)

    def exhausted(self, budget: Budget) -> str | None:
        """返回最先耗尽的预算项；未耗尽返回 None。"""
        if self.tool_calls >= budget.max_tool_calls:
            return "tool_calls"
        if self.model_calls >= budget.max_model_calls:
            return "model_calls"
        if self.elapsed_seconds >= budget.max_wall_clock_seconds:
            return "wall_clock"
        return None


class Investigation(Contract):
    id: str
    incident_id: str
    status: InvestigationStatus
    model: str
    policy_version: str
    budget: Budget
    usage: BudgetUsage = Field(default_factory=BudgetUsage)
    parent_run_id: str | None = None
    conclusion: str | None = None
    termination_reason: str | None = None
    started_at: AwareDatetime
    finished_at: AwareDatetime | None = None

    @model_validator(mode="after")
    def require_terminal_outcome(self) -> Self:
        if self.finished_at is not None and self.finished_at < self.started_at:
            raise ValueError("调查结束时间不能早于开始时间")
        if self.status.terminal and self.finished_at is None:
            raise ValueError("终止状态必须记录结束时间")
        if self.status is InvestigationStatus.COMPLETED and not self.conclusion:
            raise ValueError("completed 必须给出根因候选")
        return self


class HypothesisStatus(StrEnum):
    PENDING = "pending"
    SUPPORTED = "supported"
    REFUTED = "refuted"
    INCONCLUSIVE = "inconclusive"


class Hypothesis(Contract):
    id: str
    investigation_id: str
    parent_id: str | None = None
    statement: str
    expected_observations: list[str]
    origin: Literal["model", "runbook", "human"] = "model"
    status: HypothesisStatus = HypothesisStatus.PENDING


class ToolError(Contract):
    code: str
    message: str
    retryable: bool = False


class ToolResult(Contract):
    tool_call_id: str
    source: str
    query: dict[str, Any]
    time_range: TimeRange
    collected_at: AwareDatetime
    data: Any = None
    raw_ref: str | None = None
    truncated: bool = False
    error: ToolError | None = None

    @model_validator(mode="after")
    def distinguish_failure(self) -> Self:
        if self.error is not None and self.data is not None:
            raise ValueError("失败结果不能同时携带成功数据；部分结果应显式建模")
        return self


class ToolCall(Contract):
    id: str
    investigation_id: str
    tool: str
    parameters: dict[str, Any]
    time_range: TimeRange
    started_at: AwareDatetime
    finished_at: AwareDatetime | None = None
    error_code: str | None = None
    retry_count: int = Field(default=0, ge=0)
    truncated: bool = False
    raw_ref: str | None = None

    @model_validator(mode="after")
    def require_finish_after_start(self) -> Self:
        if self.finished_at is not None and self.finished_at < self.started_at:
            raise ValueError("调用结束时间不能早于开始时间")
        return self


class Evidence(Contract):
    id: str
    investigation_id: str
    hypothesis_id: str
    tool_call_id: str
    source: str
    independence_key: str
    observation: str
    relation: Literal["supporting", "refuting", "neutral"]
    raw_ref: str
    time_range: TimeRange


class AuditEvent(Contract):
    id: str
    correlation_id: str
    actor: str
    event: str
    occurred_at: AwareDatetime
    payload: dict[str, Any] = Field(default_factory=dict)


class ActionKind(StrEnum):
    RESTART_POD = "restart_pod"
    SCALE_REPLICAS = "scale_replicas"
    ROLLOUT_UNDO = "rollout_undo"


class ActionTarget(Contract):
    cluster: str
    namespace: str
    kind: str
    name: str
    uid: str
    resource_version: str


class ActionOrigin(StrEnum):
    OPERATOR = "operator"
    MODEL = "model"


class ProposalStatus(StrEnum):
    PENDING_APPROVAL = "pending_approval"
    APPROVED = "approved"
    REJECTED = "rejected"
    DENIED = "denied"
    EXECUTING = "executing"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    UNKNOWN = "unknown"
    EXPIRED = "expired"


class AttemptStatus(StrEnum):
    INTENT = "intent"
    EXECUTING = "executing"
    VERIFIED = "verified"
    FAILED = "failed"
    UNKNOWN = "unknown"


class AttemptOutcome(StrEnum):
    """动作后业务结果与动作前基线的比较；未知不等于失败。"""

    IMPROVED = "improved"
    NO_IMPROVEMENT = "no_improvement"
    WORSENED = "worsened"
    UNKNOWN = "unknown"


class ApprovalDecision(StrEnum):
    APPROVED = "approved"
    REJECTED = "rejected"


class ActionProposal(Contract):
    id: str
    kind: ActionKind
    target: ActionTarget
    parameters: dict[str, Any]
    idempotency_key: str
    reason: str
    created_at: AwareDatetime
    origin: ActionOrigin = ActionOrigin.OPERATOR
    investigation_id: str | None = None
    evidence_ids: list[str] = Field(default_factory=list)
    status: ProposalStatus = ProposalStatus.PENDING_APPROVAL

    @model_validator(mode="after")
    def model_proposal_needs_evidence(self) -> Self:
        if self.origin is ActionOrigin.MODEL and not self.evidence_ids:
            raise ValueError("模型提案必须绑定证据；人工提案记录操作者")
        return self


class ActionAttempt(Contract):
    id: str
    proposal_id: str
    status: AttemptStatus
    idempotency_key: str
    snapshot: dict[str, Any]
    request: dict[str, Any]
    started_at: AwareDatetime
    result: dict[str, Any] | None = None
    verification: dict[str, Any] | None = None
    finished_at: AwareDatetime | None = None


class FollowupStatus(StrEnum):
    OPEN = "open"
    RESOLVED = "resolved"


class ActionFollowup(Contract):
    """异常执行留下的人工待办；解决状态与执行记录分开。"""

    id: str
    proposal_id: str
    attempt_id: str | None = None
    cluster: str
    namespace: str
    target_kind: str
    target_name: str
    outcome: AttemptOutcome
    reason: str
    evidence: dict[str, Any] = Field(default_factory=dict)
    status: FollowupStatus = FollowupStatus.OPEN
    created_at: AwareDatetime
    resolved_at: AwareDatetime | None = None
    resolved_by: str | None = None
    resolution: str | None = None


class Approval(Contract):
    id: str
    proposal_id: str
    proposal_hash: str
    actor: str
    decision: ApprovalDecision
    reason: str
    decided_at: AwareDatetime
    expires_at: AwareDatetime | None = None


class RiskDisposition(StrEnum):
    AUTO = "auto"
    APPROVAL = "approval"
    DENY = "deny"


class RiskDecision(Contract):
    proposal_id: str
    disposition: RiskDisposition
    policy_version: str
    reasons: list[str] = Field(min_length=1)
    requires_compensation: bool
