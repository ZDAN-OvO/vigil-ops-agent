"""追问前后可比的状态快照：结论、证据、假设与授权。

快照对内容取摘要而不只记 ID，同名记录被改写也能看出来。
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Protocol

from aiops.actions.executor import proposal_hash
from aiops.contracts import Evidence, Hypothesis, Investigation

SNAPSHOT_VERSION = 2


class SnapshotStore(Protocol):
    async def list_proposals_for_investigation(self, investigation_id: str) -> list[Any]: ...
    async def get_approval(self, proposal_id: str) -> Any: ...


def _value(item: Any) -> str:
    return str(getattr(item, "value", item) or "")


def _digest(value: Any) -> str:
    canonical = json.dumps(value, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _evidence_digest(item: Evidence) -> str:
    return _digest(
        {
            "source": getattr(item, "source", ""),
            "relation": getattr(item, "relation", ""),
            "observation": getattr(item, "observation", "") or "",
            "raw_ref": getattr(item, "raw_ref", "") or "",
            "tool_call_id": getattr(item, "tool_call_id", "") or "",
            "hypothesis_id": getattr(item, "hypothesis_id", ""),
        }
    )


def _hypothesis_digest(item: Hypothesis) -> str:
    return _digest(
        {
            "statement": getattr(item, "statement", ""),
            "status": _value(getattr(item, "status", "")),
            "expected_observations": list(getattr(item, "expected_observations", [])),
        }
    )


def _authorization(proposal: Any, approval: Any) -> dict[str, str]:
    return {
        "proposal_id": proposal.id,
        "proposal_hash": proposal_hash(proposal),
        "status": _value(proposal.status),
        "approval_id": getattr(approval, "id", "") if approval is not None else "",
        "approval_decision": _value(getattr(approval, "decision", "")),
        "approval_hash": getattr(approval, "proposal_hash", ""),
        "approval_actor": getattr(approval, "actor", ""),
        "approval_expires_at": _value(getattr(approval, "expires_at", "")),
    }


async def state_snapshot(
    store: SnapshotStore,
    investigation: Investigation,
    hypotheses: list[Hypothesis],
    evidence: list[Evidence],
) -> dict[str, Any]:
    authorization = [
        _authorization(proposal, await store.get_approval(proposal.id))
        for proposal in await store.list_proposals_for_investigation(investigation.id)
    ]
    return {
        "version": SNAPSHOT_VERSION,
        "conclusion_hash": hashlib.sha256(
            (getattr(investigation, "conclusion", "") or "").encode("utf-8")
        ).hexdigest(),
        "evidence": sorted(
            ({"id": item.id, "digest": _evidence_digest(item)} for item in evidence),
            key=lambda item: item["id"],
        ),
        "hypotheses": sorted(
            ({"id": item.id, "digest": _hypothesis_digest(item)} for item in hypotheses),
            key=lambda item: item["id"],
        ),
        "authorization": sorted(authorization, key=lambda item: item["proposal_id"]),
    }
