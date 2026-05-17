"""OAuth 2.1 provider for the MCP Proxy.

This module is lazy-imported by `__init__.py` ONLY when the user has
enabled the OAuth toggle. When OAuth is off the import never runs and the
proxy behaves exactly like a vanilla unauthenticated webhook.

Implements the subset of OAuth 2.1 required by the MCP spec:
- Authorization-code grant with PKCE (S256)
- Client authentication via client_secret_basic OR client_secret_post
- Refresh tokens
- RFC 8414 Authorization Server Metadata
- RFC 9728 Protected Resource Metadata
- WWW-Authenticate: Bearer with resource_metadata pointer (so MCP clients
  discover the auth server from a 401 on the webhook URL)

Single-tenant by design: one client_id / client_secret pair, configured in
the addon. The consent screen displays the requesting redirect_uri so the
user can verify they're authorizing the connector they meant to.

Tokens are signed (HMAC-SHA256) with a per-install secret persisted at
/config/.mcp_proxy_oauth_secret. They contain enough state to validate
without a server-side store, so the integration survives restarts.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import logging
import re
import secrets
import time
from html import escape
from pathlib import Path
from typing import TypedDict
from urllib.parse import urlparse

from aiohttp import web
from homeassistant.components.http import HomeAssistantView
from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)

OAUTH_BASE = "/api/mcp_proxy/oauth"  # slot appended dynamically per view
# Authorize/token endpoints live at the root rather than under
# OAUTH_BASE because Claude.ai (and apparently other MCP clients)
# construct the authorize URL as `<host>/authorize` from the resource
# host root — they do not use the `authorization_endpoint` field of
# our authorization-server metadata document. Registering at the root
# is the only way to actually catch the redirect.
# Base paths — slot number appended per provider instance
AUTHORIZE_BASE = "/authorize"
TOKEN_BASE = "/token"
SECRET_FILE = Path("/config/.mcp_proxy_oauth_secret")

ACCESS_TOKEN_TTL = 60 * 60          # 1 hour

# Global registry: client_id → OAuthProvider
# Populated when each provider registers its views.
_PROVIDER_REGISTRY: dict[str, "OAuthProvider"] = {}
REFRESH_TOKEN_TTL = 30 * 24 * 60 * 60  # 30 days
AUTH_CODE_TTL = 5 * 60              # 5 minutes
TOKEN_KIND_ACCESS = "access"
TOKEN_KIND_REFRESH = "refresh"

# RFC 7636 §4.1: code_verifier is 43-128 chars from the unreserved URL set.
PKCE_VERIFIER_MIN = 43
PKCE_VERIFIER_MAX = 128
# SHA-256 → 32 bytes → 43 base64url chars (no padding).
PKCE_S256_CHALLENGE_LEN = 43
_PKCE_VERIFIER_RE = re.compile(r"^[A-Za-z0-9._~-]+$")
_PKCE_CHALLENGE_RE = re.compile(r"^[A-Za-z0-9_-]{43}$")

# Pending-code dict cap.
MAX_PENDING_CODES = 1000

# Minimum client_id length
MIN_CLIENT_ID_LEN = 16


class _PendingCode(TypedDict):
    redirect_uri: str
    code_challenge: str
    expires: float


def _b64url_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64url_decode(s: str) -> bytes:
    pad = "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode(s + pad)


def load_or_create_secret() -> bytes:
    """Persist a 32-byte signing secret across restarts."""
    if SECRET_FILE.exists():
        data = SECRET_FILE.read_bytes()
        if len(data) >= 32:
            return data
        _LOGGER.warning(
            "MCP Proxy OAuth: existing signing key at %s is shorter than "
            "32 bytes (got %d). Regenerating — ALL previously issued "
            "OAuth tokens are now invalid; MCP clients will need to "
            "re-authorize.",
            SECRET_FILE,
            len(data),
        )
    new_secret = secrets.token_bytes(32)
    SECRET_FILE.parent.mkdir(parents=True, exist_ok=True)
    SECRET_FILE.write_bytes(new_secret)
    try:
        SECRET_FILE.chmod(0o600)
    except OSError as e:
        _LOGGER.warning(
            "MCP Proxy OAuth: could not chmod 0600 the signing key file "
            "at %s (%s: %s). The key may have wider permissions than intended.",
            SECRET_FILE,
            type(e).__name__,
            e,
        )
    return new_secret


def _is_valid_redirect_uri(redirect_uri: str) -> bool:
    """Spec-floor validation for OAuth redirect_uri."""
    if not redirect_uri:
        return False
    try:
        parsed = urlparse(redirect_uri)
    except ValueError:
        return False
    if parsed.scheme != "https":
        return False
    if not parsed.hostname:
        return False
    return not parsed.fragment


def _build_base_url(
    request: web.Request, public_base_url: str | None = None
) -> str:
    """Build the public base URL used in OAuth metadata and redirects."""
    if public_base_url:
        return public_base_url.rstrip("/")
    host = request.headers.get("X-Forwarded-Host") or request.headers.get("Host", "")
    scheme = request.headers.get("X-Forwarded-Proto", request.scheme)
    return f"{scheme}://{host}"


class OAuthProvider:
    """Holds OAuth state and registers HA HTTP views."""

    def __init__(
        self,
        hass: HomeAssistant,
        client_id: str,
        client_secret: str,
        webhook_id: str,
        signing_key: bytes,
        public_base_url: str | None = None,
        slot: int = 1,
    ) -> None:
        if not client_id or len(client_id) < MIN_CLIENT_ID_LEN:
            raise ValueError(
                f"client_id must be a non-empty string at least "
                f"{MIN_CLIENT_ID_LEN} characters long"
            )
        if not client_secret:
            raise ValueError("client_secret must be a non-empty string")
        if len(signing_key) < 32:
            raise ValueError("signing_key must be at least 32 bytes")
        self._hass = hass
        self._client_id = client_id
        self._client_secret = client_secret
        self._webhook_id = webhook_id
        self._public_base_url = public_base_url
        self._signing_key = signing_key
        self._slot = slot
        self._codes: dict[str, _PendingCode] = {}

    @property
    def client_id(self) -> str:
        return self._client_id

    def client_id_masked(self) -> str:
        if len(self._client_id) <= 4:
            return "***"
        return self._client_id[:3] + "..." + self._client_id[-2:]

    def resource_url(self, base_url: str) -> str:
        return f"{base_url}/api/webhook/{self._webhook_id}"

    def authorization_server_url(self, base_url: str) -> str:
        return f"{base_url}{OAUTH_BASE}/slot{self._slot}"

    def base_url_for(self, request: web.Request) -> str:
        return _build_base_url(request, self._public_base_url)

    def register_views(self) -> None:
        """Register the OAuth endpoints with HA's HTTP layer."""
        # Register this provider in the global registry by client_id
        _PROVIDER_REGISTRY[self._client_id] = self
        for view in (
            ProtectedResourceMetadataView(self),
            AuthorizationServerMetadataView(self),
            AuthorizeView(self),
            TokenView(self),
        ):
            self._hass.http.register_view(view)

    @property
    def slot(self) -> int:
        return self._slot

    def _issue_token(self, kind: str, ttl: int) -> str:
        now = int(time.time())
        payload = {
            "kind": kind,
            "iat": now,
            "exp": now + ttl,
            "jti": secrets.token_urlsafe(12),
            "cid": self._client_id,
        }
        body = _b64url_encode(json.dumps(payload, separators=(",", ":")).encode())
        sig = hmac.new(self._signing_key, body.encode("ascii"), hashlib.sha256).digest()
        return f"{body}.{_b64url_encode(sig)}"

    def _validate_token(self, token: str, expected_kind: str) -> bool:
        try:
            body, sig_part = token.rsplit(".", 1)
        except ValueError:
            return False
        try:
            actual_sig = _b64url_decode(sig_part)
        except (ValueError, binascii.Error):
            return False
        expected_sig = hmac.new(
            self._signing_key, body.encode("ascii"), hashlib.sha256
        ).digest()
        if not hmac.compare_digest(actual_sig, expected_sig):
            return False
        try:
            payload = json.loads(_b64url_decode(body))
        except (ValueError, json.JSONDecodeError):
            return False
        if payload.get("kind") != expected_kind:
            return False
        if payload.get("cid") != self._client_id:
            return False
        return payload.get("exp", 0) > int(time.time())

    def issue_access_token(self) -> str:
        return self._issue_token(TOKEN_KIND_ACCESS, ACCESS_TOKEN_TTL)

    def issue_refresh_token(self) -> str:
        return self._issue_token(TOKEN_KIND_REFRESH, REFRESH_TOKEN_TTL)

    def validate_access_token(self, token: str) -> bool:
        return self._validate_token(token, TOKEN_KIND_ACCESS)

    def validate_refresh_token(self, token: str) -> bool:
        return self._validate_token(token, TOKEN_KIND_REFRESH)

    def validate_bearer(self, request: web.Request) -> bool:
        header = request.headers.get("Authorization", "")
        if not header.lower().startswith("bearer "):
            return False
        token = header[7:].strip()
        return self.validate_access_token(token)

    def issue_code(self, redirect_uri: str, code_challenge: str) -> str | None:
        """Issue a one-shot authorization code."""
        now = time.time()
        self._codes = {k: v for k, v in self._codes.items() if v["expires"] > now}
        if len(self._codes) >= MAX_PENDING_CODES:
            _LOGGER.warning(
                "MCP Proxy OAuth: pending-code store at cap (%d); refusing "
                "new issuance until existing codes expire or are consumed.",
                MAX_PENDING_CODES,
            )
            return None
        code = secrets.token_urlsafe(32)
        self._codes[code] = {
            "redirect_uri": redirect_uri,
            "code_challenge": code_challenge,
            "expires": now + AUTH_CODE_TTL,
        }
        return code

    def consume_code(self, code: str, redirect_uri: str, code_verifier: str) -> bool:
        if not (PKCE_VERIFIER_MIN <= len(code_verifier) <= PKCE_VERIFIER_MAX):
            return False
        if not _PKCE_VERIFIER_RE.match(code_verifier):
            return False
        entry = self._codes.pop(code, None)
        if entry is None:
            return False
        if entry["expires"] < time.time():
            return False
        if entry["redirect_uri"] != redirect_uri:
            return False
        derived = _b64url_encode(hashlib.sha256(code_verifier.encode()).digest())
        return hmac.compare_digest(
            derived.encode("ascii"),
            entry["code_challenge"].encode("ascii"),
        )

    def authenticate_client(
        self, client_id: str | None, client_secret: str | None
    ) -> bool:
        if not client_id or not client_secret:
            return False
        return (
            hmac.compare_digest(client_id.encode(), self._client_id.encode())
            and hmac.compare_digest(client_secret.encode(), self._client_secret.encode())
        )


# ---------------------------------------------------------------------------
# Views
# ---------------------------------------------------------------------------


class ProtectedResourceMetadataView(HomeAssistantView):
    """RFC 9728 Protected Resource Metadata."""

    requires_auth = False
    cors_allowed = True

    def __init__(self, provider: "OAuthProvider") -> None:
        self._provider = provider
        self.url = f"{OAUTH_BASE}/slot{provider.slot}/protected-resource"
        self.name = f"mcp_proxy:oauth:slot{provider.slot}:protected-resource"

    async def get(self, request: web.Request) -> web.Response:
        base = self._provider.base_url_for(request)
        return web.json_response(
            {
                "resource": self._provider.resource_url(base),
                "authorization_servers": [
                    self._provider.authorization_server_url(base)
                ],
                "bearer_methods_supported": ["header"],
                "resource_documentation": (
                    "https://github.com/homeassistant-ai/ha-mcp"
                ),
            }
        )


class AuthorizationServerMetadataView(HomeAssistantView):
    """RFC 8414 Authorization Server Metadata."""

    requires_auth = False
    cors_allowed = True

    def __init__(self, provider: "OAuthProvider") -> None:
        self._provider = provider
        self.url = f"{OAUTH_BASE}/slot{provider.slot}/authorization-server"
        self.name = f"mcp_proxy:oauth:slot{provider.slot}:authorization-server"

    async def get(self, request: web.Request) -> web.Response:
        base = self._provider.base_url_for(request)
        as_url = self._provider.authorization_server_url(base)
        return web.json_response(
            {
                "issuer": as_url,
                "authorization_endpoint": f"{base}{AUTHORIZE_BASE}",
                "token_endpoint": f"{base}{TOKEN_BASE}",
                "response_types_supported": ["code"],
                "grant_types_supported": [
                    "authorization_code",
                    "refresh_token",
                ],
                "code_challenge_methods_supported": ["S256"],
                "token_endpoint_auth_methods_supported": [
                    "client_secret_basic",
                    "client_secret_post",
                ],
            }
        )


class AuthorizeView(HomeAssistantView):
    """OAuth /authorize endpoint with a minimal consent page."""

    requires_auth = False

    def __init__(self, provider: "OAuthProvider") -> None:
        self._provider = provider
        # /authorize is global — client_id in request identifies the slot
        self.url = f"{AUTHORIZE_BASE}"
        self.name = f"mcp_proxy:oauth:slot{provider.slot}:authorize"

    @staticmethod
    def _redirect_with(redirect_uri: str, **params: str) -> web.Response:
        import yarl
        url = yarl.URL(redirect_uri).update_query(params)
        return web.Response(
            status=302,
            headers={"Location": str(url)},
        )

    async def get(self, request: web.Request) -> web.Response:
        params = request.query
        client_id = params.get("client_id", "")
        redirect_uri = params.get("redirect_uri", "")
        state = params.get("state", "")
        code_challenge = params.get("code_challenge", "")
        code_challenge_method = params.get("code_challenge_method", "")
        response_type = params.get("response_type", "")

        # Route to the correct provider based on client_id
        provider = _PROVIDER_REGISTRY.get(client_id)
        if provider is not None:
            self._provider = provider

        err = self._validate_authorize_params(
            response_type=response_type,
            client_id=client_id,
            redirect_uri=redirect_uri,
            code_challenge=code_challenge,
            code_challenge_method=code_challenge_method,
        )
        if err is not None:
            return err

        html = f"""<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
  <title>Authorize MCP Connector</title>
  <style>
    body {{ font-family: system-ui, sans-serif; max-width: 36rem; margin: 4rem auto; padding: 0 1rem; }}
    code {{ background: #f4f4f4; padding: 2px 6px; border-radius: 3px; word-break: break-all; }}
    button {{ padding: 0.5rem 1rem; font-size: 1rem; margin-right: 0.5rem; }}
    .approve {{ background: #2563eb; color: white; border: none; }}
    .deny {{ background: #e5e7eb; color: #111; border: none; }}
  </style>
</head>
<body>
  <h1>Authorize MCP Proxy</h1>
  <p>An MCP client is requesting access to your MCP server.</p>
  <p>It will redirect to:<br><code>{escape(redirect_uri)}</code></p>
  <p>Only allow this if you started this connection yourself.</p>
  <form method="POST" action="{AUTHORIZE_BASE}">
    <input type="hidden" name="client_id" value="{escape(client_id)}">
    <input type="hidden" name="redirect_uri" value="{escape(redirect_uri)}">
    <input type="hidden" name="state" value="{escape(state)}">
    <input type="hidden" name="code_challenge" value="{escape(code_challenge)}">
    <button class="approve" type="submit" name="action" value="approve">Allow</button>
    <button class="deny" type="submit" name="action" value="deny">Deny</button>
  </form>
</body>
</html>"""
        return web.Response(text=html, content_type="text/html")

    async def post(self, request: web.Request) -> web.Response:
        data = await request.post()
        action = str(data.get("action", ""))
        client_id = str(data.get("client_id", ""))
        redirect_uri = str(data.get("redirect_uri", ""))
        state = str(data.get("state", ""))
        code_challenge = str(data.get("code_challenge", ""))

        err = self._validate_authorize_params(
            response_type="code",
            client_id=client_id,
            redirect_uri=redirect_uri,
            code_challenge=code_challenge,
            code_challenge_method="S256",
        )
        if err is not None:
            return err

        if action == "deny":
            return self._redirect_with(
                redirect_uri, error="access_denied", state=state
            )
        if action != "approve":
            return web.Response(status=400, text="invalid action")

        code = self._provider.issue_code(redirect_uri, code_challenge)
        if code is None:
            return self._redirect_with(
                redirect_uri, error="temporarily_unavailable", state=state
            )
        return self._redirect_with(redirect_uri, code=code, state=state)

    def _validate_authorize_params(
        self,
        *,
        response_type: str,
        client_id: str,
        redirect_uri: str,
        code_challenge: str,
        code_challenge_method: str,
    ) -> web.Response | None:
        if response_type != "code":
            return web.Response(status=400, text="unsupported_response_type")
        if code_challenge_method != "S256":
            return web.Response(
                status=400, text="invalid code_challenge_method (S256 required)"
            )
        if not _PKCE_CHALLENGE_RE.match(code_challenge):
            return web.Response(
                status=400, text="invalid code_challenge (must be 43-char base64url)"
            )
        if client_id != self._provider.client_id:
            return web.Response(status=400, text="invalid client_id")
        if not _is_valid_redirect_uri(redirect_uri):
            return web.Response(
                status=400, text="redirect_uri must be an https:// URL with a host"
            )
        return None


class TokenView(HomeAssistantView):
    """OAuth /token endpoint: authorization_code + refresh_token grants."""

    requires_auth = False
    cors_allowed = True

    def __init__(self, provider: "OAuthProvider") -> None:
        self._provider = provider
        # /token is global — client_id in request identifies the slot
        self.url = f"{TOKEN_BASE}"
        self.name = f"mcp_proxy:oauth:slot{provider.slot}:token"

    @staticmethod
    def _extract_client_creds(
        request: web.Request, form: dict
    ) -> tuple[str | None, str | None]:
        """Pull client_id/secret from Basic auth header OR form body."""
        header = request.headers.get("Authorization", "")
        if header.lower().startswith("basic "):
            try:
                decoded = base64.b64decode(
                    header[6:].strip(), validate=True
                ).decode("utf-8")
            except (ValueError, UnicodeDecodeError, binascii.Error):
                return None, None
            if ":" in decoded:
                cid, _, sec = decoded.partition(":")
                return cid, sec
            return None, None
        return form.get("client_id"), form.get("client_secret")

    async def post(self, request: web.Request) -> web.Response:
        form = dict(await request.post())
        client_id, client_secret = self._extract_client_creds(request, form)

        # Route to the correct provider based on client_id
        provider = _PROVIDER_REGISTRY.get(client_id or "")
        if provider is not None:
            self._provider = provider

        if not self._provider.authenticate_client(client_id, client_secret):
            return web.json_response(
                {"error": "invalid_client"},
                status=401,
                headers={"WWW-Authenticate": 'Basic realm="MCP Proxy OAuth"'},
            )

        grant_type = form.get("grant_type", "")
        if grant_type == "authorization_code":
            return await self._handle_authorization_code(form)
        if grant_type == "refresh_token":
            return await self._handle_refresh(form)
        return web.json_response(
            {"error": "unsupported_grant_type"}, status=400
        )

    async def _handle_authorization_code(self, form: dict) -> web.Response:
        code = str(form.get("code", ""))
        redirect_uri = str(form.get("redirect_uri", ""))
        code_verifier = str(form.get("code_verifier", ""))
        if not (code and redirect_uri and code_verifier):
            return web.json_response({"error": "invalid_request"}, status=400)
        if not self._provider.consume_code(code, redirect_uri, code_verifier):
            return web.json_response({"error": "invalid_grant"}, status=400)
        return web.json_response(
            {
                "access_token": self._provider.issue_access_token(),
                "token_type": "Bearer",
                "expires_in": ACCESS_TOKEN_TTL,
                "refresh_token": self._provider.issue_refresh_token(),
            }
        )

    async def _handle_refresh(self, form: dict) -> web.Response:
        refresh = str(form.get("refresh_token", ""))
        if not refresh or not self._provider.validate_refresh_token(refresh):
            return web.json_response({"error": "invalid_grant"}, status=400)
        return web.json_response(
            {
                "access_token": self._provider.issue_access_token(),
                "token_type": "Bearer",
                "expires_in": ACCESS_TOKEN_TTL,
                "refresh_token": self._provider.issue_refresh_token(),
            }
        )


# ---------------------------------------------------------------------------
# Helper used by the webhook handler to build the 401 challenge response
# ---------------------------------------------------------------------------


def build_unauthorized_response(
    request: web.Request, provider: OAuthProvider
) -> web.Response:
    """Build the 401 + WWW-Authenticate response that MCP clients use to
    discover the OAuth endpoints."""
    base = provider.base_url_for(request)
    metadata_url = f"{base}{OAUTH_BASE}/slot{provider.slot}/protected-resource"
    return web.Response(
        status=401,
        text="Unauthorized",
        headers={
            "WWW-Authenticate": (
                f'Bearer realm="MCP Proxy", '
                f'resource_metadata="{metadata_url}"'
            )
        },
    )
