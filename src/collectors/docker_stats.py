from __future__ import annotations

import asyncio
import logging
import socket
import time
from datetime import datetime, timezone
from typing import Any

import docker

from src.config import Config
from src.store import MetricStore

LOGGER = logging.getLogger(__name__)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class DockerStatsCollector:
    def __init__(self, store: MetricStore, config: Config) -> None:
        self.store = store
        self.config = config
        self.interval = config.stats_interval
        self.client = docker.DockerClient(base_url="unix:///var/run/docker.sock")
        self._hostname = socket.gethostname()
        self._previous_network: dict[str, tuple[int, int, float]] = {}

    async def run_forever(self) -> None:
        while True:
            try:
                await self.collect()
            except Exception as exc:
                LOGGER.exception("Docker stats collection error: %s", exc)
            await asyncio.sleep(self.interval)

    async def collect(self) -> None:
        loop = asyncio.get_running_loop()
        containers = await loop.run_in_executor(None, lambda: self.client.containers.list(all=True))
        tasks = [self._collect_container(container) for container in containers]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        snapshot: dict[str, dict[str, Any]] = {}
        for result in results:
            if isinstance(result, Exception):
                LOGGER.warning("Skipping container after collection error: %s", result)
                continue
            if not result:
                continue
            snapshot[result["id"]] = result

        await self.store.update_containers(snapshot)

    async def _collect_container(self, container) -> dict[str, Any] | None:
        loop = asyncio.get_running_loop()
        data = await loop.run_in_executor(None, lambda: self._collect_container_sync(container))
        return data

    def _collect_container_sync(self, container) -> dict[str, Any] | None:
        if container.name == "coolify-monitor":
            return None
        if container.id.startswith(self._hostname):
            return None

        container.reload()
        attrs = container.attrs
        state = attrs.get("State", {}) or {}
        config = attrs.get("Config", {}) or {}
        image_name = (container.image.tags or [config.get("Image", "")])[0]

        info: dict[str, Any] = {
            "id": container.short_id,
            "name": container.name,
            "image": image_name,
            "status": container.status,
            "state_started_at": state.get("StartedAt"),
            "started_at": state.get("StartedAt"),
            "labels": container.labels or {},
            "created_at": attrs.get("Created"),
            "collected_at": _now_iso(),
            "cpu_percent": 0.0,
            "memory_usage_bytes": 0,
            "memory_limit_bytes": 0,
            "memory_percent": 0.0,
            "net_rx_bytes": 0,
            "net_tx_bytes": 0,
            "net_rx_bytes_per_sec": 0.0,
            "net_tx_bytes_per_sec": 0.0,
        }

        if container.status != "running":
            return info

        stats = container.stats(stream=False)
        cpu_stats = stats.get("cpu_stats", {}) or {}
        precpu_stats = stats.get("precpu_stats", {}) or {}
        cpu_delta = (
            cpu_stats.get("cpu_usage", {}).get("total_usage", 0)
            - precpu_stats.get("cpu_usage", {}).get("total_usage", 0)
        )
        system_delta = cpu_stats.get("system_cpu_usage", 0) - precpu_stats.get("system_cpu_usage", 0)
        num_cpus = cpu_stats.get("online_cpus") or len(cpu_stats.get("cpu_usage", {}).get("percpu_usage", []) or [1])
        cpu_percent = (cpu_delta / system_delta) * num_cpus * 100.0 if system_delta > 0 else 0.0

        memory_stats = stats.get("memory_stats", {}) or {}
        memory_usage = memory_stats.get("usage", 0)
        memory_cache = memory_stats.get("stats", {}).get("cache")
        if memory_cache is None:
            memory_cache = memory_stats.get("stats", {}).get("inactive_file", 0)
        memory_usage = max(memory_usage - (memory_cache or 0), 0)
        memory_limit = memory_stats.get("limit", 0)
        memory_percent = (memory_usage / memory_limit * 100.0) if memory_limit else 0.0

        net_rx = sum(int(item.get("rx_bytes", 0)) for item in (stats.get("networks", {}) or {}).values())
        net_tx = sum(int(item.get("tx_bytes", 0)) for item in (stats.get("networks", {}) or {}).values())
        now = time.monotonic()
        previous = self._previous_network.get(container.short_id)
        rx_rate = 0.0
        tx_rate = 0.0
        if previous:
            prev_rx, prev_tx, prev_time = previous
            elapsed = now - prev_time
            if elapsed > 0:
                rx_rate = max(0.0, (net_rx - prev_rx) / elapsed)
                tx_rate = max(0.0, (net_tx - prev_tx) / elapsed)
        self._previous_network[container.short_id] = (net_rx, net_tx, now)

        info.update(
            {
                "cpu_percent": cpu_percent,
                "memory_usage_bytes": memory_usage,
                "memory_limit_bytes": memory_limit,
                "memory_percent": memory_percent,
                "net_rx_bytes": net_rx,
                "net_tx_bytes": net_tx,
                "net_rx_bytes_per_sec": rx_rate,
                "net_tx_bytes_per_sec": tx_rate,
            }
        )
        return info
