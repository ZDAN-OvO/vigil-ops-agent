"""只在出站时清理页面文本，数据库保留原始审计材料。"""

import json
import re
from typing import Any

from aiops.config import Settings

SENSITIVE = re.compile(
    r"(?i)env|secret|authorization|password|credential|token|private.?key|cookie|access.?key"
)
PATTERNS = [
    re.compile(r"(?i)\b(?:authorization\s*[:=]\s*)?(?:bearer|basic)\s+[^\s,;]+"),
    re.compile(
        r"(?i)\b(?:password|passwd|pwd|secret|token|api[-_. ]?key|authorization|access[-_. ]?key)"
        r"""["']?\s*[:=]\s*(?:"[^"]*"|'[^']*'|[^\s,;]+)"""
    ),
    re.compile(r"""\b[a-zA-Z][a-zA-Z0-9+.-]+://[^\s/@'"<>]+@[^\s'"<>]+"""),
    re.compile(r"(?i)\b(?:sk[-_]|gh[pousr]_|github_pat_|AKIA|xox[baprs]-)[A-Za-z0-9_./+-]+"),
    re.compile(r"(?i)\b(?:[a-f0-9]{24,}|[a-z0-9_+-]{32,}={0,2})\b"),
    re.compile(r"""(?<![\w:])(?:/|~\/|(?:\./)?runtime/)[^\s'"`<>()\[\],;]+"""),
]


class Redactor:
    def __init__(self, settings: Settings) -> None:
        known = [
            settings.database_url,
            str(settings.runtime_path),
            str(settings.kubeconfig) if settings.kubeconfig else None,
            *(
                getattr(settings, field).get_secret_value() if getattr(settings, field) else None
                for field in (
                    "model_api_key",
                    "webhook_token",
                    "langsmith_api_key",
                    "langfuse_secret_key",
                )
            ),
        ]
        self.known = sorted({s for s in known if s and len(s) >= 4}, key=len, reverse=True)

    def __call__(self, text: str) -> str:
        for value in self.known:
            text = text.replace(value, "【已隐藏】")
        for pattern in PATTERNS:
            text = pattern.sub("【已隐藏】", text)
        return text

    def change(self, change: dict[str, Any]) -> dict[str, Any]:
        path = change["path"]
        sensitive = bool(SENSITIVE.search(path))

        def value(item: Any) -> str | None:
            if item is None:
                return None
            if sensitive:
                return "已变化"
            return self(json.dumps(item, ensure_ascii=False, sort_keys=True))

        # 涉及整个对象或列表时，键内也可能含敏感内容，整项隐藏。
        sensitive = sensitive or any(
            SENSITIVE.search(json.dumps(change[key], ensure_ascii=False))
            for key in ("from", "to")
            if isinstance(change[key], (dict, list))
        )
        return {
            "path": self(path),
            "before": value(change["from"]),
            "after": value(change["to"]),
            "sensitive": sensitive,
        }
