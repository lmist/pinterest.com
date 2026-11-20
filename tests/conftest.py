"""Shared fixtures.

Tests that need the captured HAR are skipped with a clear reason when the file
is absent, so the suite still runs on a checkout without the capture.
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from pinterest_mcp.config import Settings

REPO_ROOT = Path(__file__).resolve().parents[1]
HAR_CANDIDATES = (
    REPO_ROOT / "har" / "nl.pinterest.com.har",
    REPO_ROOT / "nl.pinterest.com.har",
)


def _har_path() -> Path:
    for candidate in HAR_CANDIDATES:
        if candidate.is_file():
            return candidate
    pytest.skip(
        "captured HAR not present; run these tests where nl.pinterest.com.har exists"
    )
    raise AssertionError("unreachable")


@pytest.fixture(scope="session")
def har_path() -> Path:
    return _har_path()


@pytest.fixture(scope="session")
def har_entries(har_path: Path) -> list[dict]:
    with har_path.open("rb") as handle:
        return json.load(handle)["log"]["entries"]


def _settings(har_path: Path, **overrides) -> Settings:
    base = Settings(
        mode="archive",
        base_url="https://nl.pinterest.com",
        cookie="",
        csrf_token="",
        app_version="2b09e40",
        user_agent="pytest-agent",
        timeout=5.0,
        min_interval=0.0,
        max_image_bytes=1024 * 1024,
        har_path=har_path,
    )
    return replace(base, **overrides)


@pytest.fixture
def settings(har_path: Path) -> Settings:
    return _settings(har_path)


@pytest.fixture
def live_settings(har_path: Path) -> Settings:
    return _settings(
        har_path, mode="live", cookie="csrftoken=abc; _auth=1", csrf_token="abc"
    )


@pytest.fixture
def archive(settings: Settings):
    from pinterest_mcp.archive import ArchiveBackend

    return ArchiveBackend(settings.har_path)
