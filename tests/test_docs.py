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
        "docs/plan.md",
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
