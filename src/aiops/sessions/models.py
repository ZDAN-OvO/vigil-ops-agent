"""会话持久化记录与出站事件外壳。"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal

TitleOrigin = Literal["pending", "auto", "user", "alert"]
SessionSource = Literal["user", "alert"]
SessionState = Literal["idle", "working", "awaiting_approval", "interrupted", "closed"]
TurnLane = Literal["main", "side"]
TurnState = Literal["queued", "running", "awaiting_approval", "completed", "aborted"]

LEASE_SECONDS = 120
HEARTBEAT_SECONDS = 30


class SessionClosed(RuntimeError):
    """会话已关闭，不能再接收输入或启动运行。"""


class LeaseLost(RuntimeError):
    """租约过期、被接手、会话关闭或轮次超出了此租约的权限。"""


@dataclass(frozen=True)
class Session:
    id: str
    source: SessionSource
    title: str
    state: SessionState
    group_key: str | None
    last_seq: int
    created_at: datetime
    updated_at: datetime
    closed_at: datetime | None
    title_origin: TitleOrigin = "user"


@dataclass(frozen=True)
class Turn:
    id: str
    session_id: str
    lane: TurnLane
    state: TurnState
    checkpoint_thread_id: str
    first_input_id: str
    created_at: datetime
    finished_at: datetime | None


@dataclass(frozen=True)
class SessionInput:
    id: str
    session_id: str
    queued_seq: int
    text: str
    turn_id: str | None
    delivered_step: int | None
    checkpoint_id: str | None
    alert_names: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class Lease:
    session_id: str
    owner: str
    generation: int
    expires_at: datetime
    side_turn_id: str | None = None


@dataclass(frozen=True)
class SessionEvent:
    type: str
    session_id: str
    seq: int
    ts: datetime
    turn_id: str | None
    payload: dict[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return {
            **self.payload,
            "type": self.type,
            "session_id": self.session_id,
            "seq": self.seq,
            "ts": self.ts.isoformat(),
            "turn_id": self.turn_id,
        }


@dataclass(frozen=True)
class AlertReceipt:
    session_id: str
    created: bool
    event: SessionEvent
    turn_id: str | None
