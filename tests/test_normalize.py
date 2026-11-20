"""Payload normalisation: compaction, tolerance of missing fields, refs."""

from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import urlparse

import pytest

from pinterest_mcp import normalize

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def _captured(har_entries: list[dict], resource: str, predicate=None) -> dict:
    """First captured JSON payload for a resource, base64 bodies decoded."""
    import base64

    fallback = None
    for entry in har_entries:
        parsed = urlparse(entry["request"]["url"])
        if not parsed.path.startswith(f"/resource/{resource}/"):
            continue
        content = entry["response"]["content"]
        text = content.get("text") or ""
        if content.get("encoding") == "base64":
            text = base64.b64decode(text).decode("utf-8", "replace")
        if not text.startswith("{"):
            continue
        payload = json.loads(text)
        if predicate is None:
            return payload
        if predicate(payload):
            return payload
        fallback = fallback or payload
    if fallback is not None:
        return fallback
    pytest.skip(f"no captured {resource} payload")


def test_pin_record_from_search_result(har_entries: list[dict]) -> None:
    payload = _captured(har_entries, "BaseSearchResource")
    pin = payload["resource_response"]["data"]["results"][0]
    record = normalize.pin_record(pin)
    assert record["id"] == str(pin["id"])
    assert record["url"].endswith(f"/pin/{pin['id']}/")
    assert record["image"].startswith("https://i.pinimg.com/")
    assert set(record["images"]) <= set(normalize.IMAGE_SIZES)
    assert record["pinner"]["username"]
    assert "board" in record
    assert len(json.dumps(record)) < len(json.dumps(pin)), (
        "records must be much smaller than raw pins"
    )


def test_best_image_prefers_largest_then_falls_back() -> None:
    pin = {
        "id": "1",
        "images": {
            "236x": {"url": "https://i.pinimg.com/236x/a.jpg"},
            "474x": {"url": "https://i.pinimg.com/474x/a.jpg"},
        },
    }
    assert normalize.best_image(pin) == "https://i.pinimg.com/474x/a.jpg"
    assert normalize.best_image(pin, size="236x") == "https://i.pinimg.com/236x/a.jpg"
    assert normalize.best_image(pin, size="orig") == "https://i.pinimg.com/474x/a.jpg"
    assert normalize.best_image({"id": "2"}) is None


def test_pin_record_drops_unusable_values() -> None:
    record = normalize.pin_record(
        {
            "id": 42,
            "title": "   ",
            "description": "",
            "link": None,
            "pinner": {},
            "board": {"id": "b1"},
            "images": {"orig": {"url": ""}},
        }
    )
    assert record == {
        "id": "42",
        "url": "https://www.pinterest.com/pin/42/",
        "board": {"id": "b1"},
    }


def test_pin_record_ignores_non_pins() -> None:
    for value in ({}, None, {"title": "no id"}, [], "pin"):
        assert normalize.pin_record(value) == {}


def test_video_pin_exposes_stream_and_thumbnail() -> None:
    record = normalize.pin_record(
        {
            "id": "7",
            "is_video": True,
            "videos": {
                "video_list": {
                    "V_HLSV3_MOBILE": {
                        "url": "https://v1.pinimg.com/small.m3u8",
                        "duration": 1000,
                    },
                    "V_HLSV4": {
                        "url": "https://v1.pinimg.com/main.m3u8",
                        "duration": 89433,
                        "thumbnail": "https://i.pinimg.com/thumb.jpg",
                    },
                }
            },
        }
    )
    assert record["is_video"] is True
    assert record["video"] == {
        "url": "https://v1.pinimg.com/main.m3u8",
        "duration_ms": 89433,
        "thumbnail": "https://i.pinimg.com/thumb.jpg",
    }


def test_detailed_pin_keeps_extra_fields() -> None:
    record = normalize.pin_record(
        {
            "id": "9",
            "category": "art",
            "did_its": [{"id": "1"}, {"id": "2"}],
            "section": None,
        },
        detailed=True,
    )
    assert record["category"] == "art"
    assert record["did_its_count"] == 2
    assert "section" not in record


def test_user_record_from_captured_profile(har_entries: list[dict]) -> None:
    payload = _captured(har_entries, "UserResource")
    record = normalize.user_record(payload["resource_response"]["data"])
    assert record["username"] == "allidoizwin"
    assert record["url"] == "https://www.pinterest.com/allidoizwin/"
    assert "followers" in record and "boards" in record


def test_user_record_requires_an_id() -> None:
    assert normalize.user_record({"username": "ghost"}) == {}


def test_board_record_from_captured_board(har_entries: list[dict]) -> None:
    payload = _captured(
        har_entries,
        "BoardResource",
        predicate=lambda p: (
            (p["resource_response"]["data"] or {}).get("name") == "brutalism"
        ),
    )
    record = normalize.board_record(payload["resource_response"]["data"])
    assert record["name"] == "brutalism"
    assert record["url"] == "https://www.pinterest.com/allidoizwin/brutalism/"
    assert record["owner"]["username"] == "allidoizwin"


def test_comment_record_shape() -> None:
    record = normalize.comment_record(
        {
            "id": "c1",
            "text": "nice",
            "created_at": "Thu, 09 Jan 2025 09:21:58 +0000",
            "user": {"id": "u1", "username": "ccbconst", "full_name": "Ccb Const"},
            "comment_count": 3,
        }
    )
    assert record["text"] == "nice"
    assert record["replies"] == 3
    assert record["user"]["profile_url"] == "https://www.pinterest.com/ccbconst/"


def test_suggestion_record_needs_a_query() -> None:
    assert normalize.suggestion_record({"id": "ac_1"}) == {}
    assert normalize.suggestion_record(
        {"query": "brunette girl", "id": "ac_1", "pin_count": 0}
    ) == {
        "query": "brunette girl",
        "id": "ac_1",
    }


def test_related_modules_group_by_bucket_title(har_entries: list[dict]) -> None:
    payload = _captured(har_entries, "RelatedModulesResource")
    data = (
        payload["resource_response"]["data"]
        if "resource_response" in payload
        else payload
    )
    modules = normalize.related_modules(data)
    assert modules, "captured related payload must contain pins"
    assert all(set(m) == {"title", "pins"} for m in modules)
    total = sum(len(m["pins"]) for m in modules)
    pins_in_payload = sum(
        1 for item in data if isinstance(item, dict) and item.get("type") == "pin"
    )
    assert total == pins_in_payload


def test_pin_list_skips_story_modules(har_entries: list[dict]) -> None:
    payload = _captured(har_entries, "RelatedModulesResource")
    data = (
        payload["resource_response"]["data"]
        if "resource_response" in payload
        else payload
    )
    records = normalize.pin_list(data)
    assert records
    assert all(isinstance(r["id"], str) for r in records)


def test_page_envelope_carries_bookmark() -> None:
    assert normalize.page([], bookmark="NEXT") == {
        "count": 0,
        "results": [],
        "bookmark": "NEXT",
    }
    assert normalize.page(None) == {"count": 0, "results": []}
