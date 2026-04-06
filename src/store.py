from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class MetricStore:
    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self.host: dict[str, Any] = {}
        self.containers: dict[str, dict[str, Any]] = {}
        self.container_disk: dict[str, dict[str, Any]] = {}
        self.docker_disk: dict[str, Any] = {}
        self.coolify_map: dict[str, dict[str, Any]] = {}
        self.last_updated: str = ""

    async def update_host(self, data: dict[str, Any]) -> None:
        async with self._lock:
            self.host = data
            self.last_updated = _now_iso()

    async def update_containers(self, data: dict[str, dict[str, Any]]) -> None:
        async with self._lock:
            self.containers = data
            self.last_updated = _now_iso()

    async def update_docker_disk(self, data: dict[str, Any]) -> None:
        async with self._lock:
            self.docker_disk = data.get("summary", {})
            self.container_disk = data.get("containers", {})
            self.last_updated = _now_iso()

    async def update_coolify(self, data: dict[str, dict[str, Any]]) -> None:
        async with self._lock:
            self.coolify_map = data
            self.last_updated = _now_iso()

    def _match_container_to_coolify(self, container: dict[str, Any]) -> dict[str, Any] | None:
        labels = container.get("labels", {}) or {}
        name = container.get("name", "")

        explicit_uuid = None
        for key in (
            "coolify.uuid",
            "coolify.resourceUuid",
            "coolify.resource_uuid",
            "coolify.applicationId",
            "coolify.serviceId",
        ):
            value = labels.get(key)
            if value and value in self.coolify_map:
                explicit_uuid = value
                break

        if explicit_uuid:
            return {"uuid": explicit_uuid, **self.coolify_map[explicit_uuid]}

        for uuid, metadata in self.coolify_map.items():
            if name == uuid or name.endswith(uuid) or name.endswith(f"-{uuid}"):
                return {"uuid": uuid, **metadata}

        if labels.get("coolify.managed") == "true":
            return {
                "uuid": None,
                "type": "unknown",
                "name": name,
                "managed_by_coolify": True,
            }

        return None

    async def get_snapshot(self) -> dict[str, Any]:
        async with self._lock:
            enriched: dict[str, dict[str, Any]] = {}
            for container_id, container_data in self.containers.items():
                merged = {**container_data}
                disk_data = self.container_disk.get(container_id)
                if disk_data:
                    merged["disk_rw_bytes"] = disk_data.get("size_rw_bytes", 0)
                    merged["disk_rootfs_bytes"] = disk_data.get("size_rootfs_bytes", 0)
                    merged["volumes"] = disk_data.get("volumes", [])
                coolify = self._match_container_to_coolify(container_data)
                if coolify:
                    merged["coolify"] = coolify
                enriched[container_id] = merged

            return {
                "host": self.host,
                "containers": enriched,
                "docker_disk": self.docker_disk,
                "last_updated": self.last_updated,
            }
