# Coolify Monitor

Single-container monitoring dashboard for host metrics, Docker metrics, and Coolify metadata enrichment.

## Quick start

1. Set `COOLIFY_API_TOKEN` in your shell or `.env`.
2. Start the service:

```bash
docker compose up -d --build
```

3. Open `http://localhost:9100`.

## Notes

- Host disk metrics require the host root bind mount declared in `docker-compose.yml`.
- If `COOLIFY_API_TOKEN` is not set or the Coolify API is unavailable, the dashboard still serves raw Docker and host metrics.
