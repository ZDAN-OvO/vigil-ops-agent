"""单 Agent 工具循环的入参与终态结构；全部拒绝未知字段。"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class HypothesisOperation(Strict):
    """一次假设维护操作：创建、细化或改状态。"""

    op: Literal["create", "refine", "update_status"]
    temp_id: str = Field(
        default="", description="create 时的临时引用，如 h1，可被同批 parent_id 引用"
    )
    hypothesis_id: str = Field(default="", description="refine/update_status 的稳定 ID")
    parent_id: str = Field(default="", description="create 的父假设：已有 ID 或同批临时 ID")
    statement: str = ""
    expected_observations: list[str] = Field(default_factory=list)
    refutation_criteria: list[str] = Field(default_factory=list)
    status: Literal["pending", "supported", "refuted", "inconclusive"] | None = None
    reason: str = Field(default="", description="状态或内容变更的依据，必填")


class ManageHypothesesArgs(Strict):
    operations: list[HypothesisOperation] = Field(min_length=1, max_length=12)


class EvidenceLink(Strict):
    observation_id: str
    hypothesis_id: str
    relation: Literal["supporting", "refuting", "neutral"]
    rationale: str = Field(min_length=1)


class HypothesisStatusUpdate(Strict):
    hypothesis_id: str
    status: Literal["pending", "supported", "refuted", "inconclusive"]
    reason: str = Field(min_length=1)


class AssessEvidenceArgs(Strict):
    links: list[EvidenceLink] = Field(min_length=1, max_length=20)
    hypothesis_updates: list[HypothesisStatusUpdate] = Field(default_factory=list, max_length=8)


class RequestActionArgs(Strict):
    """动作请求：目的、目标、依据、预测和业务验证目标。"""

    action: Literal["restart_pod", "rollout_undo"]
    purpose: Literal["diagnostic", "mitigation", "remediation"] = "remediation"
    target_pod: str = ""
    target_deployment: str = ""
    service: str = ""
    path: str = "/"
    reason: str = Field(min_length=1)
    hypothesis_id: str = Field(min_length=1)
    evidence_ids: list[str] = Field(min_length=1, description="本次调查内的观测 ID")
    prediction: str = Field(default="", description="预期出现的变化")
    absence_meaning: str = Field(default="", description="没有出现预期变化意味着什么")
    business_target: str = Field(default="", description="用什么业务请求判断恢复或变化")
    observation_window_seconds: int = Field(default=0, ge=0, le=900)


class ReportDraft(Strict):
    """最终报告候选；运行时会核对引用与任务真实状态。"""

    conclusion: str = Field(description="根因候选；没有确认时写明确的未知边界")
    root_cause_confirmed: bool
    supporting_observation_ids: list[str] = Field(default_factory=list)
    contradictory_observation_ids: list[str] = Field(default_factory=list)
    evidence_boundary: str = Field(default="", description="证据能覆盖到什么、不能覆盖什么")
    known_contradictions: list[str] = Field(default_factory=list)
    remaining_unknowns: list[str] = Field(default_factory=list)
    prediction_check: str = Field(default="", description="诊断动作的预测是否出现")
    needs_human: list[str] = Field(default_factory=list)


__all__ = [
    "AssessEvidenceArgs",
    "EvidenceLink",
    "HypothesisOperation",
    "HypothesisStatusUpdate",
    "ManageHypothesesArgs",
    "ReportDraft",
    "RequestActionArgs",
]
