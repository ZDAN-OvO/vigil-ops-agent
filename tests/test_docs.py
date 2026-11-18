"""现行文档的相对链接必须有效，且不再引用已移除的文件和命令。"""

import re
import subprocess
from pathlib import Path
from urllib.parse import unquote, urlsplit

from aiops.config import PROJECT_ROOT

LINK = re.compile(r"\]\(([^)\s]+)\)")
REMOVED = re.compile(
    r"aiops eval|aiops explain|--mode rules|RulesBaseline|langchain_gate"
    r"|DESIGN\.md|ROADMAP\.md|PLAN\.md|demos/|(decisions|milestone|mvp|iterations|eval|claims|migration"
    r"|architecture|contracts|usage|roadmap)\.md|docs/README\.md"
    r"|eval/scenarios\.json|docs/specs/|docs/agents/"
)


def _current_docs() -> list[Path]:
    out = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "*.md"],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.splitlines()
    paths = [PROJECT_ROOT / name for name in out if not name.startswith("deprecated/")]
    return [path for path in paths if path.exists()]


def test_document_entrypoints_exist():
    for name in (
        "AGENTS.md",
        "README.md",
        "ARCHITECTURE.md",
    ):
        assert (PROJECT_ROOT / name).is_file(), name


def _anchors(path: Path) -> set[str]:
    text = path.read_text()
    anchors = set(re.findall(r'<a\s+(?:id|name)=["\']([^"\']+)["\']', text))
    seen: set[str] = set()
    fence = ""
    for line in text.splitlines():
        stripped = line.lstrip()
        marker = re.match(r"(`{3,}|~{3,})", stripped)
        if marker:
            token = marker.group(1)
            if not fence:
                fence = token
            elif token[0] == fence[0] and len(token) >= len(fence):
                fence = ""
            continue
        if fence:
            continue
        heading = re.match(r"^#{1,6}\s+(.+?)\s*#*\s*$", line)
        if heading is None:
            continue
        title = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", heading.group(1))
        slug = re.sub(r"[^\w\- ]", "", title.lower()).replace(" ", "-")
        anchor = slug
        suffix = 0
        while anchor in seen:
            suffix += 1
            anchor = f"{slug}-{suffix}"
        seen.add(anchor)
        anchors.add(anchor)
    return anchors


def test_relative_links_resolve():
    broken = []
    for path in _current_docs():
        for target in LINK.findall(path.read_text()):
            url = urlsplit(target)
            if url.scheme or url.netloc:
                continue
            destination = path.parent / unquote(url.path) if url.path else path
            if not destination.exists():
                broken.append(f"{path.relative_to(PROJECT_ROOT)} -> {target}")
            elif url.fragment and destination.suffix == ".md":
                if unquote(url.fragment) not in _anchors(destination):
                    broken.append(f"{path.relative_to(PROJECT_ROOT)} -> {target}（锚点不存在）")
    assert not broken, broken


def test_no_references_to_removed_material():
    hits = []
    for path in _current_docs():
        if path.is_relative_to(PROJECT_ROOT / "docs" / "adr"):
            continue
        for number, line in enumerate(path.read_text().splitlines(), 1):
            if REMOVED.search(line):
                hits.append(f"{path.relative_to(PROJECT_ROOT)}:{number}: {line.strip()[:80]}")
    assert not hits, hits


def test_adr_thresholds_match_the_code():
    """ADR 里写死的阈值必须和代码常量一致，否则读文档的人会被误导。"""
    from aiops.evaluation.harness import MIN_DIAGNOSIS_RATE, MIN_VALID_RUNS
    from aiops.investigation.rules import (
        LATENCY_THRESHOLD_SECONDS,
        LOW_MEMORY_LIMIT_MIB,
        OOM_RESTART_THRESHOLD,
    )

    adr = (PROJECT_ROOT / "docs" / "adr" / "0013-comparative-evaluation.md").read_text()
    assert f"{LATENCY_THRESHOLD_SECONDS * 1000:.0f}ms" in adr
    assert f"{LOW_MEMORY_LIMIT_MIB}Mi" in adr
    assert f"≥ {OOM_RESTART_THRESHOLD}" in adr
    assert f"{MIN_VALID_RUNS} 次有效运行" in adr
    # 比例要按 ADR 里写的分数解析，再用常量比对；不能靠"文档里出现过 2/3"兜底。
    ratio = re.search(r"诊断正确率不低于 \*\*([\d.]+)/([\d.]+)\*\*", adr)
    assert ratio is not None, "ADR 里找不到诊断正确率门槛"
    assert float(ratio.group(1)) / float(ratio.group(2)) == MIN_DIAGNOSIS_RATE


def test_run_record_markdown_matches_the_json():
    """记录 md 的机器生成段必须等于按 JSON 重算的结果，防止手改或过期。"""
    import json

    from aiops.evaluation.harness import MANUAL_MARKER, render_markdown, summarize

    record = PROJECT_ROOT / "faults" / "runs" / "comparative-2026-11-18.json"
    markdown = record.with_suffix(".md")
    if not record.exists() or not markdown.exists():
        return
    payload = json.loads(record.read_text())
    expected = render_markdown(payload["batch"], summarize(payload["records"]))
    actual = markdown.read_text().split(MANUAL_MARKER)[0]
    assert actual.strip() == expected.strip()
