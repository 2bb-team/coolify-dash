from __future__ import annotations

import asyncio
import logging
from typing import Any

from aiohttp import ClientSession, ClientTimeout

from src.config import Config
from src.store import MetricStore

LOGGER = logging.getLogger(__name__)


class CoolifyEnricher:
    def __init__(self, store: MetricStore, config: Config) -> None:
        self.store = store
        self.config = config
        self.interval = config.coolify_poll_interval
        self._cache: dict[str, dict[str, Any]] = {}

    async def run_forever(self) -> None:
        while True:
            try:
                await self.collect()
            except Exception as exc:
                LOGGER.exception("Coolify enrichment error: %s", exc)
            await asyncio.sleep(self.interval)

    async def collect(self) -> None:
        if not self.config.coolify_api_token:
            return

        timeout = ClientTimeout(total=20)
        headers = {
            "Authorization": f"Bearer {self.config.coolify_api_token}",
            "Accept": "application/json",
        }
        async with ClientSession(headers=headers, timeout=timeout) as session:
            resources = await self._build_resource_map(session)

        self._cache = resources
        await self.store.update_coolify(resources)

    async def _api_get(self, session: ClientSession, path: str) -> Any:
        url = f"{self.config.coolify_api_url}/api/v1{path}"
        async with session.get(url) as response:
            response.raise_for_status()
            return await response.json()

    async def _build_resource_map(self, session: ClientSession) -> dict[str, dict[str, Any]]:
        applications_task = asyncio.create_task(self._api_get(session, "/applications"))
        services_task = asyncio.create_task(self._api_get(session, "/services"))
        projects = await self._api_get(session, "/projects")

        environment_lookup: dict[int, dict[str, Any]] = {}
        env_tasks = [
            asyncio.create_task(self._fetch_project_environments(session, project))
            for project in projects
            if project.get("uuid")
        ]
        for task in env_tasks:
            try:
                environment_lookup.update(await task)
            except Exception as exc:
                LOGGER.warning("Project environment lookup failed: %s", exc)

        applications = await applications_task
        services = await services_task

        resources: dict[str, dict[str, Any]] = {}
        for application in applications:
            env_meta = environment_lookup.get(application.get("environment_id"), {})
            uuid = application.get("uuid")
            if not uuid:
                continue
            resources[uuid] = {
                "type": "application",
                "name": application.get("name") or uuid,
                "project": env_meta.get("project_name"),
                "environment": env_meta.get("environment_name"),
                "fqdn": application.get("fqdn"),
                "status": application.get("status"),
                "ports": application.get("ports_exposes") or application.get("ports_mappings"),
                "health_check": application.get("health_check_enabled", False),
                "health_check_path": application.get("health_check_path"),
                "description": application.get("description"),
            }

        for service in services:
            env_meta = environment_lookup.get(service.get("environment_id"), {})
            uuid = service.get("uuid")
            if not uuid:
                continue
            resources[uuid] = {
                "type": "service",
                "name": service.get("name") or uuid,
                "project": env_meta.get("project_name"),
                "environment": env_meta.get("environment_name"),
                "fqdn": service.get("fqdn"),
                "status": service.get("status"),
                "ports": service.get("ports_exposes") or service.get("ports_mappings"),
                "health_check": False,
                "service_type": service.get("service_type"),
            }

        return resources or self._cache

    async def _fetch_project_environments(self, session: ClientSession, project: dict[str, Any]) -> dict[int, dict[str, Any]]:
        project_uuid = project.get("uuid")
        if not project_uuid:
            return {}
        environments = await self._api_get(session, f"/projects/{project_uuid}/environments")
        lookup: dict[int, dict[str, Any]] = {}
        for environment in environments:
            env_id = environment.get("id")
            if env_id is None:
                continue
            lookup[int(env_id)] = {
                "project_name": project.get("name"),
                "environment_name": environment.get("name"),
                "project_uuid": project_uuid,
                "environment_uuid": environment.get("uuid"),
            }
        return lookup
