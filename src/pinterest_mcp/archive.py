"""Archive backend: serves search and retrieval from a captured HTTP Archive.

Selected with ``PINTEREST_MODE=archive``.  Every response in the HAR is
indexed, so ``search_pins`` can answer with the captured search results for
that query, or fall back to a text match over every pin in the capture.
"""

from __future__ import annotations

import base64
import json
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from . import normalize
from .api import PinterestError, unwrap
from .backend import BackendError

RESOURCE_PATH = re.compile(
    r"^/resource/(?P<resource>[A-Za-z0-9_]+)/(?P<action>get|create|update)/"
)
_WORD = re.compile(r"[a-z0-9]+")


def _tokens(text: str) -> set[str]:
    return set(_WORD.findall(text.lower()))


def _overlap(wanted: set[str], target: set[str]) -> set[str]:
    """Query tokens with a match in ``target`` (exact or shared prefix)."""
    matched: set[str] = set()
    for token in wanted:
        if token in target:
            matched.add(token)
            continue
        if len(token) < 4:
            continue
        for candidate in target:
            if len(candidate) >= 4 and (
                candidate.startswith(token) or token.startswith(candidate)
            ):
                matched.add(token)
                break
    return matched


def _body_text(entry: dict[str, Any]) -> str:
    content = entry.get("response", {}).get("content", {}) or {}
    text = content.get("text") or ""
    if content.get("encoding") == "base64" and text:
        try:
            return base64.b64decode(text).decode("utf-8", "replace")
        except (ValueError, TypeError):  # pragma: no cover - corrupt capture
            return ""
    return text


def _request_options(entry: dict[str, Any]) -> dict[str, Any]:
    """``data`` query parameter of a resource call, as a dict."""
    query = parse_qs(urlparse(entry["request"]["url"]).query, keep_blank_values=True)
    raw = (query.get("data") or [""])[0]
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except ValueError:
        return {}
    options = parsed.get("options") if isinstance(parsed, dict) else None
    return options if isinstance(options, dict) else {}


@dataclass
class ArchiveStats:
    entries: int
    resources: dict[str, int] = field(default_factory=dict)
    pins: int = 0
    boards: int = 0
    users: int = 0
    searches: int = 0
    suggestions: int = 0


class ArchiveBackend:
    mode = "archive"

    def __init__(self, har_path: Path) -> None:
        self.path = Path(har_path)
        try:
            with self.path.open("rb") as handle:
                log = json.load(handle)["log"]
        except (OSError, ValueError, KeyError) as exc:
            raise BackendError(f"could not read HAR file {self.path}: {exc}") from exc
        self._entries: list[dict[str, Any]] = list(log.get("entries") or [])
        self._pins: dict[str, dict[str, Any]] = {}
        self._pin_details: dict[str, dict[str, Any]] = {}
        self._boards: dict[str, dict[str, Any]] = {}
        self._board_refs: dict[str, dict[str, Any]] = {}
        self._users: dict[str, dict[str, Any]] = {}
        self._user_refs: dict[str, dict[str, Any]] = {}
        self._searches: list[tuple[str, str, list[dict[str, Any]], str | None]] = []
        self._typeahead: list[tuple[str, list[dict[str, Any]]]] = []
        self._related: dict[str, Any] = {}
        self._board_pins: dict[str, Any] = {}
        self._board_pin_bookmark: dict[str, str | None] = {}
        self._comments: dict[str, Any] = {}
        self._comment_bookmarks: dict[str, str | None] = {}
        self._homefeed: Any = None
        self._homefeed_bookmark: str | None = None
        self.stats = ArchiveStats(entries=len(self._entries))
        self._index()

    # ------------------------------------------------------------------ indexing

    def _index(self) -> None:
        for entry in self._entries:
            parsed = urlparse(entry["request"]["url"])
            if parsed.netloc.endswith("recaptcha.net"):
                continue
            match = RESOURCE_PATH.match(parsed.path)
            if not match or match.group("action") != "get":
                continue
            resource = match.group("resource")
            text = _body_text(entry)
            if not text:
                continue
            try:
                payload = json.loads(text)
            except ValueError:
                continue  # truncated capture
            try:
                data = unwrap(payload)
            except PinterestError:
                continue
            bookmark = payload.get("resource_response", {}).get("bookmark") or None
            options = _request_options(entry)
            self.stats.resources[resource] = self.stats.resources.get(resource, 0) + 1
            suffix = _INDEXERS.get(resource)
            handler = getattr(self, f"_index_{suffix}") if suffix else self._ignore
            handler(resource, options, data, bookmark)

        self.stats.pins = len(self._pins)
        self.stats.boards = len(self._boards)
        self.stats.users = len(self._users)
        self.stats.searches = len(self._searches)
        self.stats.suggestions = len(self._typeahead)

    def _ignore(
        self, resource: str, options: dict[str, Any], data: Any, bookmark: str | None
    ) -> None:
        return

    def _remember_pin(self, pin: Any, *, detailed: bool = False) -> None:
        if not isinstance(pin, dict) or not pin.get("id"):
            return
        pin_id = str(pin["id"])
        if detailed:
            self._pin_details[pin_id] = pin
        self._pins.setdefault(pin_id, pin)
        pinner = pin.get("pinner")
        if isinstance(pinner, dict) and pinner.get("username"):
            self._remember_user_ref(pinner)

    def _remember_pins(self, items: Any, *, detailed: bool = False) -> None:
        for item in items if isinstance(items, list) else []:
            self._remember_pin(item, detailed=detailed)

    def _remember_user(self, user: Any) -> None:
        """Full profile payload (UserResource)."""
        if isinstance(user, dict) and user.get("username"):
            self._users.setdefault(user["username"], user)

    def _remember_user_ref(self, user: Any) -> None:
        """User object embedded in a pin or board: usable, but not a full profile."""
        if not isinstance(user, dict) or not user.get("username"):
            return
        self._user_refs.setdefault(user["username"], user)

    def _remember_board(self, board: Any, *, full: bool = False) -> None:
        """Full board payloads carry a name and url; anything else is only a ref."""
        if not isinstance(board, dict) or not board.get("id"):
            return
        board_id = str(board["id"])
        if full and board.get("name") and board.get("url"):
            self._boards.setdefault(board_id, board)
        else:
            self._board_refs.setdefault(board_id, board)
        self._remember_user_ref(board.get("owner"))

    def _index_search(
        self, resource: str, options: dict[str, Any], data: Any, bookmark: str | None
    ) -> None:
        query = options.get("query")
        if not isinstance(query, str) or not query:
            return
        results = data.get("results") if isinstance(data, dict) else None
        self._remember_pins(results)
        self._searches.append(
            (query, options.get("scope") or "pins", results or [], bookmark)
        )

    def _index_typeahead(
        self, resource: str, options: dict[str, Any], data: Any, bookmark: str | None
    ) -> None:
        term = options.get("term")
        items = data.get("items") if isinstance(data, dict) else None
        if items:
            self._typeahead.append((term or "", items))

    def _index_pin(
        self, resource: str, options: dict[str, Any], data: Any, bookmark: str | None
    ) -> None:
        self._remember_pin(data, detailed=True)
        board = (data or {}).get("board") if isinstance(data, dict) else None
        self._remember_board(board)

    def _index_related(
        self, resource: str, options: dict[str, Any], data: Any, bookmark: str | None
    ) -> None:
        pin_id = options.get("pin_id")
        if not pin_id:
            return
        self._related.setdefault(str(pin_id), data)
        self._remember_pins(data)

    def _index_board(
        self, resource: str, options: dict[str, Any], data: Any, bookmark: str | None
    ) -> None:
        self._remember_board(data, full=True)
        if isinstance(data, dict):
            self._remember_pins(data.get("pins"), detailed=True)

    def _index_user(
        self, resource: str, options: dict[str, Any], data: Any, bookmark: str | None
    ) -> None:
        self._remember_user(data)

    def _index_boards(
        self, resource: str, options: dict[str, Any], data: Any, bookmark: str | None
    ) -> None:
        for board in data if isinstance(data, list) else []:
            self._remember_board(board, full=True)

    def _index_board_feed(
        self, resource: str, options: dict[str, Any], data: Any, bookmark: str | None
    ) -> None:
        board_id = options.get("board_id")
        if not board_id:
            return
        items = data if isinstance(data, list) else (data or {}).get("results")
        self._board_pins.setdefault(str(board_id), items or [])
        self._board_pin_bookmark.setdefault(str(board_id), bookmark)
        self._remember_pins(items)

    def _index_comments(
        self, resource: str, options: dict[str, Any], data: Any, bookmark: str | None
    ) -> None:
        pin_id = options.get("aggregated_pin_id")
        if not pin_id:
            return
        items = data if isinstance(data, list) else (data or {}).get("results")
        self._comments.setdefault(str(pin_id), items or [])
        self._comment_bookmarks.setdefault(str(pin_id), bookmark)

    def _index_homefeed(
        self, resource: str, options: dict[str, Any], data: Any, bookmark: str | None
    ) -> None:
        items = data if isinstance(data, list) else (data or {}).get("results")
        if items:
            self._homefeed = items
            self._homefeed_bookmark = bookmark
            self._remember_pins(items)

    # ------------------------------------------------------------------ helpers

    def _single_page(self, bookmark: str | None) -> None:
        if bookmark:
            raise BackendError(
                "archive mode serves the captured first page only; pass no bookmark "
                "or set PINTEREST_COOKIE to run against live Pinterest"
            )

    def _not_found(self, what: str) -> BackendError:
        return BackendError(f"{what} is not in the captured archive ({self.path.name})")

    def _text_search(self, query: str, limit: int) -> list[dict[str, Any]]:
        """Rank every archived pin against the query, best match first."""
        wanted = _tokens(query)
        if not wanted:
            return []
        ranked: list[tuple[float, dict[str, Any]]] = []
        for pin in self._pins.values():
            record = normalize.pin_record(pin)
            if not record:
                continue
            haystack = " ".join(
                str(record.get(key) or "")
                for key in (
                    "title",
                    "grid_title",
                    "description",
                    "alt_text",
                    "auto_alt_text",
                    "domain",
                )
            )
            haystack_tokens = _tokens(haystack)
            matched = _overlap(wanted, haystack_tokens)
            if not matched:
                continue
            score = len(matched) / len(wanted)
            if len(matched) == len(wanted):
                score += 1.0
            ranked.append((score, record))
        ranked.sort(key=lambda item: (-item[0], item[1]["id"]))
        if not ranked:
            return []
        cutoff = ranked[0][0] - 0.35
        return [record for score, record in ranked if score >= cutoff][:limit]

    def _search_query_match(
        self, query: str, scope: str | None
    ) -> tuple[str, str, list[dict[str, Any]], str | None] | None:
        wanted = _tokens(query)
        best: tuple[float, tuple[str, str, list[dict[str, Any]], str | None]] | None = (
            None
        )
        for captured in self._searches:
            captured_query, captured_scope, results, _ = captured
            if not results:
                continue
            if captured_query.strip().lower() == query.strip().lower() and (
                not scope or captured_scope == scope
            ):
                return captured
            similarity = len(_overlap(wanted, _tokens(captured_query))) / max(
                len(wanted), 1
            )
            if scope and captured_scope != scope:
                similarity -= 0.25
            if similarity <= 0:
                continue
            if best is None or similarity > best[0]:
                best = (similarity, captured)
        if best and best[0] >= 0.6:
            return best[1]
        return None

    # -------------------------------------------------------------------- tools

    async def search_pins(
        self,
        *,
        query: str,
        scope: str = "pins",
        page_size: int | None = None,
        bookmark: str | None = None,
    ) -> dict[str, Any]:
        self._single_page(bookmark)
        limit = page_size or 25
        matched = self._search_query_match(query, scope)
        if matched is not None:
            captured_query, captured_scope, results, _ = matched
            records = normalize.pin_list(results)[:limit]
            return {
                "query": query,
                "scope": captured_scope,
                "count": len(records),
                "results": records,
                "match": "captured_search",
                "matched_query": captured_query,
            }
        records = self._text_search(query, limit)
        if not records:
            raise self._not_found(f"search {query!r}")
        return {
            "query": query,
            "scope": "archive",
            "count": len(records),
            "results": records,
            "match": "pin_text_search",
        }

    async def search_suggestions(self, *, term: str, count: int = 10) -> dict[str, Any]:
        wanted = term.strip().lower()
        ranked: list[tuple[float, str, dict[str, Any]]] = []
        for captured_term, items in self._typeahead:
            for index, item in enumerate(items):
                record = normalize.suggestion_record(item)
                if not record:
                    continue
                suggestion = record["query"]
                if wanted and suggestion.lower().startswith(wanted):
                    score = 3.0
                elif wanted and wanted in suggestion.lower():
                    score = 2.0
                elif not wanted:
                    score = 1.0
                else:
                    continue
                ranked.append((score - index * 0.001, captured_term, record))
        ranked.sort(key=lambda item: (-item[0], item[1]))
        seen: set[str] = set()
        suggestions = []
        for _, _, record in ranked:
            if record["query"] in seen:
                continue
            seen.add(record["query"])
            suggestions.append(record)
            if len(suggestions) >= count:
                break
        if not suggestions:
            raise self._not_found(f"suggestions for {term!r}")
        return {"term": term, "count": len(suggestions), "suggestions": suggestions}

    async def get_pin(self, *, pin_id: str) -> dict[str, Any]:
        pin_id = str(pin_id)
        source = self._pin_details.get(pin_id) or self._pins.get(pin_id)
        if not source:
            raise self._not_found(f"pin {pin_id}")
        return normalize.pin_record(source, detailed=True)

    async def get_related_pins(
        self, *, pin_id: str, query: str | None = None, page_size: int = 12
    ) -> dict[str, Any]:
        data = self._related.get(str(pin_id))
        if data is None:
            raise self._not_found(
                f"related pins for {pin_id} (no RelatedModulesResource capture)"
            )
        modules = normalize.related_modules(data)
        if query:
            wanted = _tokens(query)
            modules = [m for m in modules if _tokens(m["title"]) & wanted] or modules
        for module in modules:
            module["pins"] = module["pins"][:page_size]
        return {
            "pin_id": str(pin_id),
            "count": sum(len(m["pins"]) for m in modules),
            "modules": modules,
        }

    async def get_user(self, *, username: str) -> dict[str, Any]:
        wanted = username.lstrip("@").strip().lower()
        for name, data in self._users.items():
            if name.lower() == wanted:
                return normalize.user_record(data)
        for name, data in self._user_refs.items():
            if name.lower() == wanted:
                record = normalize.user_record(data)
                record["partial"] = True
                record["note"] = (
                    "Only a user reference from a pin or board was captured for this "
                    "account; follower/board counts are unavailable in archive mode."
                )
                return record
        raise self._not_found(f"user {username!r}")

    async def get_user_boards(
        self, *, username: str, page_size: int = 25
    ) -> dict[str, Any]:
        wanted = username.lstrip("@").strip().lower()
        boards = []
        for board in self._boards.values():
            owner = (board.get("owner") or {}).get("username")
            if owner and owner.lower() == wanted:
                record = normalize.board_record(board)
                if record:
                    boards.append(record)
        if not boards:
            raise self._not_found(f"boards of {username!r}")
        return {
            "username": username,
            "count": len(boards),
            "boards": boards[:page_size],
        }

    async def get_board(self, *, username: str, slug: str) -> dict[str, Any]:
        wanted = f"/{username}/{slug}/".lower()
        for board in list(self._boards.values()) + list(self._board_refs.values()):
            url = board.get("url")
            if isinstance(url, str) and url.lower() == wanted:
                record = normalize.board_record(board)
                pins = normalize.pin_list(board.get("pins"))
                if pins:
                    record["pins"] = pins
                return record
        raise self._not_found(f"board {username}/{slug}")

    async def get_board_pins(
        self,
        *,
        board_id: str,
        board_url: str | None = None,
        page_size: int = 25,
        bookmark: str | None = None,
    ) -> dict[str, Any]:
        items = self._board_pins.get(str(board_id))
        if items is None and board_url:
            known = list(self._boards.items()) + list(self._board_refs.items())
            for known_id, board in known:
                if (
                    isinstance(board.get("url"), str)
                    and board["url"].lower() == board_url.lower()
                ):
                    items = self._board_pins.get(known_id)
                    board_id = known_id
                    break
        if items is None:
            raise self._not_found(f"board feed for {board_id!r}")
        self._single_page(bookmark)
        records = normalize.pin_list(items)[:page_size]
        out: dict[str, Any] = {
            "board_id": str(board_id),
            "count": len(records),
            "results": records,
        }
        captured = self._board_pin_bookmark.get(str(board_id))
        if captured:
            out["captured_bookmark"] = captured
        return out

    async def get_pin_comments(
        self, *, pin_id: str, page_size: int = 10, bookmark: str | None = None
    ) -> dict[str, Any]:
        items = self._comments.get(str(pin_id))
        if items is None:
            raise self._not_found(f"comments for pin {pin_id}")
        self._single_page(bookmark)
        comments = normalize.comment_list(items)[:page_size]
        out: dict[str, Any] = {
            "pin_id": str(pin_id),
            "count": len(comments),
            "comments": comments,
        }
        captured = self._comment_bookmarks.get(str(pin_id))
        if captured:
            out["captured_bookmark"] = captured
        return out

    async def get_home_feed(
        self, *, page_size: int | None = None, bookmark: str | None = None
    ) -> dict[str, Any]:
        if self._homefeed is None:
            raise self._not_found("home feed")
        self._single_page(bookmark)
        records = normalize.pin_list(self._homefeed)[: page_size or 25]
        out: dict[str, Any] = {"count": len(records), "results": records}
        if self._homefeed_bookmark:
            out["captured_bookmark"] = self._homefeed_bookmark
        return out

    def captured_queries(self) -> Iterable[str]:
        return (query for query, _, results, _ in self._searches if results)

    def status(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "archive": str(self.path),
            "entries": self.stats.entries,
            "pins": self.stats.pins,
            "boards": self.stats.boards,
            "users": self.stats.users,
            "captured_searches": sorted(set(self.captured_queries())),
            "resources": dict(sorted(self.stats.resources.items())),
            "note": (
                "Archive mode: answers come from the captured HAR. Pagination, "
                "fresh content and arbitrary queries require PINTEREST_COOKIE."
            ),
        }


# resource name -> indexer suffix
_INDEXERS = {
    "BaseSearchResource": "search",
    "AdvancedTypeaheadResource": "typeahead",
    "PinResource": "pin",
    "RelatedModulesResource": "related",
    "BoardResource": "board",
    "UserResource": "user",
    "BoardsResource": "boards",
    "BoardFeedResource": "board_feed",
    "UnifiedCommentsResource": "comments",
    "UserHomefeedResource": "homefeed",
}
