from __future__ import annotations

import asyncio
import logging
import os
import time
from datetime import datetime, timezone

from src.config import Config
from src.store import MetricStore

LOGGER = logging.getLogger(__name__)

EXCLUDED_FS_TYPES = {
    "autofs",
    "binfmt_misc",
    "bpf",
    "cgroup",
    "cgroup2",
    "configfs",
    "debugfs",
    "devpts",
    "devtmpfs",
    "fusectl",
    "hugetlbfs",
    "mqueue",
    "nsfs",
    "overlay",
    "proc",
    "pstore",
    "ramfs",
    "securityfs",
    "selinuxfs",
    "squashfs",
    "sysfs",
    "tmpfs",
    "tracefs",
}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _decode_mount_field(value: str) -> str:
    return value.replace("\\040", " ").replace("\\011", "\t").replace("\\012", "\n").replace("\\134", "\\")


class HostCollector:
    def __init__(self, store: MetricStore, config: Config) -> None:
        self.store = store
        self.config = config
        self.interval = config.stats_interval
        self._prev_cpu_total: tuple[int, int] | None = None
        self._prev_per_core: dict[str, tuple[int, int]] = {}
        self._prev_net: dict[str, tuple[int, int, float]] = {}

    async def run_forever(self) -> None:
        while True:
            try:
                await self.collect()
            except Exception as exc:
                LOGGER.exception("Host collection error: %s", exc)
            await asyncio.sleep(self.interval)

    async def collect(self) -> None:
        host_snapshot = {
            "timestamp": _now_iso(),
            "cpu": self._read_cpu(),
            "memory": self._read_memory(),
            "load": self._read_load(),
            "disk": self._read_disk(),
            "network": self._read_network(),
        }
        await self.store.update_host(host_snapshot)

    def _read_lines(self, path: str) -> list[str]:
        with open(path, "r", encoding="utf-8") as handle:
            return handle.readlines()

    def _read_cpu(self) -> dict:
        lines = self._read_lines(os.path.join(self.config.host_proc, "stat"))
        aggregate = {}
        per_core = []

        for raw_line in lines:
            if not raw_line.startswith("cpu"):
                break
            parts = raw_line.split()
            name = parts[0]
            values = [int(part) for part in parts[1:]]
            idle = values[3] + (values[4] if len(values) > 4 else 0)
            total = sum(values)

            if name == "cpu":
                previous = self._prev_cpu_total
                self._prev_cpu_total = (total, idle)
                usage_percent = self._compute_cpu_percent(previous, total, idle)
                aggregate = {
                    "usage_percent": usage_percent,
                    "core_count": max(sum(1 for line in lines if line.startswith("cpu") and line[3:4].isdigit()), 1),
                }
            else:
                previous = self._prev_per_core.get(name)
                self._prev_per_core[name] = (total, idle)
                per_core.append(
                    {
                        "name": name,
                        "usage_percent": self._compute_cpu_percent(previous, total, idle),
                    }
                )

        aggregate["per_core"] = per_core
        return aggregate

    @staticmethod
    def _compute_cpu_percent(previous: tuple[int, int] | None, total: int, idle: int) -> float:
        if not previous:
            return 0.0
        prev_total, prev_idle = previous
        total_delta = total - prev_total
        idle_delta = idle - prev_idle
        if total_delta <= 0:
            return 0.0
        return max(0.0, min(100.0, 100.0 * (1 - (idle_delta / total_delta))))

    def _read_memory(self) -> dict:
        meminfo: dict[str, int] = {}
        for line in self._read_lines(os.path.join(self.config.host_proc, "meminfo")):
            key, value = line.split(":", 1)
            meminfo[key] = int(value.strip().split()[0]) * 1024

        total = meminfo.get("MemTotal", 0)
        available = meminfo.get("MemAvailable", meminfo.get("MemFree", 0))
        used = max(total - available, 0)
        swap_total = meminfo.get("SwapTotal", 0)
        swap_free = meminfo.get("SwapFree", 0)
        return {
            "total_bytes": total,
            "used_bytes": used,
            "available_bytes": available,
            "free_bytes": meminfo.get("MemFree", 0),
            "buffers_bytes": meminfo.get("Buffers", 0),
            "cached_bytes": meminfo.get("Cached", 0),
            "usage_percent": (used / total * 100.0) if total else 0.0,
            "swap_total_bytes": swap_total,
            "swap_used_bytes": max(swap_total - swap_free, 0),
        }

    def _read_load(self) -> dict:
        fields = self._read_lines(os.path.join(self.config.host_proc, "loadavg"))[0].split()
        return {
            "load_1m": float(fields[0]),
            "load_5m": float(fields[1]),
            "load_15m": float(fields[2]),
        }

    def _read_disk(self) -> list[dict]:
        mounts_path = os.path.join(self.config.host_proc, "mounts")
        candidates: dict[str, tuple[str, str]] = {}

        for raw_line in self._read_lines(mounts_path):
            parts = raw_line.split()
            if len(parts) < 3:
                continue
            device, mount, fs_type = parts[:3]
            mount = _decode_mount_field(mount)
            if fs_type in EXCLUDED_FS_TYPES:
                continue
            if mount not in candidates:
                candidates[mount] = (_decode_mount_field(device), fs_type)

        for root in self.config.disk_mount_roots:
            candidates.setdefault(root, ("unknown", "unknown"))

        disks = []
        for mount in sorted(candidates):
            device, fs_type = candidates[mount]
            host_mount = os.path.join(self.config.host_root, mount.lstrip("/")) if mount != "/" else self.config.host_root
            if not os.path.exists(host_mount):
                continue
            try:
                stats = os.statvfs(host_mount)
            except OSError:
                continue

            total = stats.f_frsize * stats.f_blocks
            free = stats.f_frsize * stats.f_bavail
            used = max(total - free, 0)
            usage_percent = (used / total * 100.0) if total else 0.0
            disks.append(
                {
                    "mount": mount,
                    "device": device,
                    "fs_type": fs_type,
                    "total_bytes": total,
                    "used_bytes": used,
                    "free_bytes": free,
                    "usage_percent": usage_percent,
                }
            )

        return disks

    def _read_network(self) -> dict:
        lines = self._read_lines(os.path.join(self.config.host_proc, "net/dev"))[2:]
        now = time.monotonic()
        network = {}

        for line in lines:
            iface, payload = line.split(":", 1)
            interface = iface.strip()
            if interface == "lo":
                continue

            fields = payload.split()
            rx_bytes = int(fields[0])
            tx_bytes = int(fields[8])
            previous = self._prev_net.get(interface)

            rx_rate = 0.0
            tx_rate = 0.0
            if previous:
                prev_rx, prev_tx, prev_time = previous
                elapsed = now - prev_time
                if elapsed > 0:
                    rx_rate = max(0.0, (rx_bytes - prev_rx) / elapsed)
                    tx_rate = max(0.0, (tx_bytes - prev_tx) / elapsed)

            self._prev_net[interface] = (rx_bytes, tx_bytes, now)
            network[interface] = {
                "rx_bytes": rx_bytes,
                "tx_bytes": tx_bytes,
                "rx_bytes_per_sec": rx_rate,
                "tx_bytes_per_sec": tx_rate,
            }

        return network
