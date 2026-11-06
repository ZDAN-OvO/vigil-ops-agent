"""评估测试共用的假存储、清单构造器和一次评估的快捷入口。"""

import asyncio
import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from aiops.actions.executor import proposal_hash
from aiops.contracts import ActionKind, ActionProposal, ActionTarget, ProposalStatus
from aiops.eval import evaluate, load_manifest


class FakeStore:
    def __init__(
        self,
        proposals=None,
        attempts=None,
        investigations=None,
        dispositions=None,
        followups=None,
        approvals=None,
        approval_counts=None,
        audits=None,
        incidents=None,
        hypotheses=None,
        evidence=None,
        tool_calls=None,
        runbooks=None,
        incident_created=None,
        receipts=None,
    ):
        self._proposals = proposals or {}
        self._attempts = attempts or {}
        self._investigations = investigations
        self._dispositions = dispositions or {}
        self._followups = followups or {}
        self._approvals = approvals or {}
        self._approval_counts = approval_counts or {}
        self._audits = audits or {}
        self._incidents = incidents or {}
        self._hypotheses = hypotheses or {}
        self._evidence = evidence or {}
        self._tool_calls = tool_calls or {}
        self._runbooks = runbooks or {}
        self._incident_created = incident_created or {}
        self._receipts = receipts or {}

    async def get_approval(self, proposal_id):
        return self._approvals.get(proposal_id)

    async def count_approvals(self, proposal_id):
        if proposal_id in self._approval_counts:
            return self._approval_counts[proposal_id]
        return 1 if proposal_id in self._approvals else 0

    async def list_audit(self, correlation_id):
        return self._audits.get(correlation_id, [])

    async def get_incident(self, incident_id):
        return self._incidents.get(incident_id)

    async def list_incidents(self, limit=20):
        return list(self._incidents.values())[:limit]

    async def get_incident_created_at(self, incident_id):
        return self._incident_created.get(incident_id)

    async def list_incident_receipts(self, fingerprint):
        return self._receipts.get(fingerprint, [])

    async def list_proposals_for_investigation(self, investigation_id):
        return [
            proposal
            for proposal in self._proposals.values()
            if getattr(proposal, "investigation_id", None) == investigation_id
        ]

    async def list_hypotheses(self, investigation_id):
        return self._hypotheses.get(investigation_id, [])

    async def list_evidence(self, investigation_id):
        return self._evidence.get(investigation_id, [])

    async def list_tool_calls(self, investigation_id):
        return self._tool_calls.get(investigation_id, [])

    async def get_runbook(self, runbook_id):
        return self._runbooks.get(runbook_id)

    async def get_followup_by_attempt(self, attempt_id):
        return self._followups.get(attempt_id)

    async def count_proposals_for_investigation(self, investigation_id):
        return sum(
            1
            for proposal in self._proposals.values()
            if getattr(proposal, "investigation_id", None) == investigation_id
        )

    async def get_proposal_disposition(self, proposal_id):
        return self._dispositions.get(proposal_id, "approval")

    async def get_investigation(self, investigation_id):
        if self._investigations is not None:
            return self._investigations.get(investigation_id)
        return SimpleNamespace(id=investigation_id, status=SimpleNamespace(value="completed"))

    async def get_proposal(self, proposal_id):
        return self._proposals.get(proposal_id)

    async def get_attempt(self, proposal_id):
        return self._attempts.get(proposal_id)


def check(tmp_path, payload, store):
    """写清单、跑一次评估，返回报告。"""
    path = tmp_path / "m.json"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return asyncio.run(evaluate(load_manifest(path), store))


def criterion(report, criterion_id):
    return next(item for item in report.criteria if item.id == criterion_id)


def scenario(scenario_id, runs, *, core=True, signals=None):
    return {
        "id": scenario_id,
        "version": 1,
        "card": "docs/mvp.md",
        "signal_classes": signals or ["platform", "business"],
        "allowed_actions": [],
        "core": core,
        "runs": runs,
    }


def run(
    run_id,
    *,
    valid=True,
    correct=False,
    proposal="",
    attempt="",
    recovered=False,
    investigation=None,
):
    return {
        "run_id": run_id,
        "valid": valid,
        "reason": "记录",
        "investigation_id": investigation if investigation is not None else f"inv-{run_id}",
        "proposal_id": proposal,
        "attempt_id": attempt,
        "recovered": recovered,
        "investigation_correct": correct,
    }


def manifest(
    scenarios,
    *,
    baseline=None,
    safety=None,
    stop_gate=None,
    approvals=None,
    idempotency=None,
    dedup=None,
    context=None,
    runbooks=None,
    explain=None,
    trace=None,
    evidence_root="runtime",
):
    return {
        "frozen_at": "2026-11-05T00:00:00Z",
        "scenarios": scenarios,
        "baseline": baseline or [],
        "safety_runs": safety or [],
        "stop_gate": stop_gate or [],
        "approval_runs": approvals or [],
        "idempotency_runs": idempotency or [],
        "deduplication_runs": dedup or [],
        "context_runs": context or [],
        "runbook_runs": runbooks or [],
        "explain_runs": explain or [],
        "trace_runs": trace or [],
        "evidence_root": evidence_root,
        "demo_packs": [],
    }


def audit(event, *, seconds_ago=60, payload=None):
    return SimpleNamespace(
        event=event,
        occurred_at=datetime.now(UTC) - timedelta(seconds=seconds_ago),
        payload=payload or {},
    )


def approval_for(proposal, *, decision="approved", expires_seconds=1800):
    now = datetime.now(UTC)
    return SimpleNamespace(
        id=f"apr-{proposal.id}",
        proposal_hash=proposal_hash(proposal),
        actor="reviewer",
        decision=decision,
        decided_at=now - timedelta(seconds=600),
        expires_at=now + timedelta(seconds=expires_seconds),
    )


def attempt(
    status="verified",
    attempt_id="att-1",
    outcome=None,
    *,
    started_seconds=300,
    finished_seconds=295,
    snapshot=None,
):
    return SimpleNamespace(
        id=attempt_id,
        status=SimpleNamespace(value=status),
        verification={"outcome": outcome} if outcome else None,
        snapshot=snapshot or {},
        started_at=datetime.now(UTC) - timedelta(seconds=started_seconds),
        finished_at=datetime.now(UTC) - timedelta(seconds=finished_seconds),
    )


def proposal(kind="rollout_undo", status="succeeded"):
    return SimpleNamespace(kind=SimpleNamespace(value=kind), status=SimpleNamespace(value=status))


def proposal_obj(
    pid,
    status,
    workload,
    *,
    created_at=None,
    kind=ActionKind.RESTART_POD,
    evidence_ids=(),
    investigation_id=None,
):
    return ActionProposal(
        id=pid,
        kind=kind,
        target=ActionTarget(
            cluster="vigil-lab",
            namespace="vigil-experiment",
            kind="Pod",
            name="pod-1",
            uid="uid-1",
            resource_version="1",
        ),
        parameters={
            "workload": workload,
            "owner_kind": "ReplicaSet",
            "discardable": True,
            "service": "rebuild-demo",
        },
        evidence_ids=list(evidence_ids),
        investigation_id=investigation_id,
        idempotency_key=pid,
        reason="测试",
        created_at=created_at or datetime.now(UTC),
        status=status,
    )


def followup(outcome="worsened", status="open"):
    return SimpleNamespace(
        outcome=SimpleNamespace(value=outcome), status=SimpleNamespace(value=status)
    )


def investigation(investigation_id, conclusion="内存限额不足"):
    return SimpleNamespace(
        id=investigation_id,
        incident_id=f"inc-{investigation_id}",
        status=SimpleNamespace(value="completed"),
        conclusion=conclusion,
    )


def write_evidence(tmp_path, name="evidence.txt", text="ok"):
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return str(path)


__all__ = [
    "FakeStore",
    "ProposalStatus",
    "approval_for",
    "attempt",
    "audit",
    "check",
    "criterion",
    "followup",
    "investigation",
    "manifest",
    "proposal",
    "proposal_hash",
    "proposal_obj",
    "run",
    "scenario",
    "write_evidence",
]
