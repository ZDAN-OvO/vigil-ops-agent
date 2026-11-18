"""Web 请求和响应的独立结构。"""

from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, Field, StringConstraints

from aiops.contracts import ApprovalDecision

RequestId = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=128)]
Content = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=8000)]
ServiceName = Annotated[
    str, StringConstraints(pattern=r"^[a-z0-9][a-z0-9-]{0,61}[a-z0-9]$|^[a-z0-9]$")
]


class WriteRequest(BaseModel, extra="forbid"):
    request_id: RequestId


class CreateRequest(WriteRequest):
    service: ServiceName
    symptoms: Content


class MessageRequest(WriteRequest):
    content: Content


class DecisionRequest(WriteRequest):
    decision: ApprovalDecision
    card_hash: Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
    reason: str = Field(default="", max_length=1000)


class ErrorResponse(BaseModel):
    code: str
    message: str


class WriteResponse(BaseModel):
    id: str


class ServicesResponse(BaseModel):
    services: list[str]


PageState = Literal[
    "investigating",
    "waiting_decision",
    "executing",
    "verifying",
    "finished",
    "interrupted",
    "needs_verification",
]


class ConversationView(BaseModel):
    id: str
    finished: bool
    service: str
    investigation_id: str
    page_state: PageState


class MessageView(BaseModel):
    id: str
    role: Literal["user", "agent"]
    kind: Literal["initial", "hint", "conclusion"]
    content: str
    delivery: Literal["received", "delivered", "unconfirmed"] | None
    delivered_round: int | None
    created_at: datetime


class HypothesisView(BaseModel):
    id: str
    statement: str


class ToolView(BaseModel):
    name: str
    started_at: datetime


class ProgressView(BaseModel):
    supported_hypotheses: list[HypothesisView]
    running_tool: ToolView | None


class ObservationView(BaseModel):
    id: str
    relation: Literal["supports", "refutes"] | None
    status: Literal["ok", "empty", "error"]
    summary: str


class EvidenceView(BaseModel):
    hypothesis_id: str
    statement: str
    status: Literal["pending", "supported", "refuted", "inconclusive"]
    observations: list[ObservationView]


class ChangeView(BaseModel):
    path: str
    before: str | None = Field(serialization_alias="from")
    after: str | None = Field(serialization_alias="to")
    sensitive: bool


class RollbackView(BaseModel):
    from_revision: int
    to_revision: int
    changes: list[ChangeView]


class ProposalView(BaseModel):
    id: str
    kind: Literal["rollout_undo"]
    target: str
    rollback: RollbackView | None
    hypothesis_id: str | None
    evidence_ids: list[str]
    reason: str
    prediction: str
    risk: list[str]
    verification: Literal["首页探针"] = "首页探针"
    status: Literal[
        "pending_approval",
        "approved",
        "rejected",
        "denied",
        "expired",
        "executing",
        "succeeded",
        "failed",
        "unknown",
    ]
    card_hash: str | None
    approval_expires_at: datetime | None


class OutcomeView(BaseModel):
    proposal_id: str
    send_state: Literal["not_sent", "sending", "sent", "uncertain"] | None = None
    stage: Literal["pre_check", "applying", "rolling_out", "verifying", "done"] | None = None
    not_executed_reason: (
        Literal[
            "expired",
            "drift",
            "revision_changed",
            "probe_ambiguous",
            "baseline_unavailable",
            "api_rejected",
        ]
        | None
    ) = None
    verification: Literal["improved", "no_improvement", "worsened", "unknown"] | None = None
    verification_reason: Literal["probe_ambiguous", "interrupted", "template_mismatch"] | None = (
        None
    )
    verification_done: bool = False


class FollowupView(BaseModel):
    id: str
    proposal_id: str
    reason: str
    status: Literal["open", "resolved"]
    resolution: str | None
    resolved_at: datetime | None


class ConclusionView(BaseModel):
    status: Literal["completed", "inconclusive", "aborted"]
    summary: str


class Snapshot(BaseModel):
    schema_version: Literal["1"] = "1"
    conversation: ConversationView | None = None
    messages: list[MessageView] = Field(default_factory=list)
    progress: ProgressView | None = None
    evidence: list[EvidenceView] = Field(default_factory=list)
    proposal: ProposalView | None = None
    outcome: OutcomeView | None = None
    followups: list[FollowupView] = Field(default_factory=list)
    conclusion: ConclusionView | None = None
    last_event_id: int = 0
