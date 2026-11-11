"""追问前后可比的状态快照：结论、证据、假设与授权。

快照对内容取摘要而不只记 ID，同名记录被改写也能看出来。
单 Agent 调查额外记录观测账本与证据关系，版本升到 3；旧调查保持 v2 形状。
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Protocol

from aiops.actions.executor import proposal_hash
from aiops.contracts import Evidence, Hypothesis, Investigation

SNAPSHOT_VERSION = 3
LEGACY_SNAPSHOT_VERSION = 2


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


def _observation_digest(item: Any) -> str:
    return _digest(
        {
            "source": getattr(item, "source", ""),
            "status": _value(getattr(item, "status", "")),
            "summary": getattr(item, "summary", "") or "",
            "raw_ref": getattr(item, "raw_ref", "") or "",
            "tool_call_id": getattr(item, "tool_call_id", "") or "",
            "truncated": bool(getattr(item, "truncated", False)),
            "independence_key": getattr(item, "independence_key", "") or "",
        }
    )


def _relation_digest(item: Any) -> str:
    return _digest(
        {
            "hypothesis_id": getattr(item, "hypothesis_id", ""),
            "observation_id": getattr(item, "observation_id", ""),
            "relation": getattr(item, "relation", ""),
            "rationale": getattr(item, "rationale", "") or "",
        }
    )


def _hypothesis_digest(item: Hypothesis, *, modern: bool = False) -> str:
    """旧调查保持 v2 摘要字段；单 Agent 快照额外纳入修订与反驳条件。"""
    payload = {
        "statement": getattr(item, "statement", ""),
        "status": _value(getattr(item, "status", "")),
        "expected_observations": list(getattr(item, "expected_observations", [])),
    }
    if modern:
        payload["revision"] = getattr(item, "revision", 0)
        payload["refutation_criteria"] = list(getattr(item, "refutation_criteria", []))
    return _digest(payload)


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
    *,
    observations: list[Any] | None = None,
    relations: list[Any] | None = None,
) -> dict[str, Any]:
    """取状态摘要。

    传入 observations 或 relations 表示单 Agent 调查：版本 3，额外记录观测与关系。
    不传则保持旧调查的 v2 形状，历史追问记录仍能逐字节比对。
    """

    authorization = [
        _authorization(proposal, await store.get_approval(proposal.id))
        for proposal in await store.list_proposals_for_investigation(investigation.id)
    ]
    agent_ledger = observations is not None or relations is not None
    snapshot: dict[str, Any] = {
        "version": SNAPSHOT_VERSION if agent_ledger else LEGACY_SNAPSHOT_VERSION,
        "conclusion_hash": hashlib.sha256(
            (getattr(investigation, "conclusion", "") or "").encode("utf-8")
        ).hexdigest(),
        "evidence": sorted(
            ({"id": item.id, "digest": _evidence_digest(item)} for item in evidence),
            key=lambda item: item["id"],
        ),
        "hypotheses": sorted(
            (
                {"id": item.id, "digest": _hypothesis_digest(item, modern=agent_ledger)}
                for item in hypotheses
            ),
            key=lambda item: item["id"],
        ),
        "authorization": sorted(authorization, key=lambda item: item["proposal_id"]),
    }
    if agent_ledger:
        snapshot["observations"] = sorted(
            ({"id": item.id, "digest": _observation_digest(item)} for item in observations or []),
            key=lambda item: item["id"],
        )
        snapshot["relations"] = sorted(
            ({"id": item.id, "digest": _relation_digest(item)} for item in relations or []),
            key=lambda item: item["id"],
        )
    return snapshot
