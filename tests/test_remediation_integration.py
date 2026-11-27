"""真实数据库和 HTTP 入口。集群传输与指标根据夹具状态返回。"""

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from secrets import token_urlsafe
from types import SimpleNamespace

import httpx
import pytest

from aiops.actions.policy import CompensationContext, load_policy
from aiops.actions.remediation_kube import PatchPlan, Snapshot, WriteReceipt
from aiops.actions.remediation_runtime import RemediationRuntime, _proposal
from aiops.actions.remediation_store import JobClaim, RemediationStore
from aiops.actions.safety import ActionSafety
from aiops.config import Settings
from aiops.contracts import ActionKind, ActionOrigin, ActionTarget, ApprovalDecision, AttemptStatus
from aiops.db import Store
from aiops.web_api import BrowserAuth, build_web_app
from aiops.webhook import build_app

CONFIG = Path(__file__).resolve().parents[1] / "config/remediation"


class Cluster:
    def __init__(self, before):
        self.before = before.copy()
        self.sent = []

    def snapshot(self, target, kind, parameters, **_):
        return Snapshot(target, target, kind, parameters, self.before.copy(), self.before.copy())

    @staticmethod
    def current_parameters(snapshot):
        return snapshot.current

    @staticmethod
    def prepare(snapshot):
        after = {key: snapshot.parameters[key] for key in snapshot.fields}
        return PatchPlan(snapshot, after, "[]", "test")

    @staticmethod
    def dry_run(plan):
        return SimpleNamespace(plan=plan)

    def send(self, preflight, *, authorize):
        authorize()
        self.sent.append(preflight.plan.after)
        return WriteReceipt(
            preflight.plan.before.object.model_copy(
                update={"resource_version": "2"},
            ),
            2,
        )

    @staticmethod
    def verify(plan, receipt):
        assert receipt.object.uid == plan.before.object.uid
        return SimpleNamespace(state="ready")

    @staticmethod
    def require_admission(*_args, **_kwargs):
        pass

    @staticmethod
    def target(cluster, namespace, kind, name):
        return ActionTarget(
            cluster=cluster,
            namespace=namespace,
            kind=kind,
            name=name,
            uid=f"{namespace}-{name}",
            resource_version="1",
        )


class Metrics:
    @staticmethod
    async def fresh(selector, now, *, max_age):
        value = 0 if "probe_success" in selector else 1
        return value, (now - timedelta(seconds=1)).timestamp()

    @staticmethod
    async def raw_samples(_selector, start, end):
        return [
            ((start + timedelta(seconds=offset)).timestamp(), 1)
            for offset in range(5, int((end - start).total_seconds()), 15)
        ]


def test_config_compensation_survives_persisting_the_authorization():
    async def run():
        settings = Settings(_env_file=None, cluster="vigil-lab", remediation_auto_enabled=True)
        policy = load_policy(CONFIG / "isolated-acceptance")
        workload = Cluster.target(
            settings.cluster, "vigil-experiment", "Deployment", "vigil-config-fixture"
        )
        obj = Cluster.target(
            settings.cluster, "vigil-experiment", "ConfigMap", "vigil-config-fixture"
        )
        parameters = {
            "source": "config_map",
            "key": "mode",
            "config_map": obj.name,
            "value": "worsen",
        }
        before = Snapshot(
            workload, obj, "update_config", parameters, {"value": "degraded"}, {"value": "degraded"}
        )
        written = Snapshot(
            workload, obj, "update_config", parameters, {"value": "worsen"}, {"value": "worsen"}
        )
        forward = PatchPlan(before, written.fields, "[]", "")
        restore = PatchPlan(written, before.fields, "[]", "", restore=True)
        kube = SimpleNamespace(
            prepare_restore=lambda plan: restore, current_parameters=lambda snap: snap.current
        )
        runtime = RemediationRuntime(settings, None, policy, kube, None)
        proposal = _proposal(
            workload,
            ActionKind.UPDATE_CONFIG,
            parameters,
            session_id="session",
            turn_id="turn",
            origin=ActionOrigin.RULES,
            reason="验证补偿授权",
            evidence_ids=["evidence"],
        )
        proposal = proposal.model_copy(
            update={"observation_conditions": {"verification_plan": runtime._spec(proposal)}}
        )
        claim = JobClaim(proposal, {"automated": True}, None)
        compensation = await runtime._compensation(claim, forward)
        assert compensation is not None
        restored, _, evidence = compensation
        context = CompensationContext.model_validate(evidence["compensation"])
        assert restored.parameters["value"] == context.original.value == "degraded"
        assert context.written.value == "worsen"

    asyncio.run(run())


@pytest.mark.parametrize(
    ("kind", "namespace", "name", "parameters", "before"),
    [
        (
            ActionKind.ADJUST_RESOURCES,
            "vigil-shop",
            "currencyservice",
            {"container": "server", "limit_memory": "128Mi"},
            {"limit_memory": "16Mi"},
        ),
        (
            ActionKind.SCALE_REPLICAS,
            "vigil-experiment",
            "vigil-replica-fixture",
            {"replicas": 2},
            {"replicas": 1},
        ),
        (
            ActionKind.UPDATE_CONFIG,
            "vigil-experiment",
            "vigil-config-fixture",
            {"source": "env", "container": "app", "key": "VIGIL_MODE", "value": "limited"},
            {"value": "degraded"},
        ),
    ],
)
def test_web_approves_the_registered_action_once(
    isolated_serve_dsn,
    kind,
    namespace,
    name,
    parameters,
    before,
):
    async def run():
        async with Store.open(isolated_serve_dsn) as store:
            await store.migrate()
            safety = ActionSafety(store)
            ledger = RemediationStore(store, safety)
            settings = Settings(_env_file=None, cluster="vigil-lab")
            kube = Cluster(before)
            runtime = RemediationRuntime(settings, ledger, load_policy(CONFIG), kube, Metrics())
            session = await ledger.sessions.create_session(title="检查登记服务")
            await ledger.sessions.enqueue_input(session.id, "检查服务", display_text="检查服务")
            lease = await ledger.sessions.acquire_main_lease(session.id, "test")
            turn = await ledger.sessions.start_main_turn(lease)
            # 通过执行器持久化的明确人工作用域测试网页接口；模型绑定另由会话用例验证。
            target = kube.target(settings.cluster, namespace, "Deployment", name)
            from aiops.actions.policy import decide
            from aiops.actions.remediation_runtime import _proposal, _set_decision
            from aiops.contracts import ActionOrigin

            snapshot = kube.snapshot(target, kind, parameters)
            proposal = _proposal(
                target,
                kind,
                parameters,
                session_id=session.id,
                turn_id=turn.id,
                origin=ActionOrigin.OPERATOR,
                reason="受控变更集成测试",
                evidence_ids=[],
            )
            decision = decide(
                proposal,
                target_busy=False,
                policy=runtime.policy,
                current=await runtime._current(snapshot, target),
            )
            proposal = _set_decision(proposal, decision)
            spec = runtime._spec(proposal)
            proposal = proposal.model_copy(
                update={
                    "observation_conditions": {
                        "verification_plan": spec,
                        "before": before,
                        "policy_version": runtime.policy.version,
                    }
                }
            )
            async with store.transaction() as conn:
                await ledger.create(
                    conn,
                    proposal,
                    decision,
                    {
                        "policy_version": runtime.policy.version,
                        "snapshot": snapshot.to_dict(),
                    },
                )
                await ledger.publish_rule(conn, proposal)
            card = await safety.get_card(proposal.id, session_id=session.id)
            assert card["parameters"] == parameters
            assert card["before"] == before
            auth = BrowserAuth(interactive=True)
            url = auth.login_url(settings.web_origin)
            app = build_web_app(settings, sessions=ledger.sessions, safety=safety, auth=auth)
            headers = {"Origin": settings.web_origin}
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url=settings.web_origin,
            ) as web:
                assert (
                    await web.post(
                        "/login",
                        data={"token": url.split("#token=")[1]},
                        headers=headers,
                    )
                ).status_code == 303
                path = f"/api/sessions/{session.id}/cards/{proposal.id}/decision"
                forged = await web.post(
                    path,
                    json={"decision": "approved", "fingerprint": "f" * 64},
                    headers=headers,
                )
                assert forged.status_code == 409 and not kube.sent
                approved = await web.post(
                    path,
                    json={"decision": "approved", "fingerprint": card["fingerprint"]},
                    headers=headers,
                )
                assert approved.status_code == 200, approved.text
                await runtime.drive(proposal.id)
                assert kube.sent == [{key: parameters[key] for key in before}]
                assert (await store.get_approval(proposal.id)).decision is ApprovalDecision.APPROVED
                attempt = await store.get_attempt(proposal.id)
                assert attempt.status is AttemptStatus.EXECUTING and attempt.result["written"]
                events = (await web.get(f"/api/sessions/{session.id}/events")).json()["events"]
                assert any(
                    event["type"] == "action_result" and event["written"] for event in events
                )
                repeated = await web.post(
                    path,
                    json={"decision": "approved", "fingerprint": card["fingerprint"]},
                    headers=headers,
                )
                assert repeated.status_code == 409 and len(kube.sent) == 1

    asyncio.run(run())


def test_resolved_notification_keeps_confirmed_execution_open(isolated_serve_dsn):
    async def run():
        async with Store.open(isolated_serve_dsn) as store:
            await store.migrate()
            safety = ActionSafety(store)
            settings = Settings(
                _env_file=None,
                webhook_host="127.0.0.2",
                webhook_token=token_urlsafe(32),
                remediation_auto_enabled=True,
            )
            app = build_app(
                settings,
                sessions=safety.sessions,
                safety=safety,
                remediation_available=True,
            )
            headers = {"Authorization": f"Bearer {settings.webhook_token.get_secret_value()}"}
            alert = {
                "status": "firing",
                "fingerprint": "aabbccddeeff0011",
                "startsAt": datetime.now(UTC).isoformat(),
                "labels": {
                    "alertname": "FrontendProbeFailed",
                    "namespace": "vigil-shop",
                    "target": "currencyservice",
                    "source": "frontend",
                },
            }
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="http://127.0.0.2:8091",
            ) as webhook:
                path = "/webhook/alertmanager"
                started = await webhook.post(
                    path,
                    headers=headers,
                    json={
                        "groupKey": "first-group",
                        "status": "firing",
                        "alerts": [alert],
                    },
                )
                assert started.json()["outcome"] == "created"
                session_id = started.json()["session_id"]
                resolved = await webhook.post(
                    path,
                    headers=headers,
                    json={
                        "groupKey": "first-group",
                        "status": "resolved",
                        "alerts": [{**alert, "status": "resolved"}],
                    },
                )
                assert resolved.json()["outcome"] == "partially_resolved"
                assert (await safety.sessions.get_session(session_id)).closed_at is None
                runtime = RemediationRuntime(
                    settings,
                    RemediationStore(store, safety),
                    load_policy(CONFIG),
                    None,
                    None,
                )
                await runtime.evaluate_one()
                assert (await safety.sessions.get_session(session_id)).closed_at is not None

    asyncio.run(run())
