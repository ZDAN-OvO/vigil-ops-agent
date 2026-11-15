"""现行文档的相对链接必须有效，且不再引用已移除的文件和命令。"""

import re
import subprocess
from pathlib import Path

from aiops.config import PROJECT_ROOT

LINK = re.compile(r"\]\(([^)\s]+)\)")
REMOVED = re.compile(
    r"aiops eval|aiops explain|--mode rules|RulesBaseline|langchain_gate"
    r"|PLAN\.md|demos/|(decisions|milestone|mvp|iterations|eval|claims|migration"
    r"|architecture|contracts|usage|roadmap)\.md|docs/README\.md"
    r"|eval/scenarios\.json"
)


def _current_docs() -> list[Path]:
    out = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "*.md"],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()
    paths = [PROJECT_ROOT / name for name in out if not name.startswith("deprecated/")]
    return [path for path in paths if path.exists()]


def test_docs_top_level_uses_uppercase_names():
    docs = PROJECT_ROOT / "docs"
    names = [path.name for path in docs.glob("*.md")]
    assert "README.md" not in names
    assert all(name == name.upper().replace(".MD", ".md") for name in names), names


def test_relative_links_resolve():
    broken = []
    for path in _current_docs():
        for target in LINK.findall(path.read_text()):
            if target.startswith(("http://", "https://", "mailto:", "#")):
                continue
            file_part = target.split("#", 1)[0]
            if file_part and not (path.parent / file_part).exists():
                broken.append(f"{path.relative_to(PROJECT_ROOT)} -> {target}")
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
