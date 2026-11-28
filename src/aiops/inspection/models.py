"""巡检检查登记、运行所有权和观察记录。"""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

from aiops.inspection.config import Signal

InspectionClock = Callable[[], datetime]
ObservationStatus = Literal["healthy", "abnormal", "insufficient"]
RunStatus = Literal["running", "completed", "timed_out", "interrupted", "failed"]


class Check(BaseModel):
    model_config = ConfigDict(frozen=True)

    ordinal: int
    check_id: str
    target: dict[str, str]
    max_age_seconds: int
    signal: Signal | None = None


@dataclass(frozen=True)
class InspectionRun:
    id: str
    session_id: str
    cluster: str
    scheduled_at: datetime
    deadline: datetime
    owner: str
    generation: int
    lease_expires_at: datetime
    sop_hash: str
    status: RunStatus
    reason: str | None
    finished_at: datetime | None
    checks: tuple[Check, ...]
    result: dict[str, Any]


@dataclass(frozen=True)
class Sample:
    status: ObservationStatus
    reason: str | None
    sampled_at: datetime | None
    evidence: dict[str, Any]

    @classmethod
    def insufficient(cls, reason: str, timestamp: datetime | None = None, **evidence) -> "Sample":
        return cls("insufficient", reason, timestamp, evidence)


@dataclass(frozen=True)
class Observation:
    id: str
    ordinal: int
    check_id: str
    target: dict[str, str]
    status: ObservationStatus
    reason: str | None
    sampled_at: datetime | None
    evidence: dict[str, Any]
    evidence_refs: list[str]
