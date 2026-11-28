"""把巡检观察、主线回执与既有处置账本衔接。"""

import asyncio
from dataclasses import dataclass, replace
from datetime import timedelta
from uuid import uuid4

import httpx

from aiops.actions.kubernetes_write import WRITE_TIMEOUT_SECONDS
from aiops.actions.policy import CurrentState, TrustedEvidence, decide
from aiops.actions.remediation_alerts import lock_workload_alerts
from aiops.actions.remediation_faults import (
    active_fault,
    create_fault,
    has_inspection_signal,
    health_signal,
    inspection_can_clear,
    jobs_settled,
    lock_fault,
    lock_fault_uid,
    previous_uid_blocks,
    save_inspection_signal,
    signal_is_late,
)
from aiops.actions.remediation_kube import RemediationKubeError, Snapshot
from aiops.actions.remediation_observation import collect_window
from aiops.actions.remediation_policy import InspectionRulePolicy
from aiops.actions.remediation_runtime import RemediationRuntime, _proposal, _set_decision
from aiops.actions.rollout_undo import ActionError
from aiops.actions.safety import _one
from aiops.agent.conversation import ConversationDriver
from aiops.agent.inspection import ReviewReceipt
from aiops.contracts import ActionOrigin, ActionTarget, RiskDisposition
from aiops.inspection.evidence import (
    WORKLOAD_CHECKS,
    fresh,
    latest_target_observations,
    review_samples,
    values,
)
from aiops.inspection.models import Observation
from aiops.inspection.runtime import InspectionRound
from aiops.sessions.projection import project_payload, project_text

_IO_FAILURES = (ActionError, RemediationKubeError, ValueError, httpx.HTTPError)


@dataclass(frozen=True)
class Candidate:
    target: ActionTarget
    fault_id: str
    rule: InspectionRulePolicy
    snapshot: Snapshot
    current: CurrentState
    observations: tuple[Observation, ...]

    @property
    def id(self) -> str:
        return f"{self.fault_id}:{self.rule.id}"

    def trusted(self, samples: tuple[Observation, ...], version: str) -> TrustedEvidence:
        return TrustedEvidence(
            source="inspection",
            rule_id=self.rule.id,
            policy_version=version,
            labels={"cluster": self.target.cluster},
            evidence_ids=tuple(item.id for item in samples),
            observed_at=min(item.sampled_at for item in samples),
            values=self.current.evidence_values(),
            inspection_values={item.check_id: values(item) for item in samples},
        )

    def description(self) -> dict:
        return project_payload(
            {
                "candidate_id": self.id,
                "target": self.target.model_dump(mode="json"),
                "target_uid": self.target.uid,
                "fault_id": self.fault_id,
                "rule_id": self.rule.id,
                "observation_ids": [item.id for item in self.observations],
                "current": self.trusted(self.observations, "").model_dump(mode="json")["values"],
                "current_conditions": [item.model_dump(mode="json") for item in self.rule.evidence],
                "observation_conditions": [
                    item.model_dump(mode="json") for item in self.rule.observations
                ],
            }
        )


class InspectionRemediation:
    def __init__(
        self, driver: ConversationDriver, runtime: RemediationRuntime | None = None
    ) -> None:
        self.driver = driver
        self.runtime = runtime

    async def on_observations(
        self, scope: InspectionRound, observations: tuple[Observation, ...]
    ) -> None:
        abnormal = [item for item in observations if item.status == "abnormal"]
        rules: list[dict] = []
        faults: list[dict] = []
        latest = {item.ordinal: item for item in observations}
        candidates: list[Candidate] = []
        if self.runtime is not None:
            targets = {}
            for check in scope.run.checks:
                if check.check_id in WORKLOAD_CHECKS:
                    key = tuple(check.target[name] for name in ("namespace", "kind", "name"))
                    targets.setdefault(key, {})[check.check_id] = check
            for key, checks in targets.items():
                candidate = await self._workload(scope, key, checks, latest, rules, faults)
                if candidate is not None:
                    candidates.append(candidate)
        results = {
            "abnormal_targets": [
                {"target": item.target, "observation_id": item.id, "check_id": item.check_id}
                for item in abnormal
            ],
            "faults": faults,
            "rules": rules,
        }
        if not abnormal:
            if faults:
                await scope.store.record_result(scope.run, results)
            return
        if self.runtime is None:
            results["remediation_reason"] = "write_identity_or_policy_unavailable"
        remaining = await scope.store.remaining(scope.run)
        # 留出准入、发送预检和回执落账的时间。
        reserve = 2.0
        if candidates:
            reserve += max(WRITE_TIMEOUT_SECONDS, 4 * self.driver.settings.tool_timeout_seconds)

        async def refresh():
            if await scope.store.remaining(scope.run) <= reserve:
                return None
            return await self._refresh_review(scope, candidates, latest, rules)

        timeout = min(self.driver.settings.investigation_budget_seconds, remaining - reserve)
        review, turn, investigation_id, reason = await self.driver.review_inspection(
            scope,
            tuple(latest.values()),
            [candidate.description() for candidate in candidates],
            timeout=timeout,
            refresh=refresh if candidates else None,
        )
        receipts = {} if review is None else {item.candidate_id: item for item in review.receipts}
        if review is not None and (
            len(receipts) != len(review.receipts)
            or not receipts.keys() <= {c.id for c in candidates}
        ):
            receipts = {}
            reason = "invalid_review_binding"
        results["investigation"] = {
            "id": investigation_id,
            "turn_id": turn.id,
            "status": "manual_review" if reason else "reviewed",
            "reason": reason,
            "explanation": review.explanation if review else None,
        }
        await scope.store.record_result(scope.run, results)
        proposal_ids = []
        for candidate in candidates:
            receipt = receipts.get(candidate.id)
            if receipt is None or receipt.status != "confirmed":
                outcome, proposal_id = "review_unconfirmed", None
            else:
                outcome, proposal_id = await self._propose(
                    scope, candidate, receipt, turn.id, investigation_id
                )
            rules.append(
                {
                    "candidate_id": candidate.id,
                    "rule_id": candidate.rule.id,
                    "target": candidate.target.name,
                    "status": outcome,
                    "proposal_id": proposal_id,
                    "review": receipt.conclusion if receipt else None,
                }
            )
            if proposal_id is not None:
                proposal_ids.append(proposal_id)
        manual_required = not candidates or any(
            item["status"] not in {"proposal_created", "fault_already_handled"}
            for item in rules
            if "candidate_id" in item
        )
        await scope.store.record_result(
            scope.run, {"rules": rules, "manual_review_required": manual_required}
        )
        await self._await_dispatch(scope, proposal_ids)

    async def _refresh_review(
        self,
        scope: InspectionRound,
        candidates: list[Candidate],
        latest: dict[int, Observation],
        rules: list[dict],
    ) -> tuple[tuple[Observation, ...], list[dict]] | None:
        now = await scope.store.now()
        changed = False
        updated = []
        for candidate in candidates:
            target = candidate.target
            rule = candidate.rule
            key = {"namespace": target.namespace, "kind": target.kind, "name": target.name}
            checks = {check.check_id: check for check in scope.run.checks if check.target == key}
            if all(
                fresh(item, checks[item.check_id], now, max_age=rule.max_evidence_age_seconds)
                for item in candidate.observations
            ):
                updated.append(candidate)
                continue
            changed = True
            async with self.runtime.ledger.store.transaction() as conn:
                observed = await latest_target_observations(conn, scope.run.id, key)
            for check_id in sorted(rule.required_checks):
                check = checks[check_id]
                item = observed.get(check_id)
                if item is None or not fresh(
                    item,
                    check,
                    await scope.store.now(),
                    max_age=rule.max_evidence_age_seconds,
                ):
                    item = await scope.resample(check)
                    observed[check_id] = item
                latest[check.ordinal] = item
            await self._track(scope, target)
            now = await scope.store.now()
            samples = review_samples(
                scope.run,
                observed,
                uid=target.uid,
                trigger=rule.check_id,
                required=rule.required_checks,
                max_age=rule.max_evidence_age_seconds,
                now=now,
            )
            if samples and candidate.trusted(samples, self.runtime.policy.version).satisfies(
                rule, now
            ):
                updated.append(replace(candidate, observations=samples))
            else:
                rules.append(
                    {
                        "candidate_id": candidate.id,
                        "rule_id": rule.id,
                        "target": target.name,
                        "status": "refreshed_evidence_unverified",
                    }
                )
        if not changed:
            return None
        candidates[:] = updated
        return tuple(latest.values()), [candidate.description() for candidate in candidates]

    async def _workload(self, scope, identity, checks, latest, rules, faults) -> Candidate | None:
        runtime = self.runtime
        registered = ActionTarget(
            cluster=scope.run.cluster,
            namespace=identity[0],
            kind=identity[1],
            name=identity[2],
            uid="unresolved",
            resource_version="",
        )
        if runtime.policy.service_for(registered) is None:
            return None
        if scope.run.cluster != runtime.settings.cluster:
            return None
        try:
            target = await runtime._read_target(*identity)
        except _IO_FAILURES as exc:
            faults.append(
                {
                    "target": identity[2],
                    "status": "target_unavailable",
                    "reason": getattr(exc, "code", type(exc).__name__),
                }
            )
            return None
        now = await scope.store.now()
        for check in checks.values():
            observation = latest[check.ordinal]
            if observation.status == "abnormal" and not fresh(observation, check, now):
                latest[check.ordinal] = await scope.resample(check)
        fault_id, status = await self._track(scope, target)
        if status != "no_activity":
            faults.append(
                {"target": target.name, "uid": target.uid, "fault_id": fault_id, "status": status}
            )
        if status == "healthy":
            try:
                if await self._close_healthy(scope, target, fault_id):
                    faults[-1]["status"] = "cleared"
            except _IO_FAILURES:
                faults[-1]["status"] = "probe_unverified"
        if status != "abnormal":
            return None
        policies = runtime.policy.inspection_rules(target)
        if not policies:
            return None
        enabled = runtime.settings.inspection_enabled and runtime.settings.remediation_auto_enabled
        if not enabled:
            rules.extend(
                {"rule_id": rule.id, "target": target.name, "status": "automatic_disabled"}
                for rule in policies
            )
            return None
        refreshed = set()
        selected = None
        for rule in policies:
            state = "not_matched"
            try:
                candidate = await self._candidate(
                    scope,
                    checks,
                    latest,
                    target,
                    fault_id,
                    rule,
                    refreshed if selected is None else None,
                )
            except _IO_FAILURES:
                candidate = None
                state = "current_state_unverified"
            if candidate is not None:
                state = "lower_priority" if selected else "selected"
                selected = selected or candidate
            rules.append({"rule_id": rule.id, "target": target.name, "status": state})
        return selected

    async def _candidate(
        self, scope, checks, latest, target, fault_id, rule, refreshed
    ) -> Candidate | None:
        if not rule.required_checks <= checks.keys():
            return None
        current_time = await scope.store.now()
        if refreshed is not None:
            for check_id, check in checks.items():
                if check_id not in rule.required_checks or check_id in refreshed:
                    continue
                observation = latest[check.ordinal]
                # 留出预检时间。读失败或过期时仍只补采原来登记的检查。
                buffer = min(
                    rule.max_evidence_age_seconds / 2, 2 * self.driver.settings.tool_timeout_seconds
                )
                if not fresh(
                    observation,
                    check,
                    current_time + timedelta(seconds=buffer),
                    max_age=rule.max_evidence_age_seconds,
                ):
                    latest[check.ordinal] = await scope.resample(check)
                    refreshed.add(check_id)
        current_time = await scope.store.now()
        observations = {name: latest[check.ordinal] for name, check in checks.items()}
        samples = review_samples(
            scope.run,
            observations,
            uid=target.uid,
            trigger=rule.check_id,
            required=rule.required_checks,
            max_age=rule.max_evidence_age_seconds,
            now=current_time,
        )
        if not samples or not all(
            item.matches(observations[item.check_id].evidence.get(item.key))
            for item in rule.observations
        ):
            return None
        runtime = self.runtime
        snapshot = await runtime._snapshot(
            target, rule.kind, rule.parameters.model_dump(exclude_none=True)
        )
        current = await runtime._current(snapshot, snapshot.workload)
        if snapshot.workload.uid != target.uid:
            return None
        candidate = Candidate(snapshot.workload, fault_id, rule, snapshot, current, samples)
        trusted = candidate.trusted(samples, runtime.policy.version)
        return candidate if trusted.satisfies(rule, await scope.store.now()) else None

    async def _track(self, scope, target) -> tuple[str | None, str]:
        key = {"namespace": target.namespace, "kind": target.kind, "name": target.name}
        async with self.runtime.ledger.store.transaction() as conn:
            await lock_workload_alerts(conn, target.cluster, target.namespace, target.name)
            run, _ = await scope.store.require_active(conn, scope.run)
            await lock_fault_uid(conn, target)
            fault = await active_fault(conn, target)
            if await previous_uid_blocks(conn, target):
                return None, "previous_uid_open"
            observations = await latest_target_observations(conn, run.id, key)
            _, now = await scope.store.require_active(conn, scope.run)
            checks = [check for check in run.checks if check.target == key]
            valid = [
                item
                for check in checks
                if (item := observations.get(check.check_id)) and fresh(item, check, now)
            ]
            readiness = observations.get("workload-readiness")
            conflicting_uid = readiness is not None and readiness.evidence.get("uid") not in (
                None,
                target.uid,
            )
            abnormal_at = max(
                (item.sampled_at for item in valid if item.status == "abnormal"), default=None
            )
            if conflicting_uid:
                return None, "target_uid_changed"
            if abnormal_at is not None and await signal_is_late(conn, target, abnormal_at):
                return None, "signal_before_clearance"
            healthy_at = None
            if abnormal_at is not None:
                fault_id = fault["id"] if fault else await create_fault(conn, target, opened_at=now)
                status = "abnormal"
            elif fault is None or not await has_inspection_signal(conn, fault["id"]):
                return None, "no_activity"
            else:
                fault_id = fault["id"]
                status = "insufficient"
                if (
                    len(valid) == len(checks)
                    and readiness in valid
                    and readiness.evidence.get("uid") == target.uid
                ):
                    healthy_at = min(item.sampled_at for item in valid)
                    status = "healthy"
            await save_inspection_signal(
                conn, run.id, fault_id, checks, observations.values(), abnormal_at, healthy_at
            )
            return fault_id, status

    async def _close_healthy(self, scope, target: ActionTarget, fault_id: str) -> bool:
        runtime = self.runtime
        async with runtime.ledger.store.transaction() as conn:
            candidate = await _one(
                conn,
                """
                SELECT f.opened_at, max(s.abnormal_at) AS abnormal_at,
                    (SELECT p.observation_conditions->'verification_plan'
                     FROM remediation_jobs j JOIN action_proposals p ON p.id = j.proposal_id
                     WHERE j.fault_id = f.id AND j.automated AND j.sent_at IS NOT NULL
                       AND j.parent_proposal_id IS NULL) AS probe
                FROM remediation_faults f JOIN remediation_inspection_signals s ON s.fault_id = f.id
                WHERE f.id = %s AND f.cleared_at IS NULL
                GROUP BY f.id
                """,
                (fault_id,),
            )
            if candidate is None or await health_signal(conn, fault_id) is None:
                return False
            jobs = await jobs_settled(conn, fault_id)
            if not jobs["settled"]:
                return False
        earliest = max(
            item
            for item in (
                candidate["opened_at"],
                candidate["abnormal_at"],
                jobs["effective_at"],
                jobs["finished_at"],
            )
            if item is not None
        )
        spec = candidate["probe"]
        service_probe = runtime.policy.service_for(target).probe
        seconds = spec["sample_seconds"] if spec else service_probe.sample_seconds
        duration = spec["duration_seconds"] if spec else service_probe.observation_seconds
        probe = spec["probe"] if spec else service_probe.promql
        now = await scope.store.now()
        count = max(2, duration // seconds)
        start = now - timedelta(seconds=count * seconds)
        if start < earliest:
            return False
        window = await collect_window(
            runtime.metrics, probe, start, now, seconds=seconds, count=count
        )
        summary = window.summary()
        if summary.samples != count or summary.failed != 0:
            return False
        current = await runtime._read_target(target.namespace, target.kind, target.name)
        if current.uid != target.uid:
            return False
        async with runtime.ledger.store.transaction() as conn:
            await lock_workload_alerts(conn, target.cluster, target.namespace, target.name)
            await scope.store.require_active(conn, scope.run)
            fault = await lock_fault(conn, target, fault_id)
            _, now = await scope.store.require_active(conn, scope.run)
            if fault is None or not await inspection_can_clear(
                conn,
                fault,
                window_start=window.start,
                window_end=window.end,
                now=now,
            ):
                return False
            rechecked = await jobs_settled(conn, fault_id)
            if rechecked["finished_at"] is not None and rechecked["finished_at"] > window.start:
                return False
            _, now = await scope.store.require_active(conn, scope.run)
            await conn.execute(
                "UPDATE remediation_faults SET cleared_at = %s WHERE id = %s",
                (now, fault_id),
            )
            return True

    async def _await_dispatch(self, scope: InspectionRound, proposal_ids: list[str]) -> None:
        while proposal_ids:
            await scope.store.remaining(scope.run)
            async with self.runtime.ledger.store.transaction() as conn:
                rows = await conn.execute(
                    """
                    SELECT proposal_id FROM remediation_jobs WHERE proposal_id = ANY(%s)
                      AND sent_at IS NULL AND state <> 'finished'
                    """,
                    (proposal_ids,),
                )
                proposal_ids = [row["proposal_id"] for row in await rows.fetchall()]
            if proposal_ids:
                await asyncio.sleep(0.1)

    async def _propose(
        self,
        scope: InspectionRound,
        candidate: Candidate,
        receipt: ReviewReceipt,
        turn_id: str,
        investigation_id: str,
    ) -> tuple[str, str | None]:
        runtime = self.runtime
        target = candidate.target
        rule = candidate.rule
        if receipt.target_uid != target.uid or not receipt.conclusion.strip():
            return "review_binding_mismatch", None
        if not (runtime.settings.inspection_enabled and runtime.settings.remediation_auto_enabled):
            return "automatic_disabled", None
        try:
            await runtime.require_admission(
                exclusive_config=candidate.snapshot.object.kind == "ConfigMap"
            )
        except _IO_FAILURES:
            return "admission_unverified", None
        async with runtime.ledger.store.transaction() as conn:
            await lock_workload_alerts(conn, target.cluster, target.namespace, target.name)
            run, _ = await scope.store.require_active(conn, scope.run)
            fault = await lock_fault(conn, target, candidate.fault_id)
            if (
                fault is None
                or fault["cleared_at"] is not None
                or await previous_uid_blocks(conn, target)
            ):
                return "fault_changed", None
            prior = await _one(
                conn,
                """
                SELECT 1 FROM remediation_jobs WHERE fault_id = %s
                AND (sent_at IS NOT NULL OR state <> 'finished') LIMIT 1
                """,
                (candidate.fault_id,),
            )
            if prior is not None:
                return "fault_already_handled", None
            observed = await latest_target_observations(
                conn,
                scope.run.id,
                {"namespace": target.namespace, "kind": target.kind, "name": target.name},
            )
            _, now = await scope.store.require_active(conn, scope.run)
            samples = review_samples(
                run,
                observed,
                uid=target.uid,
                trigger=rule.check_id,
                required=rule.required_checks,
                max_age=rule.max_evidence_age_seconds,
                now=now,
            )
            ids = [item.id for item in samples]
            if not ids or sorted(receipt.observation_ids) != sorted(ids):
                return "review_evidence_stale_or_mismatched", None
            trusted = candidate.trusted(samples, runtime.policy.version)
            proposal = _proposal(
                target,
                rule.kind,
                rule.parameters.model_dump(exclude_none=True),
                session_id=run.session_id,
                turn_id=turn_id,
                origin=ActionOrigin.RULES,
                reason=f"核对巡检规则 {rule.id}",
                evidence_ids=ids,
                investigation_id=investigation_id,
            )
            risk = decide(
                proposal,
                target_busy=False,
                policy=runtime.policy,
                current=candidate.current,
                evidence=trusted,
                auto_enabled=True,
                now=now,
            )
            if risk.disposition is not RiskDisposition.AUTO:
                return "rule_not_authorized", None
            spec = runtime._spec(proposal, rule)
            proposal = _set_decision(proposal, risk).model_copy(
                update={
                    "observation_conditions": {
                        "verification_plan": spec,
                        "before": candidate.snapshot.fields,
                        "automatic_compensation": risk.requires_compensation,
                        "policy_version": runtime.policy.version,
                    },
                    "observation_window_seconds": spec["duration_seconds"],
                }
            )
            review_id = uuid4().hex
            await conn.execute(
                """
                INSERT INTO remediation_inspection_reviews (
                    id, run_id, fault_id, turn_id, investigation_id, rule_id, policy_version,
                    sop_hash, observation_ids, conclusion, trigger_check, max_evidence_age_seconds
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    review_id,
                    run.id,
                    candidate.fault_id,
                    turn_id,
                    investigation_id,
                    rule.id,
                    runtime.policy.version,
                    run.sop_hash,
                    ids,
                    project_text(receipt.conclusion),
                    rule.check_id,
                    rule.max_evidence_age_seconds,
                ),
            )
            await runtime.ledger.create(
                conn,
                proposal,
                risk,
                {
                    "snapshot": candidate.snapshot.to_dict(),
                    "trusted": trusted.model_dump(mode="json"),
                    "policy_version": runtime.policy.version,
                },
                fault_id=candidate.fault_id,
                inspection_review_id=review_id,
            )
            await runtime.ledger.publish_rule(conn, proposal)
            await scope.store.require_active(conn, scope.run)
            return "proposal_created", proposal.id
