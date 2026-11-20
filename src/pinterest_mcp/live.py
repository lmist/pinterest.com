"""Live backend: talks to pinterest.com.

Public resources work anonymously; the configured session cookie (when present)
is sent along so the signed-in surfaces answer too.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any
from urllib.parse import urlparse

import httpx

from . import api, normalize
from .api import AuthRequiredError, PinterestError, Request, unwrap
from .backend import BackendError
from .config import Settings

IMAGE_HOSTS = ("i.pinimg.com", "s.pinimg.com", "v1.pinimg.com", "pinimg.com")

_MIME_FORMATS = {
    "image/jpeg": "jpeg",
    "image/jpg": "jpeg",
    "image/png": "png",
    "image/gif": "gif",
    "image/webp": "webp",
}


class NotFoundError(BackendError):
    """The requested object does not exist or is not visible to this account."""


# Pinterest's edge answers 503 "upstream connect error" for cold resources and
# 429 when hammered; both are worth retrying.
RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})
RETRY_ATTEMPTS = 3


def _backoff(attempt: int, retry_after: str | None) -> float:
    if retry_after:
        try:
            return min(float(retry_after), 10.0)
        except ValueError:
            pass
    return 0.5 * 2 ** (attempt - 1)


class LiveBackend:
    mode = "live"

    def __init__(
        self, settings: Settings, client: httpx.AsyncClient | None = None
    ) -> None:
        self._settings = settings
        # accept-encoding is deliberately left to httpx so responses are
        # transparently decompressed.
        self._client = client or httpx.AsyncClient(
            timeout=settings.timeout,
            follow_redirects=True,
        )
        self._lock = asyncio.Lock()
        self._last_request = 0.0

    async def aclose(self) -> None:
        await self._client.aclose()

    # ---------------------------------------------------------------- transport

    async def _throttle(self) -> None:
        interval = self._settings.min_interval
        if interval <= 0:
            return
        async with self._lock:
            wait = self._last_request + interval - time.monotonic()
            if wait > 0:
                await asyncio.sleep(wait)
            self._last_request = time.monotonic()

    async def _fetch(self, request: Request) -> dict[str, Any]:
        """Perform one resource call and return the raw JSON payload.

        Transient edge failures (Pinterest's frontend regularly answers 503
        ``upstream connect error`` on cold resources, and rate limits with 429)
        are retried a couple of times with backoff.
        """
        url = request.url(self._settings.base_url)
        headers = request.headers(
            origin=self._settings.base_url,
            app_version=self._settings.app_version,
            user_agent=self._settings.user_agent,
            cookie=self._settings.cookie,
            csrf_token=self._settings.csrf_token,
        )
        attempt = 0
        while True:
            attempt += 1
            await self._throttle()
            try:
                response = await self._client.get(url, headers=headers)
            except httpx.HTTPError as exc:
                if attempt < RETRY_ATTEMPTS and isinstance(exc, httpx.TransportError):
                    await asyncio.sleep(_backoff(attempt, None))
                    continue
                raise BackendError(f"request to Pinterest failed: {exc}") from exc

            status = response.status_code
            if status in RETRY_STATUSES and attempt < RETRY_ATTEMPTS:
                await asyncio.sleep(
                    _backoff(attempt, response.headers.get("retry-after"))
                )
                continue

            if status in (401, 403):
                raise AuthRequiredError(
                    f"Pinterest returned HTTP {status}. {api.AUTH_HINT}"
                )
            if status == 404:
                raise NotFoundError(
                    f"Pinterest returned HTTP 404 for {request.resource}"
                )
            if status >= 400:
                snippet = response.text[:200] if response.text else ""
                raise PinterestError(f"Pinterest returned HTTP {status}: {snippet}")
            try:
                return response.json()
            except ValueError as exc:
                raise PinterestError(
                    f"Pinterest returned {status} with non-JSON body "
                    f"(cookie may be expired): {response.text[:200]}"
                ) from exc

    async def _data(self, request: Request) -> Any:
        return unwrap(await self._fetch(request))

    # ------------------------------------------------------------------- tools

    async def search_pins(
        self,
        *,
        query: str,
        scope: str = "pins",
        page_size: int | None = None,
        bookmark: str | None = None,
    ) -> dict[str, Any]:
        request = api.search(
            query=query, scope=scope, page_size=page_size, bookmark=bookmark
        )
        payload = await self._fetch(request)
        data = unwrap(payload)
        results = data.get("results") if isinstance(data, dict) else None
        out = normalize.page(results, bookmark=api.bookmark_of(payload))
        out["query"] = query
        out["scope"] = scope
        return out

    async def search_suggestions(self, *, term: str, count: int = 10) -> dict[str, Any]:
        request = api.search_suggestions(term=term, count=count)
        data = await self._data(request)
        items = data.get("items") if isinstance(data, dict) else None
        suggestions = [
            s for s in (normalize.suggestion_record(i) for i in items or []) if s
        ]
        return {"term": term, "count": len(suggestions), "suggestions": suggestions}

    async def get_pin(self, *, pin_id: str) -> dict[str, Any]:
        data = await self._data(api.pin(pin_id=pin_id))
        record = normalize.pin_record(data or {}, detailed=True)
        if not record:
            raise NotFoundError(f"pin {pin_id} not found")
        return record

    async def get_related_pins(
        self, *, pin_id: str, query: str | None = None, page_size: int = 12
    ) -> dict[str, Any]:
        data = await self._data(
            api.related_pins(pin_id=pin_id, query=query, page_size=page_size)
        )
        modules = normalize.related_modules(data)
        return {
            "pin_id": pin_id,
            "count": sum(len(m["pins"]) for m in modules),
            "modules": modules,
        }

    async def get_user(self, *, username: str) -> dict[str, Any]:
        data = await self._data(api.user(username=username))
        record = normalize.user_record(data or {})
        if not record:
            raise NotFoundError(f"user {username!r} not found")
        return record

    async def get_user_boards(
        self, *, username: str, page_size: int = 25
    ) -> dict[str, Any]:
        data = await self._data(api.user_boards(username=username, page_size=page_size))
        boards = normalize.board_list(data)
        return {"username": username, "count": len(boards), "boards": boards}

    async def get_board(self, *, username: str, slug: str) -> dict[str, Any]:
        data = await self._data(api.board(username=username, slug=slug))
        record = normalize.board_record(data or {})
        if not record:
            raise NotFoundError(f"board {username}/{slug} not found")
        pins = normalize.pin_list((data or {}).get("pins"))
        if pins:
            record["pins"] = pins
        return record

    async def get_board_pins(
        self,
        *,
        board_id: str,
        board_url: str | None = None,
        page_size: int = 25,
        bookmark: str | None = None,
    ) -> dict[str, Any]:
        request = api.board_pins(
            board_id=board_id,
            board_url=board_url,
            page_size=page_size,
            bookmark=bookmark,
        )
        payload = await self._fetch(request)
        data = unwrap(payload)
        items = data if isinstance(data, list) else (data or {}).get("results")
        out = normalize.page(items, bookmark=api.bookmark_of(payload))
        out["board_id"] = board_id
        return out

    async def get_pin_comments(
        self, *, pin_id: str, page_size: int = 10, bookmark: str | None = None
    ) -> dict[str, Any]:
        request = api.comments(pin_id=pin_id, page_size=page_size, bookmark=bookmark)
        payload = await self._fetch(request)
        data = unwrap(payload)
        items = data if isinstance(data, list) else (data or {}).get("results")
        comments = normalize.comment_list(items)
        out: dict[str, Any] = {
            "pin_id": pin_id,
            "count": len(comments),
            "comments": comments,
        }
        found = api.bookmark_of(payload)
        if found:
            out["bookmark"] = found
        return out

    async def get_home_feed(
        self, *, page_size: int | None = None, bookmark: str | None = None
    ) -> dict[str, Any]:
        request = api.home_feed(page_size=page_size, bookmark=bookmark)
        payload = await self._fetch(request)
        data = unwrap(payload)
        items = data if isinstance(data, list) else (data or {}).get("results")
        return normalize.page(items, bookmark=api.bookmark_of(payload))

    def status(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "base_url": self._settings.base_url,
            "authenticated": self._settings.authenticated,
            "min_interval_seconds": self._settings.min_interval,
            "note": (
                "Public search and retrieval need no credentials. Set PINTEREST_COOKIE to "
                "reach the signed-in surfaces (scope='my_pins', get_home_feed, private boards)."
                if not self._settings.authenticated
                else "Requests are sent with the configured Pinterest session cookie."
            ),
        }


async def fetch_image(
    settings: Settings, client: httpx.AsyncClient, url: str
) -> tuple[bytes, str]:
    """Download an image from Pinterest's CDN. Returns ``(bytes, format)``."""
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if parsed.scheme not in ("http", "https") or not any(
        host == allowed or host.endswith("." + allowed) for allowed in IMAGE_HOSTS
    ):
        raise BackendError(
            "only Pinterest image hosts are allowed "
            f"({', '.join(IMAGE_HOSTS)}); refusing to fetch {url!r}"
        )
    try:
        response = await client.get(url)
    except httpx.HTTPError as exc:
        raise BackendError(f"image download failed: {exc}") from exc
    if response.status_code >= 400:
        raise BackendError(f"image download failed with HTTP {response.status_code}")
    data = response.content
    if len(data) > settings.max_image_bytes:
        raise BackendError(
            f"image is {len(data)} bytes, above PINTEREST_MAX_IMAGE_BYTES "
            f"({settings.max_image_bytes})"
        )
    mime = response.headers.get("content-type", "").split(";")[0].strip().lower()
    fmt = _MIME_FORMATS.get(mime, "jpeg")
    return data, fmt
