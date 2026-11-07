"""V09：规则基线要与同场景、同资源、同预算、同策略的 Agent 运行对照。"""

from types import SimpleNamespace

from eval_fakes import FakeStore, audit, check, criterion, manifest, run, scenario

from aiops.contracts import ActionKind, Budget, BudgetUsage


def budget():
    return Budget(max_tool_calls=16, max_model_calls=8, max_wall_clock_seconds=480)


def investigation(identifier, incident_id, *, model="mythos", status="completed", **overrides):
    values = {
        "id": identifier,
        "incident_id": incident_id,
        "status": SimpleNamespace(value=status),
        "model": model,
        "usage": BudgetUsage(model_calls=0 if model == "rules:rules-1" else 4, tool_calls=6),
        "budget": budget(),
        "policy_version": "mvp-1",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def incident():
    return SimpleNamespace(cluster="vigil-lab", namespace="vigil-shop", service="frontend")


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
                audit("baseline.started", payload={"rules_version": "rules-1"}),
                audit("baseline.rule", payload={"rule": "oom-limit"}),
                audit("investigation.completed", payload={"model_calls": 0}),
            ]
        },
        "proposals": {
            "act-rules": SimpleNamespace(
                id="act-rules", kind=ActionKind.ROLLOUT_UNDO, investigation_id="inv-rules"
            )
        },
    }
    values.update(overrides)
    return FakeStore(**values)


def f01_scenario(allowed=("rollout_undo",)):
    item = scenario("f01", [run("a", investigation="inv-agent")])
    item["allowed_actions"] = list(allowed)
    return item


def manifest_with(entry):
    return manifest([f01_scenario()], baseline=[entry])


def v09(tmp_path, entry, fake):
    return criterion(check(tmp_path, manifest_with(entry), fake), "V09")


def test_v09_accepts_a_matched_baseline(tmp_path):
    result = v09(tmp_path, baseline_run(), store())

    assert result.passed is True


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


def test_v09_rejects_a_policy_mismatch(tmp_path):
    fake = store()
    fake._investigations["inv-agent"] = investigation(
        "inv-agent", "inc-agent", policy_version="other"
    )

    result = v09(tmp_path, baseline_run(), fake)

    assert result.passed is False
    assert "策略版本不一致" in result.detail


def test_v09_rejects_a_resource_mismatch(tmp_path):
    fake = store()
    fake._incidents["inc-agent"] = SimpleNamespace(
        cluster="vigil-lab", namespace="other", service="frontend"
    )

    result = v09(tmp_path, baseline_run(), fake)

    assert result.passed is False
    assert "资源不一致" in result.detail


def test_v09_rejects_a_shared_investigation(tmp_path):
    payload = manifest(
        [scenario("f01", [run("a", investigation="inv-rules")])], baseline=[baseline_run()]
    )

    result = criterion(check(tmp_path, payload, store()), "V09")
    assert result.passed is False
    assert "与 Agent 共用调查" in result.detail


def test_v09_rejects_an_unknown_agent_run(tmp_path):
    result = v09(tmp_path, baseline_run(agent_run_ids=["missing"]), store())

    assert result.passed is False
    assert "对照运行不存在" in result.detail


def test_v09_requires_declared_comparison_and_limitations(tmp_path):
    result = v09(tmp_path, baseline_run(compared=[], limitations=None), store())

    assert result.passed is False
    assert "未声明比较维度" in result.detail
    assert "未注明不可比维度" in result.detail


def test_v09_rejects_a_proposal_outside_the_scenario(tmp_path):
    fake = store()
    fake._proposals["act-rules"] = SimpleNamespace(
        id="act-rules", kind=ActionKind.RESTART_POD, investigation_id="inv-rules"
    )

    result = v09(tmp_path, baseline_run(), fake)

    assert result.passed is False
    assert "提案超出场景动作" in result.detail


def test_v09_requires_the_baseline_audit(tmp_path):
    fake = store()
    fake._audits["inv-rules"] = [audit("investigation.completed")]

    result = v09(tmp_path, baseline_run(), fake)

    assert result.passed is False
    assert "缺 baseline.started 审计" in result.detail


def test_v09_rejects_an_aborted_baseline(tmp_path):
    fake = store()
    fake._investigations["inv-rules"] = investigation(
        "inv-rules", "inc-rules", model="rules:rules-1", status="aborted"
    )

    result = v09(tmp_path, baseline_run(), fake)

    assert result.passed is False
    assert "终态=aborted" in result.detail
