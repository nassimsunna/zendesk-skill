"""Render entrypoint for the OAuth-protected remote FastMCP server."""

from __future__ import annotations

import os
from typing import Any, Awaitable, Callable
from urllib.parse import urlsplit

from starlette.requests import Request
from starlette.routing import Route

from zendesk_skill.remote_auth import remote_auth_response

ASGIApp = Callable[
    [dict[str, Any], Callable[[], Awaitable[dict[str, Any]]], Callable[[dict[str, Any]], Awaitable[None]]],
    Awaitable[None],
]

LOCAL_ALLOWED_HOSTS = ["127.0.0.1:*", "localhost:*", "[::1]:*"]
LOCAL_ALLOWED_ORIGINS = [
    "http://127.0.0.1:*",
    "http://localhost:*",
    "http://[::1]:*",
]
CLAUDE_ALLOWED_ORIGINS = ["https://claude.ai", "https://claude.com"]


def public_netloc_from_base_url(value: str | None) -> str | None:
    """Return a safe public host[:port] from MCP_PUBLIC_BASE_URL.

    The value must be an HTTP(S) origin without credentials, a path, query, or
    fragment. Returning only the parsed netloc prevents untrusted URL content
    from being copied into the Host allowlist.
    """
    if not value:
        return None

    parsed = urlsplit(value.strip())
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("MCP_PUBLIC_BASE_URL must be an absolute HTTP(S) URL")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("MCP_PUBLIC_BASE_URL must not contain credentials")
    if parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
        raise ValueError("MCP_PUBLIC_BASE_URL must be a base origin without path, query, or fragment")

    return parsed.netloc


def configure_transport_security() -> None:
    """Configure production-safe transport settings before creating the HTTP app."""
    from mcp.server.transport_security import TransportSecuritySettings
    from zendesk_skill import server

    allowed_hosts = list(LOCAL_ALLOWED_HOSTS)
    public_netloc = public_netloc_from_base_url(os.environ.get("MCP_PUBLIC_BASE_URL"))
    if public_netloc:
        allowed_hosts.append(public_netloc)

    server.remote_mcp.settings.transport_security = TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=allowed_hosts,
        allowed_origins=[*LOCAL_ALLOWED_ORIGINS, *CLAUDE_ALLOWED_ORIGINS],
    )
    # Render instances can restart or sleep, so remote requests must not depend
    # on an in-memory MCP session surviving between calls.
    server.remote_mcp.settings.stateless_http = True
    server.remote_mcp.settings.json_response = True


class RemoteAuthASGIMiddleware:
    """Pure ASGI OAuth middleware that preserves FastMCP streaming semantics."""

    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        request = Request(scope, receive=receive)
        auth_response = remote_auth_response(
            request,
            request.headers.get("authorization"),
        )
        if auth_response is not None:
            await auth_response(scope, receive, send)
            return

        await self.app(scope, receive, send)


def create_app() -> ASGIApp:
    """Build the stateless, JSON-response FastMCP application for Render."""
    from zendesk_skill import server

    configure_transport_security()
    server._ensure_streamable_http_compatible(server.remote_mcp)
    app = server.remote_mcp.streamable_http_app()
    app.routes.append(Route("/.well-known/oauth-protected-resource", server._oauth_protected_resource_route, methods=["GET"]))
    app.routes.append(Route("/.well-known/oauth-protected-resource/mcp", server._oauth_protected_resource_route, methods=["GET"]))
    app.routes.append(Route("/.well-known/oauth-authorization-server", server._oauth_authorization_server_route, methods=["GET"]))
    return RemoteAuthASGIMiddleware(app)


def main() -> None:
    """Start the remote MCP server with raw ASGI authentication middleware."""
    import uvicorn

    uvicorn.run(create_app(), host="0.0.0.0", port=int(os.environ.get("PORT", "8000")))


if __name__ == "__main__":
    main()
