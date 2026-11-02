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


class ActionProposal(Contract):
    id: str
    investigation_id: str
    kind: ActionKind
    target: ActionTarget
    parameters: dict[str, Any]
    evidence_ids: list[str] = Field(min_length=1)
    idempotency_key: str


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
