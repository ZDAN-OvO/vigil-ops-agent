"""从同一 PostgreSQL 读取快照生成页面字段。"""

import json
from pathlib import Path

from psycopg import AsyncConnection

from aiops.config import Settings
from aiops.db import Store, _proposal_from_row
from aiops.web.card import card_hash
from aiops.web.models import (
    ChangeView,
    ConclusionView,
    ConversationView,
    EvidenceView,
    FollowupView,
    HypothesisView,
    MessageView,
    ObservationView,
    OutcomeView,
    PageState,
    ProgressView,
    ProposalView,
    RollbackView,
    Snapshot,
    ToolView,
)
from aiops.web.redaction import Redactor

TOOL_NAMES = {
    "kubernetes.list_pods": "Pod 状态",
    "kubernetes.list_deployments": "Deployment 状态",
    "kubernetes.list_services": "Service 状态",
    "kubernetes.list_events": "Kubernetes 事件",
    "kubernetes.list_logs": "Pod 日志",
    "prometheus.query_range": "监控指标",
    "knowledge.list_runbooks": "排查手册",
    "knowledge.get_runbook": "排查手册",
    "task": "委派取证",
}


def observation_summary(row: dict, settings: Settings, clean: Redactor) -> str:
    label = TOOL_NAMES.get(row.get("tool"), "调查查询")
    if row["status"] != "ok":
        return f"{label}：{'未取到数据' if row['status'] == 'empty' else '查询失败'}"
    ref = row["raw_ref"]
    if not ref:
        return f"{label}：已获得结果"
    path = (settings.runtime_path / Path(ref)).resolve()
    if not path.is_relative_to(settings.runtime_path):
        return f"{label}：已获得结果"
    try:
        with path.open("rb") as handle:
            raw = handle.read(512_001)
        if len(raw) > 512_000:
            return f"{label}：已获得结果"
        payload = json.loads(raw)
        if row.get("tool") == "prometheus.query_range":
            series = payload["data"]["result"]
            samples = [entry["values"][-1][1] for entry in series[:3] if entry.get("values")]
            return clean(f"{label}：{len(series)} 条序列，末次样本 {', '.join(map(str, samples))}")
        items = payload["items"]
        if row.get("tool") == "kubernetes.list_logs":
            chunks = [item.get("lines", [])[-2:] for item in items[:3]]
            lines = [line[:240] for chunk in chunks for line in chunk]
            return clean(
                f"{label}：{len(items)} 个采集对象" + ("\n" + "\n".join(lines) if lines else "")
            )
        sections = [f"{label}：{len(items)} 项"]
        for item in items[:4]:
            name = item.get("name")
            status = item.get("phase") or item.get("reason") or item.get("revision")
            if name:
                sections.append(f"{name}" + (f"（{status}）" if status else ""))
        return clean("，".join(sections))
    except (OSError, ValueError, TypeError, KeyError, IndexError, AttributeError):
        return f"{label}：已获得结果"


def page_state(
    conv: dict,
    proposal: ProposalView | None,
    outcome: OutcomeView | None,
    followups: list[FollowupView],
) -> PageState:
    if any(item.status == "open" for item in followups):
        return "needs_verification"
    if conv["interrupted"]:
        return "interrupted"
    if conv["finished_at"]:
        return "finished"
    if outcome and outcome.stage == "verifying":
        return "verifying"
    if outcome and outcome.stage in {"applying", "rolling_out"}:
        return "executing"
    if proposal and proposal.status == "pending_approval" and proposal.card_hash:
        return "waiting_decision"
    return "investigating"


async def rows(conn: AsyncConnection, query: str, *params) -> list[dict]:
    return await (await conn.execute(query, params)).fetchall()


async def read_snapshot(store: Store, settings: Settings) -> Snapshot:
    clean = Redactor(settings)
    async with store._pool.connection() as conn:
        await conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
        selected = await rows(
            conn,
            """
            SELECT c.*, i.status AS investigation_status, e.service, t.ready_proposal_id
              FROM conversations c JOIN investigations i ON i.id = c.investigation_id
              JOIN incidents e ON e.id = i.incident_id
              JOIN investigation_tasks t ON t.investigation_id = i.id
             ORDER BY c.created_at DESC, c.id DESC LIMIT 1
            """,
        )
        if not selected:
            return Snapshot()
        conv = selected[0]
        conv_id, inv_id = conv["id"], conv["investigation_id"]
        messages = [
            MessageView.model_validate(m | {"content": clean(m["content"])})
            for m in await rows(
                conn,
                "SELECT * FROM conversation_messages WHERE conversation_id = %s "
                "ORDER BY created_at, id",
                conv_id,
            )
        ]
        hypotheses = await rows(
            conn,
            "SELECT * FROM hypotheses WHERE investigation_id = %s ORDER BY created_at, id",
            inv_id,
        )
        observations = await rows(
            conn,
            """
            SELECT o.id, o.status, o.raw_ref, h.hypothesis_id, h.relation, t.tool
              FROM observations o
              LEFT JOIN hypothesis_evidence h ON h.observation_id = o.id
              LEFT JOIN tool_requests t
                ON o.tool_call_id = t.investigation_id || ':' || t.tool_call_id
             WHERE o.investigation_id = %s
             ORDER BY o.created_at, o.id
            """,
            inv_id,
        )
        running = await rows(
            conn,
            "SELECT tool, started_at FROM tool_requests WHERE investigation_id = %s "
            "AND state = 'running' ORDER BY started_at DESC LIMIT 1",
            inv_id,
        )
        proposal, outcome = await proposal_snapshot(conn, conv, clean)
        followups = [
            FollowupView.model_validate(
                row
                | {
                    "reason": clean(row["reason"]),
                    "resolution": clean(row["resolution"]) if row["resolution"] else None,
                }
            )
            for row in await rows(
                conn,
                "SELECT f.* FROM action_followups f JOIN action_proposals p "
                "ON p.id = f.proposal_id "
                "WHERE p.investigation_id = %s ORDER BY f.created_at, f.id",
                inv_id,
            )
        ]
        latest_event = await rows(
            conn,
            "SELECT max(id) AS id FROM conversation_events WHERE conversation_id = %s",
            conv_id,
        )
    grouped: dict[str | None, list[ObservationView]] = {}
    for row in observations:
        grouped.setdefault(row["hypothesis_id"], []).append(
            ObservationView(
                id=row["id"],
                relation={"supporting": "supports", "refuting": "refutes"}.get(row["relation"]),
                status=row["status"],
                summary=observation_summary(row, settings, clean),
            )
        )
    evidence = [
        EvidenceView(
            hypothesis_id=row["id"],
            statement=clean(row["statement"]),
            status=row["status"],
            observations=grouped.get(row["id"], []),
        )
        for row in hypotheses
    ]
    if grouped.get(None):
        evidence.append(
            EvidenceView(
                hypothesis_id="",
                statement="未关联的观测",
                status="pending",
                observations=grouped[None],
            )
        )
    conclusion = next((m.content for m in reversed(messages) if m.kind == "conclusion"), None)
    return Snapshot(
        conversation=ConversationView(
            id=conv_id,
            finished=bool(conv["finished_at"]),
            service=clean(conv["service"]),
            investigation_id=inv_id,
            page_state=page_state(conv, proposal, outcome, followups),
        ),
        messages=messages,
        progress=ProgressView(
            supported_hypotheses=[
                HypothesisView(id=h.hypothesis_id, statement=h.statement)
                for h in evidence
                if h.status == "supported"
            ],
            running_tool=ToolView(
                name=TOOL_NAMES.get(running[0]["tool"], "调查查询"),
                started_at=running[0]["started_at"],
            )
            if running
            else None,
        ),
        evidence=evidence,
        proposal=proposal,
        outcome=outcome,
        followups=followups,
        conclusion=ConclusionView(status=conv["investigation_status"], summary=conclusion)
        if conclusion and conv["investigation_status"] not in {"pending", "running"}
        else None,
        last_event_id=latest_event[0]["id"] or 0,
    )


async def proposal_snapshot(
    conn: AsyncConnection, conv: dict, clean: Redactor
) -> tuple[ProposalView | None, OutcomeView | None]:
    proposals = await rows(
        conn,
        "SELECT * FROM action_proposals WHERE investigation_id = %s "
        "ORDER BY created_at DESC, id DESC LIMIT 1",
        conv["investigation_id"],
    )
    if not proposals:
        return None, None
    row = proposals[0]
    proposal = _proposal_from_row(row)
    parameters = proposal.parameters
    approval = await rows(
        conn,
        "SELECT expires_at FROM approvals WHERE proposal_id = %s ORDER BY decided_at DESC LIMIT 1",
        proposal.id,
    )
    attempts = await rows(
        conn, "SELECT result FROM action_attempts WHERE proposal_id = %s", proposal.id
    )
    view = ProposalView(
        id=proposal.id,
        kind=proposal.kind.value,
        target=clean(f"{proposal.target.namespace}/Deployment/{proposal.target.name}"),
        rollback=RollbackView(
            from_revision=parameters["from_revision"],
            to_revision=parameters["to_revision"],
            changes=[ChangeView(**clean.change(c)) for c in parameters["changes"]],
        )
        if "changes" in parameters
        else None,
        hypothesis_id=proposal.hypothesis_id,
        evidence_ids=proposal.evidence_ids,
        reason=clean(proposal.reason),
        prediction=clean(proposal.prediction),
        risk=[clean(reason) for reason in row["risk_reasons"]],
        status=proposal.status.value,
        card_hash=await card_hash(conn, proposal)
        if (row["status"] == "pending_approval" and conv["ready_proposal_id"] == proposal.id)
        else None,
        approval_expires_at=approval[0]["expires_at"] if approval else None,
    )
    outcome = (
        OutcomeView(proposal_id=proposal.id, **(attempts[0]["result"] or {})) if attempts else None
    )
    return view, outcome
