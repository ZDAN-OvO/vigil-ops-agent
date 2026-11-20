"""调查与会话存储之间的运行边界。"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from aiops.contracts import ActionAttempt, ActionProposal, Approval
from aiops.sessions.models import Lease, SessionEvent, Turn
from aiops.sessions.store import SessionStore


class AwaitingActionReceipt(RuntimeError):
    """动作尝试仍在途，由动作模块收敛后才能继续调查。"""


@runtime_checkable
class CardActions(Protocol):
    async def publish_card(
        self, proposal_id: str, *, lease: Lease, turn_id: str
    ) -> SessionEvent: ...

    async def get_card_decision(self, proposal_id: str, *, session_id: str) -> Approval | None: ...


_CREDENTIAL = re.compile(
    r"""(?ix)
    -----BEGIN [^-]*PRIVATE\ KEY-----.*?-----END [^-]*PRIVATE\ KEY-----
    | (\b(?:bearer|basic)\s+)\S+
    | (\b(?:password|passwd|token|secret|api[_-]?key|authorization)\b["']?\s*[:=]\s*)
      (?:"[^"]*"|'[^']*'|(?:(?:bearer|basic)\s+)?[^\s"',;<>}]+)
    | \b(?:sk-(?:proj-)?|gh[pousr]_|github_pat_)[\w-]+
    | (://[^\s/@:]+:)[^\s/@]+(?=@)
    """,
    re.DOTALL,
)
_LOCAL_PATH = re.compile(
    r"""(?<![\w/:])(?:file://)?/(?:[^/\s"'<>]+/)*[^\s"'<>]+"""
    r"""|(?<![\w/:])(?:~[/\\]|[a-zA-Z]:\\)[^\s"'<>]+"""
)


def redact_text(text: str) -> str:
    """会话独立运行时的文本脱敏；入口可注入动作模块的共享投影。"""
    text = _CREDENTIAL.sub(
        lambda match: next((part for part in match.groups() if part), "") + "[已脱敏]", text
    )
    return _LOCAL_PATH.sub("[本机路径]", text)


@dataclass(frozen=True)
class RunScope:
    sessions: SessionStore
    lease: Lease
    turn: Turn
    check_heartbeat: Callable[[], None]
    project_text: Callable[[str], str]

    def transaction(self):
        self.check_heartbeat()
        return self.sessions.run(self.lease, self.turn.id)

    async def ensure_active(self) -> None:
        async with self.transaction():
            pass


def resume_token(
    proposal: ActionProposal, attempt: ActionAttempt | None, approval: Approval | None
) -> str | None:
    """Approval.id 唯一绑定决定；无批准记录的可恢复终态引用持久化记录。"""
    if attempt is not None and attempt.status.value in {"intent", "executing"}:
        raise AwaitingActionReceipt(attempt.id)
    if approval is not None:
        return approval.id
    if attempt is not None:
        return f"attempt:{attempt.id}"
    if proposal.status.value in {
        "voided",
        "expired",
        "rejected",
        "denied",
        "failed",
        "unknown",
        "succeeded",
    }:
        return f"proposal:{proposal.id}:{proposal.status.value}"
    return None
