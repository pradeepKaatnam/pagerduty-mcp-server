"""Async HTTP client for PagerDuty REST API v2."""

from __future__ import annotations

import asyncio
import os
from typing import Any

import httpx

BASE_URL = "https://api.pagerduty.com"
MAX_PAGE_SIZE = 100
RETRYABLE_STATUS_CODES = {429, 502, 503, 504}


class PagerDutyAPIError(Exception):
    """Raised when a PagerDuty API request fails."""


class PagerDutyClient:
    """Generic async transport client for PagerDuty REST API v2."""

    def __init__(
        self,
        api_token: str | None = None,
        timeout: float = 15.0,
        max_retries: int = 3,
        backoff_base: float = 0.5,
    ) -> None:
        self._api_token = api_token
        self._max_retries = max_retries
        self._backoff_base = backoff_base
        self._client = httpx.AsyncClient(
            base_url=BASE_URL,
            timeout=timeout,
            headers={"Accept": "application/vnd.pagerduty+json;version=2"},
        )

    def _auth_header(self) -> dict[str, str]:
        token = (self._api_token or os.environ.get("PAGERDUTY_API_TOKEN", "")).strip()
        if not token:
            raise PagerDutyAPIError("PAGERDUTY_API_TOKEN environment variable is not set.")
        return {"Authorization": f"Token token={token}"}

    async def request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        headers = self._auth_header()
        clean_params = {k: v for k, v in (params or {}).items() if v is not None}

        for attempt in range(self._max_retries + 1):
            try:
                resp = await self._client.request(
                    method,
                    path,
                    params=clean_params,
                    json=json,
                    headers=headers,
                )
            except httpx.HTTPError as exc:
                if attempt < self._max_retries:
                    await asyncio.sleep(self._backoff_base * (2**attempt))
                    continue
                raise PagerDutyAPIError(f"Network error calling {path}: {exc}") from exc

            if resp.status_code in RETRYABLE_STATUS_CODES and attempt < self._max_retries:
                retry_after = resp.headers.get("Retry-After")
                try:
                    delay = float(retry_after) if retry_after is not None else self._backoff_base * (2**attempt)
                except ValueError:
                    delay = self._backoff_base * (2**attempt)
                await asyncio.sleep(min(max(delay, 0.0), 30.0))
                continue

            if resp.is_success:
                return resp.json() if resp.content else {}

            detail = ""
            try:
                err = resp.json().get("error", {})
                if isinstance(err, dict):
                    msg = err.get("message") or ""
                    errors = err.get("errors")
                    if isinstance(errors, list) and errors:
                        joined = "; ".join(str(x) for x in errors)
                        detail = f"{msg} ({joined})" if msg else joined
                    else:
                        detail = str(msg)
            except ValueError:
                detail = resp.text[:200].strip()

            suffix = f": {detail}" if detail else ""
            raise PagerDutyAPIError(f"PagerDuty API error (HTTP {resp.status_code}) on {path}{suffix}")

        raise PagerDutyAPIError(f"Retry limit exceeded on {path}.")

    async def get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        return await self.request("GET", path, params=params)

    async def get_page(
        self,
        path: str,
        key: str,
        params: dict[str, Any] | None = None,
        limit: int = 25,
        offset: int = 0,
    ) -> tuple[list[dict[str, Any]], bool]:
        query = {
            **(params or {}),
            "limit": max(1, min(limit, MAX_PAGE_SIZE)),
            "offset": max(0, offset),
        }
        data = await self.get(path, params=query)
        return data.get(key) or [], bool(data.get("more", False))

    async def get_all(
        self,
        path: str,
        key: str,
        params: dict[str, Any] | None = None,
        page_size: int = MAX_PAGE_SIZE,
        max_items: int = 500,
    ) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        offset = 0
        while len(items) < max_items:
            batch_size = min(page_size, max_items - len(items))
            page, has_more = await self.get_page(path, key, params=params, limit=batch_size, offset=offset)
            items.extend(page)
            if not has_more or not page:
                break
            offset += len(page)
        return items
