"""Recover missing usage from a saved Gemini Interactions API resource."""

from __future__ import annotations

import logging
import os
from typing import Any
from urllib.parse import quote

import google.auth
from google.auth.exceptions import GoogleAuthError
import httpx
from google.auth.transport.requests import Request as GoogleAuthRequest


LOGGER = logging.getLogger(__name__)
GEMINI_API_BASE_URLS = (
    "https://generativelanguage.googleapis.com/v1",
    "https://generativelanguage.googleapis.com/v1beta",
)
AUTH_SCOPES = ("https://www.googleapis.com/auth/cloud-platform",)


class GeminiInteractionUsageHydrator:
    """Read an interaction's final usage without delaying the client response."""

    def __init__(
        self,
        *,
        http_client: httpx.Client | None = None,
        timeout_seconds: float = 5.0,
    ) -> None:
        self._http_client = http_client
        self._timeout_seconds = timeout_seconds
        self._credentials: Any = None

    def retrieve_usage(self, interaction_id: str) -> dict[str, Any] | None:
        """Return raw provider usage if the stored interaction now exposes it."""
        interaction_id = interaction_id.strip()
        if not interaction_id:
            return None
        client = self._http_client or httpx.Client(
            timeout=httpx.Timeout(self._timeout_seconds, connect=2.0)
        )
        try:
            headers = self._auth_headers()
            response = None
            encoded_id = quote(interaction_id, safe="")
            for index, base_url in enumerate(GEMINI_API_BASE_URLS):
                response = client.get(
                    f"{base_url}/interactions/{encoded_id}", headers=headers
                )
                if (
                    response.status_code not in {400, 404}
                    or index == len(GEMINI_API_BASE_URLS) - 1
                ):
                    break
            if response is None:
                return None
            if response.status_code != 200:
                LOGGER.warning(
                    "Gemini interaction usage lookup returned HTTP %s.",
                    response.status_code,
                )
                return None
            payload = response.json()
            interaction = (
                payload.get("interaction", payload)
                if isinstance(payload, dict)
                else None
            )
            usage = interaction.get("usage") if isinstance(interaction, dict) else None
            return dict(usage) if isinstance(usage, dict) else None
        except (GoogleAuthError, httpx.RequestError, ValueError, TypeError) as exc:
            LOGGER.warning(
                "Gemini interaction usage lookup failed (%s).",
                type(exc).__name__,
            )
            return None
        finally:
            if self._http_client is None:
                client.close()

    def _auth_headers(self) -> dict[str, str]:
        api_key = os.getenv("GEMINI_API_KEY", "").strip()
        if api_key:
            return {"x-goog-api-key": api_key, "Accept": "application/json"}

        if self._credentials is None:
            self._credentials, _ = google.auth.default(scopes=AUTH_SCOPES)
        if not self._credentials.valid or not self._credentials.token:
            self._credentials.refresh(GoogleAuthRequest())
        return {
            "Authorization": f"Bearer {self._credentials.token}",
            "Accept": "application/json",
        }
