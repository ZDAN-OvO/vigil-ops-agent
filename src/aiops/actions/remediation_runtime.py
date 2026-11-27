"""按登记规则执行并观察受控处置。所有写入先经过持久发送事务。"""

import asyncio
import logging
import re
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import httpx
from psycopg.types.json import Jsonb

from aiops.actions.outcome import SampleSummary
from aiops.actions.policy import (
    CompensationContext,
    CurrentState,
    RemediationPolicy,
    TrustedEvidence,
    decide,
)
from aiops.actions.remediation_alerts import (
    alert_key,
    claim_alert,
    clearance_state,
    complete_alert,
    fault_is_not_clear,
    lock_workload_alerts,
    recovery_candidate,
)
from aiops.actions.remediation_kube import (
    ObservedPod,
    PatchPlan,
    RemediationKubeAdapter,
    RemediationKubeError,
    Snapshot,
    WriteReceipt,
    WriteRejected,
    WriteUnknown,
)
from aiops.actions.remediation_observation import (
    FaultSignal,
    RemediationMetrics,
    WindowSamples,
    check_config_loaded,
    check_cpu,
    check_history,
    check_oom,
    check_replica_capacity,
    collect_window,
    latest_healthy_window,
    worsened,
)
from aiops.actions.remediation_policy import RESOURCE_FIELDS, RulePolicy
from aiops.actions.remediation_store import JobClaim, RemediationStore
from aiops.actions.rollout_undo import ActionError
from aiops.actions.safety import ACTION_HEARTBEAT_SECONDS, _now, _one
from aiops.config import Settings
from aiops.contracts import (
    ActionAttempt,
    ActionKind,
    ActionOrigin,
    ActionProposal,
    ActionTarget,
    AdjustResourcesParams,
    AttemptOutcome,
    AttemptStatus,
    ProposalStatus,
    RiskDecision,
    RiskDisposition,
    UpdateConfigParams,
    parse_action_parameters,
)
from aiops.sessions.models import Lease, LeaseLost

_LOG = logging.getLogger(__name__)
_EVIDENCE_WAIT_SECONDS = 240
_POLL_SECONDS = 5
_FAULT_POD_KEYS = {"oom_killed", "container_restarts", "throttle_ratio"}


def _proposal(
    target: ActionTarget,
    kind: ActionKind,
    parameters: dict,
    *,
    session_id: str,
    turn_id: str,
    origin: ActionOrigin,
    reason: str,
    evidence_ids: list[str],
    investigation_id: str | None = None,
) -> ActionProposal:
    identifier = f"act-{uuid4().hex}"
    return ActionProposal(
        id=identifier,
        kind=kind,
        target=target,
        parameters=parameters,
        reason=reason,
        idempotency_key=identifier,
        created_at=datetime.now(UTC),
        origin=origin,
        evidence_ids=evidence_ids,
        session_id=session_id,
        session_turn_id=turn_id,
        investigation_id=investigation_id,
    )


def _set_decision(proposal: ActionProposal, risk: RiskDecision) -> ActionProposal:
    return proposal.model_copy(
        update={
            "status": {
                RiskDisposition.AUTO: ProposalStatus.APPROVED,
                RiskDisposition.APPROVAL: ProposalStatus.PENDING_APPROVAL,
                RiskDisposition.DENY: ProposalStatus.DENIED,
            }[risk.disposition]
        }
    )


class RemediationRuntime:
    def __init__(
        self,
        settings: Settings,
        ledger: RemediationStore,
        policy: RemediationPolicy,
        kube: RemediationKubeAdapter,
        metrics: RemediationMetrics,
    ) -> None:
        self.settings = settings
        self.ledger = ledger
        self.policy = policy
        self.kube = kube
        self.metrics = metrics

    async def _read_target(self, namespace: str, kind: str, name: str) -> ActionTarget:
        return await asyncio.to_thread(
            self.kube.target, self.settings.cluster, namespace, kind, name
        )

    async def _snapshot(self, target: ActionTarget, kind: ActionKind, parameters: dict) -> Snapshot:
        typed = parse_action_parameters(kind, parameters)
        config_map = None
        if isinstance(typed, UpdateConfigParams) and typed.source == "config_map":
            await self.require_admission(exclusive_config=True)
            config_map = await self._read_target(target.namespace, "ConfigMap", typed.config_map)
        return await asyncio.to_thread(
            self.kube.snapshot,
            target,
            kind.value,
            parameters,
            config_map_target=config_map,
        )

    async def _current(self, snapshot: Snapshot, target: ActionTarget) -> CurrentState:
        data = await asyncio.to_thread(self.kube.current_parameters, snapshot)
        values = {
            key: value for key, value in (snapshot.parameters | data).items() if value is not None
        }
        typed = parse_action_parameters(ActionKind(snapshot.kind), values)
        if isinstance(typed, UpdateConfigParams):
            service = self.policy.service_for(target)
            field = service.config_field(typed)
            if field is None or typed.value not in field.allowed_values:
                raise ActionError("unregistered_current_value", "当前配置值不属于登记的普通取值")
        return CurrentState(
            target=target,
            parameters=typed,
            config_map_exclusive=snapshot.object.kind == "ConfigMap",
        )

    async def require_admission(self, *, exclusive_config: bool = False) -> None:
        await asyncio.to_thread(
            self.kube.require_admission,
            self.policy.version,
            exclusive_config=exclusive_config,
        )

    def _spec(self, proposal: ActionProposal, rule=None) -> dict:
        service = self.policy.service_for(proposal.target)
        probe = (rule.probe if rule else None) or service.probe
        params = parse_action_parameters(proposal.kind, proposal.parameters)
        if rule is None and isinstance(params, UpdateConfigParams):
            for candidate in service.rules:
                known = candidate.parameters
                if (
                    isinstance(known, UpdateConfigParams)
                    and service.config_field(known) == service.config_field(params)
                    and candidate.probe
                ):
                    probe = candidate.probe
                    break
        return {
            "method": "business_probe_window",
            "probe": probe.promql,
            "duration_seconds": probe.observation_seconds,
            "sample_seconds": probe.sample_seconds,
        }

    def _load_query(self, proposal: ActionProposal) -> str | None:
        if proposal.kind is not ActionKind.UPDATE_CONFIG:
            return None
        params = parse_action_parameters(proposal.kind, proposal.parameters)
        service = self.policy.service_for(proposal.target)
        field = service.config_field(params)
        return field.load_signal_for(params.value) if field is not None else None

    def _observation_rules(self, claim: JobClaim, plan: PatchPlan) -> tuple[RulePolicy, ...]:
        proposal = claim.proposal
        evidence = claim.job["evidence"]
        service = self.policy.service_for(proposal.target)
        if service is None or evidence.get("policy_version") != self.policy.version:
            raise ActionError("policy_changed", "无法确认动作绑定的观察规则")
        if "trusted" in evidence:
            trusted = TrustedEvidence.model_validate(evidence["trusted"])
            rule = self.policy.match_rule(proposal.target, trusted.labels)
            if (
                rule is None
                or rule.id != trusted.rule_id
                or trusted.policy_version != self.policy.version
                or rule.kind is not proposal.kind
                or rule.parameters != parse_action_parameters(proposal.kind, proposal.parameters)
            ):
                raise ActionError("policy_changed", "无法确认动作绑定的观察规则")
            return (rule,)
        if proposal.origin is ActionOrigin.RULES:
            raise ActionError("observation_rule_missing", "缺少动作绑定的观察规则")
        params = parse_action_parameters(proposal.kind, proposal.parameters)
        resources = {
            key.split("_", 1)[1]
            for key, value in plan.after.items()
            if key in RESOURCE_FIELDS and value != plan.before.fields[key]
        }
        rules = []
        for rule in service.rules:
            if rule.kind is not proposal.kind:
                continue
            known = rule.parameters
            if isinstance(params, AdjustResourcesParams) and (
                not isinstance(known, AdjustResourcesParams)
                or params.container != known.container
                or not any(
                    getattr(known, key) is not None
                    for key in RESOURCE_FIELDS
                    if key.split("_", 1)[1] in resources
                )
            ):
                continue
            if isinstance(params, UpdateConfigParams) and (
                not isinstance(known, UpdateConfigParams)
                or service.config_field(params) != service.config_field(known)
            ):
                continue
            rules.append(rule)
        return tuple(rules)

    async def _observation_pods(
        self, claim: JobClaim, plan: PatchPlan, receipt: WriteReceipt, rules: tuple[RulePolicy, ...]
    ) -> tuple[ObservedPod, ...]:
        params = parse_action_parameters(claim.proposal.kind, claim.proposal.parameters)
        if not isinstance(params, AdjustResourcesParams) or not any(
            predicate.key in _FAULT_POD_KEYS for rule in rules for predicate in rule.evidence
        ):
            return ()
        return await asyncio.to_thread(self.kube.observation_pods, plan, receipt, params.container)

    async def _fault_signals(
        self,
        claim: JobClaim,
        plan: PatchPlan,
        receipt: WriteReceipt,
        rules: tuple[RulePolicy, ...],
        end: datetime,
        load_query: str | None,
    ) -> list[FaultSignal]:
        start = claim.job["effective_at"]
        proposal = claim.proposal
        predicates = {
            predicate.key: predicate
            for rule in rules
            for predicate in rule.evidence
            if predicate.source == "observed" and predicate.key != "business_failed"
        }
        signals = []
        if predicates.keys() & _FAULT_POD_KEYS:
            pods = await self._observation_pods(claim, plan, receipt, rules)
            initial = claim.job["observation"].get("fault_pods", {})
            container = proposal.parameters["container"]
            namespace = proposal.target.namespace
            if predicates.keys() & {"oom_killed", "container_restarts"}:
                signals.extend(
                    await check_oom(
                        self.metrics,
                        namespace,
                        container,
                        {uid: pod["restarts"] for uid, pod in initial.items()},
                        pods,
                        start,
                        end,
                    )
                )
                if initial.keys() != {pod.uid for pod in pods}:
                    signals.append(FaultSignal("oom_killed", "unknown", {"pods_changed": True}))
            if "throttle_ratio" in predicates:
                if initial and {uid: pod["container_id"] for uid, pod in initial.items()} == {
                    pod.uid: pod.container_id for pod in pods
                }:
                    signals.append(
                        await check_cpu(
                            self.metrics,
                            namespace,
                            container,
                            pods,
                            start,
                            end,
                            adverse=predicates["throttle_ratio"].matches,
                        )
                    )
                else:
                    signals.append(FaultSignal("throttle_ratio", "unknown", {"pods_changed": True}))
        checks = []
        if load_query is not None:
            checks.append(check_config_loaded(self.metrics, load_query, start, end))
        rejected = predicates.get("rejected_requests")
        endpoints = predicates.get("ready_endpoints")
        if (
            proposal.kind is ActionKind.SCALE_REPLICAS
            and rejected
            and rejected.promql
            and endpoints
            and endpoints.promql
        ):
            signals.extend(
                await check_replica_capacity(
                    self.metrics,
                    rejected.promql,
                    endpoints.promql,
                    plan.after["replicas"],
                    start,
                    end,
                )
            )
            predicates.pop("rejected_requests")
            predicates.pop("ready_endpoints")
        for key, predicate in predicates.items():
            if key in _FAULT_POD_KEYS:
                continue
            if predicate.promql is None:
                signals.append(FaultSignal(key, "unknown", {"selector_missing": True}))
                continue
            options = {"valid": _probe_signal} if key == "gate_failed" else {}
            checks.append(
                check_history(
                    self.metrics,
                    key,
                    predicate.promql,
                    start,
                    end,
                    adverse=predicate.matches,
                    **options,
                )
            )
        if checks:
            signals.extend(await asyncio.gather(*checks))
        return signals

    async def prepare_manual(
        self,
        *,
        lease: Lease,
        turn_id: str,
        kind: ActionKind,
        target_kind: str,
        namespace: str,
        name: str,
        parameters: dict,
        investigation_id: str,
        evidence_ids: list[str],
        reason: str,
        **details,
    ) -> tuple[ActionProposal, RiskDecision, None]:
        # 拒绝无登记的普通字段和值，禁止它们进入提案和工具日志。
        params = parse_action_parameters(kind, parameters)
        identity = ActionTarget(
            cluster=self.settings.cluster,
            namespace=namespace,
            kind=target_kind,
            name=name,
            uid="",
            resource_version="",
        )
        service = self.policy.service_for(identity)
        if service is None or (
            isinstance(params, UpdateConfigParams)
            and (
                service.config_field(params) is None
                or params.value not in service.config_field(params).allowed_values
            )
        ):
            raise ActionError("policy_denied", "目标或普通配置值未登记")
        target = await self._read_target(namespace, target_kind, name)
        snapshot = await self._snapshot(target, kind, parameters)
        current = await self._current(snapshot, snapshot.workload)
        proposal = _proposal(
            snapshot.workload,
            kind,
            params.model_dump(exclude_none=True),
            session_id=lease.session_id,
            turn_id=turn_id,
            origin=ActionOrigin.MODEL,
            reason=reason,
            evidence_ids=evidence_ids,
            investigation_id=investigation_id,
        )
        conditions = self._spec(proposal)
        proposal = proposal.model_copy(
            update={
                "observation_conditions": {
                    "verification_plan": conditions,
                    "before": snapshot.fields,
                    "policy_version": self.policy.version,
                },
                "observation_window_seconds": conditions["duration_seconds"],
                **details,
            }
        )
        risk = decide(
            proposal,
            target_busy=False,
            policy=self.policy,
            current=current,
            auto_enabled=self.settings.remediation_auto_enabled,
        )
        proposal = _set_decision(proposal, risk)
        await self.ledger.save_manual(
            proposal,
            risk,
            {"snapshot": snapshot.to_dict(), "policy_version": self.policy.version},
            lease=lease,
            turn_id=turn_id,
        )
        return proposal, risk, None

    async def _metrics_evidence(self, rule, current: CurrentState, at: datetime) -> dict:
        values = current.evidence_values()
        sample_times = {}
        for predicate in rule.evidence:
            if predicate.promql:
                result = await self.metrics.fresh(
                    predicate.promql,
                    at,
                    max_age=rule.max_evidence_age_seconds,
                )
                if result is not None:
                    values[predicate.key], sample_times[predicate.key] = result
        params = current.parameters
        keys = {predicate.key for predicate in rule.evidence}
        if keys & {"oom_killed", "container_restarts"}:
            pods = await asyncio.to_thread(
                self.kube.pod_evidence,
                current.target,
                params.container,
                now=at,
                max_age_seconds=rule.max_evidence_age_seconds,
            )
            values["oom_killed"] = bool(pods)
            values["container_restarts"] = max((pod.restart_count for pod in pods), default=0)
            values["pod_evidence"] = [
                {
                    "pod": pod.pod_name,
                    "uid": pod.pod_uid,
                    "restarts": pod.restart_count,
                    "reason": pod.reason,
                    "finished_at": pod.finished_at.isoformat(),
                }
                for pod in pods
            ]
        if "throttle_ratio" in keys:
            pods = await asyncio.to_thread(self.kube.current_pods, current.target, params.container)
            if pods:
                pattern = "|".join(re.escape(name) for name in pods)
                selector = (
                    f'namespace="{current.target.namespace}",pod=~"{pattern}",'
                    f'container="{params.container}"'
                )
                periods = f"sum(rate(container_cpu_cfs_periods_total{{{selector}}}[1m]))"
                throttled = (
                    f"sum(rate(container_cpu_cfs_throttled_periods_total{{{selector}}}[1m]))"
                )
                metrics = (
                    "container_cpu_cfs_periods_total",
                    "container_cpu_cfs_throttled_periods_total",
                )
                stamps = [
                    await self.metrics.scalar(f"min(timestamp({metric}{{{selector}}}))", at)
                    for metric in metrics
                ]
                if all(
                    stamp is not None
                    and 0
                    <= at.timestamp() - stamp
                    <= min(
                        rule.max_evidence_age_seconds,
                        60,
                    )
                    for stamp in stamps
                ):
                    values["throttle_ratio"] = await self.metrics.scalar(
                        f"{throttled} / ({periods} > 0)",
                        at,
                    )
                    sample_times["throttle_ratio"] = min(stamps)
        values["prometheus_sample_times"] = sample_times
        return values

    async def evaluate_one(self) -> None:
        if not self.settings.remediation_auto_enabled:
            return
        alert = await claim_alert(self.ledger.store)
        if alert is None:
            return
        if alert["resolved_at"] is not None:
            await self._record_alert_result(alert, "已记录恢复，无需启动处置")
            return
        labels = alert["labels"]
        registered = next(
            (
                service
                for service in self.policy.services
                if (service.namespace, service.name)
                == (labels.get("namespace"), labels.get("target"))
            ),
            None,
        )
        if registered is None or alert["cluster"] != self.settings.cluster:
            await self._record_alert_result(alert, "没有匹配的登记目标，已转人工排查")
            return
        if labels.get("cluster", alert["cluster"]) != alert["cluster"]:
            await self._record_alert_result(alert, "告警集群与登记目标不一致，已转人工排查")
            return
        try:
            target = await self._read_target(*registered.identity)
            rule = self.policy.match_rule(target, labels)
            if rule is None:
                await self._record_alert_result(alert, "未找到匹配的处置规则，已转人工排查")
                return
            parameters = rule.parameters.model_dump(exclude_none=True)
            snapshot = await self._snapshot(target, rule.kind, parameters)
            current = await self._current(snapshot, snapshot.workload)
            at = datetime.now(UTC)
            values = await self._metrics_evidence(rule, current, at)
        except (ActionError, RemediationKubeError, httpx.HTTPError, ValueError) as exc:
            await self._record_alert_result(
                alert, f"无法核实处置现场（{type(exc).__name__}），已转人工"
            )
            return
        missing = [item.key for item in rule.evidence if not item.matches(values.get(item.key))]
        if missing and (at - alert["received_at"]).total_seconds() < _EVIDENCE_WAIT_SECONDS:
            async with self.ledger.store.transaction() as conn:
                await complete_alert(conn, alert, retry_seconds=15)
            return
        try:
            recovery = await self._recovery_window(snapshot.workload, alert)
        except httpx.HTTPError:
            if (at - alert["received_at"]).total_seconds() < _EVIDENCE_WAIT_SECONDS:
                async with self.ledger.store.transaction() as conn:
                    await complete_alert(conn, alert, retry_seconds=15)
                return
            recovery = None
        with suppress(LeaseLost):
            await self._record_rule(alert, rule, snapshot, current, values, at, missing, recovery)

    async def _recovery_window(
        self, target: ActionTarget, alert: dict
    ) -> tuple[dict, WindowSamples] | None:
        end = alert["starts_at"]
        if end > alert["received_at"]:
            return None
        async with self.ledger.store.transaction() as conn:
            candidate = await recovery_candidate(conn, target, next_start=end)
        if candidate is None:
            return None
        spec = candidate["probe"]
        if not isinstance(spec, dict) or spec.get("method") != "business_probe_window":
            return None
        seconds = spec.get("sample_seconds")
        duration = spec.get("duration_seconds")
        probe = spec.get("probe")
        if (
            not isinstance(seconds, int)
            or not isinstance(duration, int)
            or seconds <= 0
            or duration <= 0
            or duration % seconds
            or not isinstance(probe, str)
            or not probe
        ):
            return None
        # 新一轮告警之后的样本不能证明两次故障之间曾经恢复。
        window = await latest_healthy_window(
            self.metrics,
            probe,
            candidate["earliest_start"],
            end,
            seconds=seconds,
            count=max(2, duration // seconds),
        )
        return (candidate, window) if window is not None else None

    async def _record_alert_result(self, alert: dict, message: str) -> None:
        async with self.ledger.sessions.alert_transaction(alert["group_key"]) as conn:
            session = await self.ledger.sessions.lock_session(conn, alert["session_id"])
            if await complete_alert(conn, alert) and session.closed_at is None:
                await self.ledger.note(conn, alert["session_id"], message)
            await self._close_recovered(conn, session.id)

    async def _fault(
        self,
        conn,
        target: ActionTarget,
        alert: dict,
        recovery: tuple[dict, WindowSamples] | None = None,
    ) -> str | None:
        key = f"{target.cluster}/{target.namespace}/{target.uid}"
        await conn.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 1))", (key,))
        identity = (target.cluster, target.namespace, target.uid)
        last_clearance = await _one(
            conn,
            """
            SELECT cleared_at FROM remediation_faults
            WHERE (cluster, namespace, workload_uid) = (%s, %s, %s) AND cleared_at IS NOT NULL
            ORDER BY cleared_at DESC LIMIT 1
            """,
            identity,
        )
        starts_at = alert["starts_at"]
        if last_clearance and starts_at < last_clearance["cleared_at"]:
            return None
        fault = await _one(
            conn,
            """
            SELECT id FROM remediation_faults
            WHERE (cluster, namespace, workload_uid) = (%s, %s, %s)
                AND cleared_at IS NULL FOR UPDATE
            """,
            identity,
        )
        if fault is not None:
            if recovery is None or fault["id"] != recovery[0]["fault_id"]:
                return fault["id"]
            candidate, window = recovery
            confirmed = await recovery_candidate(conn, target, next_start=starts_at)
            if confirmed != candidate:
                return fault["id"]
            await conn.execute(
                "UPDATE remediation_faults SET cleared_at = %s WHERE id = %s",
                (window.end, fault["id"]),
            )
            await self.ledger.safety.audit(
                conn,
                candidate["proposal_id"],
                "rules",
                "remediation.fault_cleared",
                {
                    "fault_id": fault["id"],
                    "next_alert": {
                        "group_key": alert["group_key"],
                        "fingerprint": alert["fingerprint"],
                        "starts_at": starts_at.isoformat(),
                    },
                    "probe": candidate["probe"]["probe"],
                    "cleared_at": window.end.isoformat(),
                    "window": window.payload(),
                },
            )
        identifier = uuid4().hex
        await conn.execute(
            """
            INSERT INTO remediation_faults (id, cluster, namespace, workload_kind,
                workload_name, workload_uid) VALUES (%s, %s, %s, %s, %s, %s)
            """,
            (identifier, target.cluster, target.namespace, target.kind, target.name, target.uid),
        )
        return identifier

    async def _record_rule(
        self, alert, rule, snapshot, current, values, at, missing, recovery=None
    ) -> None:
        async with self.ledger.sessions.alert_transaction(alert["group_key"]) as conn:
            target = snapshot.workload
            # 与 webhook、发送授权和成功结清共用顺序：先目标告警锁，再会话和故障行。
            await lock_workload_alerts(conn, target.cluster, target.namespace, target.name)
            session = await self.ledger.sessions.lock_session(conn, alert["session_id"])
            saved = await _one(
                conn,
                """
                SELECT * FROM remediation_alerts
                WHERE (cluster, group_key, fingerprint, starts_at) = (%s, %s, %s, %s)
                FOR UPDATE
                """,
                alert_key(alert),
            )
            if saved["evaluation_owner"] != alert["evaluation_owner"]:
                return
            if saved["resolved_at"] is not None or session.closed_at is not None:
                await complete_alert(conn, alert)
                await self._close_recovered(conn, session.id)
                return
            fault_id = await self._fault(conn, target, saved, recovery)
            if not await complete_alert(conn, alert, fault_id=fault_id):
                raise LeaseLost(alert["session_id"])
            if fault_id is None:
                return
            prior = await _one(
                conn,
                """
                SELECT 1 FROM remediation_jobs WHERE fault_id = %s
                AND (sent_at IS NOT NULL OR state <> 'finished') LIMIT 1
                """,
                (fault_id,),
            )
            if prior is not None:
                await self.ledger.note(conn, session.id, "同一次故障已有处置记录，继续核对业务状态")
                return
            if missing:
                await self.ledger.note(
                    conn,
                    session.id,
                    "规则证据不足，已转人工核对：" + "、".join(missing),
                )
            turn = await _one(
                conn,
                "SELECT id FROM session_turns WHERE session_id = %s ORDER BY created_at LIMIT 1",
                (session.id,),
            )
            investigation_id, evidence_id = await self._rule_evidence(
                conn,
                alert,
                snapshot.workload,
                rule.id,
                values,
                at,
            )
            proposal = _proposal(
                snapshot.workload,
                rule.kind,
                rule.parameters.model_dump(exclude_none=True),
                session_id=session.id,
                turn_id=turn["id"],
                origin=ActionOrigin.RULES,
                reason=f"核对告警 {rule.alertname} 对应的规则 {rule.id}",
                evidence_ids=[evidence_id],
                investigation_id=investigation_id,
            )
            trusted = TrustedEvidence(
                rule_id=rule.id,
                policy_version=self.policy.version,
                labels=alert["labels"],
                evidence_ids=(evidence_id,),
                observed_at=_oldest_evidence_time(values, at),
                values=values,
            )
            decision = decide(
                proposal,
                target_busy=False,
                policy=self.policy,
                current=current,
                evidence=trusted,
                auto_enabled=self.settings.remediation_auto_enabled and not missing,
                now=await _now(conn),
            )
            proposal = _set_decision(proposal, decision)
            spec = self._spec(proposal, rule)
            proposal = proposal.model_copy(
                update={
                    "observation_conditions": {
                        "verification_plan": spec,
                        "before": snapshot.fields,
                        "automatic_compensation": decision.requires_compensation,
                        "policy_version": self.policy.version,
                    },
                    "observation_window_seconds": spec["duration_seconds"],
                }
            )
            await self.ledger.create(
                conn,
                proposal,
                decision,
                {
                    "snapshot": snapshot.to_dict(),
                    "trusted": trusted.model_dump(mode="json"),
                    "policy_version": self.policy.version,
                },
                fault_id=fault_id,
            )
            await self.ledger.sessions.append_event(
                conn,
                session.id,
                "tool_call",
                {
                    "id": evidence_id,
                    "observation_id": evidence_id,
                    "tool": "remediation_evidence",
                    "status": "ok",
                    "lane": "main",
                    "summary": f"复核规则 {rule.id} 的告警和现场证据",
                    "result": trusted.model_dump(mode="json")["values"],
                },
                turn_id=turn["id"],
            )
            await self.ledger.publish_rule(conn, proposal)

    async def _rule_evidence(self, conn, alert, target, rule_id: str, values: dict, at):
        incident, investigation, call, evidence = (uuid4().hex for _ in range(4))
        start = min(alert["starts_at"], at - timedelta(seconds=1))
        detail = Jsonb({"rule": rule_id, "facts": values}, dumps=_json_with_decimal)
        await conn.execute(
            """
            INSERT INTO incidents (id, environment, cluster, namespace, service, trigger,
                symptoms, time_start, time_end, alert_ids, alert_names)
            VALUES (%s, %s, %s, %s, %s, 'alert', %s, %s, %s, %s, %s)
            """,
            (
                incident,
                self.settings.environment,
                target.cluster,
                target.namespace,
                target.name,
                alert["labels"]["alertname"],
                start,
                at,
                [alert["fingerprint"]],
                [alert["labels"]["alertname"]],
            ),
        )
        await conn.execute(
            """
            INSERT INTO investigations (id, incident_id, status, model, policy_version,
                budget_tool_calls, budget_model_calls, budget_wall_clock_seconds, started_at,
                finished_at, conclusion)
            VALUES (%s, %s, 'completed', 'rules', %s, 1, 1, 120, %s, %s, %s)
            """,
            (investigation, incident, self.policy.version, start, at, f"已核对规则 {rule_id}"),
        )
        await conn.execute(
            """
            INSERT INTO tool_requests
                (investigation_id, tool_call_id, tool, arguments, state, result,
                 started_at, finished_at)
            VALUES (%s, %s, 'remediation_evidence', %s, 'observed', %s, %s, %s)
            """,
            (investigation, call, Jsonb({"rule": rule_id}), detail, start, at),
        )
        await conn.execute(
            """
            INSERT INTO observations (id, investigation_id, tool_call_id, source, independence_key,
                summary, status, time_start, time_end)
            VALUES (%s, %s, %s, 'remediation_rules', %s, %s, 'ok', %s, %s)
            """,
            (evidence, investigation, call, rule_id, f"告警 {rule_id} 的处置证据", start, at),
        )
        return investigation, evidence

    async def _close_recovered(self, conn, session_id: str) -> None:
        known, active, blocked = await clearance_state(conn, session_id)
        if known and not active and not blocked:
            await self.ledger.safety.close_session(conn, session_id, reason="alert_resolved")

    async def _heartbeat(self, claim: JobClaim) -> None:
        while True:
            await asyncio.sleep(ACTION_HEARTBEAT_SECONDS)
            await self.ledger.renew(claim)

    async def drive(self, proposal_id: str) -> None:
        try:
            claim = await self.ledger.claim(proposal_id)
        except ActionError as exc:
            _LOG.warning("处置暂不能领取 %s（%s）", proposal_id, exc.code)
            if exc.code == "target_busy":
                await self.ledger.prepare_failed_card(proposal_id)
            return
        if claim is None:
            return
        heartbeat = asyncio.create_task(self._heartbeat(claim))
        try:
            state = claim.job["state"]
            if state == "ready":
                await self._send(claim)
            elif state in {"applying", "observing"}:
                await self._observe(claim)
        except LeaseLost:
            pass
        except (ActionError, RemediationKubeError, httpx.HTTPError, ValueError) as exc:
            code = getattr(exc, "code", type(exc).__name__)
            if claim.job["sent_at"] is None:
                with suppress(ActionError, LeaseLost):
                    await self.ledger.fail_unsent(claim, code, f"写入准备失败（{code}）")
            else:
                with suppress(ActionError, LeaseLost):
                    await self._finish(claim, AttemptOutcome.UNKNOWN, f"无法核实处置结果（{code}）")
        finally:
            heartbeat.cancel()
            with suppress(asyncio.CancelledError, LeaseLost):
                await heartbeat

    async def _send(self, claim: JobClaim) -> None:
        proposal = claim.proposal
        data = claim.job["evidence"]
        if data["policy_version"] != self.policy.version:
            raise ActionError("policy_changed", "当前策略版本已变化")
        snapshot = Snapshot.from_dict(data["snapshot"])
        if claim.job["parent_proposal_id"]:
            previous = PatchPlan.from_dict(data["forward_plan"])
            plan = await asyncio.to_thread(self.kube.prepare_restore, previous)
        else:
            plan = await asyncio.to_thread(self.kube.prepare, snapshot)
        current = await self._current(plan.before, proposal.target)
        spec = proposal.observation_conditions["verification_plan"]
        at = datetime.now(UTC)
        baseline = await collect_window(
            self.metrics,
            spec["probe"],
            at - timedelta(seconds=spec["sample_seconds"] * 5),
            at,
            seconds=spec["sample_seconds"],
            count=5,
        )
        if not claim.job["parent_proposal_id"] and baseline.summary().samples != 5:
            raise ActionError("baseline_missing", "缺少五个有效的动作前业务样本")
        if (
            not claim.job["parent_proposal_id"]
            and proposal.kind is ActionKind.UPDATE_CONFIG
            and proposal.parameters["source"] == "config_map"
        ):
            params = parse_action_parameters(proposal.kind, proposal.parameters)
            field = self.policy.service_for(proposal.target).config_field(params)
            load_before = field.load_signal_for(snapshot.fields["value"])
            loaded = await self.metrics.fresh(load_before, at, max_age=60) if load_before else None
            if loaded is None or loaded[0] != 1:
                raise ActionError("config_load_unverified", "无法确认配置当前已加载")
        preflight = await asyncio.to_thread(self.kube.dry_run, plan)
        await self.require_admission(exclusive_config=snapshot.object.kind == "ConfigMap")
        trusted = None
        compensation = None
        if claim.job["parent_proposal_id"]:
            compensation = CompensationContext.model_validate(data["compensation"])
        elif claim.job["automated"] and "trusted" in data:
            trusted = TrustedEvidence.model_validate(data["trusted"])
            rule = self.policy.match_rule(proposal.target, trusted.labels)
            if rule is None:
                raise ActionError("policy_changed", "当前规则与已持久化授权不一致")
            fresher = datetime.now(UTC)
            values = await self._metrics_evidence(rule, current, fresher)
            trusted = trusted.model_copy(
                update={
                    "observed_at": _oldest_evidence_time(values, fresher),
                    "values": values,
                }
            )
        at = datetime.now(UTC)
        attempt = ActionAttempt(
            id=f"att-{uuid4().hex}",
            proposal_id=proposal.id,
            status=AttemptStatus.INTENT,
            idempotency_key=proposal.idempotency_key,
            started_at=at,
            snapshot={"plan": plan.to_dict(), "baseline": baseline.payload()},
            request={"object": plan.before.object.model_dump(mode="json"), "fields": plan.after},
        )
        loop = asyncio.get_running_loop()

        def authorize(candidate: ActionProposal, job: dict, now: datetime) -> None:
            if job["evidence"] != data:
                raise ActionError("evidence_changed", "执行账本已变化")
            risk = decide(
                candidate,
                target_busy=False,
                policy=self.policy,
                current=current,
                evidence=trusted,
                auto_enabled=self.settings.remediation_auto_enabled and job["automated"],
                compensation=compensation,
                now=now,
            )
            expected = RiskDisposition.AUTO if job["automated"] else RiskDisposition.APPROVAL
            if risk.disposition is not expected or risk.policy_version != data["policy_version"]:
                raise ActionError("policy_denied", "当前证据或字段已不符合审批与规则")

        def grant() -> None:
            future = asyncio.run_coroutine_threadsafe(
                self.ledger.grant_send(claim, attempt, authorize=authorize),
                loop,
            )
            future.result(timeout=30)

        try:
            receipt = await asyncio.to_thread(self.kube.send, preflight, authorize=grant)
        except WriteRejected as exc:
            await self.ledger.finish(
                claim,
                outcome=AttemptOutcome.NO_IMPROVEMENT,
                reason="API Server 拒绝写入",
                result={"stage": "rejected", "written": False, "code": exc.code},
                verification={},
            )
            return
        except WriteUnknown:
            await self.ledger.finish(
                claim,
                outcome=AttemptOutcome.UNKNOWN,
                reason="写入结果未知，需人工核实",
                result={"stage": "manual_verification", "written": None},
                verification={},
                keep_locked=True,
            )
            return
        await self.ledger.applied(claim, {"receipt": receipt.to_dict()})

    async def _observe(self, claim: JobClaim) -> None:
        proposal = claim.proposal
        job = claim.job
        attempt = claim.attempt
        plan = PatchPlan.from_dict(attempt.snapshot["plan"])
        receipt = WriteReceipt.from_dict(attempt.result["receipt"])
        state = await asyncio.to_thread(self.kube.verify, plan, receipt)
        now = datetime.now(UTC)
        if state.state == "drift":
            await self._finish(claim, AttemptOutcome.UNKNOWN, "目标或已写字段发生变化，需人工核对")
            return
        load_query = self._load_query(proposal)
        if proposal.kind is ActionKind.UPDATE_CONFIG and load_query is None:
            await self._finish(claim, AttemptOutcome.UNKNOWN, "缺少已登记的配置加载信号")
            return
        loaded = True
        if load_query is not None and job["effective_at"] is None:
            # 加载信号必须来自已确认写入后的实际抓取。
            result = await self.metrics.fresh(load_query, now, max_age=60)
            loaded = (
                result is not None
                and result[0] == 1
                and (result[1] >= job["applied_at"].timestamp())
            )
        if job["effective_at"] is None and (state.state != "ready" or not loaded):
            deadline = job["applied_at"] + timedelta(
                seconds=self.settings.action_rollout_timeout_seconds,
            )
            if now >= deadline:
                await self._finish(claim, AttemptOutcome.UNKNOWN, "变更生效超时，需人工核实")
            else:
                await self.ledger.reschedule(claim, delay=_POLL_SECONDS)
            return
        if job["parent_proposal_id"]:
            await self._finish(
                claim,
                AttemptOutcome.WORSENED,
                "已补偿并核实字段，原故障仍需人工处理",
                compensated=True,
            )
            return
        rules = self._observation_rules(claim, plan)
        if job["effective_at"] is None:
            pods = await self._observation_pods(claim, plan, receipt, rules)
            await self.ledger.reschedule(
                claim,
                delay=_POLL_SECONDS,
                effective_at=datetime.now(UTC),
                observation={
                    "method": "business_probe_window",
                    "slots_collected": 0,
                    "fault_pods": {
                        pod.uid: {"container_id": pod.container_id, "restarts": pod.restarts}
                        for pod in pods
                    },
                },
            )
            return
        spec = proposal.observation_conditions["verification_plan"]
        seconds = spec["sample_seconds"]
        count = spec["duration_seconds"] // seconds
        start = job["effective_at"]
        post = await collect_window(
            self.metrics,
            spec["probe"],
            start,
            now,
            seconds=seconds,
            count=count,
        )
        progress = post.payload()
        if "fault_pods" in job["observation"]:
            progress["fault_pods"] = job["observation"]["fault_pods"]
        baseline = attempt.snapshot["baseline"]
        before = SampleSummary(baseline["slots_collected"], baseline["failed_slots"])
        if now >= start + timedelta(seconds=5 * seconds) and worsened(before, post):
            compensation = await self._compensation(claim, plan)
            await self._finish(
                claim,
                AttemptOutcome.WORSENED,
                "业务失败率较基线上升，已记录恶化",
                progress=progress,
                compensation=compensation,
            )
            return
        end = start + timedelta(seconds=count * seconds)
        if now < end + timedelta(seconds=15):
            await self.ledger.reschedule(claim, delay=_POLL_SECONDS, observation=progress)
            return
        summary = post.summary()
        if summary.samples != count:
            await self._finish(
                claim, AttemptOutcome.UNKNOWN, "业务探针观察窗缺少样本", progress=progress
            )
        elif summary.failed != 0 or await self._recurred(claim):
            await self._finish(
                claim,
                AttemptOutcome.NO_IMPROVEMENT,
                "业务请求失败或同类故障仍在，需人工处理",
                progress=progress,
            )
        else:
            try:
                signals = await self._fault_signals(claim, plan, receipt, rules, end, load_query)
            except (RemediationKubeError, httpx.HTTPError, ValueError) as exc:
                code = getattr(exc, "code", type(exc).__name__)
                signals = [FaultSignal("fault_signals", "unknown", {"code": code})]
            progress["fault_signals"] = [signal.payload() for signal in signals]
            if any(signal.status == "recurred" for signal in signals):
                await self._finish(
                    claim,
                    AttemptOutcome.NO_IMPROVEMENT,
                    "观察窗内故障信号复发，需人工处理",
                    progress=progress,
                )
                return
            if state.state != "ready" or any(signal.status == "unknown" for signal in signals):
                await self._finish(
                    claim,
                    AttemptOutcome.UNKNOWN,
                    "观察窗缺少可核实的故障信号或就绪证据",
                    progress=progress,
                )
                return
            await self._finish(
                claim,
                AttemptOutcome.IMPROVED,
                "完整观察窗内业务探针全通过，故障信号未复发",
                progress=progress,
            )

    async def _recurred(self, claim: JobClaim) -> bool:
        job = claim.job
        if job["fault_id"] is None:
            return False
        async with self.ledger.store.transaction() as conn:
            return await fault_is_not_clear(
                conn,
                fault_id=job["fault_id"],
                started_at=job["effective_at"],
            )

    async def _compensation(self, claim: JobClaim, plan: PatchPlan):
        original_proposal = claim.proposal
        job = claim.job
        if not job["automated"] or not self.settings.remediation_auto_enabled:
            return None
        try:
            spec = original_proposal.observation_conditions["verification_plan"]
            revert = await asyncio.to_thread(self.kube.prepare_restore, plan)
            changed = {
                key: value
                for key, value in plan.before.fields.items()
                if revert.after.get(key) == value and plan.after.get(key) != value
            }
            if not changed or any(value is None for value in changed.values()):
                return None
            params = original_proposal.parameters | changed
            proposal = _proposal(
                revert.before.workload,
                original_proposal.kind,
                params,
                session_id=original_proposal.session_id,
                turn_id=original_proposal.session_turn_id,
                origin=ActionOrigin.RULES,
                reason=f"恢复 {original_proposal.id} 的限定字段",
                evidence_ids=original_proposal.evidence_ids,
                investigation_id=original_proposal.investigation_id,
            ).model_copy(
                update={
                    "observation_conditions": {
                        "verification_plan": spec,
                        "before": revert.before.fields,
                        "compensates": original_proposal.id,
                        "policy_version": self.policy.version,
                    }
                }
            )
            compensation = CompensationContext(
                original=parse_action_parameters(proposal.kind, params),
                written=parse_action_parameters(proposal.kind, original_proposal.parameters),
                policy_version=self.policy.version,
            )
            current = await self._current(revert.before, proposal.target)
            decision = decide(
                proposal,
                target_busy=False,
                policy=self.policy,
                current=current,
                compensation=compensation,
                auto_enabled=True,
            )
            if decision.disposition is not RiskDisposition.AUTO:
                return None
            return (
                _set_decision(proposal, decision),
                decision,
                {
                    "snapshot": revert.before.to_dict(),
                    "forward_plan": plan.to_dict(),
                    "policy_version": self.policy.version,
                    "compensation": compensation.model_dump(mode="json", exclude_none=True),
                },
            )
        except (RemediationKubeError, ValueError):
            return None

    async def _finish(
        self,
        claim: JobClaim,
        outcome: AttemptOutcome,
        reason: str,
        *,
        progress: dict | None = None,
        compensation=None,
        compensated: bool = False,
    ) -> None:
        outcome = await self.ledger.finish(
            claim,
            outcome=outcome,
            reason=reason,
            result={**claim.attempt.result, "compensated": compensated},
            verification=progress or claim.job["observation"],
            compensation=compensation,
        )
        if outcome is not AttemptOutcome.IMPROVED:
            return
        session = await self.ledger.sessions.get_session(claim.proposal.session_id)
        if session.group_key is None:
            return
        async with self.ledger.sessions.alert_transaction(session.group_key) as conn:
            await self._close_recovered(conn, session.id)


def _probe_signal(value: float) -> bool:
    return value in (0, 1)


def _json_with_decimal(value: Any) -> str:
    import json

    return json.dumps(value, default=float)


def _oldest_evidence_time(values: dict, queried_at: datetime) -> datetime:
    times = [queried_at.timestamp(), *values.get("prometheus_sample_times", {}).values()]
    times.extend(
        datetime.fromisoformat(pod["finished_at"]).timestamp()
        for pod in values.get("pod_evidence", ())
    )
    return datetime.fromtimestamp(min(times), UTC)
