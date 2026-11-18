"""只在出站时清理页面文本，数据库保留原始审计材料。"""

import json
import re
from typing import Any

from aiops.config import Settings

SENSITIVE = re.compile(
    r"(?i)env|secret|authorization|password|credential|token|private.?key|cookie|access.?key"
)
PATTERNS = [
    # 凭据不含引号：吞掉闭合引号会让后续键值规则只遮住引号内的首个单词。
    re.compile(r"""(?i)\b(?:authorization\s*[:=]\s*)?(?:bearer|basic)\s+[^\s,;"']+"""),
    re.compile(
        r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?(?:-----END [A-Z ]*PRIVATE KEY-----|$)", re.S
    ),
    re.compile(r"""\b[a-zA-Z][a-zA-Z0-9+.-]+://[^\s/@'"<>]+@[^\s'"<>]+"""),
    re.compile(r"(?i)\b(?:sk[-_]|gh[pousr]_|github_pat_|AKIA|xox[baprs]-)[A-Za-z0-9_./+-]+"),
    re.compile(r"(?i)\b(?:[a-f0-9]{24,}|[a-z0-9_+-]{32,}={0,2})\b"),
    re.compile(r"""(?i)\bfile:[^\s'"`<>()\[\],;]+"""),
    re.compile(r"""(?<![\w:])(?:/|~\/|(?:\./)?runtime/)[^\s'"`<>()\[\],;]+"""),
]
# 键名允许带前缀或后缀（DB_PASSWORD、SSL_PRIVATE_KEY、MY_API_TOKEN）。先在分隔符边界取完整键名，
# 再检查是否含敏感词，不在键名内部逐位置尝试，长串标点不会拖慢过滤。
KEY_VALUE = re.compile(
    r"""(?<![A-Za-z0-9_.-])([A-Za-z0-9_.-]+)["']?\s*[:=]\s*(?:"[^"]*"|'[^']*'|[^\s,;]+)"""
)
SECRET_KEY = re.compile(
    r"(?i)password|passwd|pwd|secret|token|api[-_. ]?key|authorization|access[-_. ]?key"
    r"|private[-_. ]?key|credential"
)
API_KEY_SPACED = re.compile(r"""(?i)\bapi key["']?\s*[:=]\s*(?:"[^"]*"|'[^']*'|[^\s,;]+)""")
# 只有卡片变更清单的镜像字段允许显示摘要，且必须是完整的镜像引用。
IMAGE_REF = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/:-]*@sha256:[a-f0-9]{64}$")


def _hide_secret_pair(match: re.Match[str]) -> str:
    return "【已隐藏】" if SECRET_KEY.search(match.group(1)) else match.group(0)


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
        # 先遮整段凭据（Authorization 头、PEM 块），再遮键值；否则键值规则会先吞掉起始标记。
        for pattern in PATTERNS[:2]:
            text = pattern.sub("【已隐藏】", text)
        text = KEY_VALUE.sub(_hide_secret_pair, text)
        text = API_KEY_SPACED.sub("【已隐藏】", text)
        for pattern in PATTERNS[2:]:
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
            if path.endswith(".image") and isinstance(item, str) and IMAGE_REF.match(item):
                # 只豁免摘要本身，仓库与名称部分照常过滤。
                name, digest = item.split("@sha256:", 1)
                return json.dumps(f"{self(name)}@sha256:{digest}", ensure_ascii=False)
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
