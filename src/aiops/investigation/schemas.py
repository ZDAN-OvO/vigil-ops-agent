"""调查循环对模型暴露的结构：全部字段必填，边界外一律拒绝。"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class HypothesisDraft(BaseModel):
    model_config = ConfigDict(extra="forbid")

    statement: str = Field(min_length=1)
    expected_observations: list[str] = Field(min_length=1)
    rationale: str


class QueryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    hypothesis: int = Field(ge=0, description="当前假设列表的下标")
    tool: Literal[
        "prometheus.query_range",
        "kubernetes.list_deployments",
        "kubernetes.list_pods",
        "kubernetes.list_events",
        "kubernetes.list_services",
        "kubernetes.list_logs",
        "knowledge.list_runbooks",
        "knowledge.get_runbook",
    ]
    purpose: str
    expression: str
    lookback_seconds: int
    step_seconds: int
    selector: str
    involved_name: str


class PlanRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    hypotheses: list[HypothesisDraft] = Field(min_length=1)
    queries: list[QueryRequest]


class HypothesisUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    index: int = Field(ge=0)
    status: Literal["pending", "supported", "refuted", "inconclusive"]
    rationale: str


class Verdict(BaseModel):
    model_config = ConfigDict(extra="forbid")

    evidence_index: int = Field(ge=0)
    relation: Literal["supporting", "refuting", "neutral"]


class RunbookUse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    runbook_id: str = Field(min_length=1)
    accepted: bool
    reason: str = Field(min_length=1)


class Assessment(BaseModel):
    model_config = ConfigDict(extra="forbid")

    hypotheses: list[HypothesisUpdate]
    verdicts: list[Verdict]
    next_queries: list[QueryRequest]
    finish: bool
    root_cause: bool
    conclusion: str
    action_kind: Literal["none", "rollout_undo", "restart_pod"]
    action_deployment: str
    action_pod: str
    action_service: str
    action_path: str
    action_reason: str
    action_evidence: list[int]
    runbook_uses: list[RunbookUse]
