"""以固定节拍请求真实首页，记录已发出、跳过及完成的请求。"""

import os
import threading
import time
import urllib.error
import urllib.request
from collections import Counter
from concurrent.futures import ThreadPoolExecutor

URL = "http://frontend.vigil-shop.svc.cluster.local./"
RATE = int(os.environ.get("VIGIL_LOAD_RPS", "1"))
MAX_IN_FLIGHT = 80
slots = threading.BoundedSemaphore(MAX_IN_FLIGHT)
lock = threading.Lock()
counts = Counter()


def request():
    try:
        with urllib.request.urlopen(URL, timeout=4) as response:
            response.read()
            status = response.status
    except urllib.error.HTTPError as error:
        status = error.code
    except OSError:
        status = "network_error"
    finally:
        slots.release()
    with lock:
        counts[status] += 1


if __name__ == "__main__":
    if RATE < 1:
        raise ValueError("VIGIL_LOAD_RPS 必须为正整数")
    with ThreadPoolExecutor(max_workers=MAX_IN_FLIGHT) as pool:
        while True:
            started = time.monotonic()
            issued = 0
            for index in range(RATE):
                time.sleep(max(0, started + index / RATE - time.monotonic()))
                if slots.acquire(blocking=False):
                    pool.submit(request)
                    issued += 1
            time.sleep(max(0, started + 1 - time.monotonic()))
            with lock:
                completed = dict(counts)
                counts.clear()
            print(
                f"epoch={int(time.time())} scheduled={RATE} issued={issued} completed={completed}"
            )
