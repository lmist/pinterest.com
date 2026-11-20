"""Pinterest web API contract.

Everything here was reverse engineered from the captured HAR
(``nl.pinterest.com.har``).  ``Request.url`` reproduces the captured request
URLs byte for byte - see ``tests/test_api_contract.py``.

The web app talks to ``{base}/resource/<Resource>/get/`` with three query
parameters:

``source_url``  the UI route the call is made from, e.g. ``/pin/123/``
``data``        JSON blob ``{"options": {...}, "context": {}}``
``_``           cache buster (epoch milliseconds)
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

# Pinterest encodes query parameters the way JavaScript's encodeURIComponent
# does: ``!~*'()`` stay literal, everything else non-alphanumeric is escaped.
_SAFE = "!~*'()"


def encode(value: str) -> str:
    return quote(value, safe=_SAFE)


@dataclass(frozen=True)
class Request:
    """A single ``/resource/<resource>/get/`` call."""

    resource: str
    options: dict[str, Any]
    source_url: str
    pws_handler: str = "www/index.js"

    def query(self, cache_buster: int | None = None) -> str:
        data = json.dumps(
            {"options": self.options, "context": {}},
            separators=(",", ":"),
            ensure_ascii=False,
        )
        params = {
            "source_url": self.source_url,
            "data": data,
            "_": str(
                cache_buster if cache_buster is not None else int(time.time() * 1000)
            ),
        }
        return "&".join(f"{key}={encode(value)}" for key, value in params.items())

    def url(self, base_url: str, cache_buster: int | None = None) -> str:
        return f"{base_url}/resource/{self.resource}/get/?{self.query(cache_buster)}"

    def headers(
        self,
        *,
        origin: str,
        app_version: str,
        user_agent: str,
        cookie: str,
        csrf_token: str,
    ) -> dict[str, str]:
        headers = {
            "accept": "application/json, text/javascript, */*, q=0.01",
            "accept-language": "en-US,en;q=0.9",
            "referer": f"{origin}/",
            "user-agent": user_agent,
            "x-app-version": app_version,
            "x-pinterest-appstate": "active",
            "x-pinterest-pws-handler": self.pws_handler,
            "x-pinterest-source-url": self.source_url,
            "x-requested-with": "XMLHttpRequest",
        }
        if cookie:
            headers["cookie"] = cookie
        if csrf_token:
            headers["x-csrftoken"] = csrf_token
        return headers


# --------------------------------------------------------------------------
# option templates - key sets and defaults mirror the captured requests
# --------------------------------------------------------------------------

_SEARCH_OPTIONS: dict[str, Any] = {
    "query": "",
    "scope": "pins",
    "appliedProductFilters": None,
    "domains": None,
    "user": None,
    "seoDrawerEnabled": False,
    "applied_unified_filters": None,
    "auto_correction_disabled": False,
    "filter_genai": False,
    "journey_depth": None,
    "source_id": None,
    "source_module_id": None,
    "source_url": "",
    "static_feed": False,
    "selected_one_bar_modules": None,
    "query_pin_sigs": None,
    "page_size": None,
    "gated": None,
    "price_max": None,
    "price_min": None,
    "query_image_pins": None,
    "request_params": None,
    "top_pin_ids": None,
    "article": None,
    "corpus": None,
    "filters": None,
    "rs": "typed",
}

_PIN_OPTIONS: dict[str, Any] = {
    "id": "",
    "field_set_key": "auth_web_main_pin",
    "add_fields": (
        "pin.gen_ai_topics,pin.is_eligible_for_image_download,"
        "pin.is_eligible_for_ai_comparison,pin.has_hidden_inner_pin"
    ),
    "noCache": True,
    "fetch_visual_search_objects": False,
    "get_page_metadata": False,
}

_RELATED_OPTIONS: dict[str, Any] = {
    "additional_fields": ["pin.gen_ai_topics"],
    "pin_id": "",
    "context_pin_ids": [],
    "context_near_dup_image_sigs": [],
    "homefeed_source_sig": None,
    "page_size": 12,
    "search_query": None,
    "source": "search",
    "top_level_source": "search",
    "top_level_source_depth": 1,
    "is_pdp": False,
}

_BOARD_FEED_OPTIONS: dict[str, Any] = {
    "board_id": "",
    "board_url": None,
    "currentFilter": -1,
    "field_set_key": "react_grid_pin",
    "filter_section_pins": True,
    "sort": "default",
    "layout": "default",
    "page_size": 25,
    "redux_normalize_feed": True,
}

_COMMENTS_OPTIONS: dict[str, Any] = {
    "aggregated_pin_id": "",
    "comment_featured_ids": [],
    "page_size": 10,
    "redux_normalize_feed": True,
    "is_reversed": False,
}

_BOARD_OPTIONS: dict[str, Any] = {
    "add_fields": (
        "board.meal_plan(),mealplan.{id,meal_groups,shopping_lists},meal.{pins()},"
        "pin.{id,images[236x,474x,orig],domain,dominant_color,rich_summary(),"
        "tracking_params,type}"
    ),
    "username": "",
    "slug": "",
    "field_set_key": "detailed",
    "orbac_subject_id": "",
}


def search_source_url(query: str, scope: str = "pins") -> str:
    """UI route a search request is made from, e.g. ``/search/pins/?q=cats&rs=typed``."""
    return f"/search/{scope}/?q={quote(query, safe='')}&rs=typed"


def search(
    *,
    query: str,
    scope: str = "pins",
    page_size: int | None = None,
    bookmark: str | None = None,
) -> Request:
    if not query.strip():
        raise ValueError("query must not be empty")
    source_url = search_source_url(query, scope)
    options = dict(_SEARCH_OPTIONS)
    options["query"] = query
    options["scope"] = scope
    options["source_url"] = source_url
    options["page_size"] = page_size
    if bookmark:
        options["bookmark"] = bookmark
    return Request("BaseSearchResource", options, source_url, "www/search/[scope].js")


def search_suggestions(*, term: str, count: int = 10) -> Request:
    options = {
        "pin_scope": "pins",
        "autocomplete_request_surface": 0,
        "count": count,
        "term": term,
    }
    return Request("AdvancedTypeaheadResource", options, "/", "www/index.js")


def pin(*, pin_id: str) -> Request:
    options = dict(_PIN_OPTIONS)
    options["id"] = pin_id
    return Request("PinResource", options, f"/pin/{pin_id}/", "www/pin/[id].js")


def related_pins(
    *, pin_id: str, query: str | None = None, page_size: int = 12
) -> Request:
    options = dict(_RELATED_OPTIONS)
    options["pin_id"] = pin_id
    options["page_size"] = page_size
    options["search_query"] = query
    # ``top_level_source`` is validated against a fixed enum server side; the
    # only value that works for a pin close-up request is "search" (Pinterest
    # rejects "pin" with HTTP 400).
    options["source"] = "search"
    options["top_level_source"] = "search"
    options["top_level_source_depth"] = 1
    return Request(
        "RelatedModulesResource", options, f"/pin/{pin_id}/", "www/pin/[id].js"
    )


def user(*, username: str, user_id: str | None = None) -> Request:
    options: dict[str, Any] = {"field_set_key": "profile", "username": username}
    if user_id:
        options["user_id"] = user_id
    return Request("UserResource", options, f"/{username}/", "www/[username].js")


def user_boards(*, username: str, page_size: int = 25) -> Request:
    options = {
        "page_size": page_size,
        "privacy_filter": "all",
        "sort": "last_pinned_to",
        "username": username,
    }
    return Request("BoardsResource", options, f"/{username}/", "www/[username].js")


def board(*, username: str, slug: str, board_id: str | None = None) -> Request:
    options = dict(_BOARD_OPTIONS)
    options["username"] = username
    options["slug"] = slug
    if board_id:
        options["board_id"] = board_id
    return Request(
        "BoardResource", options, f"/{username}/{slug}/", "www/[username]/[slug].js"
    )


def board_pins(
    *,
    board_id: str,
    board_url: str | None = None,
    page_size: int = 25,
    bookmark: str | None = None,
) -> Request:
    options = dict(_BOARD_FEED_OPTIONS)
    options["board_id"] = board_id
    options["board_url"] = board_url
    options["page_size"] = page_size
    if bookmark:
        options["bookmarks"] = [bookmark]
    source_url = board_url or f"/{board_id}/"
    return Request("BoardFeedResource", options, source_url, "www/[username]/[slug].js")


def comments(
    *, pin_id: str, page_size: int = 10, bookmark: str | None = None
) -> Request:
    options = dict(_COMMENTS_OPTIONS)
    options["aggregated_pin_id"] = pin_id
    options["page_size"] = page_size
    if bookmark:
        options["bookmark"] = bookmark
    return Request(
        "UnifiedCommentsResource", options, f"/pin/{pin_id}/", "www/pin/[id].js"
    )


def home_feed(*, page_size: int | None = None, bookmark: str | None = None) -> Request:
    options: dict[str, Any] = {
        "field_set_key": "hf_grid",
        "in_nux": False,
        "in_news_hub": False,
        "static_feed": False,
    }
    if page_size is not None:
        options["page_size"] = page_size
    if bookmark:
        options["bookmark"] = bookmark
    return Request("UserHomefeedResource", options, "/", "www/index.js")


# --------------------------------------------------------------------------
# response handling
# --------------------------------------------------------------------------


class PinterestError(RuntimeError):
    """Pinterest returned an error response."""


class AuthRequiredError(PinterestError):
    """The session cookie is missing, expired or invalid."""


AUTH_HINT = (
    "Pinterest rejected the request as unauthenticated. Open pinterest.com in a "
    "logged-in browser, copy the 'cookie' request header of any /resource/ call "
    "and set it as PINTEREST_COOKIE, then restart the server."
)


def unwrap(payload: Any) -> Any:
    """Return ``resource_response.data`` or raise a descriptive error."""
    if not isinstance(payload, dict):
        raise PinterestError(f"unexpected response payload: {type(payload).__name__}")
    response = payload.get("resource_response")
    if not isinstance(response, dict):
        raise PinterestError(f"response has no resource_response: {str(payload)[:200]}")
    status = response.get("status")
    code = response.get("code")
    ok = status in (None, "success") and code in (None, 0)
    if not ok:
        message = response.get("message") or response.get("error") or status
        http_status = response.get("http_status")
        if http_status in (401, 403) or code in (1, 2, 3, 7, 8, 11):
            raise AuthRequiredError(
                f"Pinterest returned {status}/{code}: {message}. {AUTH_HINT}"
            )
        raise PinterestError(f"Pinterest returned {status}/{code}: {message}")
    return response.get("data")


def bookmark_of(payload: Any) -> str | None:
    """Pagination cursor from a resource response, if any."""
    if not isinstance(payload, dict):
        return None
    response = payload.get("resource_response")
    if not isinstance(response, dict):
        return None
    bookmark = response.get("bookmark")
    return bookmark if isinstance(bookmark, str) and bookmark else None
