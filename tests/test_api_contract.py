"""The request builder must reproduce the captured Pinterest traffic exactly.

Every GET ``/resource/<X>/get/`` entry in the HAR is rebuilt from its own
parsed ``options``/``source_url`` and compared byte for byte with the recorded
URL.  This is what makes the live backend trustworthy without a session cookie:
if Pinterest changes the contract, this test fails.
"""

from __future__ import annotations

import json
from urllib.parse import parse_qs, urlparse

import pytest

from pinterest_mcp import api
from pinterest_mcp.api import Request

RESOURCE_PREFIX = "/resource/"


def _captured_gets(entries: list[dict]) -> list[dict]:
    out = []
    for entry in entries:
        request = entry["request"]
        parsed = urlparse(request["url"])
        if request["method"] != "GET" or not parsed.path.startswith(RESOURCE_PREFIX):
            continue
        if not parsed.path.endswith("/get/"):
            continue
        if not parse_qs(parsed.query).get("data"):
            continue
        out.append(entry)
    return out


def _rebuild(entry: dict) -> str:
    request = entry["request"]
    parsed = urlparse(request["url"])
    query = parse_qs(parsed.query, keep_blank_values=True)
    payload = json.loads(query["data"][0])
    rebuilt = Request(
        resource=parsed.path.split("/")[2],
        options=payload["options"],
        source_url=query["source_url"][0],
    )
    return rebuilt.url(
        f"{parsed.scheme}://{parsed.netloc}", cache_buster=int(query["_"][0])
    )


def test_captured_resource_urls_reproduce_exactly(har_entries: list[dict]) -> None:
    entries = _captured_gets(har_entries)
    assert len(entries) > 200, "expected a substantial captured API surface"
    mismatches = []
    for entry in entries:
        expected = entry["request"]["url"]
        actual = _rebuild(entry)
        if actual != expected:
            mismatches.append((expected, actual))
    assert not mismatches, (
        f"{len(mismatches)} of {len(entries)} URLs differ, first: {mismatches[0]}"
    )


def test_cache_buster_is_milliseconds() -> None:
    request = api.search(query="cats")
    url = request.url("https://nl.pinterest.com", cache_buster=1700000000000)
    assert "&_=1700000000000" in url


def test_search_request_matches_captured_shape(har_entries: list[dict]) -> None:
    """A search we build must carry the same option keys as the captured ones."""
    captured = None
    for entry in _captured_gets(har_entries):
        if entry["request"]["url"].startswith(
            "https://nl.pinterest.com/resource/BaseSearchResource/get/"
        ):
            captured = json.loads(
                parse_qs(urlparse(entry["request"]["url"]).query)["data"][0]
            )["options"]
            break
    assert captured is not None
    ours = api.search(query="brutalist architecture").options
    assert set(ours) == set(captured)
    assert ours["query"] == "brutalist architecture"
    assert ours["source_url"] == "/search/pins/?q=brutalist%20architecture&rs=typed"


@pytest.mark.parametrize(
    ("builder", "expected_resource", "expected_source_url", "expected_handler"),
    [
        (
            lambda: api.pin(pin_id="914862421434174"),
            "PinResource",
            "/pin/914862421434174/",
            "www/pin/[id].js",
        ),
        (
            lambda: api.user(username="allidoizwin"),
            "UserResource",
            "/allidoizwin/",
            "www/[username].js",
        ),
        (
            lambda: api.board(username="allidoizwin", slug="brutalism"),
            "BoardResource",
            "/allidoizwin/brutalism/",
            "www/[username]/[slug].js",
        ),
        (
            lambda: api.board_pins(
                board_id="1149403204839652960", board_url="/allidoizwin/brutalism/"
            ),
            "BoardFeedResource",
            "/allidoizwin/brutalism/",
            "www/[username]/[slug].js",
        ),
        (
            lambda: api.comments(pin_id="5396881410717955008"),
            "UnifiedCommentsResource",
            "/pin/5396881410717955008/",
            "www/pin/[id].js",
        ),
        (lambda: api.home_feed(), "UserHomefeedResource", "/", "www/index.js"),
        (
            lambda: api.search_suggestions(term="bru"),
            "AdvancedTypeaheadResource",
            "/",
            "www/index.js",
        ),
        (
            lambda: api.user_boards(username="allidoizwin"),
            "BoardsResource",
            "/allidoizwin/",
            "www/[username].js",
        ),
        (
            lambda: api.related_pins(
                pin_id="914862421434174", query="brutalist architecture"
            ),
            "RelatedModulesResource",
            "/pin/914862421434174/",
            "www/pin/[id].js",
        ),
    ],
)
def test_request_builders_target_the_captured_endpoints(
    builder, expected_resource: str, expected_source_url: str, expected_handler: str
) -> None:
    request = builder()
    assert request.resource == expected_resource
    assert request.source_url == expected_source_url
    assert request.pws_handler == expected_handler


def test_board_pins_carries_bookmark_array() -> None:
    request = api.board_pins(board_id="1", bookmark="CURSOR")
    assert request.options["bookmarks"] == ["CURSOR"]


def test_search_carries_bookmark_string() -> None:
    request = api.search(query="cats", bookmark="CURSOR")
    assert request.options["bookmark"] == "CURSOR"


def test_empty_query_is_rejected() -> None:
    with pytest.raises(ValueError):
        api.search(query="   ")


def test_headers_include_session_and_version() -> None:
    headers = api.pin(pin_id="1").headers(
        origin="https://nl.pinterest.com",
        app_version="2b09e40",
        user_agent="agent",
        cookie="csrftoken=abc",
        csrf_token="abc",
    )
    assert headers["x-app-version"] == "2b09e40"
    assert headers["x-pinterest-pws-handler"] == "www/pin/[id].js"
    assert headers["x-pinterest-source-url"] == "/pin/1/"
    assert headers["cookie"] == "csrftoken=abc"
    assert headers["x-csrftoken"] == "abc"
    assert headers["referer"] == "https://nl.pinterest.com/"


def test_unwrap_raises_auth_error_for_expired_session() -> None:
    payload = {
        "resource_response": {
            "status": "error",
            "code": 1,
            "message": "Invalid session",
            "http_status": 403,
        }
    }
    with pytest.raises(api.AuthRequiredError) as excinfo:
        api.unwrap(payload)
    assert "PINTEREST_COOKIE" in str(excinfo.value)


def test_unwrap_rejects_other_errors() -> None:
    payload = {
        "resource_response": {"status": "error", "code": 42, "message": "no such pin"}
    }
    with pytest.raises(api.PinterestError) as excinfo:
        api.unwrap(payload)
    assert not isinstance(excinfo.value, api.AuthRequiredError)
    assert "no such pin" in str(excinfo.value)


def test_unwrap_returns_data_and_reports_bookmark() -> None:
    payload = {
        "resource_response": {
            "status": "success",
            "data": {"results": []},
            "bookmark": "NEXT",
        }
    }
    assert api.unwrap(payload) == {"results": []}
    assert api.bookmark_of(payload) == "NEXT"
    assert api.bookmark_of({"resource_response": {"data": {}}}) is None
