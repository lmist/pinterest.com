"""Shared backend interface and payload shapes.

``LiveBackend`` (network) and ``ArchiveBackend`` (captured HAR) implement the
same methods and return the same records, so the MCP tools do not care which
one is active.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


class BackendError(RuntimeError):
    """A backend could not satisfy a request."""


@runtime_checkable
class Backend(Protocol):
    mode: str

    async def search_pins(
        self,
        *,
        query: str,
        scope: str = "pins",
        page_size: int | None = None,
        bookmark: str | None = None,
    ) -> dict[str, Any]: ...

    async def search_suggestions(
        self, *, term: str, count: int = 10
    ) -> dict[str, Any]: ...

    async def get_pin(self, *, pin_id: str) -> dict[str, Any]: ...

    async def get_related_pins(
        self, *, pin_id: str, query: str | None = None, page_size: int = 12
    ) -> dict[str, Any]: ...

    async def get_user(self, *, username: str) -> dict[str, Any]: ...

    async def get_user_boards(
        self, *, username: str, page_size: int = 25
    ) -> dict[str, Any]: ...

    async def get_board(self, *, username: str, slug: str) -> dict[str, Any]: ...

    async def get_board_pins(
        self,
        *,
        board_id: str,
        board_url: str | None = None,
        page_size: int = 25,
        bookmark: str | None = None,
    ) -> dict[str, Any]: ...

    async def get_pin_comments(
        self, *, pin_id: str, page_size: int = 10, bookmark: str | None = None
    ) -> dict[str, Any]: ...

    async def get_home_feed(
        self, *, page_size: int | None = None, bookmark: str | None = None
    ) -> dict[str, Any]: ...

    def status(self) -> dict[str, Any]: ...
