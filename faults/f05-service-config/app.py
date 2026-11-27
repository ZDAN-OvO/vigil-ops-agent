"""带有应用配置加载信号的业务 HTTP 夹具。"""

import os
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

MODE_FILE = Path("/service-config/mode")
PUBLIC_MODES = {"healthy", "degraded", "worsen", "limited"}


def loaded_mode(source: str) -> str:
    try:
        mode = MODE_FILE.read_text().strip() if source == "configmap" else os.environ["VIGIL_MODE"]
    except (OSError, KeyError):
        return "missing"
    return mode if mode in PUBLIC_MODES else "unknown"


def business_status(mode: str, profile: str, now: float, *, gate: bool) -> int:
    if mode in {"healthy", "limited"}:
        return HTTPStatus.OK
    if not gate:
        minute = int(now // 60) % 5
        if mode == "degraded" and profile in {"compensate", "low-margin"}:
            return HTTPStatus.SERVICE_UNAVAILABLE if minute == 0 else HTTPStatus.OK
        if mode == "worsen" and profile == "low-margin":
            return HTTPStatus.SERVICE_UNAVAILABLE if minute < 2 else HTTPStatus.OK
    return HTTPStatus.SERVICE_UNAVAILABLE


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        route = urlsplit(self.path)
        if route.path == "/live":
            self.reply(HTTPStatus.OK, "ok\n")
            return
        if route.path == "/metrics":
            lines = []
            for source in ("configmap", "env"):
                mode = loaded_mode(source)
                lines.append(
                    f'vigil_config_fixture_loaded_mode{{source="{source}",mode="{mode}"}} 1\n'
                )
            self.reply(HTTPStatus.OK, "".join(lines))
            return
        if route.path not in {"/configuration", "/business"}:
            self.reply(HTTPStatus.NOT_FOUND, "unknown path\n")
            return
        sources = parse_qs(route.query).get("source", [])
        if sources not in (["configmap"], ["env"]):
            self.reply(HTTPStatus.BAD_REQUEST, "invalid source\n")
            return
        source = sources[0]
        mode = loaded_mode(source)
        profile = os.environ.get("VIGIL_PROFILE", "normal") if source == "configmap" else "normal"
        status = business_status(mode, profile, time.time(), gate=route.path == "/configuration")
        body = "service available\n" if status == HTTPStatus.OK else "service unavailable\n"
        self.reply(status, body)

    def reply(self, status: int, body: str):
        self.send_response(status)
        self.send_header("Content-Type", "text/plain; version=0.0.4")
        self.end_headers()
        self.wfile.write(body.encode())


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", 8080), Handler).serve_forever()
