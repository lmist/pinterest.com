"""Archive backend: retrieval and search quality over the captured HAR."""

from __future__ import annotations

import pytest

from pinterest_mcp.archive import ArchiveBackend
from pinterest_mcp.backend import BackendError

PIN_ID = "914862421434174"
BOARD_ID = "1149403204839652960"
COMMENTED_PIN = "5396881410717955008"


def test_status_reports_capture_contents(archive: ArchiveBackend) -> None:
    status = archive.status()
    assert status["mode"] == "archive"
    assert status["entries"] > 700
    assert status["pins"] > 100
    assert status["boards"] > 0
    assert "brutalist architecture" in status["captured_searches"]


async def test_search_returns_the_captured_page(archive: ArchiveBackend) -> None:
    result = await archive.search_pins(query="brutalist architecture")
    assert result["match"] == "captured_search"
    assert result["scope"] == "pins"
    assert result["count"] == 16
    assert all(r["id"] and r["url"] for r in result["results"])


async def test_search_is_case_insensitive_and_scope_agnostic(
    archive: ArchiveBackend,
) -> None:
    result = await archive.search_pins(
        query="  Brutalist Architecture ", scope="my_pins"
    )
    assert result["match"] == "captured_search"


async def test_search_falls_back_to_fuzzy_captured_query(
    archive: ArchiveBackend,
) -> None:
    result = await archive.search_pins(query="niche coffee")
    assert result["match"] == "captured_search"
    assert result["matched_query"] == "niche coffee shop"


async def test_search_falls_back_to_pin_text_search(archive: ArchiveBackend) -> None:
    result = await archive.search_pins(query="concrete stairs")
    assert result["match"] == "pin_text_search"
    assert result["count"] > 0
    assert any(
        "concrete"
        in (
            r.get("description", "")
            + " "
            + r.get("alt_text", "")
            + " "
            + r.get("auto_alt_text", "")
        ).lower()
        for r in result["results"]
    )


async def test_search_prefers_captured_query_over_text_match(
    archive: ArchiveBackend,
) -> None:
    result = await archive.search_pins(query="edinburgh castles")
    assert result["match"] == "captured_search"
    assert result["matched_query"] == "castles in edinburgh"


async def test_search_honours_page_size(archive: ArchiveBackend) -> None:
    result = await archive.search_pins(query="brutalist architecture", page_size=3)
    assert result["count"] == 3


async def test_search_rejects_unknown_query(archive: ArchiveBackend) -> None:
    with pytest.raises(BackendError) as excinfo:
        await archive.search_pins(query="zzzqqqxx")
    assert "not in the captured archive" in str(excinfo.value)


async def test_search_refuses_bookmark_pagination(archive: ArchiveBackend) -> None:
    with pytest.raises(BackendError) as excinfo:
        await archive.search_pins(query="brutalist architecture", bookmark="CURSOR")
    assert "PINTEREST_COOKIE" in str(excinfo.value)


async def test_suggestions_are_prefix_ranked(archive: ArchiveBackend) -> None:
    result = await archive.search_suggestions(term="brut")
    assert result["count"] > 0
    assert all(s["query"].startswith("brut") for s in result["suggestions"])


async def test_suggestions_for_empty_term_return_capture(
    archive: ArchiveBackend,
) -> None:
    result = await archive.search_suggestions(term="")
    assert result["count"] > 0


async def test_unknown_suggestion_term_raises(archive: ArchiveBackend) -> None:
    with pytest.raises(BackendError):
        await archive.search_suggestions(term="qwertyuiopasdf")


async def test_get_pin_returns_detail(archive: ArchiveBackend) -> None:
    pin = await archive.get_pin(pin_id=PIN_ID)
    assert pin["id"] == PIN_ID
    assert pin["url"] == f"https://www.pinterest.com/pin/{PIN_ID}/"
    assert "orig" in pin["images"]
    assert pin["board"]["name"] == "Constructivism"


async def test_get_pin_unknown_id_raises(archive: ArchiveBackend) -> None:
    with pytest.raises(BackendError):
        await archive.get_pin(pin_id="000000000000000000")


async def test_get_pin_falls_back_to_grid_record(archive: ArchiveBackend) -> None:
    # A pin that only ever appeared in a board feed still has to be retrievable.
    result = await archive.get_board_pins(board_id=BOARD_ID)
    for pin in result["results"][:1]:
        fetched = await archive.get_pin(pin_id=pin["id"])
        assert fetched["id"] == pin["id"]


async def test_related_pins_grouped(archive: ArchiveBackend) -> None:
    result = await archive.get_related_pins(pin_id=PIN_ID)
    assert result["count"] > 0
    assert sum(len(m["pins"]) for m in result["modules"]) == result["count"]


async def test_related_pins_unknown_pin_raises(archive: ArchiveBackend) -> None:
    with pytest.raises(BackendError):
        await archive.get_related_pins(pin_id="123")


async def test_user_profile(archive: ArchiveBackend) -> None:
    user = await archive.get_user(username="allidoizwin")
    assert user["username"] == "allidoizwin"
    assert "partial" not in user


async def test_user_accepts_at_prefix(archive: ArchiveBackend) -> None:
    user = await archive.get_user(username="@allidoizwin")
    assert user["username"] == "allidoizwin"


async def test_pinner_only_user_is_marked_partial(archive: ArchiveBackend) -> None:
    pin = await archive.get_pin(pin_id=PIN_ID)
    username = pin["pinner"]["username"]
    user = await archive.get_user(username=username)
    assert user["username"] == username
    if "followers" not in user:
        assert user["partial"] is True


async def test_unknown_user_raises(archive: ArchiveBackend) -> None:
    with pytest.raises(BackendError):
        await archive.get_user(username="definitely-not-a-user-xyz")


async def test_user_boards_only_lists_full_boards(archive: ArchiveBackend) -> None:
    result = await archive.get_user_boards(username="allidoizwin")
    assert result["count"] >= 1
    assert all(board.get("name") and board.get("url") for board in result["boards"])
    assert any(board["name"] == "brutalism" for board in result["boards"])


async def test_board_lookup_by_owner_and_slug(archive: ArchiveBackend) -> None:
    board = await archive.get_board(username="allidoizwin", slug="brutalism")
    assert board["id"] == BOARD_ID
    assert board["pin_count"] == 3


async def test_unknown_board_raises(archive: ArchiveBackend) -> None:
    with pytest.raises(BackendError):
        await archive.get_board(username="allidoizwin", slug="nope")


async def test_board_pins(archive: ArchiveBackend) -> None:
    result = await archive.get_board_pins(
        board_id=BOARD_ID, board_url="/allidoizwin/brutalism/"
    )
    assert result["count"] > 0
    assert result["results"][0]["image"].startswith("https://i.pinimg.com/")


async def test_board_pins_resolves_id_from_url(archive: ArchiveBackend) -> None:
    result = await archive.get_board_pins(
        board_id="unknown", board_url="/allidoizwin/brutalism/"
    )
    assert result["board_id"] == BOARD_ID


async def test_board_pins_unknown_board_raises(archive: ArchiveBackend) -> None:
    with pytest.raises(BackendError):
        await archive.get_board_pins(board_id="999")


async def test_comments(archive: ArchiveBackend) -> None:
    result = await archive.get_pin_comments(pin_id=COMMENTED_PIN)
    assert result["count"] >= 1
    assert result["comments"][0]["text"]
    assert result["comments"][0]["user"]["username"]


async def test_comments_unknown_pin_raises(archive: ArchiveBackend) -> None:
    with pytest.raises(BackendError):
        await archive.get_pin_comments(pin_id="123")


async def test_home_feed_is_normalised(archive: ArchiveBackend) -> None:
    result = await archive.get_home_feed()
    assert result["count"] > 0
    assert all("images" in pin for pin in result["results"])


async def test_home_feed_honours_page_size(archive: ArchiveBackend) -> None:
    result = await archive.get_home_feed(page_size=5)
    assert result["count"] == 5
