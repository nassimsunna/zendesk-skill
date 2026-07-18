"""Render entrypoint with explicit FastMCP transport-security allowlists."""

from __future__ import annotations

import os
from urllib.parse import urlsplit

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
    """Apply a strict public-host allowlist before the HTTP app is created."""
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


def main() -> None:
    """Configure transport security and start the existing MCP server."""
    configure_transport_security()
    from zendesk_skill.server import main as server_main

    server_main()


if __name__ == "__main__":
    main()
