from __future__ import annotations

import base64

from aiohttp import web


@web.middleware
async def cors_middleware(request: web.Request, handler):
    if request.method == "OPTIONS":
        response = web.Response(status=204)
    else:
        response = await handler(request)
    response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Access-Control-Allow-Headers"] = "Authorization, Content-Type"
    response.headers["Access-Control-Allow-Methods"] = "GET, OPTIONS"
    return response


@web.middleware
async def basic_auth_middleware(request: web.Request, handler):
    config = request.app["config"]
    if request.path == "/api/health":
        return await handler(request)
    if not config.basic_auth_enabled:
        return await handler(request)

    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Basic "):
        return _unauthorized()

    try:
        encoded = auth_header.split(" ", 1)[1].strip()
        decoded = base64.b64decode(encoded).decode("utf-8")
        username, password = decoded.split(":", 1)
    except Exception:
        return _unauthorized()

    if username != config.basic_auth_user or password != config.basic_auth_pass:
        return _unauthorized()

    return await handler(request)


def _unauthorized() -> web.Response:
    response = web.Response(status=401, text="Authentication required")
    response.headers["WWW-Authenticate"] = 'Basic realm="Coolify Monitor"'
    return response
