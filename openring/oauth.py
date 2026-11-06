"""OAuth 2.0 authentication support for AI provider APIs.

This module provides OAuth 2.0 authentication infrastructure for connecting to third-party AI service providers
like Google, Microsoft, GitHub, etc. It includes token management, PKCE flow, secure redirects, and provider-specific implementations.
"""

from __future__ import annotations

import hashlib
import json
import hmac
import secrets
import time
from datetime import datetime, timedelta
from enum import Enum
from typing import Any, Dict, Optional, Tuple
from urllib.parse import urlencode

from cryptography.fernet import Fernet

import httpx

from .config import Settings, get_settings
from .store import Store


class OAuthError(Exception):
    """Base exception for OAuth-related errors."""
    pass


class OAuthProvider(Enum):
    """Supported OAuth providers."""
    GOOGLE = "google"
    MICROSOFT = "microsoft"
    GITHUB = "github"
    FACEBOOK = "facebook"
    APPLE = "apple"
    LINKEDIN = "linkedin"
    TWITTER = "twitter"
    OPENAI = "openai"


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
        """Check if the token is expired."""
        return datetime.now() >= self.expires_at


class OAuthManager:
    """Manage OAuth authentication for providers."""

    def __init__(self, encryption_key: bytes):
        self.encryption_key = encryption_key
        self.settings = get_settings()

    def is_provider_supported(self, provider: str) -> bool:
        """Check if a provider is supported."""
        try:
            OAuthProvider(provider)
            return True
        except ValueError:
            return False

    def generate_pkce_pair(self) -> Tuple[str, str]:
        """Generate PKCE verifier and challenge."""
        verifier = secrets.token_urlsafe(32)
        challenge = hashlib.sha256(verifier.encode()).digest().hex()
        return verifier, challenge

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
        provider_urls = {
            OAuthProvider.GOOGLE: "https://accounts.google.com/o/oauth2/v2/auth",
            OAuthProvider.MICROSOFT: "https://login.microsoftonline.com/common/oauth2/v2.0/authorize",
            OAuthProvider.GITHUB: "https://github.com/login/oauth/authorize",
            OAuthProvider.FACEBOOK: "https://www.facebook.com/v18.0/dialog/oauth",
            OAuthProvider.APPLE: "https://appleid.apple.com/auth/authorize",
            OAuthProvider.LINKEDIN: "https://www.linkedin.com/oauth/v2/authorization",
            OAuthProvider.TWITTER: "https://api.twitter.com/oauth/2.0/authorize",
            OAuthProvider.OPENAI: "https://platform.openai.com/oauth/authorize",
        }

        auth_url = provider_urls.get(provider, "https://example.com/oauth/authorize")

        params = {
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": scope,
            "state": state,
        }

        if code_challenge:
            params["code_challenge"] = code_challenge
            params["code_challenge_method"] = "S256"

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
        """Exchange authorization code for access token."""
        token_urls = {
            OAuthProvider.GOOGLE: "https://oauth2.googleapis.com/token",
            OAuthProvider.MICROSOFT: "https://login.microsoftonline.com/common/oauth2/v2.0/token",
            OAuthProvider.GITHUB: "https://github.com/login/oauth/access_token",
            OAuthProvider.FACEBOOK: "https://graph.facebook.com/v18.0/oauth/access_token",
            OAuthProvider.APPLE: "https://appleid.apple.com/auth/token",
            OAuthProvider.LINKEDIN: "https://www.linkedin.com/oauth/v2/accessToken",
            OAuthProvider.TWITTER: "https://api.twitter.com/2/oauth2/token",
            OAuthProvider.OPENAI: "https://platform.openai.com/oauth/token",
        }

        token_url = token_urls.get(provider, "https://example.com/oauth/token")

        headers = {"Content-Type": "application/x-www-form-urlencoded"}

        data = {
            "grant_type": "authorization_code",
            "code": authorization_code,
            "redirect_uri": redirect_uri,
            "client_id": client_id,
            "client_secret": client_secret,
            "code_verifier": code_verifier,
        }

        async with httpx.AsyncClient() as client:
            response = await client.post(token_url, data=data, headers=headers)

            if response.status_code != 200:
                raise OAuthError(f"OAuth token exchange failed: {response.text}")

            token_data = response.json()

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

    def store_token(self, provider: OAuthProvider, token: OAuthToken) -> None:
        """Store OAuth token (encrypted)."""
        store = Store(self.settings.db_path)
        encrypted_token = self._encrypt_token(token)
        store.set(
            "oauth_tokens",
            provider.value,
            {"token": encrypted_token, "expires_at": token.expires_at.isoformat()},
        )

    def get_token(self, provider: OAuthProvider) -> Optional[OAuthToken]:
        """Get OAuth token for provider."""
        store = Store(self.settings.db_path)
        token_data = store.get("oauth_tokens", provider.value)

        if not token_data:
            return None

        try:
            encrypted_token = token_data["token"]
            token = self._decrypt_token(encrypted_token)

            if token.is_expired():
                self.remove_token(provider)
                return None

            return token
        except Exception:
            return None

    def remove_token(self, provider: OAuthProvider) -> None:
        """Remove OAuth token for provider."""
        store = Store(self.settings.db_path)
        store.delete("oauth_tokens", provider.value)

    def _encrypt_token(self, token: OAuthToken) -> str:
        """Encrypt OAuth token."""
        f = Fernet(self.encryption_key)
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
        encrypted = f.encrypt(token_json.encode())
        return encrypted.decode()

    def _decrypt_token(self, encrypted_token: str) -> OAuthToken:
        """Decrypt OAuth token."""
        f = Fernet(self.encryption_key)
        decrypted = f.decrypt(encrypted_token.encode())
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