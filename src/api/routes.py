from __future__ import annotations

import asyncio
import json
from pathlib import Path

from aiohttp import web


async def metrics_handler(request: web.Request) -> web.Response:
    snapshot = await request.app["store"].get_snapshot()
    return web.json_response(snapshot)


async def sse_handler(request: web.Request) -> web.StreamResponse:
    response = web.StreamResponse(
        status=200,
        headers={
            "Content-Type": "text/event-stream",
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
    await response.prepare(request)

    interval = request.app["config"].stats_interval
    try:
        while True:
            snapshot = await request.app["store"].get_snapshot()
            payload = json.dumps(snapshot, separators=(",", ":"))
            await response.write(f"data: {payload}\n\n".encode("utf-8"))
            await asyncio.sleep(interval)
    except (asyncio.CancelledError, ConnectionResetError, RuntimeError):
        pass

    return response


async def index_handler(request: web.Request) -> web.FileResponse:
    frontend_dir = Path(request.app["frontend_dir"])
    return web.FileResponse(frontend_dir / "index.html")


async def static_handler(request: web.Request) -> web.StreamResponse:
    path = request.match_info.get("path", "")
    if not path or path.startswith("api/"):
        raise web.HTTPNotFound()
    frontend_dir = Path(request.app["frontend_dir"])
    file_path = (frontend_dir / path).resolve()
    try:
        file_path.relative_to(frontend_dir.resolve())
    except ValueError as exc:
        raise web.HTTPForbidden() from exc
    if not file_path.exists() or not file_path.is_file():
        raise web.HTTPNotFound()
    return web.FileResponse(file_path)


def setup_routes(app: web.Application) -> None:
    app.router.add_get("/api/metrics", metrics_handler)
    app.router.add_get("/api/stream", sse_handler)
    app.router.add_get("/", index_handler)
    app.router.add_get("/{path:.*}", static_handler)
