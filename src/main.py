from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from aiohttp import web

from src.api.middleware import basic_auth_middleware, cors_middleware
from src.api.routes import setup_routes
from src.collectors.docker_disk import DockerDiskCollector
from src.collectors.docker_stats import DockerStatsCollector
from src.collectors.host import HostCollector
from src.config import Config
from src.enrichers.coolify import CoolifyEnricher
from src.store import MetricStore


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)


async def start_background_tasks(app: web.Application) -> None:
    store = app["store"]
    config = app["config"]

    app["host_task"] = asyncio.create_task(HostCollector(store, config).run_forever())
    app["docker_task"] = asyncio.create_task(DockerStatsCollector(store, config).run_forever())
    app["disk_task"] = asyncio.create_task(DockerDiskCollector(store, config).run_forever())
    if config.coolify_api_token:
        app["coolify_task"] = asyncio.create_task(CoolifyEnricher(store, config).run_forever())


async def cleanup_background_tasks(app: web.Application) -> None:
    for key in ("host_task", "docker_task", "disk_task", "coolify_task"):
        task = app.get(key)
        if task is None:
            continue
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


def create_app() -> web.Application:
    config = Config.from_env()
    store = MetricStore()
    app = web.Application(middlewares=[cors_middleware, basic_auth_middleware])
    app["config"] = config
    app["store"] = store
    app["frontend_dir"] = str(Path(__file__).resolve().parent.parent / "frontend")

    setup_routes(app)

    app.on_startup.append(start_background_tasks)
    app.on_cleanup.append(cleanup_background_tasks)
    return app


if __name__ == "__main__":
    application = create_app()
    web.run_app(application, port=application["config"].monitor_port)
