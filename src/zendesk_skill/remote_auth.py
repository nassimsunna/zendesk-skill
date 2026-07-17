"""OAuth 2.1 resource-server authentication for remote MCP deployments.

The MCP server remains a resource server. A standards-compliant external OAuth
2.1/OIDC authorization server issues JWT access tokens after an authorization
code + PKCE flow. This module publishes MCP protected-resource metadata and
validates bearer tokens with PyJWT/JWKS.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

import jwt
from jwt import ExpiredSignatureError, InvalidAudienceError, InvalidIssuerError, InvalidTokenError, PyJWKClient
from starlette.responses import JSONResponse, Response

DEFAULT_REQUIRED_SCOPE = "zendesk:read"
_JWKS_CLIENTS: dict[str, PyJWKClient] = {}


@dataclass(frozen=True)
class OAuthConfig:
    issuer: str
    audience: str
    jwks_url: str
    authorization_endpoint: str
    token_endpoint: str
    required_scope: str = DEFAULT_REQUIRED_SCOPE
    allowed_subject: str | None = None
    allowed_email: str | None = None
    allowed_account: str | None = None

    @property
    def configured_for_oauth(self) -> bool:
        return bool(self.issuer and self.audience and self.jwks_url)


def _strip_slash(value: str) -> str:
    return value.rstrip("/")


def get_auth_mode() -> str:
    """Return remote MCP auth mode: oauth by default, static only for development."""
    return os.environ.get("MCP_AUTH_MODE", "oauth").strip().lower()


def get_oauth_config() -> OAuthConfig:
    """Read OAuth resource-server configuration from environment variables."""
    issuer = os.environ.get("MCP_OAUTH_ISSUER", "")
    issuer_for_endpoints = _strip_slash(issuer)
    jwks_url = os.environ.get("MCP_OAUTH_JWKS_URL") or (f"{issuer_for_endpoints}/.well-known/jwks.json" if issuer_for_endpoints else "")
    return OAuthConfig(
        issuer=issuer,
        audience=os.environ.get("MCP_OAUTH_AUDIENCE", ""),
        jwks_url=jwks_url,
        authorization_endpoint=os.environ.get("MCP_OAUTH_AUTHORIZATION_ENDPOINT", f"{issuer_for_endpoints}/authorize" if issuer_for_endpoints else ""),
        token_endpoint=os.environ.get("MCP_OAUTH_TOKEN_ENDPOINT", f"{issuer_for_endpoints}/oauth/token" if issuer_for_endpoints else ""),
        required_scope=os.environ.get("MCP_OAUTH_REQUIRED_SCOPE", DEFAULT_REQUIRED_SCOPE),
        allowed_subject=os.environ.get("MCP_ALLOWED_SUBJECT"),
        allowed_email=os.environ.get("MCP_ALLOWED_EMAIL"),
        allowed_account=os.environ.get("MCP_ALLOWED_ACCOUNT"),
    )


def _base_url_from_request(request: Any) -> str:
    configured = os.environ.get("MCP_PUBLIC_BASE_URL")
    if configured:
        return _strip_slash(configured)
    return _strip_slash(str(request.base_url))


def resource_metadata_url(base_url: str) -> str:
    return f"{_strip_slash(base_url)}/.well-known/oauth-protected-resource/mcp"


def protected_resource_metadata(base_url: str) -> dict[str, Any]:
    """Build RFC 9728 protected-resource metadata for the MCP endpoint."""
    config = get_oauth_config()
    return {
        "resource": f"{_strip_slash(base_url)}/mcp",
        "authorization_servers": [config.issuer] if config.issuer else [],
        "scopes_supported": [config.required_scope],
        "bearer_methods_supported": ["header"],
        "resource_name": "Zendesk Talk MCP",
    }


def authorization_server_metadata() -> dict[str, Any]:
    """Build authorization-server metadata for a pre-registered OAuth client."""
    config = get_oauth_config()
    return {
        "issuer": config.issuer,
        "authorization_endpoint": config.authorization_endpoint,
        "token_endpoint": config.token_endpoint,
        "jwks_uri": config.jwks_url,
        "response_types_supported": ["code"],
        "grant_types_supported": ["authorization_code", "refresh_token"],
        "code_challenge_methods_supported": ["S256"],
        "token_endpoint_auth_methods_supported": ["client_secret_basic", "client_secret_post"],
        "scopes_supported": [config.required_scope],
    }


def metadata_response(request: Any) -> JSONResponse:
    return JSONResponse(protected_resource_metadata(_base_url_from_request(request)))


def authorization_server_metadata_response(request: Any) -> JSONResponse:
    return JSONResponse(authorization_server_metadata())


def unauthorized_response(request: Any, error: str = "invalid_token", description: str = "OAuth access token required") -> JSONResponse:
    """Return MCP/RFC9728-compatible 401 with WWW-Authenticate metadata pointer."""
    base_url = _base_url_from_request(request)
    config = get_oauth_config()
    header = (
        'Bearer '
        f'resource_metadata="{resource_metadata_url(base_url)}", '
        f'scope="{config.required_scope}", '
        f'error="{error}", '
        f'error_description="{description}"'
    )
    return JSONResponse({"error": error, "error_description": description}, status_code=401, headers={"WWW-Authenticate": header})


def get_jwks_client(jwks_url: str) -> PyJWKClient:
    """Return a cached JWKS client for a URL so provider keys are reused."""
    client = _JWKS_CLIENTS.get(jwks_url)
    if client is None:
        client = PyJWKClient(jwks_url)
        _JWKS_CLIENTS[jwks_url] = client
    return client


def _scope_values(claims: dict[str, Any]) -> set[str]:
    scope = claims.get("scope", "")
    scopes = set(scope.split()) if isinstance(scope, str) else set(scope or [])
    permissions = claims.get("permissions", [])
    if isinstance(permissions, list):
        scopes.update(str(permission) for permission in permissions)
    return scopes


def _subject_allowed(claims: dict[str, Any], config: OAuthConfig) -> bool:
    if config.allowed_subject and claims.get("sub") != config.allowed_subject:
        return False
    if config.allowed_email and claims.get("email") != config.allowed_email:
        return False
    if config.allowed_account and claims.get("sameday_account") != config.allowed_account and claims.get("account") != config.allowed_account:
        return False
    return True


def _extract_bearer_token(authorization: str | None) -> tuple[str | None, str | None]:
    if not authorization:
        return None, "Bearer token required"
    parts = authorization.split(" ", 1)
    if len(parts) != 2 or not parts[0] or not parts[1]:
        return None, "Malformed authorization header"
    scheme, token = parts[0], parts[1]
    if scheme.lower() != "bearer":
        return None, "Bearer token required"
    if token.strip() != token or not token or any(char.isspace() for char in token):
        return None, "Malformed authorization header"
    return token, None


def validate_oauth_bearer(authorization: str | None) -> tuple[bool, str]:
    """Validate an OAuth bearer JWT for issuer, audience, expiration, scope, and allowed user/account."""
    config = get_oauth_config()
    if not config.configured_for_oauth:
        return False, "OAuth is not configured for this MCP server"
    token, token_error = _extract_bearer_token(authorization)
    if token_error:
        return False, token_error

    try:
        signing_key = get_jwks_client(config.jwks_url).get_signing_key_from_jwt(token)
        claims = jwt.decode(
            token,
            signing_key.key,
            algorithms=["RS256", "ES256"],
            audience=config.audience,
            issuer=config.issuer,
            options={"require": ["exp", "iss", "aud", "sub"]},
        )
    except ExpiredSignatureError:
        return False, "Access token expired"
    except InvalidAudienceError:
        return False, "Access token audience is not this MCP resource"
    except InvalidIssuerError:
        return False, "Access token issuer is not trusted"
    except InvalidTokenError as exc:
        return False, f"Invalid access token: {exc}"
    except Exception as exc:
        return False, f"Unable to validate access token: {exc}"

    if config.required_scope not in _scope_values(claims):
        return False, "Access token is missing the required scope"
    if not _subject_allowed(claims, config):
        return False, "Access token is not authorized for the configured SameDayCustom user or account"
    return True, "ok"


def remote_auth_response(request: Any, authorization: str | None) -> Response | None:
    """Return auth/metadata response for remote HTTP MCP requests, or None when allowed."""
    path = request.url.path
    if path == "/health":
        return JSONResponse({"status": "ok"})
    if path in {"/.well-known/oauth-protected-resource", "/.well-known/oauth-protected-resource/mcp"}:
        return metadata_response(request)
    if path == "/.well-known/oauth-authorization-server":
        return authorization_server_metadata_response(request)

    if get_auth_mode() == "static":
        token = os.environ.get("MCP_AUTH_TOKEN")
        if not token:
            return JSONResponse({"error": "MCP_AUTH_TOKEN is required for static development auth"}, status_code=503)
        if authorization != f"Bearer {token}":
            return JSONResponse({"error": "Unauthorized"}, status_code=401)
        return None

    valid, reason = validate_oauth_bearer(authorization)
    if not valid:
        return unauthorized_response(request, description=reason)
    return None
