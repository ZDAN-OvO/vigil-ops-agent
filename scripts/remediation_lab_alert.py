"""在隔离实验中延迟告警投递，或重放已采集的 Alertmanager 通知。"""

import argparse
import json
import os
import sys
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.request import Request, urlopen

ROUTED_ALERTS = (
    "FrontendProbeFailed",
    "VigilCpuLimitDegraded",
    "VigilReplicaCapacityDegraded",
    "VigilServiceConfigDegraded",
)
GROUP_LABELS = ("alertname", "namespace", "source", "target")


def time_text(timestamp):
    return timestamp.isoformat().replace("+00:00", "Z")


def request_json(url, *, method="GET", payload=None, token=None):
    headers = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    data = None
    if payload is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(payload).encode()
    request = Request(url, data=data, headers=headers, method=method)
    with urlopen(request, timeout=15) as response:
        raw = response.read()
    return json.loads(raw) if raw else None


def silence(url, alertname, source, minutes):
    labels = {"alertname": alertname}
    if source:
        labels["source"] = source
    now = datetime.now(UTC)
    return request_json(
        url + "/api/v2/silences",
        method="POST",
        payload={
            "matchers": [
                {"name": key, "value": value, "isRegex": False, "isEqual": True}
                for key, value in labels.items()
            ],
            "startsAt": time_text(now - timedelta(seconds=5)),
            "endsAt": time_text(now + timedelta(minutes=minutes)),
            "createdBy": "vigil-lab-operator",
            "comment": "Delay delivery while gathering acceptance evidence",
        },
    )


def capture(url, alertname, source):
    matches = [
        row
        for row in request_json(url + "/api/v2/alerts/groups")
        if row["receiver"]["name"] == "vigil-webhook"
        and row["labels"].get("alertname") == alertname
        and (source is None or row["labels"].get("source") == source)
    ]
    if len(matches) != 1:
        raise ValueError(f"预期匹配一个告警组，实际匹配 {len(matches)} 个")
    group = matches[0]
    labels = group["labels"]
    label_set = "{" + ", ".join(f"{key}={json.dumps(labels[key])}" for key in GROUP_LABELS) + "}"
    route = "{}/{alertname=" + json.dumps(alertname) + "}"
    return {
        "version": "4",
        "receiver": group["receiver"]["name"],
        "status": "firing",
        "groupKey": route + ":" + label_set,
        "groupLabels": labels,
        "alerts": [
            {**alert, "status": "firing"}
            for alert in group["alerts"]
            if alert["status"]["state"] in {"active", "suppressed"}
        ],
    }


def replay(url, file, token_file, mode):
    body = json.loads(file.read_text(encoding="utf-8"))
    now = datetime.now(UTC)
    if mode == "new":
        if body["status"] != "firing":
            raise ValueError("新告警需要 firing 格式")
        for alert in body["alerts"]:
            alert["fingerprint"] = uuid.uuid4().hex[:16]
            alert["startsAt"] = time_text(now)
            alert["endsAt"] = "0001-01-01T00:00:00Z"
    elif mode == "resolved":
        body["status"] = "resolved"
        for alert in body["alerts"]:
            alert["status"] = "resolved"
            alert["endsAt"] = time_text(now)
    if not body["alerts"]:
        raise ValueError("告警组为空")
    token = token_file.read_text(encoding="utf-8").strip()
    if not token:
        raise ValueError("令牌文件为空")
    response = request_json(url, method="POST", payload=body, token=token)
    return {"delivery": response, "payload": body}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--alertmanager",
        default=os.environ.get("VIGIL_LAB_ALERTMANAGER_URL", "http://127.0.0.1:9093"),
    )
    commands = parser.add_subparsers(dest="command", required=True)
    for operation in ("silence", "capture"):
        command = commands.add_parser(operation)
        command.add_argument("alertname", choices=ROUTED_ALERTS)
        command.add_argument("--source")
        if operation == "silence":
            command.add_argument("--minutes", type=int, default=20)
    expire = commands.add_parser("expire")
    expire.add_argument("silence_id", type=uuid.UUID)
    deliver = commands.add_parser("replay")
    deliver.add_argument("url")
    deliver.add_argument("payload", type=Path)
    deliver.add_argument("--token-file", type=Path, required=True)
    deliver.add_argument("--mode", choices=("verbatim", "new", "resolved"), default="verbatim")
    args = parser.parse_args()
    url = args.alertmanager.rstrip("/")
    try:
        if args.command == "silence":
            if args.minutes < 1:
                parser.error("--minutes 至少为 1")
            result = silence(url, args.alertname, args.source, args.minutes)
        elif args.command == "capture":
            result = capture(url, args.alertname, args.source)
        elif args.command == "expire":
            request_json(url + f"/api/v2/silence/{args.silence_id}", method="DELETE")
            result = {"expired": str(args.silence_id)}
        else:
            result = replay(args.url, args.payload, args.token_file, args.mode)
    except (KeyError, OSError, ValueError) as error:
        parser.exit(2, f"告警工具执行失败：{error}\n")
    json.dump(result, sys.stdout, ensure_ascii=False, indent=2)
    print()


if __name__ == "__main__":
    main()
