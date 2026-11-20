"""MCP server exposing Pinterest search and retrieval.

Tools answer from pinterest.com by default (public data needs no credentials;
a session cookie unlocks the signed-in surfaces) or from a captured HAR when
``PINTEREST_MODE=archive``.
"""

from __future__ import annotations

import argparse
import functools
import logging
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import httpx
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.mcpserver.utilities.types import Image

from .api import PinterestError
from .archive import ArchiveBackend
from .backend import Backend, BackendError
from .config import Settings
from .live import LiveBackend, fetch_image

SERVER_NAME = "pinterest"

# MCPServer configures INFO logging; httpx would then log every request URL into
# the client's MCP logs.
logging.getLogger("httpx").setLevel(logging.WARNING)

SERVER_INSTRUCTIONS = (
    "Search and retrieve Pinterest pins, boards, users and search suggestions. "
    "Pins come back as compact records with image URLs; pass a pin id from any "
    "result to get_pin, get_related_pins or get_pin_comments. Use fetch_image to "
    "actually look at an image. List responses carry a 'bookmark' for the next "
    "page when the backend supports pagination. A saved cookie is sent when one "
    "exists, so ranking can follow the signed-in account; use `pinterest search "
    "--no-account` for a result that does not. Call pinterest_status first if "
    "you need to know whether results are live or from the offline capture."
)


class PinterestApp:
    """Wires the backend and shared HTTP client behind every tool."""

    def __init__(
        self,
        settings: Settings,
        *,
        backend: Backend | None = None,
        http: httpx.AsyncClient | None = None,
    ) -> None:
        self.settings = settings
        if backend is None:
            if settings.mode == "archive":
                if settings.har_path is None:
                    raise BackendError(
                        "PINTEREST_MODE=archive needs a capture: set PINTEREST_HAR to a .har "
                        "file, or drop nl.pinterest.com.har in the working directory."
                    )
                backend = ArchiveBackend(settings.har_path)
            else:
                backend = LiveBackend(settings)
        self.backend = backend
        self._http = http or httpx.AsyncClient(
            timeout=settings.timeout, follow_redirects=True
        )
        self._owns_http = http is None

    async def aclose(self) -> None:
        if self._owns_http:
            await self._http.aclose()
        if isinstance(self.backend, LiveBackend):
            await self.backend.aclose()

    # ------------------------------------------------------------------- tools

    async def search_pins(
        self,
        *,
        query: str,
        scope: str = "pins",
        page_size: int | None = None,
        bookmark: str | None = None,
    ) -> dict[str, Any]:
        return await self.backend.search_pins(
            query=query, scope=scope, page_size=page_size, bookmark=bookmark
        )

    async def search_suggestions(self, *, term: str, count: int = 10) -> dict[str, Any]:
        return await self.backend.search_suggestions(term=term, count=count)

    async def get_pin(self, *, pin_id: str) -> dict[str, Any]:
        return await self.backend.get_pin(pin_id=str(pin_id))

    async def get_related_pins(
        self, *, pin_id: str, query: str | None = None, page_size: int = 12
    ) -> dict[str, Any]:
        return await self.backend.get_related_pins(
            pin_id=str(pin_id), query=query, page_size=page_size
        )

    async def get_user(self, *, username: str) -> dict[str, Any]:
        return await self.backend.get_user(username=username.lstrip("@"))

    async def get_user_boards(
        self, *, username: str, page_size: int = 25
    ) -> dict[str, Any]:
        return await self.backend.get_user_boards(
            username=username.lstrip("@"), page_size=page_size
        )

    async def get_board(self, *, username: str, slug: str) -> dict[str, Any]:
        return await self.backend.get_board(username=username.lstrip("@"), slug=slug)

    async def get_board_pins(
        self,
        *,
        board_id: str,
        board_url: str | None = None,
        page_size: int = 25,
        bookmark: str | None = None,
    ) -> dict[str, Any]:
        return await self.backend.get_board_pins(
            board_id=str(board_id),
            board_url=board_url,
            page_size=page_size,
            bookmark=bookmark,
        )

    async def get_pin_comments(
        self, *, pin_id: str, page_size: int = 10, bookmark: str | None = None
    ) -> dict[str, Any]:
        return await self.backend.get_pin_comments(
            pin_id=str(pin_id), page_size=page_size, bookmark=bookmark
        )

    async def get_home_feed(
        self, *, page_size: int | None = None, bookmark: str | None = None
    ) -> dict[str, Any]:
        return await self.backend.get_home_feed(page_size=page_size, bookmark=bookmark)

    async def fetch_image(self, *, url: str) -> Image:
        data, fmt = await fetch_image(self.settings, self._http, url)
        return Image(data=data, format=fmt)

    def pinterest_status(self) -> dict[str, Any]:
        info = self.backend.status()
        info["server"] = SERVER_NAME
        return info


def _guard(fn):
    """Translate backend failures into tool errors the model can act on."""

    @functools.wraps(fn)
    async def wrapper(*args: Any, **kwargs: Any) -> Any:
        try:
            return await fn(*args, **kwargs)
        except (PinterestError, BackendError) as exc:
            raise ToolError(str(exc)) from exc
        except ValueError as exc:
            raise ToolError(str(exc)) from exc

    return wrapper


def create_server(
    settings: Settings | None = None, app: PinterestApp | None = None
) -> MCPServer:
    """Build the MCP server. ``app`` may be injected for tests."""
    settings = settings or Settings.from_env()
    application = app or PinterestApp(settings)

    @asynccontextmanager
    async def lifespan(_: MCPServer) -> AsyncIterator[None]:
        try:
            yield
        finally:
            await application.aclose()

    server = MCPServer(
        name=SERVER_NAME,
        title="Pinterest",
        version="0.1.0",
        instructions=SERVER_INSTRUCTIONS,
        lifespan=lifespan,
    )

    @server.tool(
        description=(
            "Search Pinterest pins for a query. scope='pins' searches all of Pinterest; "
            "scope='my_pins' is the signed-in account's own pins. A saved cookie is "
            "sent when one exists. Returns compact pin records plus a 'bookmark' to "
            "pass back for the next page."
        )
    )
    @_guard
    async def search_pins(
        query: str,
        scope: str = "pins",
        page_size: int | None = None,
        bookmark: str | None = None,
    ) -> dict[str, Any]:
        return await application.search_pins(
            query=query, scope=scope, page_size=page_size, bookmark=bookmark
        )

    @server.tool(
        description=(
            "Autocomplete a search term: what Pinterest itself suggests people type. "
            "Use it to expand or disambiguate a query before search_pins."
        )
    )
    @_guard
    async def search_suggestions(term: str, count: int = 10) -> dict[str, Any]:
        return await application.search_suggestions(term=term, count=count)

    @server.tool(
        description=(
            "Fetch one pin by id with full detail: description, alt text, link, pinner, "
            "board, all image sizes and video URL when present. Pin ids appear in search "
            "and feed results (the 'id' field)."
        )
    )
    @_guard
    async def get_pin(pin_id: str) -> dict[str, Any]:
        return await application.get_pin(pin_id=pin_id)

    @server.tool(
        description=(
            "Get pins Pinterest considers related to a pin, grouped into modules by the "
            "originating query. Ideal for expanding from one good pin to a set. Pass "
            "query to bias modules toward a topic."
        )
    )
    @_guard
    async def get_related_pins(
        pin_id: str, query: str | None = None, page_size: int = 12
    ) -> dict[str, Any]:
        return await application.get_related_pins(
            pin_id=pin_id, query=query, page_size=page_size
        )

    @server.tool(
        description=(
            "Look up a Pinterest user profile by username (with or without '@'): display "
            "name, bio, follower/pin/board counts and avatar."
        )
    )
    @_guard
    async def get_user(username: str) -> dict[str, Any]:
        return await application.get_user(username=username)

    @server.tool(
        description=(
            "List the public boards of a user. Each board record includes the board id "
            "needed by get_board_pins."
        )
    )
    @_guard
    async def get_user_boards(username: str, page_size: int = 25) -> dict[str, Any]:
        return await application.get_user_boards(username=username, page_size=page_size)

    @server.tool(
        description=(
            "Fetch one board's metadata by owner username and board slug, e.g. "
            "username='allidoizwin', slug='brutalism'."
        )
    )
    @_guard
    async def get_board(username: str, slug: str) -> dict[str, Any]:
        return await application.get_board(username=username, slug=slug)

    @server.tool(
        description=(
            "List the pins on a board. Takes the board id (from get_board or "
            "get_user_boards) and optionally its url path for fidelity. Returns a "
            "'bookmark' for the next page."
        )
    )
    @_guard
    async def get_board_pins(
        board_id: str,
        board_url: str | None = None,
        page_size: int = 25,
        bookmark: str | None = None,
    ) -> dict[str, Any]:
        return await application.get_board_pins(
            board_id=board_id,
            board_url=board_url,
            page_size=page_size,
            bookmark=bookmark,
        )

    @server.tool(
        description="Fetch the comments on a pin, newest first, with author and reply counts."
    )
    @_guard
    async def get_pin_comments(
        pin_id: str, page_size: int = 10, bookmark: str | None = None
    ) -> dict[str, Any]:
        return await application.get_pin_comments(
            pin_id=pin_id, page_size=page_size, bookmark=bookmark
        )

    @server.tool(
        description=(
            "The signed-in account's own home feed. Requires credentials; in archive "
            "mode it replays the captured feed."
        )
    )
    @_guard
    async def get_home_feed(
        page_size: int | None = None, bookmark: str | None = None
    ) -> dict[str, Any]:
        return await application.get_home_feed(page_size=page_size, bookmark=bookmark)

    @server.tool(
        description=(
            "Download a pin image so you can see it. Pass the 'image' URL (or any entry "
            "of 'images') from a pin record; only Pinterest image hosts are accepted."
        )
    )
    @_guard
    async def fetch_image(url: str) -> Image:
        return await application.fetch_image(url=url)

    @server.tool(
        description=(
            "Report how this server is configured: 'live' (real Pinterest, authenticated) "
            "or 'archive' (answers come from a captured HAR), plus archive contents."
        )
    )
    def pinterest_status() -> dict[str, Any]:
        return application.pinterest_status()

    return server


def _check() -> int:
    """Verify configuration (and credentials) from the command line."""
    import asyncio
    import json

    async def run() -> int:
        settings = Settings.from_env()
        print(
            json.dumps(
                {
                    "mode": settings.mode,
                    "base_url": settings.base_url,
                    "app_version": settings.app_version,
                    "timeout": settings.timeout,
                    "min_interval": settings.min_interval,
                    "max_image_bytes": settings.max_image_bytes,
                    "har": str(settings.har_path) if settings.har_path else None,
                    "cookie_set": bool(settings.cookie),
                    "csrf_token_set": bool(settings.csrf_token),
                },
                indent=2,
            )
        )
        app = PinterestApp(settings)
        try:
            result = await app.search_pins(query="brutalist architecture", page_size=1)
            detail = f" ({result['match']})" if "match" in result else ""
            print(f"{settings.mode} search OK: {result['count']} result(s){detail}")
            return 0
        except (PinterestError, BackendError) as exc:
            print(f"FAILED: {exc}", file=sys.stderr)
            return 1
        finally:
            await app.aclose()

    return asyncio.run(run())


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="pinterest-mcp",
        description="MCP server for Pinterest search and retrieval (stdio transport).",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="print the resolved configuration, run one search and exit",
    )
    args = parser.parse_args()
    if args.check:
        raise SystemExit(_check())
    create_server().run(transport="stdio")


if __name__ == "__main__":  # pragma: no cover
    main()
