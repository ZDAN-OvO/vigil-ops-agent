"""把带时间戳的命令输出渲染成终端样式图片，作为 PR 证据。

输入是真实运行留下的输出文件，脚本只做排版和截图，不改写内容。
用法示例：
    python3 scripts/render_evidence.py \
        --title "A01 故障持续" \
        --command "bash faults/a01-pod-rebuild/repeat.sh verify" \
        --source faults/a01-pod-rebuild/runs/02-verify.txt \
        --out demos/ms05/screenshots/02-fault-sustained.png
"""

from __future__ import annotations

import argparse
import html
import shutil
import subprocess
import tempfile
import unicodedata
from datetime import UTC, datetime
from pathlib import Path

CHROME_CANDIDATES = ("google-chrome", "chromium", "chromium-browser")

PAGE = """<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<style>
  :root { color-scheme: dark; }
  * { box-sizing: border-box; }
  body {
    margin: 0; padding: 28px;
    background: #101418;
    font-family: "Noto Sans CJK SC", "Source Han Sans SC", sans-serif;
  }
  .window {
    background: #1b2027; border: 1px solid #2c333d; border-radius: 10px;
    box-shadow: 0 18px 40px rgba(0, 0, 0, 0.45); overflow: hidden;
  }
  .bar {
    display: flex; align-items: center; gap: 8px;
    padding: 10px 14px; background: #232a33; border-bottom: 1px solid #2c333d;
  }
  .dot { width: 11px; height: 11px; border-radius: 50%; }
  .red { background: #ff5f57; } .yellow { background: #febc2e; } .green { background: #28c840; }
  .title { margin-left: 10px; color: #c9d1d9; font-size: 14px; font-weight: 600; }
  .cmd { padding: 12px 18px 0; color: #7ee787; font: 13px/1.5 "JetBrains Mono", "DejaVu Sans Mono", monospace; }
  pre {
    margin: 0; padding: 10px 18px 18px;
    color: #d6dee8; font: 13px/1.55 "JetBrains Mono", "DejaVu Sans Mono", monospace;
    white-space: pre-wrap; word-break: break-word;
  }
  .foot { margin-top: 14px; color: #8b949e; font-size: 12px; }
</style>
</head>
<body>
  <div class="window">
    <div class="bar">
      <span class="dot red"></span><span class="dot yellow"></span><span class="dot green"></span>
      <span class="title">__TITLE__</span>
    </div>
    <div class="cmd">$ __COMMAND__</div>
    <pre>__BODY__</pre>
  </div>
  <div class="foot">__FOOT__</div>
</body>
</html>
"""


def display_width(text: str) -> int:
    """中日韩全角字符占两列，按终端列宽而不是字符数计算换行。"""
    return sum(2 if unicodedata.east_asian_width(char) in {"W", "F"} else 1 for char in text)


def estimate_height(parts: list[str], width: int) -> int:
    """估算整页高度，含来源之间的空行、页脚和余量，避免页脚被截。"""
    columns = max(60, int((width - 36) / 7.8))
    visual = 0
    for item in parts:
        for line in item.splitlines() or [""]:
            visual += max(1, -(-display_width(line) // columns))
    separators = max(0, len(parts) - 1)
    return max(340, 170 + (visual + separators) * 22 + 60 + 24)


def find_chrome() -> str:
    for name in CHROME_CANDIDATES:
        path = shutil.which(name)
        if path:
            return path
    raise SystemExit("找不到 Chrome，无法截图")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--title", required=True)
    parser.add_argument("--command", required=True)
    parser.add_argument("--source", action="append", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--width", type=int, default=1280)
    parser.add_argument(
        "--no-command",
        action="store_true",
        help="来源不是单条可复跑命令时隐藏命令行，避免写出跑不出来的命令",
    )
    args = parser.parse_args()

    sources = [Path(item) for item in args.source]
    parts = []
    for path in sources:
        try:
            text = path.read_text(encoding="utf-8").rstrip("\n")
        except OSError as exc:
            raise SystemExit(f"读取来源失败：{path}（{exc.strerror}）") from exc
        if len(sources) > 1:
            parts.append(f"# {path.as_posix()}\n{text}")
        else:
            parts.append(text)
    body = html.escape("\n\n".join(parts))

    rendered_at = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    foot = html.escape(
        "来源 " + "、".join(str(path) for path in sources) + f" · 原始输出按终端样式渲染 · {rendered_at}"
    )
    command_line = "" if args.no_command else f"<div class=\"cmd\">$ {html.escape(args.command)}</div>"
    page = (
        PAGE.replace("__TITLE__", html.escape(args.title))
        .replace('<div class="cmd">$ __COMMAND__</div>', command_line)
        .replace("__BODY__", body)
        .replace("__FOOT__", foot)
    )

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    height = estimate_height(parts, args.width)

    with tempfile.TemporaryDirectory() as tmp:
        page_path = Path(tmp) / "evidence.html"
        page_path.write_text(page, encoding="utf-8")
        chrome = find_chrome()
        subprocess.run(
            [
                chrome,
                "--headless=new",
                "--disable-gpu",
                "--hide-scrollbars",
                "--force-device-scale-factor=2",
                f"--window-size={args.width},{min(height, 4000)}",
                f"--screenshot={out.resolve()}",
                page_path.resolve().as_uri(),
            ],
            check=True,
            capture_output=True,
        )
    print(out)


if __name__ == "__main__":
    main()
