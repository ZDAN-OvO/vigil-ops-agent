"""实验环境的只读发布指标端点。"""

from __future__ import annotations

import argparse
import json
from datetime import datetime

import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Route

from aiops.config import Settings
from aiops.monitoring.snapshot import Monitoring


def release_metrics(panel: dict) -> str:
    lines = [
        "# HELP vigil_release_source_available 发布来源可用为 1，缺数或失败为 0。",
        "# TYPE vigil_release_source_available gauge",
    ]
    for source in ("replicasets", "rollbacks"):
        available = source not in panel["failed"] and panel["data"] is not None
        if source == "replicasets" and {
            "replicaset_history",
            "rollback_classification",
        }.intersection(panel["missing"]):
            available = False
        lines.append(f'vigil_release_source_available{{source="{source}"}} {int(available)}')

    lines += [
        "# HELP vigil_release_timestamp_seconds 保留的发布或已核实回滚的 Unix 时间。",
        "# TYPE vigil_release_timestamp_seconds gauge",
    ]
    samples = set()
    for row in (panel["data"] or {}).get("items", []):
        timestamp = datetime.fromisoformat(row["occurred_at"]).timestamp()
        labels = {
            "namespace": row["namespace"],
            "service": row["service"],
            "version": row["version"],
            "change_type": row["kind"],
            "change_at": str(timestamp),
        }
        encoded = ",".join(
            f"{key}={json.dumps(value, ensure_ascii=False)}" for key, value in labels.items()
        )
        samples.add(f"vigil_release_timestamp_seconds{{{encoded}}} {timestamp}")
    return "\n".join(lines + sorted(samples)) + "\n"


def create_app(monitoring: Monitoring) -> Starlette:
    async def metrics(_request: Request) -> Response:
        panel = await monitoring.release_changes()
        return Response(
            release_metrics(panel),
            headers={"content-type": "text/plain; version=0.0.4; charset=utf-8"},
        )

    return Starlette(routes=[Route("/metrics", metrics, methods=["GET"])])


def main() -> None:
    parser = argparse.ArgumentParser(description="导出 Vigil 实验环境发布指标")
    parser.add_argument("--host", required=True, help="Prometheus 可以访问的实验网络接口")
    parser.add_argument("--port", type=int, default=8092)
    args = parser.parse_args()
    uvicorn.run(
        create_app(Monitoring.from_settings(Settings())),
        host=args.host,
        port=args.port,
        access_log=False,
    )


if __name__ == "__main__":
    main()
