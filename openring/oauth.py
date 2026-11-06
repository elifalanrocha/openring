"""OAuth 2.0 for AI provider subscriptions (Claude Code, Codex, xAI Grok, OpenRouter).

OpenRing providers that have OAuth:
- openrouter: PKCE flow, swaps code for API key (keys.exchange_openrouter_code)
- xai: Grok OAuth (api.x.ai) via device code flow (RFC 8628), token stored encrypted
- anthropic: Claude Code OAuth (claude.ai) via PKCE
- openai: Codex OAuth (chatgpt.com / platform) via PKCE

Storage: tokens encrypted with Fernet, key from OAUTH_ENCRYPTION_KEY env or ephemeral (warns).
"""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
from datetime import datetime, timedelta
from enum import Enum
from typing import Any, Dict, Optional, Tuple
from urllib.parse import urlencode

import httpx
from cryptography.fernet import Fernet, InvalidToken

from .store import Store


class OAuthError(Exception):
    """Base exception for OAuth-related errors."""
    pass


class OAuthProvider(Enum):
    """Supported OAuth providers (only OpenRing LLM providers)."""
    OPENROUTER = "openrouter"
    XAI = "xai"
    ANTHROPIC = "anthropic"
    OPENAI = "openai"


SUPPORTED_PROVIDERS = {p.value for p in OAuthProvider}


class OAuthToken:
    """OAuth 2.0 token container."""

    def __init__(
        self,
        provider: OAuthProvider,
        access_token: str,
        token_type: str,
        expires_at: datetime,
        refresh_token: Optional[str] = None,
        scope: Optional[str] = None,
        extra_data: Optional[Dict[str, Any]] = None,
    ):
        self.provider = provider
        self.access_token = access_token
        self.token_type = token_type
        self.expires_at = expires_at
        self.refresh_token = refresh_token
        self.scope = scope
        self.extra_data = extra_data or {}

    def is_expired(self) -> bool:
        """Check if the token is expired (with 1min clock skew)."""
        return datetime.now() >= self.expires_at - timedelta(seconds=60)


# Provider configs from OmniRoute public CLI credentials
# These are public OAuth client IDs for native/installed apps using PKCE (RFC 8252)
# Not secrets — can be overridden via env vars
DEFAULT_CLIENT_IDS = {
    "openrouter": None,  # OpenRouter uses its own custom flow (see keys.exchange_openrouter_code)
    # Grok Build public client ID (from OmniRoute publicCreds.ts grok_id)
    "xai": "b1a00492-073a-47ea-816f-4c329264a828",
    "anthropic": "9d1c250a-e61b-44d9-88ed-5944d1962f5e",  # Claude Code public client ID
    "openai": "app_EMoamEEZ73f0CkXaXp7hrann",  # Codex public client ID
}

# xAI uses device code flow (RFC 8628) for Grok Build
# Other providers use Authorization Code + PKCE
PROVIDER_CONFIGS: Dict[str, Dict[str, Any]] = {
    "openrouter": {
        # OpenRouter has custom PKCE exchange handled in keys.py
        # Dashboard uses `signInOpenRouter()` inside Connect dialog (not /oauth/* endpoints)
        "authorize_url": "https://openrouter.ai/auth",
        "token_url": "https://openrouter.ai/api/v1/auth/token",
        "flow": "pkce",
        "default_scope": "openid profile email",
    },
    "xai": {
        # Grok Build (xAI) — PKCE flow (OmniRoute XAI_OAUTH_CONFIG / GROK_BUILD_OAUTH_CONFIG)
        # Same auth.x.ai oauth2 endpoints and public client_id as Grok Build CLI
        "authorize_url": "https://auth.x.ai/oauth2/authorize",
        "token_url": "https://auth.x.ai/oauth2/token",
        "flow": "pkce",
        "default_scope": "openid profile email offline_access grok-cli:access",
        "client_id": DEFAULT_CLIENT_IDS["xai"],
        "referrer": "cli-proxy-api",
        "plan": "generic",
    },
    "anthropic": {
        # Claude Code OAuth (Authorization Code + PKCE) - OmniRoute CLAUDE_CONFIG
        "authorize_url": "https://claude.ai/oauth/authorize",
        "token_url": "https://api.anthropic.com/v1/oauth/token",
        "flow": "pkce",
        "default_scope": "org:create_api_key user:profile user:inference user:sessions:claude_code user:mcp_servers",
        "client_id": DEFAULT_CLIENT_IDS["anthropic"],
        "redirect_uri": "https://platform.claude.com/oauth/code/callback",
    },
    "openai": {
        # OpenAI Codex OAuth (Authorization Code + PKCE) - OmniRoute CODEX_CONFIG
        "authorize_url": "https://auth.openai.com/oauth/authorize",
        "token_url": "https://auth.openai.com/oauth/token",
        "flow": "pkce",
        "default_scope": "openid profile email offline_access",
        "client_id": DEFAULT_CLIENT_IDS["openai"],
        "extra_params": {
            "id_token_add_organizations": "true",
            "codex_cli_simplified_flow": "true",
            "originator": "codex_cli_rs",
            "prompt": "login",
        },
    },
}


class OAuthManager:
    """Manage OAuth authentication for OpenRing providers."""

    def __init__(self, encryption_key: bytes, store: Store):
        self.encryption_key = encryption_key
        self.store = store
        self._fernet = Fernet(encryption_key)

    def is_provider_supported(self, provider: str) -> bool:
        """Check if a provider is supported."""
        return provider in SUPPORTED_PROVIDERS

    def get_client_id(self, provider: str, settings: Any = None) -> str:
        """Get client ID for provider, checking env override first."""
        config = PROVIDER_CONFIGS.get(provider, {})
        if settings:
            env_name = config.get("client_id_env") or f"{provider.upper()}_CLIENT_ID"
            env_val = getattr(settings, env_name.lower(), "")
            if env_val:
                return env_val
        return config.get("client_id") or DEFAULT_CLIENT_IDS.get(provider, "")

    def generate_pkce_pair(self) -> Tuple[str, str]:
        """Generate PKCE verifier and challenge (RFC 7636, S256)."""
        # verifier: 43-128 chars, URL-safe, no padding (secrets.token_urlsafe does this)
        verifier = secrets.token_urlsafe(32)  # ~43 chars
        challenge = hashlib.sha256(verifier.encode()).digest()
        challenge_b64 = base64.urlsafe_b64encode(challenge).decode().rstrip("=")
        return verifier, challenge_b64

    def verify_pkce(self, verifier: str, challenge: str) -> bool:
        """Verify PKCE pair (for testing/validation)."""
        computed = hashlib.sha256(verifier.encode()).digest()
        computed_b64 = base64.urlsafe_b64encode(computed).decode().rstrip("=")
        return secrets.compare_digest(computed_b64, challenge)

    def generate_authorization_url(
        self,
        provider: OAuthProvider,
        client_id: str,
        redirect_uri: str,
        scope: str = "",
        state: str = "",
        code_challenge: str = "",
    ) -> str:
        """Generate OAuth authorization URL."""
        config = PROVIDER_CONFIGS.get(provider.value)
        if not config:
            raise OAuthError(f"OAuth not supported for provider '{provider.value}'")

        auth_url = config["authorize_url"]
        params = {
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": scope or config["default_scope"],
            "state": state,
        }

        if code_challenge:
            params["code_challenge"] = code_challenge
            params["code_challenge_method"] = "S256"

        # xAI Grok requires these params (from OmniRoute XAI_OAUTH_CONFIG / GROK_BUILD_OAUTH_CONFIG)
        if provider.value == "xai":
            params["nonce"] = secrets.token_hex(16)
            params["plan"] = config.get("plan", "generic")
            params["referrer"] = config.get("referrer", "cli-proxy-api")

        # OpenAI Codex requires extra_params (from OmniRoute CODEX_CONFIG)
        if provider.value == "openai" and config.get("extra_params"):
            params.update(config["extra_params"])

        query_string = urlencode(params)
        return f"{auth_url}?{query_string}"

    async def exchange_code(
        self,
        provider: OAuthProvider,
        client_id: str,
        client_secret: str,
        authorization_code: str,
        redirect_uri: str,
        code_verifier: str,
    ) -> OAuthToken:
        """Exchange authorization code for access token (PKCE flow)."""
        config = PROVIDER_CONFIGS.get(provider.value)
        if not config or config.get("flow") != "pkce":
            raise OAuthError(f"PKCE flow not supported for provider '{provider.value}'")

        token_url = config["token_url"]
        headers = {"Content-Type": "application/x-www-form-urlencoded"}

        data = {
            "grant_type": "authorization_code",
            "code": authorization_code,
            "redirect_uri": redirect_uri,
            "client_id": client_id,
            "code_verifier": code_verifier,
        }

        # Only include client_secret if provided (PKCE public clients may omit it)
        if client_secret:
            data["client_secret"] = client_secret

        async with httpx.AsyncClient(timeout=30.0) as client:
            try:
                response = await client.post(token_url, data=data, headers=headers)
            except httpx.TimeoutException as exc:
                raise OAuthError(f"Timeout exchanging code for token: {exc}") from exc
            except httpx.RequestError as exc:
                raise OAuthError(f"Could not reach OAuth server: {exc}") from exc

            if response.status_code != 200:
                raise OAuthError(f"OAuth token exchange failed ({response.status_code}): {response.text[:200]}")

            try:
                token_data = response.json()
            except json.JSONDecodeError as exc:
                raise OAuthError("OAuth server returned invalid JSON") from exc

            expires_at = datetime.now() + timedelta(
                seconds=token_data.get("expires_in", 3600)
            )

            return OAuthToken(
                provider=provider,
                access_token=token_data["access_token"],
                token_type=token_data.get("token_type", "Bearer"),
                expires_at=expires_at,
                refresh_token=token_data.get("refresh_token"),
                scope=token_data.get("scope", ""),
                extra_data=token_data,
            )

    async def refresh_token(
        self,
        provider: OAuthProvider,
        client_id: str,
        client_secret: str,
        refresh_token: str,
    ) -> OAuthToken:
        """Refresh access token using refresh token."""
        config = PROVIDER_CONFIGS.get(provider.value)
        if not config:
            raise OAuthError(f"OAuth not supported for provider '{provider.value}'")

        token_url = config["token_url"]
        headers = {"Content-Type": "application/x-www-form-urlencoded"}

        data = {
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": client_id,
            "client_secret": client_secret,
        }

        async with httpx.AsyncClient(timeout=30.0) as client:
            try:
                response = await client.post(token_url, data=data, headers=headers)
            except httpx.TimeoutException as exc:
                raise OAuthError(f"Timeout refreshing token: {exc}") from exc
            except httpx.RequestError as exc:
                raise OAuthError(f"Could not reach OAuth server: {exc}") from exc

            if response.status_code != 200:
                raise OAuthError(f"OAuth token refresh failed ({response.status_code}): {response.text[:200]}")

            try:
                token_data = response.json()
            except json.JSONDecodeError as exc:
                raise OAuthError("OAuth server returned invalid JSON") from exc

            expires_at = datetime.now() + timedelta(
                seconds=token_data.get("expires_in", 3600)
            )

            return OAuthToken(
                provider=provider,
                access_token=token_data["access_token"],
                token_type=token_data.get("token_type", "Bearer"),
                expires_at=expires_at,
                refresh_token=token_data.get("refresh_token", refresh_token),
                scope=token_data.get("scope", ""),
                extra_data=token_data,
            )

    def store_token(self, provider: OAuthProvider, token: OAuthToken) -> None:
        """Store OAuth token (encrypted) in settings table."""
        encrypted = self._encrypt_token(token)
        # Use get_setting/set_setting key-value store (no new collection needed)
        key = f"oauth_token:{provider.value}"
        self.store.set_setting(key, encrypted)

    def get_token(self, provider: OAuthProvider) -> Optional[OAuthToken]:
        """Get OAuth token for provider."""
        key = f"oauth_token:{provider.value}"
        encrypted = self.store.get_setting(key)

        if not encrypted:
            return None

        try:
            token = self._decrypt_token(encrypted)

            if token.is_expired():
                self.remove_token(provider)
                return None

            return token
        except (InvalidToken, KeyError, ValueError, json.JSONDecodeError):
            # Corrupted or wrong key - remove and signal missing
            self.remove_token(provider)
            return None

    def remove_token(self, provider: OAuthProvider) -> None:
        """Remove OAuth token for provider."""
        key = f"oauth_token:{provider.value}"
        self.store.set_setting(key, None)

    def _encrypt_token(self, token: OAuthToken) -> str:
        """Encrypt OAuth token with Fernet."""
        token_dict = {
            "provider": token.provider.value,
            "access_token": token.access_token,
            "token_type": token.token_type,
            "expires_at": token.expires_at.isoformat(),
            "refresh_token": token.refresh_token,
            "scope": token.scope,
            "extra_data": token.extra_data,
        }
        token_json = json.dumps(token_dict)
        encrypted = self._fernet.encrypt(token_json.encode())
        return encrypted.decode()

    def _decrypt_token(self, encrypted_token: str) -> OAuthToken:
        """Decrypt OAuth token with Fernet."""
        decrypted = self._fernet.decrypt(encrypted_token.encode())
        token_dict = json.loads(decrypted.decode())

        return OAuthToken(
            provider=OAuthProvider(token_dict["provider"]),
            access_token=token_dict["access_token"],
            token_type=token_dict["token_type"],
            expires_at=datetime.fromisoformat(token_dict["expires_at"]),
            refresh_token=token_dict.get("refresh_token"),
            scope=token_dict.get("scope"),
            extra_data=token_dict.get("extra_data", {}),
        )
