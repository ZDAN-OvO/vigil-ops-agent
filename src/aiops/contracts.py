"""W0 跨模块数据契约；存储、模型与集群实现留到后续阶段。"""

from enum import StrEnum
from typing import Any, Literal, Self

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

# 新调查运行时的版本标识；用于区分“旧调查循环”与“单 Agent 工具循环”的任务记录。
AGENT_RUNTIME_VERSION = "langchain-agent-1"


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
    """分阶段预算：调查与动作各自累计，暂停和进程未运行时间不计入。

    `max_wall_clock_seconds` 是调查阶段的有效运行时间上限，不是任务日历时长；
    动作阶段另有 `max_action_seconds`，两者共同受总有效时间约束。
    """

    max_tool_calls: int = Field(gt=0)
    max_model_calls: int = Field(gt=0)
    max_wall_clock_seconds: int = Field(gt=0)
    max_action_seconds: int = Field(default=450, gt=0)

    @property
    def max_total_seconds(self) -> int:
        return self.max_wall_clock_seconds + self.max_action_seconds


class BudgetUsage(Contract):
    tool_calls: int = Field(default=0, ge=0)
    model_calls: int = Field(default=0, ge=0)
    elapsed_seconds: float = Field(default=0, ge=0)
    action_seconds: float = Field(default=0, ge=0)

    def exhausted(self, budget: Budget) -> str | None:
        """返回最先耗尽的预算项；未耗尽返回 None。"""
        if self.tool_calls >= budget.max_tool_calls:
            return "tool_calls"
        if self.model_calls >= budget.max_model_calls:
            return "model_calls"
        if self.elapsed_seconds >= budget.max_wall_clock_seconds:
            return "wall_clock"
        if self.action_seconds >= budget.max_action_seconds:
            return "action_seconds"
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
    # 反驳条件与修订信息：假设不能删除，被反驳后可由新证据重新打开。
    refutation_criteria: list[str] = Field(default_factory=list)
    revision: int = Field(default=1, ge=1)
    update_reason: str = ""


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
    """旧版证据记录：一条记录绑定一个假设；保留读取兼容，新循环写观测与关系。"""

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


class ObservationStatus(StrEnum):
    OK = "ok"
    EMPTY = "empty"
    ERROR = "error"


class Observation(Contract):
    """一次工具调用的观测记录；原始结果只存一份，可关联多个假设。

    `empty` 表示没有取到数据，不是反证；`error` 表示查询失败，不构成任何证据。
    """

    id: str
    investigation_id: str
    tool_call_id: str
    source: str
    independence_key: str
    summary: str
    raw_ref: str = ""
    status: ObservationStatus = ObservationStatus.OK
    truncated: bool = False
    time_range: TimeRange
    created_at: AwareDatetime | None = None


class HypothesisEvidence(Contract):
    """观测与假设的支持/反驳关系；同一条观测可同时支持一个假设、反驳另一个。"""

    id: str
    investigation_id: str
    hypothesis_id: str
    observation_id: str
    relation: Literal["supporting", "refuting", "neutral"]
    rationale: str
    created_at: AwareDatetime | None = None


class ToolRequestState(StrEnum):
    RUNNING = "running"
    OBSERVED = "observed"
    FAILED = "failed"
    ACTION_PENDING = "action_pending"
    ACTION_EXECUTED = "action_executed"
    REJECTED = "rejected"


class ToolRequest(Contract):
    """工具请求账本：`(investigation_id, tool_call_id)` 唯一，用于重放与审批恢复。

    业务记录记录真实副作用，checkpoint 只记录 Agent 进度；同一请求重放时先读这里。
    """

    investigation_id: str
    tool_call_id: str
    tool: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    state: ToolRequestState
    attempt: int = Field(default=1, ge=1)
    result: Any = None
    proposal_id: str | None = None
    action_attempt_id: str | None = None
    error_code: str | None = None
    started_at: AwareDatetime
    finished_at: AwareDatetime | None = None


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
    RULES = "rules"


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


class ActionPurpose(StrEnum):
    """动作目的；目的不改变权限，只决定必须提供的依据和允许的适用范围。"""

    DIAGNOSTIC = "diagnostic"
    MITIGATION = "mitigation"
    REMEDIATION = "remediation"


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
    purpose: ActionPurpose = ActionPurpose.REMEDIATION
    hypothesis_id: str | None = None
    prediction: str = ""
    observation_conditions: dict[str, Any] = Field(default_factory=dict)
    observation_window_seconds: int | None = None

    @model_validator(mode="after")
    def automatic_proposal_needs_evidence(self) -> Self:
        if self.origin in {ActionOrigin.MODEL, ActionOrigin.RULES} and not self.evidence_ids:
            raise ValueError("自动提案必须绑定证据；人工提案记录操作者")
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


class TaskPhase(StrEnum):
    RUNNING = "running"
    WAITING_APPROVAL = "waiting_approval"
    TERMINAL = "terminal"


class TaskOutcome(StrEnum):
    """业务与任务结果；与 InvestigationStatus 的诊断语义分开记录。"""

    RECOVERED = "recovered"
    DIAGNOSIS_ONLY = "diagnosis_only"
    INCONCLUSIVE = "inconclusive"
    NEEDS_HUMAN = "needs_human"
    ABORTED = "aborted"


class TerminationCode(StrEnum):
    """稳定的停止原因代码；报告用代码，不靠中文错误文本判断。"""

    BUSINESS_RECOVERED = "business_recovered"
    NO_APPLICABLE_ACTION = "no_applicable_action"
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"
    NO_PROGRESS = "no_progress"
    NO_DIFFERENTIATING_TEST = "no_differentiating_test"
    ACTION_NO_IMPROVEMENT = "action_no_improvement"
    ACTION_WORSENED = "action_worsened"
    ACTION_UNKNOWN = "action_unknown"
    POLICY_DENIED = "policy_denied"
    BUDGET_EXHAUSTED = "budget_exhausted"
    CANCELLED = "cancelled"
    MODEL_FAILURE = "model_failure"
    STORE_FAILURE = "store_failure"
    INVALID_REPORT = "invalid_report"
    INTERRUPTED = "interrupted"
    RUNTIME_ERROR = "runtime_error"
    WAITING_APPROVAL = "waiting_approval"


class InvestigationTask(Contract):
    """与 Investigation 一对一的任务运行记录；业务结果与诊断状态分开。"""

    investigation_id: str
    phase: TaskPhase
    outcome: TaskOutcome | None = None
    termination_code: str | None = None
    proposal_id: str | None = None
    runtime_version: str = AGENT_RUNTIME_VERSION
    used_model_calls: int = Field(default=0, ge=0)
    used_tool_calls: int = Field(default=0, ge=0)
    investigation_seconds: float = Field(default=0, ge=0)
    action_seconds: float = Field(default=0, ge=0)
    calendar_started_at: AwareDatetime
    paused_at: AwareDatetime | None = None
    finished_at: AwareDatetime | None = None
    cancel_requested_at: AwareDatetime | None = None
    no_progress_rounds: int = Field(default=0, ge=0)
    recovered_at: AwareDatetime | None = None
    lease_owner: str | None = None
    lease_expires_at: AwareDatetime | None = None
    updated_at: AwareDatetime | None = None

    @model_validator(mode="after")
    def terminal_requires_outcome(self) -> Self:
        if self.phase is TaskPhase.TERMINAL and self.outcome is None:
            raise ValueError("terminal 任务必须有 outcome")
        if self.phase is not TaskPhase.TERMINAL and self.outcome is not None:
            raise ValueError("运行中的任务不能有 outcome")
        if self.phase is TaskPhase.TERMINAL and not self.termination_code:
            raise ValueError("terminal 任务必须有 termination_code")
        return self
