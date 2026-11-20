"""Live backend behaviour against a mocked Pinterest, without network access."""

from __future__ import annotations

import json

import httpx
import pytest
from support import Recorder, json_response, options_of, query_params, resource_payload

from pinterest_mcp import api
from pinterest_mcp.api import AuthRequiredError
from pinterest_mcp.backend import BackendError
from pinterest_mcp.live import LiveBackend, NotFoundError, fetch_image

PIN = {
    "id": "123",
    "title": "Brutalist",
    "description": "concrete",
    "images": {
        "orig": {"url": "https://i.pinimg.com/originals/aa/bb/cc.jpg"},
        "236x": {"url": "https://i.pinimg.com/236x/aa/bb/cc.jpg"},
    },
    "pinner": {"id": "u1", "username": "someone", "full_name": "Some One"},
    "board": {"id": "b1", "name": "brutalism", "url": "/someone/brutalism/"},
}


def make_backend(settings, responder):
    recorder = Recorder(responder)
    backend = LiveBackend(settings, client=recorder.client())
    return backend, recorder


async def test_search_sends_the_captured_contract(live_settings) -> None:
    backend, recorder = make_backend(
        live_settings, lambda r: json_response(resource_payload({"results": [PIN]}))
    )
    try:
        result = await backend.search_pins(query="brutalist architecture", page_size=10)
    finally:
        await backend.aclose()

    request = recorder.requests[0]
    params = query_params(request)
    assert request.url.path == "/resource/BaseSearchResource/get/"
    assert params["source_url"] == "/search/pins/?q=brutalist%20architecture&rs=typed"
    options = options_of(request)
    assert options["query"] == "brutalist architecture"
    assert options["scope"] == "pins"
    assert options["page_size"] == 10
    assert options["source_url"] == params["source_url"]
    assert request.headers["cookie"] == "csrftoken=abc; _auth=1"
    assert request.headers["x-app-version"] == "2b09e40"
    assert request.headers["x-pinterest-pws-handler"] == "www/search/[scope].js"
    assert request.headers["x-pinterest-source-url"] == params["source_url"]
    assert request.headers["user-agent"] == "pytest-agent"
    assert result["count"] == 1
    assert result["results"][0]["id"] == "123"
    assert request.url.query.decode().count("&_=") == 1, "cache buster must be present"


async def test_search_propagates_bookmark_and_returns_next(live_settings) -> None:
    backend, recorder = make_backend(
        live_settings,
        lambda r: json_response(resource_payload({"results": [PIN]}, bookmark="NEXT")),
    )
    try:
        result = await backend.search_pins(query="cats", bookmark="CURSOR")
    finally:
        await backend.aclose()
    assert options_of(recorder.requests[0])["bookmark"] == "CURSOR"
    assert result["bookmark"] == "NEXT"


async def test_pin_detail_request(live_settings) -> None:
    backend, recorder = make_backend(
        live_settings, lambda r: json_response(resource_payload(PIN))
    )
    try:
        pin = await backend.get_pin(pin_id="123")
    finally:
        await backend.aclose()
    request = recorder.requests[0]
    assert request.url.path == "/resource/PinResource/get/"
    assert options_of(request)["id"] == "123"
    assert options_of(request)["field_set_key"] == "auth_web_main_pin"
    assert pin["image"] == "https://i.pinimg.com/originals/aa/bb/cc.jpg"


async def test_board_pins_uses_bookmark_array(live_settings) -> None:
    backend, recorder = make_backend(
        live_settings, lambda r: json_response(resource_payload([PIN]))
    )
    try:
        result = await backend.get_board_pins(
            board_id="b1", board_url="/someone/brutalism/", bookmark="CUR"
        )
    finally:
        await backend.aclose()
    options = options_of(recorder.requests[0])
    assert options["bookmarks"] == ["CUR"]
    assert options["board_id"] == "b1"
    assert result["count"] == 1


async def test_unified_comments_list_payload(live_settings) -> None:
    comments = [
        {"id": "c1", "text": "nice", "user": {"id": "u1", "username": "someone"}}
    ]
    backend, recorder = make_backend(
        live_settings, lambda r: json_response(resource_payload(comments))
    )
    try:
        result = await backend.get_pin_comments(pin_id="123")
    finally:
        await backend.aclose()
    assert recorder.requests[0].url.path == "/resource/UnifiedCommentsResource/get/"
    assert result["comments"][0]["text"] == "nice"


async def test_suggestions(live_settings) -> None:
    items = [{"id": "ac_1", "query": "brutalist architecture", "pin_count": 12}]
    backend, _ = make_backend(
        live_settings, lambda r: json_response(resource_payload({"items": items}))
    )
    try:
        result = await backend.search_suggestions(term="brut")
    finally:
        await backend.aclose()
    assert result["suggestions"] == [
        {"query": "brutalist architecture", "id": "ac_1", "pin_count": 12}
    ]


async def test_user_and_boards(live_settings) -> None:
    user = {
        "id": "u1",
        "username": "someone",
        "full_name": "Some One",
        "follower_count": 5,
    }
    board = {
        "id": "b1",
        "name": "brutalism",
        "url": "/someone/brutalism/",
        "pin_count": 3,
        "owner": user,
    }

    def responder(request: httpx.Request) -> httpx.Response:
        data = user if request.url.path.endswith("UserResource/get/") else [board]
        return json_response(resource_payload(data))

    backend, recorder = make_backend(live_settings, responder)
    try:
        fetched_user = await backend.get_user(username="@someone")
        boards = await backend.get_user_boards(username="someone")
    finally:
        await backend.aclose()
    assert fetched_user["followers"] == 5
    assert boards["boards"][0]["name"] == "brutalism"
    assert recorder.requests[0].url.path == "/resource/UserResource/get/"
    assert recorder.requests[1].url.path == "/resource/BoardsResource/get/"


async def test_auth_error_on_http_403(live_settings) -> None:
    backend, _ = make_backend(
        live_settings, lambda r: httpx.Response(403, text="Invalid Resource Request")
    )
    try:
        with pytest.raises(AuthRequiredError) as excinfo:
            await backend.search_pins(query="cats")
    finally:
        await backend.aclose()
    assert "PINTEREST_COOKIE" in str(excinfo.value)


async def test_auth_error_from_payload_status(live_settings) -> None:
    payload = {
        "resource_response": {"status": "error", "code": 11, "message": "not logged in"}
    }
    backend, _ = make_backend(live_settings, lambda r: json_response(payload))
    try:
        with pytest.raises(AuthRequiredError):
            await backend.get_home_feed()
    finally:
        await backend.aclose()


async def test_not_found_on_http_404(live_settings) -> None:
    backend, _ = make_backend(live_settings, lambda r: httpx.Response(404, text="nope"))
    try:
        with pytest.raises(NotFoundError):
            await backend.get_pin(pin_id="0")
    finally:
        await backend.aclose()


async def test_html_instead_of_json_is_reported(live_settings) -> None:
    backend, _ = make_backend(
        live_settings, lambda r: httpx.Response(200, text="<html>login</html>")
    )
    try:
        with pytest.raises(api.PinterestError) as excinfo:
            await backend.search_pins(query="cats")
    finally:
        await backend.aclose()
    assert "cookie may be expired" in str(excinfo.value)


async def test_network_failure_is_reported(live_settings) -> None:
    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route to host")

    backend, _ = make_backend(live_settings, boom)
    try:
        with pytest.raises(BackendError) as excinfo:
            await backend.search_pins(query="cats")
    finally:
        await backend.aclose()
    assert "no route to host" in str(excinfo.value)


async def test_throttle_spaces_out_requests(live_settings) -> None:
    import dataclasses
    import time

    throttled = dataclasses.replace(live_settings, min_interval=0.15)
    backend, recorder = make_backend(
        throttled, lambda r: json_response(resource_payload({"results": []}))
    )
    try:
        start = time.monotonic()
        for _ in range(3):
            await backend.search_pins(query="cats")
        elapsed = time.monotonic() - start
    finally:
        await backend.aclose()
    assert len(recorder.requests) == 3
    assert elapsed >= 0.3, f"min_interval was not honoured (elapsed {elapsed:.3f}s)"


async def test_transient_503_is_retried(live_settings, monkeypatch) -> None:
    import pinterest_mcp.live as live_module

    monkeypatch.setattr(live_module, "_backoff", lambda attempt, retry_after: 0.0)
    calls = {"n": 0}

    def responder(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(
                503, text="upstream connect error or disconnect/reset before headers"
            )
        return json_response(resource_payload({"results": [PIN]}))

    backend, _ = make_backend(live_settings, responder)
    try:
        result = await backend.search_pins(query="cats")
    finally:
        await backend.aclose()
    assert calls["n"] == 2, "one retry is expected"
    assert result["count"] == 1


async def test_repeated_503_surfaces_as_an_error(live_settings, monkeypatch) -> None:
    import pinterest_mcp.live as live_module

    monkeypatch.setattr(live_module, "_backoff", lambda attempt, retry_after: 0.0)
    calls = {"n": 0}

    def responder(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(503, text="upstream connect error")

    backend, _ = make_backend(live_settings, responder)
    try:
        with pytest.raises(api.PinterestError) as excinfo:
            await backend.search_pins(query="cats")
    finally:
        await backend.aclose()
    assert calls["n"] == 3
    assert "503" in str(excinfo.value)


async def test_auth_errors_are_not_retried(live_settings) -> None:
    calls = {"n": 0}

    def responder(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(403, text="Invalid Resource Request")

    backend, _ = make_backend(live_settings, responder)
    try:
        with pytest.raises(AuthRequiredError):
            await backend.search_pins(query="cats")
    finally:
        await backend.aclose()
    assert calls["n"] == 1


async def test_retry_after_header_is_honoured(live_settings, monkeypatch) -> None:
    import pinterest_mcp.live as live_module

    slept: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        slept.append(seconds)

    monkeypatch.setattr(live_module.asyncio, "sleep", fake_sleep)
    calls = {"n": 0}

    def responder(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, headers={"retry-after": "2"}, text="slow down")
        return json_response(resource_payload({"results": []}))

    backend, _ = make_backend(live_settings, responder)
    try:
        await backend.search_pins(query="cats")
    finally:
        await backend.aclose()
    assert slept == [2.0]


async def test_image_download(live_settings) -> None:
    recorder = Recorder(
        lambda r: httpx.Response(
            200, content=b"\xff\xd8\xff", headers={"content-type": "image/jpeg"}
        )
    )
    async with recorder.client() as client:
        data, fmt = await fetch_image(
            live_settings, client, "https://i.pinimg.com/originals/aa/bb/cc.jpg"
        )
    assert (data, fmt) == (b"\xff\xd8\xff", "jpeg")


async def test_image_download_rejects_foreign_hosts(live_settings) -> None:
    async with httpx.AsyncClient() as client:
        with pytest.raises(BackendError) as excinfo:
            await fetch_image(
                live_settings, client, "http://169.254.169.254/latest/meta-data/"
            )
    assert "only Pinterest image hosts" in str(excinfo.value)


async def test_image_download_enforces_size_limit(live_settings) -> None:
    import dataclasses

    small = dataclasses.replace(live_settings, max_image_bytes=4)
    recorder = Recorder(
        lambda r: httpx.Response(
            200, content=b"x" * 10, headers={"content-type": "image/png"}
        )
    )
    async with recorder.client() as client:
        with pytest.raises(BackendError) as excinfo:
            await fetch_image(small, client, "https://i.pinimg.com/a.png")
    assert "PINTEREST_MAX_IMAGE_BYTES" in str(excinfo.value)


PIN_RECORD_KEYS = {
    "id",
    "url",
    "title",
    "grid_title",
    "description",
    "closeup_description",
    "alt_text",
    "auto_alt_text",
    "link",
    "domain",
    "dominant_color",
    "created_at",
    "is_video",
    "reactions",
    "saves",
    "comments",
    "image",
    "images",
    "video",
    "pinner",
    "board",
    "category",
    "section",
    "is_repin",
    "is_promoted",
    "is_story_pin",
    "rich_summary",
    "did_its_count",
}


async def test_record_shape_is_identical_to_archive_mode(
    live_settings, archive
) -> None:
    """Pins from the live and archive backends must obey one record contract."""
    backend, _ = make_backend(
        live_settings, lambda r: json_response(resource_payload(PIN))
    )
    try:
        live_pin = await backend.get_pin(pin_id="123")
    finally:
        await backend.aclose()
    archived = await archive.get_pin(pin_id="914862421434174")
    for record in (live_pin, archived):
        assert set(record) <= PIN_RECORD_KEYS, set(record) - PIN_RECORD_KEYS
        assert {"id", "url"} <= set(record)
        assert json.loads(json.dumps(record)) == record, (
            "records must be JSON serialisable"
        )
