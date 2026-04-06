from __future__ import annotations

import os
from dataclasses import dataclass, field


def _parse_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _parse_mounts(value: str | None) -> list[str]:
    if not value:
        return ["/", "/data", "/mnt"]
    mounts = [part.strip() for part in value.split(",") if part.strip()]
    return mounts or ["/", "/data", "/mnt"]


@dataclass(slots=True)
class Config:
    coolify_api_token: str = ""
    coolify_api_url: str = "http://localhost:8000"
    monitor_port: int = 9100
    stats_interval: int = 5
    disk_interval: int = 60
    coolify_poll_interval: int = 60
    host_proc: str = "/host/proc"
    host_sys: str = "/host/sys"
    host_root: str = "/host/root"
    disk_mount_roots: list[str] = field(default_factory=lambda: ["/", "/data", "/mnt"])
    basic_auth_user: str = ""
    basic_auth_pass: str = ""

    @property
    def basic_auth_enabled(self) -> bool:
        return bool(self.basic_auth_user and self.basic_auth_pass)

    @classmethod
    def from_env(cls) -> "Config":
        return cls(
            coolify_api_token=os.getenv("COOLIFY_API_TOKEN", "").strip(),
            coolify_api_url=os.getenv("COOLIFY_API_URL", "http://localhost:8000").rstrip("/"),
            monitor_port=_parse_int("MONITOR_PORT", 9100),
            stats_interval=max(_parse_int("STATS_INTERVAL", 5), 1),
            disk_interval=max(_parse_int("DISK_INTERVAL", 60), 5),
            coolify_poll_interval=max(_parse_int("COOLIFY_POLL_INTERVAL", 60), 5),
            host_proc=os.getenv("HOST_PROC", "/host/proc"),
            host_sys=os.getenv("HOST_SYS", "/host/sys"),
            host_root=os.getenv("HOST_ROOT", "/host/root"),
            disk_mount_roots=_parse_mounts(os.getenv("HOST_DISK_MOUNTS")),
            basic_auth_user=os.getenv("BASIC_AUTH_USER", ""),
            basic_auth_pass=os.getenv("BASIC_AUTH_PASS", ""),
        )
