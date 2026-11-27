"""使用真实的 HTTP 交易核验单实例与双实例的并发容量。"""

import os
import socket
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

BACKENDS = "vigil-replica-backends.vigil-experiment.svc.cluster.local."
state_lock = threading.Lock()
last_batch = (0, 0, 0.0)


def request_transaction(address: tuple[str, int], start: threading.Barrier) -> int:
    start.wait(timeout=2)
    host, port = address
    try:
        with urllib.request.urlopen(f"http://{host}:{port}/transaction", timeout=2) as response:
            return response.status
    except urllib.error.HTTPError as error:
        return error.code
    except OSError:
        return HTTPStatus.SERVICE_UNAVAILABLE


def run_batch() -> int:
    global last_batch
    try:
        endpoints = sorted(
            {entry[4] for entry in socket.getaddrinfo(BACKENDS, 8080, socket.AF_INET)}
        )
    except socket.gaierror:
        endpoints = []
    statuses = []
    if endpoints:
        start = threading.Barrier(2)
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(request_transaction, endpoints[0], start)
            second = pool.submit(request_transaction, endpoints[-1], start)
            statuses = [first.result(), second.result()]
    with state_lock:
        last_batch = (len(endpoints), statuses.count(HTTPStatus.TOO_MANY_REQUESTS), time.time())
    if statuses == [HTTPStatus.OK, HTTPStatus.OK]:
        return HTTPStatus.OK
    return HTTPStatus.SERVICE_UNAVAILABLE


def metrics() -> str:
    with state_lock:
        endpoints, rejected, collected_at = last_batch
    if time.time() - collected_at > 45:
        return ""
    return (
        f"vigil_replica_fixture_endpoints {endpoints}\n"
        f"vigil_replica_fixture_rejected {rejected}\n"
        f"vigil_replica_fixture_batch_timestamp_seconds {collected_at}\n"
    )


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/live":
            self.reply(HTTPStatus.OK, "ok\n")
        elif self.server.gateway and self.path == "/business":
            self.reply(run_batch(), "transaction batch\n")
        elif self.server.gateway and self.path == "/metrics":
            self.reply(HTTPStatus.OK, metrics())
        elif not self.server.gateway and self.path == "/transaction":
            if not self.server.slot.acquire(blocking=False):
                self.reply(HTTPStatus.TOO_MANY_REQUESTS, "capacity exceeded\n")
                return
            try:
                time.sleep(0.5)
                self.reply(HTTPStatus.OK, "transaction completed\n")
            finally:
                self.server.slot.release()
        else:
            self.reply(HTTPStatus.NOT_FOUND, "unknown path\n")

    def reply(self, status: int, body: str):
        self.send_response(status)
        self.send_header("Content-Type", "text/plain; version=0.0.4")
        self.end_headers()
        self.wfile.write(body.encode())


class Server(ThreadingHTTPServer):
    def __init__(self, address, *, gateway):
        self.gateway = gateway
        self.slot = threading.BoundedSemaphore()
        super().__init__(address, Handler)


if __name__ == "__main__":
    Server(("0.0.0.0", 8080), gateway=os.environ["VIGIL_ROLE"] == "gateway").serve_forever()
