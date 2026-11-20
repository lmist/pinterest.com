"""MCP surface: tool registration, argument plumbing, error translation."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import httpx
import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from support import Recorder, json_response, options_of, resource_payload

from pinterest_mcp.backend import BackendError
from pinterest_mcp.config import Settings
from pinterest_mcp.server import PinterestApp, create_server

REPO_ROOT = Path(__file__).resolve().parents[1]

EXPECTED_TOOLS = {
    "search_pins",
    "search_suggestions",
    "get_pin",
    "get_related_pins",
    "get_user",
    "get_user_boards",
    "get_board",
    "get_board_pins",
    "get_pin_comments",
    "get_home_feed",
    "fetch_image",
    "pinterest_status",
}

PIN_ID = "914862421434174"


def archive_app(settings: Settings) -> PinterestApp:
    return PinterestApp(settings)


def live_app(live_settings: Settings, responder) -> tuple[PinterestApp, Recorder]:
    from pinterest_mcp.live import LiveBackend

    recorder = Recorder(responder)
    backend = LiveBackend(live_settings, client=recorder.client())
    app = PinterestApp(live_settings, backend=backend, http=recorder.client())
    return app, recorder


async def test_all_tools_are_registered(settings) -> None:
    app = archive_app(settings)
    server = create_server(settings, app=app)
    names = {tool.name for tool in await server.list_tools()}
    assert names == EXPECTED_TOOLS
    await app.aclose()


async def test_tools_have_descriptions_and_schemas(settings) -> None:
    app = archive_app(settings)
    server = create_server(settings, app=app)
    for tool in await server.list_tools():
        assert tool.description and len(tool.description) > 40, tool.name
        assert tool.input_schema["type"] == "object"
    await app.aclose()


async def test_search_pin_arguments_reach_the_backend(settings) -> None:
    app = archive_app(settings)
    server = create_server(settings, app=app)
    result = await server.call_tool(
        "search_pins", {"query": "brutalist architecture", "page_size": 2}
    )
    payload = json.loads(result.content[0].text)
    assert payload["count"] == 2
    assert result.structured_content["count"] == 2
    await app.aclose()


async def test_get_pin_returns_a_record(settings) -> None:
    app = archive_app(settings)
    server = create_server(settings, app=app)
    result = await server.call_tool("get_pin", {"pin_id": PIN_ID})
    payload = json.loads(result.content[0].text)
    assert payload["id"] == PIN_ID
    await app.aclose()


async def test_backend_failures_become_tool_errors(settings) -> None:
    app = archive_app(settings)
    server = create_server(settings, app=app)
    with pytest.raises(Exception) as excinfo:
        await server.call_tool("get_pin", {"pin_id": "000000000"})
    assert "not in the captured archive" in str(excinfo.value)
    await app.aclose()


async def test_live_mode_sends_the_session_cookie(live_settings) -> None:
    pin = {
        "id": "123",
        "images": {"orig": {"url": "https://i.pinimg.com/originals/a.jpg"}},
    }
    app, recorder = live_app(
        live_settings, lambda r: json_response(resource_payload(pin))
    )
    server = create_server(live_settings, app=app)
    try:
        result = await server.call_tool("get_pin", {"pin_id": "123"})
    finally:
        await app.aclose()
    assert json.loads(result.content[0].text)["id"] == "123"
    assert recorder.requests[0].headers["cookie"] == "csrftoken=abc; _auth=1"


async def test_live_search_passes_query_scope_and_bookmark(live_settings) -> None:
    app, recorder = live_app(
        live_settings, lambda r: json_response(resource_payload({"results": []}))
    )
    server = create_server(live_settings, app=app)
    try:
        await server.call_tool(
            "search_pins",
            {"query": "cats", "scope": "my_pins", "page_size": 5, "bookmark": "CUR"},
        )
    finally:
        await app.aclose()
    options = options_of(recorder.requests[0])
    assert options["query"] == "cats"
    assert options["scope"] == "my_pins"
    assert options["bookmark"] == "CUR"
    assert options["page_size"] == 5
    assert recorder.requests[0].url.path == "/resource/BaseSearchResource/get/"


async def test_auth_failure_is_reported_as_a_tool_error(live_settings) -> None:
    app, _ = live_app(
        live_settings, lambda r: httpx.Response(403, text="Invalid Resource Request")
    )
    server = create_server(live_settings, app=app)
    try:
        with pytest.raises(Exception) as excinfo:
            await server.call_tool("search_pins", {"query": "cats"})
    finally:
        await app.aclose()
    assert "PINTEREST_COOKIE" in str(excinfo.value)


async def test_fetch_image_returns_image_content(live_settings) -> None:
    app, _ = live_app(
        live_settings,
        lambda r: httpx.Response(
            200, content=b"\xff\xd8\xff", headers={"content-type": "image/jpeg"}
        ),
    )
    server = create_server(live_settings, app=app)
    try:
        result = await server.call_tool(
            "fetch_image", {"url": "https://i.pinimg.com/originals/a.jpg"}
        )
    finally:
        await app.aclose()
    assert result.content[0].type == "image"
    assert result.content[0].mime_type == "image/jpeg"


async def test_fetch_image_rejects_non_pinterest_urls(live_settings) -> None:
    app, _ = live_app(live_settings, lambda r: httpx.Response(200, content=b""))
    server = create_server(live_settings, app=app)
    try:
        with pytest.raises(Exception) as excinfo:
            await server.call_tool(
                "fetch_image", {"url": "https://example.com/evil.jpg"}
            )
    finally:
        await app.aclose()
    assert "only Pinterest image hosts" in str(excinfo.value)


async def test_status_reports_archive_mode(settings) -> None:
    app = archive_app(settings)
    server = create_server(settings, app=app)
    result = await server.call_tool("pinterest_status", {})
    payload = json.loads(result.content[0].text)
    assert payload["mode"] == "archive"
    assert payload["archive"].endswith(".har")
    assert payload["pins"] > 0
    await app.aclose()


def test_archive_mode_without_a_capture_is_a_clear_error() -> None:
    bare = Settings(
        mode="archive",
        base_url="https://nl.pinterest.com",
        cookie="",
        csrf_token="",
        app_version="1",
        user_agent="agent",
        timeout=1.0,
        min_interval=0.0,
        max_image_bytes=1024,
        har_path=None,
    )
    with pytest.raises(BackendError) as excinfo:
        PinterestApp(bare)
    assert "PINTEREST_HAR" in str(excinfo.value)


def test_live_mode_needs_no_cookie() -> None:
    anonymous = Settings(
        mode="live",
        base_url="https://nl.pinterest.com",
        cookie="",
        csrf_token="",
        app_version="1",
        user_agent="agent",
        timeout=1.0,
        min_interval=0.0,
        max_image_bytes=1024,
        har_path=None,
    )
    app = PinterestApp(anonymous)
    assert app.pinterest_status()["mode"] == "live"
    assert app.pinterest_status()["authenticated"] is False


async def test_end_to_end_over_stdio(settings) -> None:
    """Drive the real server process over stdio, as an MCP client would."""
    import os

    env = dict(os.environ)
    env["PINTEREST_HAR"] = str(settings.har_path)
    env["PINTEREST_MODE"] = "archive"
    env.pop("PINTEREST_COOKIE", None)
    env.pop("PINTEREST_COOKIE_FILE", None)

    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "pinterest_mcp"],
        env=env,
        cwd=str(REPO_ROOT),
    )
    async with (
        stdio_client(params) as (read, write),
        ClientSession(read, write) as session,
    ):
        await session.initialize()
        tools = await session.list_tools()
        assert {tool.name for tool in tools.tools} == EXPECTED_TOOLS

        status = await session.call_tool("pinterest_status", {})
        assert json.loads(status.content[0].text)["mode"] == "archive"

        search = await session.call_tool(
            "search_pins", {"query": "brutalist architecture"}
        )
        payload = json.loads(search.content[0].text)
        assert payload["count"] > 0
        assert payload["results"][0]["image"].startswith("https://i.pinimg.com/")

        pin_id = payload["results"][0]["id"]
        pin = await session.call_tool("get_pin", {"pin_id": pin_id})
        assert json.loads(pin.content[0].text)["id"] == pin_id
