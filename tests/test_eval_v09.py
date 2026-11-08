"""V09：规则基线要与同场景、同输入、同预算、同策略的 Agent 运行对照。"""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from eval_fakes import FakeStore, audit, check, criterion, manifest, run, scenario
from pydantic import ValidationError

from aiops.contracts import ActionKind, ActionOrigin, Budget, BudgetUsage
from aiops.eval import Manifest

SYMPTOMS = "首页 500，productcatalogservice 刚发布新镜像"


def budget():
    return Budget(max_tool_calls=16, max_model_calls=8, max_wall_clock_seconds=480)


def investigation(identifier, incident_id, *, model="mythos", status="completed", **overrides):
    values = {
        "id": identifier,
        "incident_id": incident_id,
        "status": SimpleNamespace(value=status),
        "model": model,
        "usage": BudgetUsage(model_calls=0 if model.startswith("rules:") else 4, tool_calls=6),
        "budget": budget(),
        "policy_version": "mvp-1",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def incident(
    *,
    service="frontend",
    minutes=15,
    end_skew_seconds=0,
    environment="local",
    trigger="manual",
    alert_ids=(),
    context=(),
    symptoms=SYMPTOMS,
):
    end = datetime(2026, 11, 6, 8, 0, tzinfo=UTC) + timedelta(seconds=end_skew_seconds)
    return SimpleNamespace(
        environment=environment,
        cluster="vigil-lab",
        namespace="vigil-shop",
        service=service,
        trigger=trigger,
        time_range=SimpleNamespace(
            start=datetime(2026, 11, 6, 8, 0, tzinfo=UTC) - timedelta(minutes=minutes),
            end=end,
        ),
        alert_ids=list(alert_ids),
        context=list(context),
        symptoms=symptoms,
    )


def proposal(
    identifier,
    investigation_id,
    *,
    kind=ActionKind.ROLLOUT_UNDO,
    origin=ActionOrigin.RULES,
    policy="mvp-policy-2",
):
    return SimpleNamespace(
        id=identifier,
        kind=kind,
        investigation_id=investigation_id,
        origin=origin,
        policy_version=policy,
    )


def baseline_run(**overrides):
    payload = {
        "scenario": "f01",
        "run_id": "f01-rules",
        "investigation_id": "inv-rules",
        "rules_version": "rules-1",
        "agent_run_ids": ["a"],
        "compared": ["调查结论", "动作适用性"],
        "limitations": ["规则基线不生成自然语言解释"],
    }
    payload.update(overrides)
    return payload


def store(**overrides):
    values = {
        "investigations": {
            "inv-rules": investigation("inv-rules", "inc-rules", model="rules:rules-1"),
            "inv-agent": investigation("inv-agent", "inc-agent"),
        },
        "incidents": {
            "inc-rules": incident(),
            "inc-agent": incident(),
        },
        "audits": {
            "inv-rules": [
                audit(
                    "baseline.started",
                    payload={"rules_version": "rules-1", "policy_version": "mvp-policy-2"},
                ),
                audit("baseline.rule", payload={"rule": "recent-rollout"}),
                audit("investigation.completed", payload={"model_calls": 0}),
            ]
        },
        "proposals": {
            "act-rules": proposal("act-rules", "inv-rules", origin=ActionOrigin.RULES),
            "act-agent": proposal("act-agent", "inv-agent", origin=ActionOrigin.MODEL),
        },
    }
    values.update(overrides)
    return FakeStore(**values)


def f01_scenario(allowed=("rollout_undo",), extra_runs=()):
    item = scenario("f01", [run("a", investigation="inv-agent"), *extra_runs])
    item["allowed_actions"] = list(allowed)
    return item


def manifest_with(entry, scenarios=None):
    return manifest(scenarios or [f01_scenario()], baseline=[entry])


def v09(tmp_path, entry, fake, scenarios=None):
    return criterion(check(tmp_path, manifest_with(entry, scenarios), fake), "V09")


def test_v09_accepts_a_matched_baseline(tmp_path):
    assert v09(tmp_path, baseline_run(), store()).passed is True


def test_v09_requires_a_baseline_entry(tmp_path):
    report = check(tmp_path, manifest([f01_scenario()]), store())

    result = criterion(report, "V09")
    assert result.passed is False
    assert "规则基线 0 条" in result.detail


def test_v09_rejects_model_calls_in_the_baseline(tmp_path):
    fake = store()
    fake._investigations["inv-rules"] = investigation(
        "inv-rules", "inc-rules", model="rules:rules-1"
    )
    fake._investigations["inv-rules"].usage = BudgetUsage(model_calls=1, tool_calls=6)

    result = v09(tmp_path, baseline_run(), fake)

    assert result.passed is False
    assert "模型调用=1" in result.detail


def test_v09_rejects_a_budget_mismatch(tmp_path):
    fake = store()
    fake._investigations["inv-agent"] = investigation(
        "inv-agent",
        "inc-agent",
        budget=Budget(max_tool_calls=8, max_model_calls=6, max_wall_clock_seconds=300),
    )

    result = v09(tmp_path, baseline_run(), fake)

    assert result.passed is False
    assert "预算不一致" in result.detail


def test_v09_rejects_a_authorization_verdict_mismatch(tmp_path):
    fake = store()
    fake._dispositions["act-agent"] = "deny"

    result = v09(tmp_path, baseline_run(), fake)

    assert result.passed is False
    assert "授权判定不一致" in result.detail
    assert "deny" in result.detail


def test_v09_notes_when_only_the_baseline_proposes(tmp_path):
    fake = store()
    fake._proposals.pop("act-agent")

    result = v09(tmp_path, baseline_run(), fake)

    assert result.passed is True
    assert "仅基线提出" in result.detail


def test_v09_notes_when_authorization_never_triggered(tmp_path):
    fake = store()
    fake._proposals = {}

    result = v09(tmp_path, baseline_run(), fake)

    assert result.passed is True
    assert "授权判定未触发" in result.detail


def test_v09_rejects_a_pattern_run_as_the_control(tmp_path):
    fake = store()
    fake._investigations["inv-agent"] = investigation(
        "inv-agent", "inc-agent", model="rules:rules-1"
    )

    result = v09(tmp_path, baseline_run(), fake)

    assert result.passed is False
    assert "对照是规则运行" in result.detail


def test_v09_rejects_a_control_that_has_not_finished(tmp_path):
    fake = store()
    fake._investigations["inv-agent"] = investigation("inv-agent", "inc-agent", status="running")

    result = v09(tmp_path, baseline_run(), fake)

    assert result.passed is False
    assert "对照终态=running" in result.detail


def test_v09_rejects_an_aborted_control(tmp_path):
    fake = store()
    fake._investigations["inv-agent"] = investigation("inv-agent", "inc-agent", status="aborted")

    result = v09(tmp_path, baseline_run(), fake)

    assert result.passed is False
    assert "对照终态=aborted" in result.detail


def test_v09_rejects_a_control_without_model_calls(tmp_path):
    fake = store()
    fake._investigations["inv-agent"] = investigation("inv-agent", "inc-agent")
    fake._investigations["inv-agent"].usage = BudgetUsage(model_calls=0, tool_calls=6)

    result = v09(tmp_path, baseline_run(), fake)

    assert result.passed is False
    assert "对照没有模型调用" in result.detail


def test_v09_rejects_a_control_without_tool_calls(tmp_path):
    fake = store()
    fake._investigations["inv-agent"] = investigation("inv-agent", "inc-agent")
    fake._investigations["inv-agent"].usage = BudgetUsage(model_calls=4, tool_calls=0)

    result = v09(tmp_path, baseline_run(), fake)

    assert result.passed is False
    assert "对照没有工具调用" in result.detail


def test_v09_rejects_a_resource_mismatch(tmp_path):
    fake = store()
    fake._incidents["inc-agent"] = incident(service="frontend-probe")

    result = v09(tmp_path, baseline_run(), fake)

    assert result.passed is False
    assert "资源不一致" in result.detail


def test_v09_rejects_a_window_mismatch(tmp_path):
    fake = store()
    fake._incidents["inc-agent"] = incident(minutes=30)

    result = v09(tmp_path, baseline_run(), fake)

    assert result.passed is False
    assert "观察窗不一致" in result.detail


def test_v09_rejects_a_context_mismatch(tmp_path):
    fake = store()
    fake._incidents["inc-agent"] = incident(context=["告警来自前端探针"])

    result = v09(tmp_path, baseline_run(), fake)

    assert result.passed is False
    assert "Context 不一致" in result.detail


def test_v09_rejects_a_symptoms_mismatch(tmp_path):
    fake = store()
    fake._incidents["inc-agent"] = incident(symptoms="首页慢")

    result = v09(tmp_path, baseline_run(), fake)

    assert result.passed is False
    assert "现象描述不一致" in result.detail


def test_v09_rejects_a_shared_investigation(tmp_path):
    payload = manifest(
        [scenario("f01", [run("a", investigation="inv-rules")])], baseline=[baseline_run()]
    )

    result = criterion(check(tmp_path, payload, store()), "V09")
    assert result.passed is False
    assert "与 Agent 共用调查" in result.detail

    payload = manifest(
        [
            scenario(
                "f01",
                [run("a", investigation="inv-agent"), run("b", investigation="inv-rules")],
            )
        ],
        baseline=[baseline_run()],
    )

    report = check(tmp_path, payload, store())
    assert criterion(report, "V09").passed is False
    assert "基线调查被登记为场景运行" in criterion(report, "V09").detail


def test_v09_rejects_an_unknown_scenario(tmp_path):
    result = v09(tmp_path, baseline_run(scenario="f99"), store())

    assert result.passed is False
    assert "场景不存在 f99" in result.detail


def test_v09_rejects_an_unknown_agent_run(tmp_path):
    result = v09(tmp_path, baseline_run(agent_run_ids=["missing"]), store())

    assert result.passed is False
    assert "对照运行不存在" in result.detail


def test_v09_requires_declared_comparison_and_limitations(tmp_path):
    result = v09(tmp_path, baseline_run(compared=[], limitations=None), store())

    assert result.passed is False
    assert "未声明比较维度" in result.detail
    assert "未注明不可比维度" in result.detail


def test_v09_rejects_blank_declarations(tmp_path):
    result = v09(tmp_path, baseline_run(compared=["  "], limitations=[""]), store())

    assert result.passed is False
    assert "未声明比较维度" in result.detail
    assert "不可比维度为空" in result.detail


def test_v09_rejects_an_empty_limitations_list(tmp_path):
    result = v09(tmp_path, baseline_run(limitations=[]), store())

    assert result.passed is False
    assert "不可比维度为空" in result.detail


def test_v09_rejects_duplicate_run_ids(tmp_path):
    payload = manifest(
        [f01_scenario()],
        baseline=[baseline_run(), baseline_run(investigation_id="inv-other")],
    )

    result = criterion(check(tmp_path, payload, store()), "V09")
    assert result.passed is False
    assert "run_id 重复" in result.detail


def test_v09_rejects_duplicate_investigations(tmp_path):
    payload = manifest(
        [f01_scenario()],
        baseline=[baseline_run(), baseline_run(run_id="f01-rules-2")],
    )

    result = criterion(check(tmp_path, payload, store()), "V09")
    assert result.passed is False
    assert "重复引用同一调查" in result.detail


def test_v09_requires_every_core_scenario(tmp_path):
    payload = manifest(
        [f01_scenario(), scenario("f02", [run("b", investigation="inv-agent")])],
        baseline=[baseline_run()],
    )

    result = criterion(check(tmp_path, payload, store()), "V09")
    assert result.passed is False
    assert "缺核心场景基线 ['f02']" in result.detail


def test_v09_rejects_a_proposal_outside_the_scenario(tmp_path):
    fake = store()
    fake._proposals["act-rules"] = proposal("act-rules", "inv-rules", kind=ActionKind.RESTART_POD)

    result = v09(tmp_path, baseline_run(), fake)

    assert result.passed is False
    assert "提案超出场景动作" in result.detail


def test_v09_rejects_a_control_proposal_outside_the_scenario(tmp_path):
    fake = store()
    fake._proposals["act-agent"] = proposal(
        "act-agent", "inv-agent", kind=ActionKind.RESTART_POD, origin=ActionOrigin.MODEL
    )

    result = v09(tmp_path, baseline_run(), fake)

    assert result.passed is False
    assert "对照提案超出场景动作" in result.detail


def test_v09_requires_the_baseline_audit(tmp_path):
    fake = store()
    fake._audits["inv-rules"] = [audit("investigation.completed")]

    result = v09(tmp_path, baseline_run(), fake)

    assert result.passed is False
    assert "缺 baseline.started 审计" in result.detail


def test_v09_requires_the_baseline_policy_version(tmp_path):
    fake = store()
    fake._audits["inv-rules"] = [audit("baseline.started", payload={"rules_version": "rules-1"})]

    result = v09(tmp_path, baseline_run(), fake)

    assert result.passed is False
    assert "审计缺策略版本" in result.detail


def test_v09_rejects_an_aborted_baseline(tmp_path):
    fake = store()
    fake._investigations["inv-rules"] = investigation(
        "inv-rules", "inc-rules", model="rules:rules-1", status="aborted"
    )

    result = v09(tmp_path, baseline_run(), fake)

    assert result.passed is False
    assert "终态=aborted" in result.detail


def test_v09_rejects_duplicate_run_ids_in_the_scenario(tmp_path):
    fake = store()
    item = scenario(
        "f01",
        [
            run("a", valid=False, investigation="inv-missing"),
            run("a", investigation="inv-agent"),
        ],
    )
    item["allowed_actions"] = ["rollout_undo"]

    result = v09(tmp_path, baseline_run(), fake, [item])

    assert result.passed is False
    assert "场景运行 id 重复" in result.detail


def test_v09_reports_missing_input_fields_instead_of_crashing(tmp_path):
    fake = store()
    broken = incident()
    del broken.environment
    del broken.trigger
    broken.alert_ids = None
    broken.context = None
    del broken.symptoms
    fake._incidents["inc-agent"] = broken

    result = v09(tmp_path, baseline_run(), fake)

    assert result.passed is False
    for message in ("环境缺失", "触发方式缺失", "关联告警缺失", "Context 缺失", "现象描述缺失"):
        assert message in result.detail

    both = store()
    for incident_id in ("inc-rules", "inc-agent"):
        item = incident()
        del item.environment
        both._incidents[incident_id] = item

    assert "环境缺失" in v09(tmp_path, baseline_run(), both).detail


def test_manifest_rejects_duplicate_scenario_ids():
    payload = manifest([f01_scenario(), f01_scenario()], baseline=[baseline_run()])

    with pytest.raises(ValidationError, match="场景 id 重复"):
        Manifest.model_validate(payload)


def test_v09_rejects_an_environment_mismatch(tmp_path):
    fake = store()
    fake._incidents["inc-agent"] = incident(environment="staging")

    result = v09(tmp_path, baseline_run(), fake)

    assert result.passed is False
    assert "环境不一致" in result.detail


def test_v09_rejects_a_trigger_mismatch(tmp_path):
    fake = store()
    fake._incidents["inc-agent"] = incident(trigger="alert")

    result = v09(tmp_path, baseline_run(), fake)

    assert result.passed is False
    assert "触发方式不一致" in result.detail


def test_v09_rejects_an_alert_mismatch(tmp_path):
    fake = store()
    fake._incidents["inc-agent"] = incident(alert_ids=["alert-1"])

    result = v09(tmp_path, baseline_run(), fake)

    assert result.passed is False
    assert "关联告警不一致" in result.detail


def test_v09_rejects_reordered_context(tmp_path):
    fake = store()
    fake._incidents["inc-rules"] = incident(context=["告警来自前端探针", "刚发布新镜像"])
    fake._incidents["inc-agent"] = incident(context=["刚发布新镜像", "告警来自前端探针"])

    result = v09(tmp_path, baseline_run(), fake)

    assert result.passed is False
    assert "Context 不一致" in result.detail


def test_v09_rejects_repeated_context(tmp_path):
    fake = store()
    fake._incidents["inc-rules"] = incident(context=["告警来自前端探针", "告警来自前端探针"])
    fake._incidents["inc-agent"] = incident(context=["告警来自前端探针"])

    result = v09(tmp_path, baseline_run(), fake)

    assert result.passed is False
    assert "Context 不一致" in result.detail


def test_v09_rejects_a_window_difference_under_a_minute(tmp_path):
    fake = store()
    fake._incidents["inc-agent"] = incident(end_skew_seconds=24)

    result = v09(tmp_path, baseline_run(), fake)

    assert result.passed is False
    assert "观察窗不一致" in result.detail


def test_v09_rejects_an_operator_proposal_in_the_baseline(tmp_path):
    fake = store()
    fake._proposals["act-rules"] = proposal("act-rules", "inv-rules", origin=ActionOrigin.OPERATOR)

    result = v09(tmp_path, baseline_run(), fake)

    assert result.passed is False
    assert "基线提案来源=operator" in result.detail


def test_v09_rejects_an_operator_proposal_in_the_control(tmp_path):
    fake = store()
    fake._proposals["act-agent"] = proposal("act-agent", "inv-agent", origin=ActionOrigin.OPERATOR)

    result = v09(tmp_path, baseline_run(), fake)

    assert result.passed is False
    assert "对照提案来源=operator" in result.detail


def test_v09_compares_each_control_separately(tmp_path):
    fake = store()
    fake._investigations["inv-agent-b"] = investigation("inv-agent-b", "inc-agent-b")
    fake._incidents["inc-agent-b"] = incident()

    scenarios = [f01_scenario(extra_runs=[run("b", investigation="inv-agent-b")])]

    result = v09(tmp_path, baseline_run(agent_run_ids=["a", "b"]), fake, scenarios)

    assert result.passed is True
    assert "f01-rules/b:rollout_undo 仅基线提出" in result.detail
    assert "f01-rules/a:rollout_undo 仅基线提出" not in result.detail


def test_v09_rejects_a_verdict_mismatch_on_one_of_two_controls(tmp_path):
    fake = store()
    fake._investigations["inv-agent-b"] = investigation("inv-agent-b", "inc-agent-b")
    fake._incidents["inc-agent-b"] = incident()
    fake._proposals["act-agent-b"] = proposal(
        "act-agent-b", "inv-agent-b", origin=ActionOrigin.MODEL
    )
    fake._dispositions["act-agent-b"] = "deny"

    scenarios = [f01_scenario(extra_runs=[run("b", investigation="inv-agent-b")])]

    result = v09(tmp_path, baseline_run(agent_run_ids=["a", "b"]), fake, scenarios)

    assert result.passed is False
    assert "授权判定不一致 rollout_undo ['approval']/['deny'] b" in result.detail
