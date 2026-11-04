"""F03 实验代理：透明转发 Redis TCP 流量，并按块增加固定延迟。"""

from __future__ import annotations

import asyncio
import os

DELAY = float(os.environ.get("DELAY_MS", "500")) / 1000
UPSTREAM_HOST = os.environ.get("UPSTREAM_HOST", "redis-cart-direct")
UPSTREAM_PORT = int(os.environ.get("UPSTREAM_PORT", "6379"))
LISTEN_PORT = int(os.environ.get("PORT", "6379"))


async def pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        while True:
            data = await reader.read(65536)
            if not data:
                break
            if DELAY > 0:
                await asyncio.sleep(DELAY)
            writer.write(data)
            await writer.drain()
    except (ConnectionError, asyncio.CancelledError):
        pass
    finally:
        writer.close()


async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        upstream_reader, upstream_writer = await asyncio.open_connection(
            UPSTREAM_HOST, UPSTREAM_PORT
        )
    except OSError:
        writer.close()
        return
    await asyncio.gather(
        pipe(reader, upstream_writer),
        pipe(upstream_reader, writer),
    )


async def main() -> None:
    server = await asyncio.start_server(handle, "0.0.0.0", LISTEN_PORT)
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    asyncio.run(main())
