"""A01 实验负载：请求时读取 Pod 级 emptyDir 状态，状态损坏则业务请求失败。

状态文件位于 emptyDir，容器重启不会清除它；只有 Pod 重建才会得到全新卷。
"""

from __future__ import annotations

import os
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

STATE_FILE = os.environ.get("STATE_FILE", "/state/work_state")
POISON_FILE = os.environ.get("POISON_FILE", "/config/poison")


def apply_startup_poison() -> None:
    """启动时读取挂载开关；开关打开就把新 Pod 的状态写成损坏。"""
    try:
        with open(POISON_FILE, encoding="utf-8") as handle:
            armed = handle.read().strip() == "yes"
    except FileNotFoundError:
        return
    if not armed:
        return
    with open(STATE_FILE, "w", encoding="utf-8") as handle:
        handle.write("corrupt")


def state_corrupt() -> bool:
    try:
        with open(STATE_FILE, encoding="utf-8") as handle:
            return handle.read().strip() == "corrupt"
    except FileNotFoundError:
        return False


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _respond(self, status: int, body: str) -> None:
        payload = body.encode()
        self.send_response(status)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/healthz":
            # 只反映进程存活，不读取业务状态，避免 kubelet 自行重启掩盖故障。
            self._respond(200, "ok")
        elif self.path == "/work":
            if state_corrupt():
                self._respond(500, "workload state corrupt")
            else:
                self._respond(200, "ok")
        elif self.path == "/crash":
            self._respond(202, "exiting")
            self.wfile.flush()
            os._exit(1)
        else:
            self._respond(404, "not found")

    def log_message(self, fmt: str, *args: object) -> None:
        sys.stderr.write("%s %s\n" % (self.log_date_time_string(), fmt % args))


def main() -> None:
    apply_startup_poison()
    server = ThreadingHTTPServer(("0.0.0.0", 8080), Handler)
    print(f"listening on 8080 state={STATE_FILE}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
