from __future__ import annotations

import asyncio
import logging
from typing import Any

import docker

from src.config import Config
from src.store import MetricStore

LOGGER = logging.getLogger(__name__)


class DockerDiskCollector:
    def __init__(self, store: MetricStore, config: Config) -> None:
        self.store = store
        self.config = config
        self.interval = config.disk_interval
        self.client = docker.DockerClient(base_url="unix:///var/run/docker.sock")

    async def run_forever(self) -> None:
        while True:
            try:
                await self.collect()
            except Exception as exc:
                LOGGER.exception("Docker disk collection error: %s", exc)
            await asyncio.sleep(self.interval)

    async def collect(self) -> None:
        loop = asyncio.get_running_loop()
        payload = await loop.run_in_executor(None, self._collect_sync)
        await self.store.update_docker_disk(payload)

    def _collect_sync(self) -> dict[str, Any]:
        df = self.client.df()
        volumes_by_name = {
            volume.get("Name"): {
                "name": volume.get("Name"),
                "size_bytes": (volume.get("UsageData") or {}).get("Size", 0) or 0,
                "ref_count": (volume.get("UsageData") or {}).get("RefCount", 0) or 0,
                "mount_point": volume.get("Mountpoint"),
            }
            for volume in df.get("Volumes", []) or []
        }

        containers = self.client.containers.list(all=True)
        containers_by_short_id: dict[str, dict[str, Any]] = {}
        for container in containers:
            try:
                container.reload()
                mounts = container.attrs.get("Mounts", []) or []
            except Exception as exc:
                LOGGER.warning("Unable to inspect mounts for %s: %s", container.name, exc)
                mounts = []

            volume_mounts = []
            for mount in mounts:
                if mount.get("Type") != "volume":
                    continue
                name = mount.get("Name")
                volume_data = volumes_by_name.get(name, {})
                volume_mounts.append(
                    {
                        "name": name,
                        "mount_point": mount.get("Destination"),
                        "size_bytes": volume_data.get("size_bytes", 0),
                    }
                )
            containers_by_short_id[container.short_id] = {
                "volumes": volume_mounts,
            }

        container_payload: dict[str, dict[str, Any]] = {}
        for container in df.get("Containers", []) or []:
            container_id = (container.get("Id") or "")[:12]
            base = containers_by_short_id.get(container_id, {"volumes": []})
            container_payload[container_id] = {
                "container_id": container_id,
                "size_rw_bytes": container.get("SizeRw", 0) or 0,
                "size_rootfs_bytes": container.get("SizeRootFs", 0) or 0,
                "volumes": base["volumes"],
            }

        total_images = sum((image.get("Size", 0) or 0) for image in df.get("Images", []) or [])
        total_containers = sum((container.get("SizeRw", 0) or 0) for container in df.get("Containers", []) or [])
        total_volumes = sum(
            ((volume.get("UsageData") or {}).get("Size", 0) or 0) for volume in df.get("Volumes", []) or []
        )
        total_build_cache = sum((item.get("Size", 0) or 0) for item in df.get("BuildCache", []) or [])

        return {
            "summary": {
                "images_bytes": total_images,
                "containers_bytes": total_containers,
                "volumes_bytes": total_volumes,
                "build_cache_bytes": total_build_cache,
                "total_bytes": total_images + total_containers + total_volumes + total_build_cache,
            },
            "containers": container_payload,
        }
