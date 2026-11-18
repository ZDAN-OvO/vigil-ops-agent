"""审批卡片依据取自与提案关联的数据库记录。"""

import hashlib
import json

from psycopg import AsyncConnection

from aiops.actions.executor import proposal_hash
from aiops.contracts import ActionProposal


async def card_hash(conn: AsyncConnection, proposal: ActionProposal) -> str:
    statement = None
    if proposal.hypothesis_id is not None:
        cur = await conn.execute(
            "SELECT statement FROM hypotheses WHERE id = %s AND investigation_id = %s",
            (proposal.hypothesis_id, proposal.investigation_id),
        )
        row = await cur.fetchone()
        statement = row["statement"] if row else None
    cur = await conn.execute(
        """
        SELECT observation_id, relation FROM hypothesis_evidence
         WHERE hypothesis_id = %s AND observation_id = ANY(%s)
         ORDER BY observation_id, relation
        """,
        (proposal.hypothesis_id, proposal.evidence_ids),
    )
    links = await cur.fetchall()
    cur = await conn.execute(
        "SELECT risk_reasons FROM action_proposals WHERE id = %s", (proposal.id,)
    )
    risk = await cur.fetchone()
    payload = [
        proposal_hash(proposal),
        statement,
        proposal.evidence_ids,
        links,
        proposal.reason,
        proposal.prediction,
        risk["risk_reasons"],
    ]
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode()).hexdigest()
