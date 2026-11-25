"""Official API HTTP client with retry/backoff, OAuth token caching, and secret redaction."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Self

import httpx

logger = logging.getLogger(__name__)

_SENSITIVE_HEADER_KEYS = frozenset(
    {
        "authorization",
        "fk-affiliate-token",
        "x-api-key",
        "cookie",
        "set-cookie",
    }
)


def redact_headers(headers: Mapping[str, str]) -> dict[str, str]:
    """Return a copy of headers with sensitive token/key values redacted."""
    sanitized: dict[str, str] = {}
    for k, v in headers.items():
        if k.lower() in _SENSITIVE_HEADER_KEYS:
            sanitized[k] = "[REDACTED]"
        else:
            sanitized[k] = v
    return sanitized


def redact_secrets(text: str, secrets: Sequence[str | None]) -> str:
    """Replace any non-empty secret substrings in `text` with `[REDACTED]`."""
    result = text
    for sec in secrets:
        if sec and len(sec) >= 4:
            result = result.replace(sec, "[REDACTED]")
    return result


@dataclass
class CachedOAuthToken:
    """In-memory OAuth 2.0 access token with monotonic expiration timestamp."""

    access_token: str
    token_type: str
    expires_at_monotonic: float

    def is_valid(self, buffer_seconds: float = 60.0) -> bool:
        """Return True if the token has not expired (accounting for `buffer_seconds`)."""
        return time.monotonic() + buffer_seconds < self.expires_at_monotonic


class OfficialApiClient:
    """Resilient httpx wrapper for official affiliate/partner APIs (Cuelinks, Amazon, Flipkart)."""

    def __init__(
        self,
        service_name: str,
        base_url: str = "",
        default_headers: Mapping[str, str] | None = None,
        timeout_seconds: float = 20.0,
        max_retries: int = 3,
        backoff_base_seconds: float = 1.0,
        sleep_fn: Callable[[float], None] = time.sleep,
        transport: httpx.BaseTransport | None = None,
        secrets_to_redact: list[str | None] | None = None,
    ) -> None:
        self.service_name = service_name
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds
        self.max_retries = max_retries
        self.backoff_base_seconds = backoff_base_seconds
        self._sleep = sleep_fn
        self._secrets = [s for s in (secrets_to_redact or []) if s]
        self._oauth_cache: dict[str, CachedOAuthToken] = {}
        self._client = httpx.Client(
            base_url=self.base_url,
            headers=dict(default_headers or {}),
            timeout=timeout_seconds,
            transport=transport,
        )

    def close(self) -> None:
        """Close the underlying httpx client."""
        self._client.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def get_oauth_token(
        self,
        token_url: str,
        client_id: str,
        client_secret: str,
        scope: str | None = None,
        cache_key: str = "default",
        *,
        use_json: bool = False,
    ) -> str:
        """Fetch or reuse a cached OAuth 2.0 client_credentials token."""
        cached = self._oauth_cache.get(cache_key)
        if cached is not None and cached.is_valid():
            return cached.access_token

        if client_secret not in self._secrets:
            self._secrets.append(client_secret)

        data: dict[str, str] = {
            "grant_type": "client_credentials",
            "client_id": client_id,
            "client_secret": client_secret,
        }
        if scope:
            data["scope"] = scope

        if use_json:
            resp = self.request(
                "POST",
                token_url,
                json=data,
                headers={"Content-Type": "application/json", "Accept": "application/json"},
            )
        else:
            resp = self.request("POST", token_url, data=data)
        resp.raise_for_status()
        payload: dict[str, Any] = resp.json()
        access_token = str(payload["access_token"])
        expires_in = float(payload.get("expires_in", 3600))
        token_type = str(payload.get("token_type", "Bearer"))

        self._oauth_cache[cache_key] = CachedOAuthToken(
            access_token=access_token,
            token_type=token_type,
            expires_at_monotonic=time.monotonic() + expires_in,
        )
        return access_token

    def clear_oauth_token(self, cache_key: str = "default") -> None:
        """Invalidate a cached OAuth 2.0 token so the next `get_oauth_token` call refreshes it."""
        self._oauth_cache.pop(cache_key, None)

    def request(
        self,
        method: str,
        url: str,
        *,
        params: Mapping[str, Any] | None = None,
        json: Any | None = None,
        data: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> httpx.Response:
        """Perform an HTTP request with retry/backoff on 429/5xx and secret redaction."""
        last_exc: Exception | None = None

        for attempt in range(1, self.max_retries + 1):
            try:
                response = self._client.request(
                    method=method,
                    url=url,
                    params=params,
                    json=json,
                    data=data,
                    headers=headers,
                )
            except httpx.TransportError as exc:
                last_exc = exc
                safe_msg = redact_secrets(str(exc), self._secrets)
                logger.warning(
                    "[%s] Transport error on %s %s (attempt %d/%d): %s",
                    self.service_name,
                    method,
                    url,
                    attempt,
                    self.max_retries,
                    safe_msg,
                )
                if attempt < self.max_retries:
                    self._sleep(self.backoff_base_seconds * (2 ** (attempt - 1)))
                continue

            if response.status_code in (429, 500, 502, 503, 504) and attempt < self.max_retries:
                delay = self.backoff_base_seconds * (2 ** (attempt - 1))
                logger.warning(
                    "[%s] HTTP %d on %s %s (attempt %d/%d); retrying in %.2fs (headers=%s)",
                    self.service_name,
                    response.status_code,
                    method,
                    url,
                    attempt,
                    self.max_retries,
                    delay,
                    redact_headers(dict(response.request.headers)),
                )
                self._sleep(delay)
                continue

            return response

        assert last_exc is not None
        raise last_exc

    def get(
        self,
        url: str,
        *,
        params: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> httpx.Response:
        """Perform a GET request."""
        return self.request("GET", url, params=params, headers=headers)

    def post(
        self,
        url: str,
        *,
        json: Any | None = None,
        data: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> httpx.Response:
        """Perform a POST request."""
        return self.request("POST", url, json=json, data=data, headers=headers)

