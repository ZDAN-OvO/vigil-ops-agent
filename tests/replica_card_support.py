"""通过会话租约与 checkpoint 发布真实的受控副本审批卡。"""

from controlled_action_support import REPLICA_NAMESPACE, REPLICA_TARGET
from langgraph.checkpoint.base import empty_checkpoint

from aiops.actions.safety import ActionSafety
from aiops.contracts import ActionKind, ActionOrigin, ProposalStatus
from aiops.sessions.checkpoint import SessionCheckpointer, setup_checkpoints


async def replica_card(runtime, *, group_key=None, web_url=None):
    store = runtime.ledger.store
    await setup_checkpoints(store)
    sessions = runtime.ledger.sessions
    if group_key is None:
        session = await sessions.create_session(title="检查副本容量")
        session_id = session.id
        await sessions.enqueue_input(session_id, "检查容量", display_text="检查容量")
    else:
        async with sessions.alert_transaction(group_key) as conn:
            receipt = await sessions.record_alert(
                conn,
                group_key,
                title="副本容量告警",
                text="检查容量",
                display_text="检查容量",
                repeat_display_text="再次告警",
            )
        session_id = receipt.session_id
    lease = await sessions.acquire_main_lease(session_id, "tester")
    turn = await sessions.start_main_turn(lease)
    proposal, _, _ = await runtime.prepare_manual(
        lease=lease,
        turn_id=turn.id,
        kind=ActionKind.SCALE_REPLICAS,
        target_kind="Deployment",
        namespace=REPLICA_NAMESPACE,
        name=REPLICA_TARGET,
        parameters={"replicas": 2},
        investigation_id=None,
        evidence_ids=["fixture-observation"],
        reason="扩展业务容量",
        origin=ActionOrigin.OPERATOR,
    )
    assert proposal.status is ProposalStatus.PENDING_APPROVAL

    saver = SessionCheckpointer(store, lease, turn)
    cfg = {"configurable": {"thread_id": turn.checkpoint_thread_id, "checkpoint_ns": ""}}
    cfg = await saver.aput(cfg, empty_checkpoint(), {"source": "loop", "step": 1}, {})
    await saver.aput_writes(cfg, [("__interrupt__", [{"proposal_id": proposal.id}])], "card")
    async with sessions.run(lease, turn.id) as tx:
        await tx.set_turn_state("awaiting_approval")
    safety = ActionSafety(store, web_url=web_url)
    card = await safety.publish_card(proposal.id, lease=lease, turn_id=turn.id)
    return lease, proposal, card
