import pytest
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

from zendesk_skill import render_entrypoint
from zendesk_skill.render_entrypoint import (
    RemoteAuthASGIMiddleware,
    configure_transport_security,
    public_netloc_from_base_url,
)


def test_public_netloc_accepts_render_origin():
    assert public_netloc_from_base_url("https://zendesk-talk-mcp.onrender.com") == "zendesk-talk-mcp.onrender.com"
    assert public_netloc_from_base_url("https://zendesk-talk-mcp.onrender.com/") == "zendesk-talk-mcp.onrender.com"


def test_public_netloc_allows_explicit_port():
    assert public_netloc_from_base_url("https://mcp.example.com:8443") == "mcp.example.com:8443"


@pytest.mark.parametrize(
    "value",
    [
        "zendesk-talk-mcp.onrender.com",
        "ftp://zendesk-talk-mcp.onrender.com",
        "https://user:secret@zendesk-talk-mcp.onrender.com",
        "https://zendesk-talk-mcp.onrender.com/mcp",
        "https://zendesk-talk-mcp.onrender.com?debug=1",
        "https://zendesk-talk-mcp.onrender.com#fragment",
    ],
)
def test_public_netloc_rejects_unsafe_values(value):
    with pytest.raises(ValueError):
        public_netloc_from_base_url(value)


def test_public_netloc_allows_missing_optional_value():
    assert public_netloc_from_base_url(None) is None
    assert public_netloc_from_base_url("") is None


def _scope(path="/mcp", authorization=None):
    headers = [(b"host", b"zendesk-talk-mcp.onrender.com")]
    if authorization:
        headers.append((b"authorization", authorization.encode()))
    return {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "https",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "headers": headers,
        "client": ("127.0.0.1", 12345),
        "server": ("zendesk-talk-mcp.onrender.com", 443),
    }


async def _invoke(app, scope):
    sent = []
    received = False

    async def receive():
        nonlocal received
        if not received:
            received = True
            return {"type": "http.request", "body": b"", "more_body": False}
        return {"type": "http.disconnect"}

    async def send(message):
        sent.append(message)

    await app(scope, receive, send)
    return sent


def test_auth_middleware_is_raw_asgi_not_base_http_middleware():
    assert not issubclass(RemoteAuthASGIMiddleware, BaseHTTPMiddleware)


@pytest.mark.asyncio
async def test_authorized_request_reaches_downstream(monkeypatch):
    reached = []

    async def downstream(scope, receive, send):
        reached.append(scope["path"])
        await send({"type": "http.response.start", "status": 204, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    monkeypatch.setattr(render_entrypoint, "remote_auth_response", lambda request, authorization: None)
    messages = await _invoke(RemoteAuthASGIMiddleware(downstream), _scope(authorization="Bearer valid"))

    assert reached == ["/mcp"]
    assert messages[0]["status"] == 204


@pytest.mark.asyncio
async def test_unauthorized_request_returns_401(monkeypatch):
    async def downstream(scope, receive, send):
        raise AssertionError("unauthorized request must not reach FastMCP")

    monkeypatch.setattr(
        render_entrypoint,
        "remote_auth_response",
        lambda request, authorization: JSONResponse({"error": "Unauthorized"}, status_code=401),
    )
    messages = await _invoke(RemoteAuthASGIMiddleware(downstream), _scope())

    assert messages[0]["status"] == 401


@pytest.mark.asyncio
async def test_health_is_public():
    async def downstream(scope, receive, send):
        raise AssertionError("health must be handled before FastMCP")

    messages = await _invoke(RemoteAuthASGIMiddleware(downstream), _scope(path="/health"))
    assert messages[0]["status"] == 200


def _oauth_env(monkeypatch):
    monkeypatch.setenv("MCP_AUTH_MODE", "oauth")
    monkeypatch.setenv("MCP_PUBLIC_BASE_URL", "https://zendesk-talk-mcp.onrender.com")
    monkeypatch.setenv("MCP_OAUTH_ISSUER", "https://auth.example.com/")
    monkeypatch.setenv("MCP_OAUTH_AUDIENCE", "https://zendesk-talk-mcp.onrender.com/mcp")
    monkeypatch.setenv("MCP_OAUTH_JWKS_URL", "https://auth.example.com/.well-known/jwks.json")
    monkeypatch.setenv("MCP_OAUTH_AUTHORIZATION_ENDPOINT", "https://auth.example.com/authorize")
    monkeypatch.setenv("MCP_OAUTH_TOKEN_ENDPOINT", "https://auth.example.com/oauth/token")
    monkeypatch.setenv("MCP_OAUTH_REQUIRED_SCOPE", "zendesk:read")


@pytest.mark.asyncio
async def test_oauth_metadata_is_public(monkeypatch):
    _oauth_env(monkeypatch)

    async def downstream(scope, receive, send):
        raise AssertionError("metadata must be handled before FastMCP")

    messages = await _invoke(
        RemoteAuthASGIMiddleware(downstream),
        _scope(path="/.well-known/oauth-protected-resource/mcp"),
    )
    assert messages[0]["status"] == 200


def test_remote_mcp_uses_stateless_json_and_keeps_host_protection(monkeypatch):
    from zendesk_skill import server

    _oauth_env(monkeypatch)
    configure_transport_security()

    settings = server.remote_mcp.settings
    assert settings.stateless_http is True
    assert settings.json_response is True

    transport_security = getattr(settings, "transport_security", None)
    if transport_security is not None:
        assert transport_security.enable_dns_rebinding_protection is True
        assert "zendesk-talk-mcp.onrender.com" in transport_security.allowed_hosts
        assert "unrelated.example.com" not in transport_security.allowed_hosts
        assert "https://claude.ai" in transport_security.allowed_origins
