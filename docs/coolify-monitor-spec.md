# Coolify Monitor — Build Spec

## Overview

Build a lightweight, single-container monitoring dashboard that provides real-time visibility into:

1. **Host system metrics** — CPU, RAM, disk usage, load average, network I/O
2. **Per-container Docker metrics** — CPU%, memory usage/limit, disk footprint, state, uptime
3. **Coolify enrichment** — project name, environment, FQDN/domain, health status, exposed ports

The dashboard fills a gap in Coolify's UI where you can't see aggregate resource usage across all containers or host-level disk consumption from a single view.

---

## Architecture

```
┌─────────────────────────────────────────────────────┐
│                   Host Machine                       │
│                                                      │
│  ┌──────────────────┐   ┌────────────────────────┐  │
│  │ /var/run/docker.  │   │ /proc, /sys            │  │
│  │ sock (bind mount) │   │ (bind mount, read-only)│  │
│  └────────┬─────────┘   └───────────┬────────────┘  │
│           │                         │                │
│  ┌────────▼─────────────────────────▼────────────┐  │
│  │         coolify-monitor container              │  │
│  │                                                │  │
│  │  Python agent (aiohttp)                        │  │
│  │  ├─ DockerCollector  (docker SDK, every 5s)    │  │
│  │  ├─ DiskCollector    (docker SDK, every 60s)   │  │
│  │  ├─ HostCollector    (/proc parsing, every 5s) │  │
│  │  ├─ CoolifyEnricher  (HTTP API, every 60s)     │  │
│  │  ├─ REST API         GET /api/metrics          │  │
│  │  ├─ SSE stream       GET /api/stream           │  │
│  │  └─ Static files     GET / (dashboard)         │  │
│  │                                                │  │
│  │  Serves everything on :9100                    │  │
│  └────────────────────────────────────────────────┘  │
│           │                                          │
│  ┌────────▼──────────────────────┐                  │
│  │ Coolify API (:8000)           │                  │
│  │ GET /api/v1/applications      │                  │
│  │ GET /api/v1/services          │                  │
│  │ GET /api/v1/servers/{uuid}/   │                  │
│  │     resources                 │                  │
│  └───────────────────────────────┘                  │
└─────────────────────────────────────────────────────┘
```

**Single process, single container.** The Python agent collects metrics, enriches them with Coolify metadata, and serves both the API and the static HTML dashboard on port 9100.

---

## Tech Stack

| Component | Choice | Rationale |
|-----------|--------|-----------|
| Language | Python 3.12+ | Fast to develop, rich ecosystem |
| Async framework | `aiohttp` | Handles SSE, REST, and static files in one process. Lightweight. |
| Docker access | `docker` SDK (pip package `docker`) | Reliable wrapper over the socket. Handles stats streaming, system df, inspect. |
| Host metrics | Direct `/proc` parsing | Zero dependencies. Works with bind-mounted `/host/proc`. |
| Frontend | Single `index.html` with vanilla JS | No build step. Connects to SSE for live updates. |
| Container image | `python:3.12-slim` base | Small image (~150MB). |

**Do NOT use** Flask, FastAPI, uvicorn, or any other framework. `aiohttp` handles everything.

---

## Project Structure

```
coolify-monitor/
├── docker-compose.yml
├── Dockerfile
├── requirements.txt          # aiohttp, docker
├── src/
│   ├── __init__.py
│   ├── main.py               # Entry point: starts aiohttp app
│   ├── config.py             # Env var parsing, defaults
│   ├── collectors/
│   │   ├── __init__.py
│   │   ├── docker_stats.py   # Per-container CPU, memory, state
│   │   ├── docker_disk.py    # Per-container and total disk usage (system df)
│   │   └── host.py           # CPU, RAM, disk partitions, load, network
│   ├── enrichers/
│   │   ├── __init__.py
│   │   └── coolify.py        # Calls Coolify API, builds UUID→metadata map
│   ├── api/
│   │   ├── __init__.py
│   │   ├── routes.py         # REST + SSE endpoints
│   │   └── middleware.py     # CORS, optional basic auth
│   └── store.py              # In-memory metric store (latest snapshot)
├── frontend/
│   └── index.html            # Dashboard UI (single file, vanilla JS + CSS)
└── README.md
```

---

## Configuration (Environment Variables)

```env
# Required
COOLIFY_API_TOKEN=           # Bearer token from Coolify → Keys & Tokens → API tokens

# Optional with defaults
COOLIFY_API_URL=http://localhost:8000   # Coolify instance base URL (no trailing slash)
MONITOR_PORT=9100                       # Port the dashboard serves on
STATS_INTERVAL=5                        # Seconds between Docker stats + host metrics polls
DISK_INTERVAL=60                        # Seconds between disk usage polls (expensive)
COOLIFY_POLL_INTERVAL=60                # Seconds between Coolify API refresh
HOST_PROC=/host/proc                    # Mounted /proc path inside container
HOST_SYS=/host/sys                      # Mounted /sys path inside container
BASIC_AUTH_USER=                         # Optional: enable basic auth (leave empty to disable)
BASIC_AUTH_PASS=                         # Optional: password for basic auth
```

---

## Docker Compose

```yaml
services:
  coolify-monitor:
    build: .
    container_name: coolify-monitor
    ports:
      - "9100:9100"
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock:ro
      - /proc:/host/proc:ro
      - /sys:/host/sys:ro
    environment:
      - COOLIFY_API_URL=http://localhost:8000
      - COOLIFY_API_TOKEN=${COOLIFY_API_TOKEN}
    restart: unless-stopped
    # Keep the container lightweight
    mem_limit: 128m
    cpus: 0.25
```

> **Note on `COOLIFY_API_URL`**: If the monitor container runs on the same Docker host as Coolify, use `http://host.docker.internal:8000` or the host's LAN IP. `localhost` inside the container refers to the container itself. Alternatively, if Coolify uses a known Docker network, connect this container to it and use Coolify's container name.

---

## Dockerfile

```dockerfile
FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY src/ ./src/
COPY frontend/ ./frontend/

EXPOSE 9100

CMD ["python", "-m", "src.main"]
```

**requirements.txt:**
```
aiohttp>=3.9,<4
docker>=7.0,<8
```

---

## Collector Specifications

### 1. Host Collector (`collectors/host.py`)

Reads from the bind-mounted `/host/proc` and `/host/sys` to get the **host's** metrics, not the container's.

#### CPU Usage
- Read `/host/proc/stat` — parse the first `cpu` line
- Calculate delta between two reads: `usage% = 100 * (1 - idle_delta / total_delta)`
- Store per-core and aggregate

#### Memory
- Read `/host/proc/meminfo`
- Extract: `MemTotal`, `MemAvailable`, `MemFree`, `Buffers`, `Cached`, `SwapTotal`, `SwapFree`
- Calculate: `used = total - available`, `usage_percent = used / total * 100`

#### Disk Partitions
- Use `os.statvfs()` on common mount points: `/`, `/data`, `/mnt` (configurable)
- **Also** parse `/host/proc/mounts` to find all real (non-tmpfs, non-overlay) filesystems
- Report: `total_bytes`, `used_bytes`, `free_bytes`, `usage_percent` per mount

#### Load Average
- Read `/host/proc/loadavg`
- Return: `load_1m`, `load_5m`, `load_15m`

#### Network I/O
- Read `/host/proc/net/dev`
- Calculate delta bytes rx/tx per second for each interface (skip `lo`)

#### Output Schema
```json
{
  "timestamp": "2025-01-15T10:30:00Z",
  "cpu": {
    "usage_percent": 23.5,
    "core_count": 8
  },
  "memory": {
    "total_bytes": 34359738368,
    "used_bytes": 18253611008,
    "available_bytes": 16106127360,
    "usage_percent": 53.1,
    "swap_total_bytes": 8589934592,
    "swap_used_bytes": 0
  },
  "load": {
    "load_1m": 1.23,
    "load_5m": 0.98,
    "load_15m": 0.87
  },
  "disk": [
    {
      "mount": "/",
      "device": "/dev/sda1",
      "total_bytes": 500107862016,
      "used_bytes": 234881024000,
      "free_bytes": 265226838016,
      "usage_percent": 47.0
    }
  ],
  "network": {
    "eth0": {
      "rx_bytes_per_sec": 125000,
      "tx_bytes_per_sec": 50000
    }
  }
}
```

### 2. Docker Stats Collector (`collectors/docker_stats.py`)

Uses the `docker` Python SDK connected to `/var/run/docker.sock`.

#### Per-Container Stats (every `STATS_INTERVAL` seconds)
```python
import docker
client = docker.DockerClient(base_url='unix:///var/run/docker.sock')

for container in client.containers.list(all=True):
    # Get one-shot stats (stream=False)
    stats = container.stats(stream=False)

    # Container metadata
    info = {
        "id": container.short_id,
        "name": container.name,
        "image": container.image.tags[0] if container.image.tags else container.attrs['Config']['Image'],
        "status": container.status,  # running, exited, paused, etc.
        "state_started_at": container.attrs['State'].get('StartedAt'),
        "labels": container.labels,  # Important: Coolify sets labels here
    }

    # CPU calculation
    cpu_delta = stats['cpu_stats']['cpu_usage']['total_usage'] - stats['precpu_stats']['cpu_usage']['total_usage']
    system_delta = stats['cpu_stats']['system_cpu_usage'] - stats['precpu_stats']['system_cpu_usage']
    num_cpus = stats['cpu_stats'].get('online_cpus', 1)
    cpu_percent = (cpu_delta / system_delta) * num_cpus * 100.0 if system_delta > 0 else 0.0

    # Memory
    mem_usage = stats['memory_stats']['usage'] - stats['memory_stats'].get('stats', {}).get('cache', 0)
    mem_limit = stats['memory_stats']['limit']
    mem_percent = (mem_usage / mem_limit) * 100.0 if mem_limit > 0 else 0.0

    # Network I/O (aggregate all interfaces)
    net_rx = sum(v['rx_bytes'] for v in stats.get('networks', {}).values())
    net_tx = sum(v['tx_bytes'] for v in stats.get('networks', {}).values())
```

#### Important Notes
- `container.stats(stream=False)` blocks briefly per container. Run these concurrently with `asyncio.gather` using `loop.run_in_executor`.
- Skip the monitor container itself (filter by container name or ID).
- For stopped containers (`status != 'running'`), stats aren't available — just report the state and metadata.

#### Container Labels from Coolify
Coolify injects labels into containers. Key labels to look for:
- `coolify.managed=true` — indicates Coolify manages this container
- `coolify.applicationId` or similar — links back to the Coolify resource
- Container names follow the pattern: `{service-name}-{uuid}` (e.g., `postgres-vgsco4o`, `myapp-abc123def`)
- Coolify also sets env vars: `COOLIFY_APP_NAME`, `COOLIFY_PROJECT_NAME`, `COOLIFY_ENVIRONMENT_NAME`, `COOLIFY_CONTAINER_NAME`

The container name contains the Coolify UUID which is the key join field.

### 3. Docker Disk Collector (`collectors/docker_disk.py`)

This is separate because `docker system df` is expensive — run every `DISK_INTERVAL` (60s).

```python
# System-wide Docker disk usage
df = client.df()

# df['Containers'] — list of all containers with SizeRw (writable layer) and SizeRootFs
for c in df['Containers']:
    container_id = c['Id'][:12]
    size_rw = c.get('SizeRw', 0)        # Writable layer size (container's own data)
    size_root_fs = c.get('SizeRootFs', 0) # Total size including image layers

# df['Images'] — total image disk usage
# df['Volumes'] — per-volume disk usage
# df['BuildCache'] — build cache size

# Also calculate total Docker disk usage
total_images = sum(img.get('Size', 0) for img in df['Images'])
total_containers = sum(c.get('SizeRw', 0) for c in df['Containers'])
total_volumes = sum(v['UsageData']['Size'] for v in df['Volumes'] if v['UsageData']['Size'] > 0)
total_build_cache = sum(bc.get('Size', 0) for bc in df.get('BuildCache', []))
```

#### Per-Container Disk Output
```json
{
  "container_id": "abc123",
  "size_rw_bytes": 52428800,
  "size_rootfs_bytes": 524288000,
  "volumes": [
    {
      "name": "myapp-data-vgsco4o",
      "mount_point": "/data",
      "size_bytes": 1073741824
    }
  ]
}
```

#### Docker-Wide Disk Summary
```json
{
  "images_bytes": 5368709120,
  "containers_bytes": 209715200,
  "volumes_bytes": 10737418240,
  "build_cache_bytes": 2147483648,
  "total_bytes": 18463416320
}
```

---

## Coolify API Enricher (`enrichers/coolify.py`)

Calls the Coolify API to build a lookup map: `container_uuid → coolify_metadata`.

### Authentication
```
Authorization: Bearer <COOLIFY_API_TOKEN>
Accept: application/json
```
Base URL: `COOLIFY_API_URL` + `/api/v1`

### API Calls to Make

**1. List all applications:**
```
GET /api/v1/applications
```
Response is an array. Key fields per application:
```json
{
  "id": 1,
  "uuid": "abc123def456",
  "name": "my-web-app",
  "fqdn": "https://myapp.example.com",
  "status": "running",
  "description": "Main web application",
  "ports_exposes": "3000",
  "ports_mappings": "3000:3000",
  "health_check_enabled": true,
  "health_check_path": "/health",
  "build_pack": "dockerfile",
  "environment_id": 5,
  "server_id": 1
}
```

**2. List all services:**
```
GET /api/v1/services
```
Response is an array. Key fields:
```json
{
  "id": 1,
  "uuid": "xyz789",
  "name": "redis-cache",
  "status": "running",
  "fqdn": null,
  "service_type": "redis",
  "environment_id": 5,
  "server_id": 1
}
```

**3. List all projects (for project name mapping):**
```
GET /api/v1/projects
```
Response array. **Note:** The `environments` field may NOT be included due to a known Coolify API bug (#7702). Response fields:
```json
{
  "id": 1,
  "uuid": "proj-uuid-123",
  "name": "My Project",
  "description": ""
}
```

**4. List environments per project:**
```
GET /api/v1/projects/{project_uuid}/environments
```
This returns environments for a specific project. You'll need to iterate projects.

**5. (Alternative) Server resources endpoint:**
```
GET /api/v1/servers/{server_uuid}/resources
```
Returns ALL resources (applications, databases, services) on a given server in one call. This may be more efficient than calling separate list endpoints. The response includes a `type` field (`application`, `database`, `service`) for each resource.

### Joining Strategy

The critical challenge is mapping **Docker container names** → **Coolify resource UUIDs**.

Coolify names containers using the pattern: `{service}-{uuid}` or just the UUID for single-service apps. Examples:
- `myapp-vgsco4o`
- `postgres-abc123def`
- `redis-xyz789`

**Approach:**
1. Fetch all applications and services from the Coolify API
2. Build a map: `uuid → {name, fqdn, status, project_name, environment_name, ...}`
3. For each Docker container, extract the UUID suffix from the container name
4. Also check container labels — Coolify sets `coolify.managed=true` and other labels
5. Match by checking if the container name **ends with** a known Coolify UUID

```python
# Build lookup from Coolify API
coolify_resources = {}  # uuid -> metadata

for app in applications:
    coolify_resources[app['uuid']] = {
        'type': 'application',
        'name': app['name'],
        'fqdn': app.get('fqdn'),
        'status': app.get('status'),
        'ports': app.get('ports_exposes'),
        'health_check': app.get('health_check_enabled', False),
    }

for svc in services:
    coolify_resources[svc['uuid']] = {
        'type': 'service',
        'name': svc['name'],
        'fqdn': svc.get('fqdn'),
        'status': svc.get('status'),
        'service_type': svc.get('service_type'),
    }

# Match Docker containers to Coolify resources
def match_container_to_coolify(container_name: str, labels: dict) -> dict | None:
    """Try to find the Coolify resource for a Docker container."""
    # Method 1: Check if container name ends with a known UUID
    for uuid, meta in coolify_resources.items():
        if container_name.endswith(uuid) or container_name.endswith(f"-{uuid}"):
            return {**meta, 'uuid': uuid}

    # Method 2: Check coolify.managed label
    if labels.get('coolify.managed') == 'true':
        # Container is managed by Coolify but we couldn't match by name
        # Return partial info
        return {'type': 'unknown', 'name': container_name, 'managed_by_coolify': True}

    # Not a Coolify-managed container (e.g., the monitor itself)
    return None
```

### Project Name Resolution

Since the projects list API doesn't include environments, build the project mapping separately:

```python
projects = {}  # project_id -> project_name

for project in api_get('/projects'):
    projects[project['id']] = project['name']

# Applications have environment_id, and environments have project_id
# You may need to iterate: for each project UUID, fetch its environments
# Then map environment_id -> (project_name, environment_name)
```

**If the project/environment mapping proves too complex or API-limited, fall back to:**
- Just showing the application/service `name` from the Coolify API
- The `fqdn` (domain) is usually the most useful identifier anyway

### Error Handling
- The Coolify API may be temporarily unavailable. Cache the last successful response and keep serving it.
- Log API errors but don't crash the collector loop.
- If `COOLIFY_API_TOKEN` is not set, skip enrichment entirely and just show raw Docker data.

---

## In-Memory Store (`store.py`)

A simple class that holds the latest snapshot and provides it to the API:

```python
import asyncio
from datetime import datetime, timezone

class MetricStore:
    def __init__(self):
        self._lock = asyncio.Lock()
        self.host: dict = {}
        self.containers: dict[str, dict] = {}   # container_id -> merged metrics
        self.docker_disk: dict = {}              # system-wide docker disk
        self.coolify_map: dict = {}              # uuid -> coolify metadata
        self.last_updated: str = ""

    async def update_host(self, data: dict):
        async with self._lock:
            self.host = data
            self.last_updated = datetime.now(timezone.utc).isoformat()

    async def update_containers(self, data: dict[str, dict]):
        async with self._lock:
            self.containers = data
            self.last_updated = datetime.now(timezone.utc).isoformat()

    async def update_docker_disk(self, data: dict):
        async with self._lock:
            self.docker_disk = data

    async def update_coolify(self, data: dict):
        async with self._lock:
            self.coolify_map = data

    async def get_snapshot(self) -> dict:
        async with self._lock:
            # Merge coolify metadata into container data
            enriched = {}
            for cid, cdata in self.containers.items():
                enriched[cid] = {**cdata}
                # Try to match to Coolify resource
                # ... matching logic here
            return {
                "host": self.host,
                "containers": enriched,
                "docker_disk": self.docker_disk,
                "last_updated": self.last_updated,
            }
```

---

## API Endpoints (`api/routes.py`)

### `GET /api/metrics`

Returns the full current snapshot as JSON.

```json
{
  "host": { ... },
  "containers": {
    "abc123": {
      "name": "myapp-vgsco4o",
      "image": "myapp:latest",
      "status": "running",
      "started_at": "2025-01-15T08:00:00Z",
      "cpu_percent": 2.3,
      "memory_usage_bytes": 134217728,
      "memory_limit_bytes": 536870912,
      "memory_percent": 25.0,
      "net_rx_bytes": 1048576,
      "net_tx_bytes": 524288,
      "disk_rw_bytes": 52428800,
      "disk_rootfs_bytes": 524288000,
      "coolify": {
        "uuid": "vgsco4o",
        "type": "application",
        "name": "My Web App",
        "project": "Main Project",
        "environment": "production",
        "fqdn": "https://myapp.example.com",
        "ports": "3000",
        "health_check": true
      }
    }
  },
  "docker_disk": {
    "images_bytes": 5368709120,
    "containers_bytes": 209715200,
    "volumes_bytes": 10737418240,
    "build_cache_bytes": 2147483648,
    "total_bytes": 18463416320
  },
  "last_updated": "2025-01-15T10:30:05Z"
}
```

### `GET /api/stream`

Server-Sent Events stream. Pushes the full snapshot every `STATS_INTERVAL` seconds.

```python
async def sse_handler(request):
    response = web.StreamResponse()
    response.content_type = 'text/event-stream'
    response.headers['Cache-Control'] = 'no-cache'
    response.headers['Connection'] = 'keep-alive'
    response.headers['X-Accel-Buffering'] = 'no'  # Disable nginx buffering
    await response.prepare(request)

    store = request.app['store']
    while True:
        snapshot = await store.get_snapshot()
        data = json.dumps(snapshot)
        await response.write(f"data: {data}\n\n".encode())
        await asyncio.sleep(request.app['config'].stats_interval)

    return response
```

### `GET /` (and all static files)

Serve `frontend/index.html` and any static assets from the `frontend/` directory.

---

## Frontend Dashboard (`frontend/index.html`)

Single HTML file. No build tools. Uses vanilla JS + CSS. Connects to the SSE stream.

### Layout

```
┌─────────────────────────────────────────────────────────┐
│  Coolify Monitor                         Last: 10:30:05 │
├─────────────┬──────────────┬──────────────┬─────────────┤
│   CPU       │   Memory     │   Disk (/)   │   Load      │
│   23.5%     │   53.1%      │   47.0%      │   1.23      │
│   [██████░] │   [████████░]│   [███████░] │   5m: 0.98  │
│   8 cores   │   17.0/32 GB │   234/500 GB │   15m: 0.87 │
├─────────────┴──────────────┴──────────────┴─────────────┤
│  Docker Disk: 18.5 GB total                             │
│  Images: 5.4 GB │ Volumes: 10.7 GB │ Containers: 0.2GB │
├─────────────────────────────────────────────────────────┤
│  Containers (sorted by CPU ▼)              Filter: [__] │
│                                                         │
│  Name/Project       Status  CPU%  Memory     Disk  FQDN│
│  ─────────────────────────────────────────────────────  │
│  myapp              🟢 run  2.3%  128/512MB  50MB  myap│
│  └ Main Project / production                    p.ex.co│
│                                                         │
│  postgres           🟢 run  1.1%  256/1024MB 2.1GB  —  │
│  └ Main Project / production                            │
│                                                         │
│  redis              🟢 run  0.2%  64/256MB   12MB   —  │
│  └ Cache Service / production                           │
│                                                         │
│  coolify            🟢 run  3.5%  512/2048MB 1.2GB  —  │
│  └ (Coolify system)                                     │
│                                                         │
│  coolify-monitor    🟢 run  0.1%  24/128MB   5MB    —  │
│  └ (Not managed by Coolify)                             │
└─────────────────────────────────────────────────────────┘
```

### Frontend Requirements

1. **SSE Connection**: Connect to `/api/stream` on page load. Reconnect automatically on disconnect (use `EventSource` with retry).

2. **Host Gauges**: Top section shows CPU, Memory, Disk, Load as visual gauges (CSS-only progress bars or simple SVG arcs). Update in-place on each SSE event.

3. **Docker Disk Summary**: Show total Docker disk usage broken down by images, volumes, containers, build cache.

4. **Container Table**: Sortable by any column (click header to sort). Default sort: CPU% descending. Include a text filter input that filters by container name, project name, or FQDN.

5. **Container Rows**: Each row shows:
   - Container name (from Docker) and Coolify project/environment underneath if available
   - Status indicator (colored dot: green=running, red=exited, yellow=paused, gray=created)
   - CPU% with mini inline bar
   - Memory: used/limit with percentage
   - Disk: writable layer size (from docker df)
   - FQDN as a clickable link (if available from Coolify)
   - Coolify health status badge if health checks are enabled

6. **Responsive**: Should work on mobile (table scrolls horizontally on small screens).

7. **Dark/Light Mode**: Respect `prefers-color-scheme`. Use CSS custom properties for theming.

8. **Formatting Helpers**: Human-readable byte formatting (KB/MB/GB), percentage with 1 decimal, relative time for uptime ("2d 5h" etc.).

### Frontend Implementation Notes

```javascript
// SSE connection with auto-reconnect
const evtSource = new EventSource('/api/stream');
evtSource.onmessage = (event) => {
    const data = JSON.parse(event.data);
    updateDashboard(data);
};
evtSource.onerror = () => {
    // EventSource auto-reconnects by default
    console.warn('SSE connection lost, reconnecting...');
};
```

---

## Main Entry Point (`main.py`)

```python
import asyncio
from aiohttp import web
from src.config import Config
from src.store import MetricStore
from src.collectors.host import HostCollector
from src.collectors.docker_stats import DockerStatsCollector
from src.collectors.docker_disk import DockerDiskCollector
from src.enrichers.coolify import CoolifyEnricher
from src.api.routes import setup_routes

async def start_background_tasks(app):
    """Start all collector loops as background tasks."""
    store = app['store']
    config = app['config']

    app['host_task'] = asyncio.create_task(
        HostCollector(store, config).run_forever()
    )
    app['docker_task'] = asyncio.create_task(
        DockerStatsCollector(store, config).run_forever()
    )
    app['disk_task'] = asyncio.create_task(
        DockerDiskCollector(store, config).run_forever()
    )
    if config.coolify_api_token:
        app['coolify_task'] = asyncio.create_task(
            CoolifyEnricher(store, config).run_forever()
        )

async def cleanup_background_tasks(app):
    """Cancel background tasks on shutdown."""
    for key in ['host_task', 'docker_task', 'disk_task', 'coolify_task']:
        task = app.get(key)
        if task:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

def create_app():
    config = Config.from_env()
    store = MetricStore()

    app = web.Application()
    app['config'] = config
    app['store'] = store

    setup_routes(app)

    app.on_startup.append(start_background_tasks)
    app.on_cleanup.append(cleanup_background_tasks)

    return app

if __name__ == '__main__':
    app = create_app()
    web.run_app(app, port=app['config'].monitor_port)
```

---

## Important Implementation Details

### Concurrency
- Docker `container.stats(stream=False)` is a blocking call. Wrap it in `loop.run_in_executor(None, ...)` to avoid blocking the event loop.
- Run all container stats calls concurrently using `asyncio.gather`.
- The Coolify API enricher should use `aiohttp.ClientSession` for async HTTP.

### CPU Calculation Gotcha
The Docker stats CPU calculation requires both the current and previous read. The formula:
```python
cpu_percent = (cpu_delta / system_delta) * num_cpus * 100.0
```
Where `cpu_delta` and `system_delta` are differences between the current and previous (`precpu_stats`) values. The `precpu_stats` field in the Docker API response already provides the previous values, so you get both in one call.

### Memory Calculation
Docker reports `usage` which includes cache. Subtract the cache:
```python
# For cgroup v1:
actual_usage = stats['memory_stats']['usage'] - stats['memory_stats']['stats'].get('cache', 0)

# For cgroup v2 (newer kernels):
actual_usage = stats['memory_stats']['usage'] - stats['memory_stats']['stats'].get('inactive_file', 0)
```
Handle both cases. Try `cache` first, fall back to `inactive_file`.

### Network Metrics
Docker stats give cumulative bytes. To get per-second rates, store the previous reading and calculate the delta:
```python
rx_per_sec = (current_rx - previous_rx) / interval_seconds
```

### Self-Exclusion
The monitor should exclude itself from the container list, or at minimum mark itself clearly. Check `container.name == 'coolify-monitor'` or compare against the container's own hostname (`socket.gethostname()`).

### Graceful Error Handling
Each collector loop should catch exceptions individually and continue:
```python
async def run_forever(self):
    while True:
        try:
            await self.collect()
        except Exception as e:
            logger.error(f"Collection error: {e}")
        await asyncio.sleep(self.interval)
```

---

## Testing Checklist

Before considering the project done, verify:

- [ ] `docker compose up -d` starts cleanly with only `COOLIFY_API_TOKEN` set
- [ ] Dashboard loads at `http://<host>:9100`
- [ ] Host CPU, memory, load, and disk gauges update in real-time
- [ ] Container table populates with all running containers
- [ ] CPU and memory percentages match `docker stats` output (approximately)
- [ ] Docker disk usage (images, volumes, containers) displays correctly
- [ ] Coolify metadata (project name, FQDN, status) appears for managed containers
- [ ] Table sorting works (click column headers)
- [ ] Text filter filters containers by name/project/domain
- [ ] SSE reconnects after a brief network interruption
- [ ] Dashboard works on mobile (responsive)
- [ ] Dark mode works
- [ ] Monitor uses <128MB RAM and <0.25 CPU under normal operation
- [ ] Works when Coolify API is unreachable (graceful degradation — shows Docker data only)
- [ ] Works when COOLIFY_API_TOKEN is not set (skips enrichment)
