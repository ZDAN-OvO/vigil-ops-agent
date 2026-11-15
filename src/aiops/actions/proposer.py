"""动作提案协议：调查侧只声明意图，具体准备与执行由执行器负责。"""

from __future__ import annotations

from typing import Protocol

from aiops.contracts import ActionOrigin, ActionProposal, RiskDecision


class ActionProposer(Protocol):
    """把调查结论转成提案；自动允许时由执行器按既有链路执行并验证。"""

    async def propose_rollback(
        self,
        *,
        deployment: str,
        reason: str,
        actor: str,
        namespace: str | None = None,
        investigation_id: str | None = None,
        origin: ActionOrigin = ActionOrigin.OPERATOR,
        evidence_ids: list[str] | None = None,
    ) -> tuple[ActionProposal, RiskDecision]: ...

    async def propose_restart_pod(
        self,
        *,
        pod: str,
        service: str,
        reason: str,
        actor: str,
        path: str = "/",
        namespace: str | None = None,
        investigation_id: str | None = None,
        origin: ActionOrigin = ActionOrigin.OPERATOR,
        evidence_ids: list[str] | None = None,
    ) -> tuple[ActionProposal, RiskDecision]: ...


__all__ = ["ActionProposer"]
