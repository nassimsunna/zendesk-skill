from types import SimpleNamespace

import pytest


def request(path="/mcp", base_url="https://mcp.example.com/"):
    return SimpleNamespace(url=SimpleNamespace(path=path), base_url=base_url)


def oauth_env(monkeypatch):
    monkeypatch.setenv("MCP_AUTH_MODE", "oauth")
    monkeypatch.setenv("MCP_PUBLIC_BASE_URL", "https://mcp.example.com")
    monkeypatch.setenv("MCP_OAUTH_ISSUER", "https://auth.example.com")
    monkeypatch.setenv("MCP_OAUTH_AUDIENCE", "https://mcp.example.com/mcp")
    monkeypatch.setenv("MCP_OAUTH_JWKS_URL", "https://auth.example.com/.well-known/jwks.json")
    monkeypatch.setenv("MCP_OAUTH_AUTHORIZATION_ENDPOINT", "https://auth.example.com/oauth/authorize")
    monkeypatch.setenv("MCP_OAUTH_TOKEN_ENDPOINT", "https://auth.example.com/oauth/token")
    monkeypatch.setenv("MCP_OAUTH_REQUIRED_SCOPE", "zendesk:read")
    monkeypatch.setenv("MCP_ALLOWED_EMAIL", "nassim@samedaycustom.example")


def test_oauth_protected_resource_metadata(monkeypatch):
    from zendesk_skill.remote_auth import protected_resource_metadata

    oauth_env(monkeypatch)
    metadata = protected_resource_metadata("https://mcp.example.com")

    assert metadata["resource"] == "https://mcp.example.com/mcp"
    assert metadata["authorization_servers"] == ["https://auth.example.com"]
    assert metadata["scopes_supported"] == ["zendesk:read"]


def test_oauth_authorization_server_metadata_advertises_pkce(monkeypatch):
    from zendesk_skill.remote_auth import authorization_server_metadata

    oauth_env(monkeypatch)
    metadata = authorization_server_metadata()

    assert metadata["issuer"] == "https://auth.example.com"
    assert metadata["authorization_endpoint"] == "https://auth.example.com/oauth/authorize"
    assert metadata["token_endpoint"] == "https://auth.example.com/oauth/token"
    assert "authorization_code" in metadata["grant_types_supported"]
    assert metadata["code_challenge_methods_supported"] == ["S256"]
    assert "client_secret_basic" in metadata["token_endpoint_auth_methods_supported"]


def test_unauthorized_response_has_www_authenticate_resource_metadata(monkeypatch):
    from zendesk_skill.remote_auth import remote_auth_response

    oauth_env(monkeypatch)
    response = remote_auth_response(request("/mcp"), None)

    assert response.status_code == 401
    assert "WWW-Authenticate" in response.headers
    assert 'resource_metadata="https://mcp.example.com/.well-known/oauth-protected-resource/mcp"' in response.headers["WWW-Authenticate"]
    assert 'scope="zendesk:read"' in response.headers["WWW-Authenticate"]


def test_health_works_without_authentication(monkeypatch):
    from zendesk_skill.remote_auth import remote_auth_response

    oauth_env(monkeypatch)
    response = remote_auth_response(request("/health"), None)

    assert response.status_code == 200


def test_invalid_token_rejected(monkeypatch):
    from zendesk_skill.remote_auth import remote_auth_response

    oauth_env(monkeypatch)
    response = remote_auth_response(request("/mcp"), "Bearer invalid")

    assert response.status_code == 401


def test_expired_token_rejected(monkeypatch):
    from jwt import ExpiredSignatureError
    from zendesk_skill import remote_auth

    oauth_env(monkeypatch)
    monkeypatch.setattr(remote_auth.PyJWKClient, "get_signing_key_from_jwt", lambda self, token: SimpleNamespace(key="key"))
    monkeypatch.setattr(remote_auth.jwt, "decode", lambda *args, **kwargs: (_ for _ in ()).throw(ExpiredSignatureError("expired")))

    valid, reason = remote_auth.validate_oauth_bearer("Bearer expired")

    assert valid is False
    assert "expired" in reason.lower()


def test_incorrect_audience_rejected(monkeypatch):
    from jwt import InvalidAudienceError
    from zendesk_skill import remote_auth

    oauth_env(monkeypatch)
    monkeypatch.setattr(remote_auth.PyJWKClient, "get_signing_key_from_jwt", lambda self, token: SimpleNamespace(key="key"))
    monkeypatch.setattr(remote_auth.jwt, "decode", lambda *args, **kwargs: (_ for _ in ()).throw(InvalidAudienceError("bad aud")))

    valid, reason = remote_auth.validate_oauth_bearer("Bearer wrong-audience")

    assert valid is False
    assert "audience" in reason.lower()


def test_valid_access_requires_scope_and_allowed_user(monkeypatch):
    from zendesk_skill import remote_auth

    oauth_env(monkeypatch)
    monkeypatch.setattr(remote_auth.PyJWKClient, "get_signing_key_from_jwt", lambda self, token: SimpleNamespace(key="key"))
    monkeypatch.setattr(remote_auth.jwt, "decode", lambda *args, **kwargs: {"sub": "user-1", "email": "nassim@samedaycustom.example", "scope": "zendesk:read", "iss": "https://auth.example.com", "aud": "https://mcp.example.com/mcp", "exp": 9999999999})

    valid, reason = remote_auth.validate_oauth_bearer("Bearer valid")

    assert valid is True
    assert reason == "ok"


def test_valid_token_wrong_user_rejected(monkeypatch):
    from zendesk_skill import remote_auth

    oauth_env(monkeypatch)
    monkeypatch.setattr(remote_auth.PyJWKClient, "get_signing_key_from_jwt", lambda self, token: SimpleNamespace(key="key"))
    monkeypatch.setattr(remote_auth.jwt, "decode", lambda *args, **kwargs: {"sub": "user-2", "email": "other@example.com", "scope": "zendesk:read", "iss": "https://auth.example.com", "aud": "https://mcp.example.com/mcp", "exp": 9999999999})

    valid, reason = remote_auth.validate_oauth_bearer("Bearer valid-other-user")

    assert valid is False
    assert "samedaycustom" in reason.lower()


def test_static_auth_mode_is_development_only(monkeypatch):
    from zendesk_skill.remote_auth import remote_auth_response

    monkeypatch.setenv("MCP_AUTH_MODE", "static")
    monkeypatch.setenv("MCP_AUTH_TOKEN", "dev-secret")

    assert remote_auth_response(request("/mcp"), "Bearer dev-secret") is None
    assert remote_auth_response(request("/mcp"), "Bearer wrong").status_code == 401


def test_streamable_http_app_routes_and_tools_registered(monkeypatch):
    from zendesk_skill import server

    class FakeApp:
        def __init__(self):
            self.routes = []
            self.middleware = []

        def add_middleware(self, middleware):
            self.middleware.append(middleware)

    fake_app = FakeApp()
    called = {}

    def fake_streamable_http_app(path="/mcp"):
        called["path"] = path
        return fake_app

    monkeypatch.setattr(server.mcp, "streamable_http_app", fake_streamable_http_app)
    # _run_remote_http imports uvicorn inside the function, so patch the actual uvicorn module.
    import uvicorn
    monkeypatch.setattr(uvicorn, "run", lambda *args, **kwargs: None)

    server._run_remote_http()

    assert called["path"] == "/mcp"
    route_paths = {route.path for route in fake_app.routes}
    assert "/.well-known/oauth-protected-resource" in route_paths
    assert "/.well-known/oauth-protected-resource/mcp" in route_paths
    assert "/.well-known/oauth-authorization-server" in route_paths
    assert hasattr(server.mcp, "streamable_http_app")

    tool_names = {tool.name for tool in server.mcp._tool_manager._tools.values()}
    assert "zendesk_talk_get_calls" in tool_names
    assert "zendesk_talk_get_legs" in tool_names
    assert "zendesk_talk_analytics" in tool_names
