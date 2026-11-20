"""会话、终端和网页共用的出站脱敏。内部执行对象应继续使用原始记录。"""

import re
from collections.abc import Mapping
from typing import Any
from urllib.parse import parse_qsl, quote, unquote, urlencode, urlsplit, urlunsplit

from aiops.contracts import ActionAttempt, AttemptStatus

REDACTED = "[已遮盖]"
_SENSITIVE = re.compile(
    r"password|passwd|passphrase|secret|token|credential|authorization|cookie|"
    r"api[-_]?key|private[-_]?key|client[-_]?key|kubeconfig|database[-_]?url|"
    r"access[-_]?key|session[-_]?key|connection[-_]?string|ticket|^dsn$|^stringData$|"
    r"^raw[-_]?ref$|^file[-_]?path$|^hostPath$|^mountPath$|^(?:pod_?)?template$",
    re.I,
)
_BEARER = re.compile(r"\b(?:bearer|basic)\s+[^\s,;'\"<>]+", re.I)
_ASSIGNMENT_KEY = re.compile(r"['\"]?(?P<key>[\w.-]+)['\"]?\s*[:=]\s*")
_ASSIGNMENT_VALUE = re.compile(r'(?:"[^"\n]*"|\'[^\'\n]*\'|\[已遮盖\]|[^\s,;}\]]+)')
_CREDENTIAL_HEADER = re.compile(r"(?im)(\b(?:authorization|(?:set-)?cookie)\s*:)[ \t]+[^\r\n]+")
_PRIVATE_KEY = re.compile(
    r"-----BEGIN [^-]*PRIVATE KEY-----.*?(?:-----END [^-]*PRIVATE KEY-----|$)", re.S
)
_TOKEN = re.compile(
    r"\b(?:sk-[\w-]{8,}|gh[pousr]_[\w]{8,}|github_pat_[\w]{8,}"
    r"|eyJ[\w-]+\.[\w-]+\.[\w-]+)\b"
)
_LOCAL_PATH = re.compile(
    r"(?<![\w/])(?:file://)?(?:~|/+(?:home|root|tmp|var|run|etc|opt|mnt|Users|private"
    r"|workspace|workspaces|proc|srv|usr|dev|sys))(/[^\s'\"<>,;\])}]*)"
    r"|\b[A-Za-z]:\\[^\s'\"<>,;\])}]+"
    r"|(?<![\w/])(?:\.\.?/[\w/.-]+|\.env(?:\.[\w.-]+)?)"
)
_URL = re.compile(
    r"\b[a-z][a-z0-9+.-]*://(?:\[[0-9a-f:]+\])?[^\s'\"<>\])}]*",
    re.I,
)
_URL_PLACEHOLDER = re.compile(r"\ufff0([0-9]+)\ufff1")


def _redact_assignments(text: str) -> str:
    text = _PRIVATE_KEY.sub(REDACTED, text)
    text = _CREDENTIAL_HEADER.sub(lambda match: match[1] + " " + REDACTED, text)
    text = _BEARER.sub(REDACTED, text)
    chunks: list[str] = []
    after_value = 0
    for match in _ASSIGNMENT_KEY.finditer(text):
        if match.start() < after_value or not _SENSITIVE.search(match["key"]):
            continue
        value = _ASSIGNMENT_VALUE.match(text, match.end())
        if value is not None:
            chunks.extend((text[after_value : match.end()], REDACTED))
            after_value = value.end()
    chunks.append(text[after_value:])
    return "".join(chunks)


def _redact_text(text: str) -> str:
    return _LOCAL_PATH.sub(REDACTED, _TOKEN.sub(REDACTED, _redact_assignments(text)))


def project_url(value: str) -> str | None:
    """公开 HTTP(S) 地址仅保留脱敏后的路径和参数，去除用户信息。"""
    try:
        url = urlsplit(value)
        host = url.hostname
        port = url.port
        if url.scheme not in {"http", "https"} or not host:
            return None
        authority = f"[{host}]" if ":" in host else host
        if port is not None:
            authority += f":{port}"
        decoded_path = unquote(url.path)
        if re.search(r"/(?:login|auth|token|credential)(?:/|$)", decoded_path, re.I):
            return None
        redacted_path = _redact_text(decoded_path)
        path = url.path if decoded_path == redacted_path else quote(redacted_path, safe="/")
        query = urlencode(
            [
                (_redact_text(key), REDACTED if _SENSITIVE.search(key) else _redact_text(item))
                for key, item in parse_qsl(url.query, keep_blank_values=True)
            ]
        )
        fragment = unquote(url.fragment)
        redacted_fragment = _redact_text(fragment)
        return urlunsplit(
            (
                url.scheme,
                authority,
                path,
                query,
                url.fragment if fragment == redacted_fragment else quote(redacted_fragment),
            )
        )
    except ValueError:
        return None


def project_text(value: str) -> str:
    # URL 分开投影，避免口令赋值或认证头把地址的一部分落在遮盖范围之外。
    urls: list[str] = []

    def mask(match: re.Match[str]) -> str:
        urls.append(project_url(match[0]) or REDACTED)
        return f"\ufff0{len(urls) - 1}\ufff1"

    redacted = _redact_text(_URL.sub(mask, value))
    return _URL_PLACEHOLDER.sub(
        lambda match: urls[int(match[1])] if int(match[1]) < len(urls) else REDACTED,
        redacted,
    )


def project_payload(value: Any) -> Any:
    """递归投影 JSON 载荷，调用方应先挑出入口需要的字段。"""
    if isinstance(value, Mapping):
        return {
            project_text(key): REDACTED if _SENSITIVE.search(key) else project_payload(item)
            for key, item in value.items()
            if isinstance(key, str)
        }
    if isinstance(value, (list, tuple)):
        return [project_payload(item) for item in value]
    if isinstance(value, str):
        return project_text(value)
    return value


_TEMPLATE_PRIVATE = re.compile(
    r"(^|/)(?:env|envFrom|args|command|annotations|hostPath|imagePullSecrets"
    r"|secret|secretKeyRef|projected|httpHeaders)(?:/|$)",
    re.I,
)


def project_template_diff(changes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """保留差异的位置与操作，遮盖模板中可能内嵌凭据的值。"""
    projected = []
    for change in changes:
        path = change["path"]
        item = {"op": change["op"], "path": project_text(path)}
        for field in ("current", "value"):
            if field in change:
                item[field] = (
                    REDACTED if _TEMPLATE_PRIVATE.search(path) else _project_template(change[field])
                )
        projected.append(item)
    return projected


def _project_template(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {
            project_text(key): REDACTED
            if _TEMPLATE_PRIVATE.search(key) or _SENSITIVE.search(key)
            else _project_template(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_project_template(item) for item in value]
    return project_payload(value)


def card_url(web_url: str | None, session_id: str, card_id: str) -> str | None:
    if not web_url:
        return None
    try:
        url = urlsplit(web_url)
        if (
            url.scheme not in {"http", "https"}
            or url.hostname not in {"localhost", "127.0.0.1", "::1"}
            or url.username is not None
            or url.password is not None
            or url.path not in {"", "/"}
            or url.query
            or url.fragment
        ):
            return None
        # 访问 port 会验证端口表示。
        port = url.port
        host = f"[{url.hostname}]" if url.hostname == "::1" else url.hostname
        authority = host if port is None else f"{host}:{port}"
        return urlunsplit(
            (
                url.scheme,
                authority,
                f"/sessions/{quote(session_id, safe='')}",
                "",
                f"card-{quote(card_id, safe='')}",
            )
        )
    except ValueError:
        return None


def project_action_result(
    attempt: ActionAttempt | None,
    *,
    proposal_id: str | None = None,
    status: str | None = None,
) -> dict[str, Any]:
    if attempt is None:
        return {
            "card_id": proposal_id,
            "attempt_id": None,
            "status": status,
            "phase": "intent",
            "written": False,
            "verification": None,
        }
    result = attempt.result or {}
    stage = result.get("stage")
    reason = (attempt.verification or {}).get("outcome_reason") or (attempt.result or {}).get(
        "code"
    )
    sent = attempt.send_granted_at is not None or attempt.status in {
        AttemptStatus.EXECUTING,
        AttemptStatus.UNKNOWN,
        AttemptStatus.VERIFIED,
    }
    phase = "intent"
    written = False
    if sent:
        phase = "send_granted"
        written = None
        if attempt.status is AttemptStatus.VERIFIED or stage in {
            "applied",
            "rebuilt",
            "deleted",
            "verify_after_apply",
            "verify_after_delete",
        }:
            phase = "verification"
            written = True
    if stage == "manual_verification":
        phase = "verification"
    explicit = result.get("written", result.get("write_sent"))
    if isinstance(explicit, bool):
        written = explicit
    return {
        "card_id": attempt.proposal_id,
        "attempt_id": attempt.id,
        "status": attempt.status.value,
        "phase": phase,
        "written": written,
        "reason": project_text(reason) if isinstance(reason, str) else None,
        "verification": project_payload(attempt.verification),
    }
