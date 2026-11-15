"""仓库内置 Skill 的解析、哈希与信号触发。"""

from datetime import UTC, datetime, timedelta

import pytest

from aiops.config import Settings
from aiops.contracts import Incident, TimeRange
from aiops.skills import (
    SkillLoadError,
    load_skill_catalog,
    match_signal_skills,
    parse_skill,
    render_signal_block,
)

NOW = datetime(2026, 11, 13, 9, 0, tzinfo=UTC)


def incident(*, trigger: str, alert_names: list[str] | None = None) -> Incident:
    return Incident(
        id="inc-skill",
        environment="local",
        cluster="vigil-lab",
        namespace="vigil-shop",
        service="frontend",
        trigger=trigger,  # type: ignore[arg-type]
        symptoms="测试",
        time_range=TimeRange(start=NOW, end=NOW + timedelta(minutes=10)),
        alert_names=alert_names or [],
    )


def test_catalog_loads_builtin_skills():
    catalog = load_skill_catalog(Settings().skills_path)
    assert {item.name for item in catalog} == {
        "hypothesis-driven-investigation",
        "bad-release",
        "oom",
        "dependency-latency",
        "pod-rebuild",
    }
    assert all(len(item.content_hash) == 64 for item in catalog)


def test_content_hash_is_stable_and_tracks_content(tmp_path):
    skill = tmp_path / "demo"
    skill.mkdir()
    path = skill / "SKILL.md"
    path.write_text(
        "---\nname: demo\ndescription: 演示\n---\n\n# 正文\n",
        encoding="utf-8",
    )
    first = parse_skill(path)
    second = parse_skill(path)
    assert first.content_hash == second.content_hash
    path.write_text(
        "---\nname: demo\ndescription: 演示\n---\n\n# 正文已改\n",
        encoding="utf-8",
    )
    assert parse_skill(path).content_hash != first.content_hash


def test_signal_matching_uses_source_and_alertname():
    catalog = load_skill_catalog(Settings().skills_path)
    manual = match_signal_skills(catalog, incident(trigger="manual"))
    assert [item.name for item in manual] == ["hypothesis-driven-investigation"]

    manual_pod = match_signal_skills(
        catalog, incident(trigger="manual", alert_names=["PodCrashLooping"])
    )
    assert {item.name for item in manual_pod} == {
        "hypothesis-driven-investigation",
        "pod-rebuild",
    }

    alert = match_signal_skills(catalog, incident(trigger="alert", alert_names=["PodOOMKilled"]))
    assert {item.name for item in alert} == {
        "hypothesis-driven-investigation",
        "oom",
    }

    scheduled = match_signal_skills(catalog, incident(trigger="scheduled"))
    assert [item.name for item in scheduled] == ["hypothesis-driven-investigation"]


def test_signal_block_contains_full_skill_text():
    catalog = load_skill_catalog(Settings().skills_path)
    oom = [item for item in catalog if item.name == "oom"]
    block = render_signal_block(oom)
    assert "# 持续 OOM 排查" in block
    assert "已按告警信号自动加载的 Skill" in block
    assert render_signal_block([]) == ""


def test_invalid_skill_is_rejected(tmp_path):
    path = tmp_path / "bad" / "SKILL.md"
    path.parent.mkdir()
    path.write_text("# 缺少 frontmatter\n", encoding="utf-8")
    with pytest.raises(SkillLoadError):
        parse_skill(path)
