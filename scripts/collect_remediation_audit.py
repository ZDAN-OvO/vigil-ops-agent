"""从 Kubernetes Metadata 审计日志提取实验期间的写请求。"""

import argparse
import gzip
import json
import sys
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

REMEDIATION_ACTOR = "system:serviceaccount:vigil-monitoring:vigil-remediation"
WRITE_VERBS = {"create", "update", "patch", "delete", "deletecollection", "connect"}
TERMINAL_STAGES = {"ResponseComplete", "Panic"}


def utc_time(text):
    timestamp = datetime.fromisoformat(text)
    if timestamp.tzinfo is None:
        raise ValueError(f"时间必须包含时区：{text}")
    return timestamp.astimezone(UTC)


def utc_text(timestamp):
    return timestamp.isoformat().replace("+00:00", "Z")


def read_records(paths):
    for path in paths:
        if path == "-":
            yield from sys.stdin
        elif path.endswith(".gz"):
            with gzip.open(path, "rt", encoding="utf-8") as stream:
                yield from stream
        else:
            with Path(path).open(encoding="utf-8") as stream:
                yield from stream


def collect(lines, since, until, actors):
    requests = {}
    for line in lines:
        if not line.strip():
            continue
        event = json.loads(line)
        actor = event["user"]["username"]
        effective_actor = event.get("impersonatedUser", {}).get("username", actor)
        verb = event["verb"]
        if verb not in WRITE_VERBS or effective_actor not in actors:
            continue
        received_at = utc_time(event["requestReceivedTimestamp"])
        if not since <= received_at < until:
            continue
        audit_id = event["auditID"]
        uri = event["requestURI"]
        query = parse_qs(urlsplit(uri).query, keep_blank_values=True)
        dry_run = "All" in query.get("dryRun", [])
        identity = {
            "audit_id": audit_id,
            "actor": effective_actor,
            "authenticated_actor": actor,
            "verb": verb,
            "uri": uri,
            "target": event.get("objectRef", {}),
            "started_at": utc_text(received_at),
            "dry_run": dry_run,
        }
        request = requests.setdefault(audit_id, {"identity": identity, "stages": {}})
        # objectRef 在早期阶段可能还未包含 UID、名称或资源版本。
        target = request["identity"]["target"]
        if any(
            identity[key] != value for key, value in request["identity"].items() if key != "target"
        ):
            raise ValueError(f"同一 audit ID 对应不同请求：{audit_id}")
        target.update(identity["target"])
        status = event.get("responseStatus") or {}
        stage = {
            "stage": event["stage"],
            "at": utc_text(utc_time(event["stageTimestamp"])),
            "code": status.get("code"),
            "reason": status.get("reason"),
        }
        request["stages"][tuple(stage.values())] = stage

    result = []
    for request in sorted(
        requests.values(), key=lambda item: utc_time(item["identity"]["started_at"])
    ):
        entry = request["identity"]
        stages = sorted(request["stages"].values(), key=lambda stage: utc_time(stage["at"]))
        terminal = next(
            (stage for stage in reversed(stages) if stage["stage"] in TERMINAL_STAGES), None
        )
        code = terminal["code"] if terminal else None
        if not terminal:
            outcome = "incomplete"
        elif code in {408, 504} or terminal["reason"] in {"Timeout", "ServerTimeout"}:
            outcome = "timeout"
        elif terminal["stage"] == "Panic":
            outcome = "panic"
        elif code is None:
            outcome = "unknown"
        elif 200 <= code < 300:
            outcome = "success"
        elif 400 <= code < 500:
            outcome = "rejected"
        else:
            outcome = "error"
        result.append({**entry, "outcome": outcome, "status_code": code, "stages": stages})
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--since", required=True, type=utc_time)
    parser.add_argument("--until", required=True, type=utc_time)
    parser.add_argument("--actor", action="append", default=[], help="add an operator username")
    parser.add_argument("logs", nargs="+", help="JSONL paths, .gz files, or - for stdin")
    args = parser.parse_args()
    if args.since >= args.until:
        parser.error("since 必须早于 until")
    actors = {REMEDIATION_ACTOR, *args.actor}
    try:
        requests = collect(read_records(args.logs), args.since, args.until, actors)
    except (KeyError, OSError, TypeError, ValueError) as error:
        parser.exit(2, f"审计采集失败：{error}\n")
    summary = {actor: {"real": Counter(), "dry_run": Counter()} for actor in sorted(actors)}
    for item in requests:
        kind = "dry_run" if item["dry_run"] else "real"
        summary[item["actor"]][kind][item["outcome"]] += 1
    json.dump(
        {
            "since": utc_text(args.since),
            "until": utc_text(args.until),
            "summary": summary,
            "requests": requests,
        },
        sys.stdout,
        ensure_ascii=False,
        indent=2,
    )
    print()


if __name__ == "__main__":
    main()
