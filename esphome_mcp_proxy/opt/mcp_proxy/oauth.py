"""OAuth 2.1 provider for the MCP Proxy.

Auto-approves the consent screen — client_id, client_secret and webhook URL
are already the access gate. No user interaction required.

Tokens are signed (HMAC-SHA256) with a per-install secret persisted at
/config/.mcp_proxy_oauth_secret.
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
from pathlib import Path
from typing import TypedDict
from urllib.parse import urlparse

from aiohttp import web
from homeassistant.components.http import HomeAssistantView
from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)

OAUTH_BASE = "/api/mcp_proxy/oauth"
AUTHORIZE_BASE = "/authorize"
TOKEN_BASE = "/token"
SECRET_FILE = Path("/config/.mcp_proxy_oauth_secret")

ACCESS_TOKEN_TTL = 60 * 60

_PROVIDER_REGISTRY: dict[str, "OAuthProvider"] = {}
REFRESH_TOKEN_TTL = 30 * 24 * 60 * 60
AUTH_CODE_TTL = 5 * 60
TOKEN_KIND_ACCESS = "access"
TOKEN_KIND_REFRESH = "refresh"

PKCE_VERIFIER_MIN = 43
PKCE_VERIFIER_MAX = 128
_PKCE_VERIFIER_RE = re.compile(r"^[A-Za-z0-9._~-]+$")
_PKCE_CHALLENGE_RE = re.compile(r"^[A-Za-z0-9_-]{43}$")

MAX_PENDING_CODES = 1000
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
    if SECRET_FILE.exists():
        data = SECRET_FILE.read_bytes()
        if len(data) >= 32:
            return data
    new_secret = secrets.token_bytes(32)
    SECRET_FILE.parent.mkdir(parents=True, exist_ok=True)
    SECRET_FILE.write_bytes(new_secret)
    try:
        SECRET_FILE.chmod(0o600)
    except OSError:
        pass
    return new_secret


def _is_valid_redirect_uri(redirect_uri: str) -> bool:
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


def _build_base_url(request: web.Request, public_base_url: str | None = None) -> str:
    if public_base_url:
        return public_base_url.rstrip("/")
    host = request.headers.get("X-Forwarded-Host") or request.headers.get("Host", "")
    scheme = request.headers.get("X-Forwarded-Proto", request.scheme)
    return f"{scheme}://{host}"


class OAuthProvider:
    def __init__(self, hass: HomeAssistant, client_id: str, client_secret: str,
                 webhook_id: str, signing_key: bytes, public_base_url: str | None = None,
                 slot: int = 1) -> None:
        if not client_id or len(client_id) < MIN_CLIENT_ID_LEN:
            raise ValueError(f"client_id must be at least {MIN_CLIENT_ID_LEN} characters")
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
        payload = {"kind": kind, "iat": now, "exp": now + ttl,
                   "jti": secrets.token_urlsafe(12), "cid": self._client_id}
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
        expected_sig = hmac.new(self._signing_key, body.encode("ascii"), hashlib.sha256).digest()
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
        return self.validate_access_token(header[7:].strip())

    def issue_code(self, redirect_uri: str, code_challenge: str) -> str | None:
        now = time.time()
        self._codes = {k: v for k, v in self._codes.items() if v["expires"] > now}
        if len(self._codes) >= MAX_PENDING_CODES:
            return None
        code = secrets.token_urlsafe(32)
        self._codes[code] = {"redirect_uri": redirect_uri, "code_challenge": code_challenge,
                             "expires": now + AUTH_CODE_TTL}
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
        return hmac.compare_digest(derived.encode("ascii"), entry["code_challenge"].encode("ascii"))

    def authenticate_client(self, client_id: str | None, client_secret: str | None) -> bool:
        if not client_id or not client_secret:
            return False
        return (hmac.compare_digest(client_id.encode(), self._client_id.encode())
                and hmac.compare_digest(client_secret.encode(), self._client_secret.encode()))


class ProtectedResourceMetadataView(HomeAssistantView):
    requires_auth = False
    cors_allowed = True

    def __init__(self, provider: "OAuthProvider") -> None:
        self._provider = provider
        self.url = f"{OAUTH_BASE}/slot{provider.slot}/protected-resource"
        self.name = f"mcp_proxy:oauth:slot{provider.slot}:protected-resource"

    async def get(self, request: web.Request) -> web.Response:
        base = self._provider.base_url_for(request)
        return web.json_response({
            "resource": self._provider.resource_url(base),
            "authorization_servers": [self._provider.authorization_server_url(base)],
            "bearer_methods_supported": ["header"],
            "resource_documentation": "https://github.com/eragon02424/Home-Assistant",
        })


class AuthorizationServerMetadataView(HomeAssistantView):
    requires_auth = False
    cors_allowed = True

    def __init__(self, provider: "OAuthProvider") -> None:
        self._provider = provider
        self.url = f"{OAUTH_BASE}/slot{provider.slot}/authorization-server"
        self.name = f"mcp_proxy:oauth:slot{provider.slot}:authorization-server"

    async def get(self, request: web.Request) -> web.Response:
        base = self._provider.base_url_for(request)
        as_url = self._provider.authorization_server_url(base)
        return web.json_response({
            "issuer": as_url,
            "authorization_endpoint": f"{base}{AUTHORIZE_BASE}",
            "token_endpoint": f"{base}{TOKEN_BASE}",
            "response_types_supported": ["code"],
            "grant_types_supported": ["authorization_code", "refresh_token"],
            "code_challenge_methods_supported": ["S256"],
            "token_endpoint_auth_methods_supported": ["client_secret_basic", "client_secret_post"],
        })


class AuthorizeView(HomeAssistantView):
    """OAuth /authorize endpoint — auto-approves without consent screen."""

    requires_auth = False

    def __init__(self, provider: "OAuthProvider") -> None:
        self._provider = provider
        self.url = f"{AUTHORIZE_BASE}"
        self.name = f"mcp_proxy:oauth:slot{provider.slot}:authorize"

    @staticmethod
    def _redirect_with(redirect_uri: str, **params: str) -> web.Response:
        import yarl
        url = yarl.URL(redirect_uri).update_query(params)
        return web.Response(status=302, headers={"Location": str(url)})

    async def get(self, request: web.Request) -> web.Response:
        params = request.query
        client_id = params.get("client_id", "")
        redirect_uri = params.get("redirect_uri", "")
        state = params.get("state", "")
        code_challenge = params.get("code_challenge", "")
        code_challenge_method = params.get("code_challenge_method", "")
        response_type = params.get("response_type", "")

        provider = _PROVIDER_REGISTRY.get(client_id)
        if provider is not None:
            self._provider = provider

        err = self._validate_authorize_params(
            response_type=response_type, client_id=client_id,
            redirect_uri=redirect_uri, code_challenge=code_challenge,
            code_challenge_method=code_challenge_method,
        )
        if err is not None:
            return err

        # Auto-approve: issue code immediately without consent screen
        code = self._provider.issue_code(redirect_uri, code_challenge)
        if code is None:
            return self._redirect_with(redirect_uri, error="temporarily_unavailable", state=state)
        _LOGGER.debug("MCP Proxy OAuth: auto-approved for client %s", self._provider.client_id_masked())
        return self._redirect_with(redirect_uri, code=code, state=state)

    async def post(self, request: web.Request) -> web.Response:
        """POST kept for backwards compatibility — also auto-approves."""
        data = await request.post()
        client_id = str(data.get("client_id", ""))
        redirect_uri = str(data.get("redirect_uri", ""))
        state = str(data.get("state", ""))
        code_challenge = str(data.get("code_challenge", ""))

        provider = _PROVIDER_REGISTRY.get(client_id)
        if provider is not None:
            self._provider = provider

        err = self._validate_authorize_params(
            response_type="code", client_id=client_id,
            redirect_uri=redirect_uri, code_challenge=code_challenge,
            code_challenge_method="S256",
        )
        if err is not None:
            return err

        code = self._provider.issue_code(redirect_uri, code_challenge)
        if code is None:
            return self._redirect_with(redirect_uri, error="temporarily_unavailable", state=state)
        return self._redirect_with(redirect_uri, code=code, state=state)

    def _validate_authorize_params(self, *, response_type: str, client_id: str,
                                   redirect_uri: str, code_challenge: str,
                                   code_challenge_method: str) -> web.Response | None:
        if response_type != "code":
            return web.Response(status=400, text="unsupported_response_type")
        if code_challenge_method != "S256":
            return web.Response(status=400, text="invalid code_challenge_method (S256 required)")
        if not _PKCE_CHALLENGE_RE.match(code_challenge):
            return web.Response(status=400, text="invalid code_challenge (must be 43-char base64url)")
        if client_id != self._provider.client_id:
            return web.Response(status=400, text="invalid client_id")
        if not _is_valid_redirect_uri(redirect_uri):
            return web.Response(status=400, text="redirect_uri must be an https:// URL with a host")
        return None


class TokenView(HomeAssistantView):
    requires_auth = False
    cors_allowed = True

    def __init__(self, provider: "OAuthProvider") -> None:
        self._provider = provider
        self.url = f"{TOKEN_BASE}"
        self.name = f"mcp_proxy:oauth:slot{provider.slot}:token"

    @staticmethod
    def _extract_client_creds(request: web.Request, form: dict) -> tuple[str | None, str | None]:
        header = request.headers.get("Authorization", "")
        if header.lower().startswith("basic "):
            try:
                decoded = base64.b64decode(header[6:].strip(), validate=True).decode("utf-8")
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

        provider = _PROVIDER_REGISTRY.get(client_id or "")
        if provider is not None:
            self._provider = provider

        if not self._provider.authenticate_client(client_id, client_secret):
            return web.json_response({"error": "invalid_client"}, status=401,
                                     headers={"WWW-Authenticate": 'Basic realm="MCP Proxy OAuth"'})

        grant_type = form.get("grant_type", "")
        if grant_type == "authorization_code":
            return await self._handle_authorization_code(form)
        if grant_type == "refresh_token":
            return await self._handle_refresh(form)
        return web.json_response({"error": "unsupported_grant_type"}, status=400)

    async def _handle_authorization_code(self, form: dict) -> web.Response:
        code = str(form.get("code", ""))
        redirect_uri = str(form.get("redirect_uri", ""))
        code_verifier = str(form.get("code_verifier", ""))
        if not (code and redirect_uri and code_verifier):
            return web.json_response({"error": "invalid_request"}, status=400)
        if not self._provider.consume_code(code, redirect_uri, code_verifier):
            return web.json_response({"error": "invalid_grant"}, status=400)
        return web.json_response({
            "access_token": self._provider.issue_access_token(),
            "token_type": "Bearer",
            "expires_in": ACCESS_TOKEN_TTL,
            "refresh_token": self._provider.issue_refresh_token(),
        })

    async def _handle_refresh(self, form: dict) -> web.Response:
        refresh = str(form.get("refresh_token", ""))
        if not refresh or not self._provider.validate_refresh_token(refresh):
            return web.json_response({"error": "invalid_grant"}, status=400)
        return web.json_response({
            "access_token": self._provider.issue_access_token(),
            "token_type": "Bearer",
            "expires_in": ACCESS_TOKEN_TTL,
            "refresh_token": self._provider.issue_refresh_token(),
        })


def build_unauthorized_response(request: web.Request, provider: OAuthProvider) -> web.Response:
    base = provider.base_url_for(request)
    metadata_url = f"{base}{OAUTH_BASE}/slot{provider.slot}/protected-resource"
    return web.Response(status=401, text="Unauthorized",
                        headers={"WWW-Authenticate": f'Bearer realm="MCP Proxy", resource_metadata="{metadata_url}"'})
