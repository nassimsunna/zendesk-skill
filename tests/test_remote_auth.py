import os
from pathlib import Path
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

    def fake_streamable_http_app():
        called["created"] = True
        return fake_app

    monkeypatch.setattr(server.remote_mcp, "streamable_http_app", fake_streamable_http_app)
    # _run_remote_http imports uvicorn inside the function, so patch the actual uvicorn module.
    import uvicorn
    monkeypatch.setattr(uvicorn, "run", lambda *args, **kwargs: None)

    server._run_remote_http()

    assert called["created"] is True
    route_paths = {route.path for route in fake_app.routes}
    assert "/.well-known/oauth-protected-resource" in route_paths
    assert "/.well-known/oauth-protected-resource/mcp" in route_paths
    assert "/.well-known/oauth-authorization-server" in route_paths
    assert hasattr(server.remote_mcp, "streamable_http_app")

    tool_names = {tool.name for tool in server.remote_mcp._tool_manager._tools.values()}
    assert "zendesk_talk_get_calls" in tool_names
    assert "zendesk_talk_get_legs" in tool_names
    assert "zendesk_talk_analytics" in tool_names


def test_repeated_authentication_reuses_jwks_client(monkeypatch):
    from zendesk_skill import remote_auth

    oauth_env(monkeypatch)
    remote_auth._JWKS_CLIENTS.clear()
    created = []

    class FakeJwksClient:
        def __init__(self, url):
            created.append(url)
            self.url = url

        def get_signing_key_from_jwt(self, token):
            return SimpleNamespace(key="key")

    monkeypatch.setattr(remote_auth, "PyJWKClient", FakeJwksClient)
    monkeypatch.setattr(remote_auth.jwt, "decode", lambda *args, **kwargs: {"sub": "user-1", "email": "nassim@samedaycustom.example", "scope": "zendesk:read", "iss": "https://auth.example.com", "aud": "https://mcp.example.com/mcp", "exp": 9999999999})

    assert remote_auth.validate_oauth_bearer("Bearer one") == (True, "ok")
    assert remote_auth.validate_oauth_bearer("Bearer two") == (True, "ok")
    assert created == ["https://auth.example.com/.well-known/jwks.json"]


def test_different_jwks_urls_use_separate_clients(monkeypatch):
    from zendesk_skill import remote_auth

    oauth_env(monkeypatch)
    remote_auth._JWKS_CLIENTS.clear()
    created = []

    class FakeJwksClient:
        def __init__(self, url):
            created.append(url)
            self.url = url

        def get_signing_key_from_jwt(self, token):
            return SimpleNamespace(key="key")

    monkeypatch.setattr(remote_auth, "PyJWKClient", FakeJwksClient)
    monkeypatch.setattr(remote_auth.jwt, "decode", lambda *args, **kwargs: {"sub": "user-1", "email": "nassim@samedaycustom.example", "scope": "zendesk:read", "iss": "https://auth.example.com", "aud": "https://mcp.example.com/mcp", "exp": 9999999999})

    assert remote_auth.validate_oauth_bearer("Bearer one") == (True, "ok")
    monkeypatch.setenv("MCP_OAUTH_JWKS_URL", "https://auth2.example.com/.well-known/jwks.json")
    assert remote_auth.validate_oauth_bearer("Bearer two") == (True, "ok")

    assert created == ["https://auth.example.com/.well-known/jwks.json", "https://auth2.example.com/.well-known/jwks.json"]


def test_bearer_scheme_is_case_insensitive_and_preserves_token(monkeypatch):
    from zendesk_skill import remote_auth

    oauth_env(monkeypatch)
    remote_auth._JWKS_CLIENTS.clear()
    seen_tokens = []

    class FakeJwksClient:
        def __init__(self, url):
            self.url = url

        def get_signing_key_from_jwt(self, token):
            seen_tokens.append(token)
            return SimpleNamespace(key="key")

    monkeypatch.setattr(remote_auth, "PyJWKClient", FakeJwksClient)
    monkeypatch.setattr(remote_auth.jwt, "decode", lambda *args, **kwargs: {"sub": "user-1", "email": "nassim@samedaycustom.example", "scope": "zendesk:read", "iss": "https://auth.example.com", "aud": "https://mcp.example.com/mcp", "exp": 9999999999})

    assert remote_auth.validate_oauth_bearer("Bearer Token-Exact") == (True, "ok")
    assert remote_auth.validate_oauth_bearer("bearer Token-Exact") == (True, "ok")
    assert remote_auth.validate_oauth_bearer("BEARER Token-Exact") == (True, "ok")
    assert seen_tokens == ["Token-Exact", "Token-Exact", "Token-Exact"]


@pytest.mark.parametrize("header", [None, "Bearer", "Bearer ", "Basic token", "Bearer token extra", " Bearer token"])
def test_invalid_authorization_headers_are_rejected(monkeypatch, header):
    from zendesk_skill import remote_auth

    oauth_env(monkeypatch)

    valid, reason = remote_auth.validate_oauth_bearer(header)

    assert valid is False
    assert reason in {"Bearer token required", "Malformed authorization header"}


def test_remote_mcp_exposes_only_read_only_tool_allowlist():
    from zendesk_skill import server

    remote_tool_names = set(server.remote_mcp._tool_manager._tools.keys())

    assert "zendesk_talk_get_calls" in remote_tool_names
    assert "zendesk_talk_get_legs" in remote_tool_names
    assert "zendesk_talk_analytics" in remote_tool_names
    assert "zendesk_search" in remote_tool_names
    assert "zendesk_get_ticket" in remote_tool_names
    assert "zendesk_get_ticket_metrics" in remote_tool_names
    assert "zendesk_get_satisfaction_ratings" in remote_tool_names
    assert "zendesk_list_groups" in remote_tool_names
    assert "zendesk_list_views" in remote_tool_names

    forbidden = {
        "zendesk_create_ticket",
        "zendesk_update_ticket",
        "zendesk_add_private_note",
        "zendesk_add_public_note",
    }
    assert forbidden.isdisjoint(remote_tool_names)


def test_remote_mcp_does_not_auto_expose_future_local_write_tools(monkeypatch):
    from zendesk_skill import server

    original_tools = dict(server.mcp._tool_manager._tools)
    try:
        existing_tool = next(iter(server.mcp._tool_manager._tools.values()))
        server.mcp._tool_manager._tools["zendesk_delete_everything"] = existing_tool
        rebuilt = server.create_remote_read_only_mcp()

        assert "zendesk_delete_everything" not in rebuilt._tool_manager._tools
        assert set(rebuilt._tool_manager._tools) == server.REMOTE_READ_ONLY_TOOL_NAMES
    finally:
        server.mcp._tool_manager._tools.clear()
        server.mcp._tool_manager._tools.update(original_tools)


def test_remote_mcp_tool_schemas_do_not_expose_filesystem_path_fields():
    from zendesk_skill import server

    forbidden = server.REMOTE_FORBIDDEN_SCHEMA_FIELDS
    remote_input_models = [
        server.RemoteOutputOnlyInput,
        server.RemoteTicketIdInput,
        server.RemoteViewIdInput,
        server.RemoteUserIdInput,
        server.RemoteOrgIdInput,
        server.RemoteRatingIdInput,
        server.RemoteSearchQueryInput,
        server.RemoteSearchInput,
        server.RemotePaginatedInput,
        server.RemoteViewTicketsInput,
        server.RemoteSatisfactionRatingsInput,
        server.RemoteTalkAnalyticsInput,
        server.RemoteAuthStatusInput,
    ]

    for model in remote_input_models:
        assert forbidden.isdisjoint(model.model_fields.keys())
        assert model.model_config.get("extra") == "forbid"


def test_remote_schema_rejects_caller_controlled_output_path():
    from pydantic import ValidationError
    from zendesk_skill import server

    with pytest.raises(ValidationError):
        server.RemoteTalkAnalyticsInput(start_date="2026-01-01", end_date="2026-01-02", output_path="/tmp/evil.json")


def test_remote_schema_rejects_absolute_and_traversal_path_fields():
    from pydantic import ValidationError
    from zendesk_skill import server

    with pytest.raises(ValidationError):
        server.RemoteTicketIdInput(ticket_id="123", output_path="/etc/passwd")
    with pytest.raises(ValidationError):
        server.RemoteTicketIdInput(ticket_id="123", file_path="../escape.json")
    with pytest.raises(ValidationError):
        server.RemoteTicketIdInput(ticket_id="123", directory_path="../escape")


def test_remote_generated_storage_path_stays_inside_remote_storage_dir(tmp_path, monkeypatch):
    from pathlib import Path
    from zendesk_skill import server

    monkeypatch.setenv("REMOTE_STORAGE_DIR", str(tmp_path))

    generated = Path(server._remote_output_path("talk_calls")).resolve()

    assert tmp_path.resolve() in generated.parents
    assert generated.name.startswith("talk_calls-")
    assert generated.suffix == ".json"
    assert not generated.exists()


def test_local_stdio_schemas_still_allow_output_path():
    from zendesk_skill import server

    assert "output_path" in server.TalkAnalyticsInput.model_fields
    assert "output_path" in server.TicketIdInput.model_fields


def test_fastmcp_exposes_streamable_http_app():
    from mcp.server.fastmcp import FastMCP

    assert callable(getattr(FastMCP("compatibility-check"), "streamable_http_app", None))


def test_remote_streamable_http_app_can_be_created():
    from zendesk_skill import server

    server._ensure_streamable_http_compatible(server.remote_mcp)
    app = server.remote_mcp.streamable_http_app()

    assert app is not None


def test_startup_compatibility_check_reports_missing_streamable_http(monkeypatch):
    from zendesk_skill import server

    monkeypatch.setattr(server, "package_version", lambda package: "1.8.0")

    with pytest.raises(RuntimeError, match="FastMCP.streamable_http_app"):
        server._ensure_streamable_http_compatible(SimpleNamespace())


def test_startup_compatibility_check_rejects_incompatible_mcp_versions(monkeypatch):
    from zendesk_skill import server

    fake_server = SimpleNamespace(streamable_http_app=lambda: object())
    monkeypatch.setattr(server, "package_version", lambda package: "1.7.1")
    with pytest.raises(RuntimeError, match=r"mcp\[cli\]>=1.8.0,<2"):
        server._ensure_streamable_http_compatible(fake_server)

    monkeypatch.setattr(server, "package_version", lambda package: "2.0.0")
    with pytest.raises(RuntimeError, match=r"mcp\[cli\]>=1.8.0,<2"):
        server._ensure_streamable_http_compatible(fake_server)


def test_pyproject_requires_streamable_http_compatible_mcp_v1():
    import tomllib
    from pathlib import Path

    pyproject = tomllib.loads(Path("pyproject.toml").read_text())
    dependencies = pyproject["project"]["dependencies"]
    mcp_dependencies = [dependency for dependency in dependencies if dependency.startswith("mcp[cli]")]

    assert mcp_dependencies == ["mcp[cli]>=1.8.0,<2"]
    dependency = mcp_dependencies[0]
    assert ">=1.6" not in dependency
    assert ">=1.8.0" in dependency
    assert "<2" in dependency

    lock_text = Path("uv.lock").read_text()
    assert 'specifier = ">=1.8.0,<2"' in lock_text
    assert 'specifier = ">=1.6.0"' not in lock_text


def test_remote_storage_directory_permissions_are_owner_only(tmp_path, monkeypatch):
    import stat
    from zendesk_skill import server

    root = tmp_path / "remote-storage"
    monkeypatch.setenv("REMOTE_STORAGE_DIR", str(root))

    resolved = server._remote_storage_root()

    assert resolved == root.resolve()
    assert stat.S_IMODE(resolved.stat().st_mode) == 0o700


def test_remote_storage_file_permissions_are_owner_only(tmp_path, monkeypatch):
    import stat
    from zendesk_skill import server
    from zendesk_skill.storage import save_response

    monkeypatch.setenv("REMOTE_STORAGE_DIR", str(tmp_path))
    path = server._remote_output_path("secure")

    saved_path, _ = save_response("secure", {}, {"ok": True}, output_path=path)

    assert saved_path == path
    assert stat.S_IMODE(Path(path).stat().st_mode) == 0o600


def test_remote_storage_tightens_existing_broad_permissions(tmp_path, monkeypatch):
    import stat
    from zendesk_skill import server

    tmp_path.chmod(0o777)
    monkeypatch.setenv("REMOTE_STORAGE_DIR", str(tmp_path))

    resolved = server._remote_storage_root()

    assert stat.S_IMODE(resolved.stat().st_mode) == 0o700


def test_remote_storage_rejects_symlink_escape(tmp_path, monkeypatch):
    from zendesk_skill import server

    target = tmp_path / "target"
    target.mkdir()
    link = tmp_path / "link"
    link.symlink_to(target, target_is_directory=True)
    monkeypatch.setenv("REMOTE_STORAGE_DIR", str(link))

    with pytest.raises(RuntimeError, match="symlink"):
        server._remote_storage_root()


def test_remote_output_path_stays_inside_managed_directory(tmp_path, monkeypatch):
    from zendesk_skill import server

    monkeypatch.setenv("REMOTE_STORAGE_DIR", str(tmp_path))

    generated = Path(server._remote_output_path("safe")).resolve()

    assert generated.parent == tmp_path.resolve()
    assert generated.name.startswith("safe-")


def test_remote_output_path_does_not_overwrite_existing_file(tmp_path, monkeypatch):
    from zendesk_skill import server

    monkeypatch.setenv("REMOTE_STORAGE_DIR", str(tmp_path))
    monkeypatch.setattr(server.uuid, "uuid4", lambda: SimpleNamespace(hex="fixed"))
    existing = tmp_path / "secure-fixed.json"
    existing.write_text("already here")

    with pytest.raises(RuntimeError, match="unique remote storage"):
        server._remote_output_path("secure")

    assert existing.read_text() == "already here"


def test_remote_storage_cleanup_removes_only_expired_managed_files(tmp_path, monkeypatch):
    import time
    from zendesk_skill import server

    managed = tmp_path / "managed"
    outside = tmp_path / "outside"
    managed.mkdir()
    outside.mkdir()
    old_managed = managed / "old.json"
    new_managed = managed / "new.json"
    old_outside = outside / "old.json"
    for path in (old_managed, new_managed, old_outside):
        path.write_text("{}")
    old_time = time.time() - 10_000
    os.utime(old_managed, (old_time, old_time))
    os.utime(old_outside, (old_time, old_time))
    monkeypatch.setenv("REMOTE_STORAGE_DIR", str(managed))
    monkeypatch.setenv("REMOTE_STORAGE_RETENTION_SECONDS", "60")

    server._remote_storage_root()

    assert not old_managed.exists()
    assert new_managed.exists()
    assert old_outside.exists()


def test_talk_storage_minimization_redacts_recordings_and_transcripts():
    from zendesk_skill import operations

    minimized = operations._minimize_talk_for_storage({
        "id": "call-1",
        "recording_url": "https://recording.example/call-1",
        "transcript": "private transcript",
        "nested": {"voicemail_recording_url": "https://recording.example/vm"},
        "talk_time": 30,
    })

    assert minimized["recording_url"] == "[redacted]"
    assert minimized["transcript"] == "[redacted]"
    assert minimized["nested"]["voicemail_recording_url"] == "[redacted]"
    assert minimized["talk_time"] == 30
