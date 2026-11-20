"""Environment handling and mode selection."""

from __future__ import annotations

import pytest

from pinterest_mcp.config import ConfigError, Settings


@pytest.fixture(autouse=True)
def clean_env(monkeypatch: pytest.MonkeyPatch, tmp_path):
    # Never pick up a real cookie saved by `pinterest login`.
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    for name in (
        "PINTEREST_COOKIE",
        "PINTEREST_COOKIE_FILE",
        "PINTEREST_CSRF_TOKEN",
        "PINTEREST_BASE_URL",
        "PINTEREST_APP_VERSION",
        "PINTEREST_USER_AGENT",
        "PINTEREST_TIMEOUT",
        "PINTEREST_MIN_INTERVAL",
        "PINTEREST_MAX_IMAGE_BYTES",
        "PINTEREST_HAR",
        "PINTEREST_MODE",
    ):
        monkeypatch.delenv(name, raising=False)
    return monkeypatch


def test_live_is_the_default_mode(clean_env, har_path) -> None:
    clean_env.setenv("PINTEREST_HAR", str(har_path))
    settings = Settings.from_env()
    assert settings.mode == "live"
    assert settings.live is True
    assert settings.authenticated is False, "public Pinterest access needs no cookie"


def test_archive_mode_is_opt_in(clean_env, har_path) -> None:
    clean_env.setenv("PINTEREST_HAR", str(har_path))
    clean_env.setenv("PINTEREST_MODE", "archive")
    settings = Settings.from_env()
    assert settings.mode == "archive"
    assert settings.live is False


def test_unknown_mode_is_rejected(clean_env, har_path) -> None:
    clean_env.setenv("PINTEREST_HAR", str(har_path))
    clean_env.setenv("PINTEREST_MODE", "cached")
    with pytest.raises(ConfigError):
        Settings.from_env()


def test_no_account_ignores_cookie_sources(clean_env, tmp_path, har_path) -> None:
    cookie_file = tmp_path / "cookie"
    cookie_file.write_text("csrftoken=fromfile; _auth=1\n", encoding="utf-8")
    clean_env.setenv("PINTEREST_COOKIE", "csrftoken=inline; _auth=1")
    clean_env.setenv("PINTEREST_COOKIE_FILE", str(cookie_file))
    clean_env.setenv("PINTEREST_NO_ACCOUNT", "1")
    settings = Settings.from_env()
    assert settings.authenticated is False
    assert settings.csrf_token == ""


def test_cookie_marks_the_session_authenticated(clean_env, har_path) -> None:
    clean_env.setenv("PINTEREST_HAR", str(har_path))
    clean_env.setenv("PINTEREST_COOKIE", "csrftoken=xyz; _auth=1")
    settings = Settings.from_env()
    assert settings.mode == "live"
    assert settings.authenticated is True
    assert settings.csrf_token == "xyz", "csrf token is read from the cookie when unset"


def test_explicit_csrf_token_wins(clean_env, har_path) -> None:
    clean_env.setenv("PINTEREST_HAR", str(har_path))
    clean_env.setenv("PINTEREST_COOKIE", "csrftoken=xyz")
    clean_env.setenv("PINTEREST_CSRF_TOKEN", "explicit")
    assert Settings.from_env().csrf_token == "explicit"


def test_cookie_file_with_pasted_header(clean_env, tmp_path, har_path) -> None:
    cookie_file = tmp_path / "cookies.txt"
    cookie_file.write_text("Cookie: csrftoken=fromfile; _auth=2\n", encoding="utf-8")
    clean_env.setenv("PINTEREST_HAR", str(har_path))
    clean_env.setenv("PINTEREST_COOKIE_FILE", str(cookie_file))
    settings = Settings.from_env()
    assert settings.cookie == "csrftoken=fromfile; _auth=2"
    assert settings.csrf_token == "fromfile"


def test_missing_cookie_file_is_an_error(clean_env, tmp_path, har_path) -> None:
    clean_env.setenv("PINTEREST_HAR", str(har_path))
    clean_env.setenv("PINTEREST_COOKIE_FILE", str(tmp_path / "nope.txt"))
    with pytest.raises(ConfigError):
        Settings.from_env()


def test_base_url_is_normalised(clean_env, har_path) -> None:
    clean_env.setenv("PINTEREST_HAR", str(har_path))
    clean_env.setenv("PINTEREST_BASE_URL", "https://www.pinterest.co.uk/")
    assert Settings.from_env().base_url == "https://www.pinterest.co.uk"


def test_invalid_base_url_rejected(clean_env, har_path) -> None:
    clean_env.setenv("PINTEREST_HAR", str(har_path))
    clean_env.setenv("PINTEREST_BASE_URL", "pinterest.com")
    with pytest.raises(ConfigError):
        Settings.from_env()


def test_numeric_overrides(clean_env, har_path) -> None:
    clean_env.setenv("PINTEREST_HAR", str(har_path))
    clean_env.setenv("PINTEREST_TIMEOUT", "12.5")
    clean_env.setenv("PINTEREST_MIN_INTERVAL", "2")
    clean_env.setenv("PINTEREST_MAX_IMAGE_BYTES", "2048")
    settings = Settings.from_env()
    assert (settings.timeout, settings.min_interval, settings.max_image_bytes) == (
        12.5,
        2.0,
        2048,
    )


def test_bad_number_is_reported(clean_env, har_path) -> None:
    clean_env.setenv("PINTEREST_HAR", str(har_path))
    clean_env.setenv("PINTEREST_TIMEOUT", "soon")
    with pytest.raises(ConfigError):
        Settings.from_env()


def test_missing_har_is_reported(clean_env, tmp_path) -> None:
    clean_env.setenv("PINTEREST_HAR", str(tmp_path / "missing.har"))
    with pytest.raises(ConfigError):
        Settings.from_env()
