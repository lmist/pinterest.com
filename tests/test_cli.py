"""CLI surface: argument parsing and dispatch onto PinterestApp."""

from __future__ import annotations

import argparse

import pytest

from pinterest_mcp.cli import build_parser, main, pin_id, run
from pinterest_mcp.server import PinterestApp

PIN_ID = "914862421434174"


def test_pin_id_accepts_ids_and_urls() -> None:
    assert pin_id(PIN_ID) == PIN_ID
    assert pin_id(f"https://nl.pinterest.com/pin/{PIN_ID}/") == PIN_ID
    with pytest.raises(argparse.ArgumentTypeError):
        pin_id("https://nl.pinterest.com/someone/board/")


def test_global_flags_work_before_or_after_the_command() -> None:
    parser = build_parser()
    before = parser.parse_args(["--mode", "archive", "--compact", "status"])
    after = parser.parse_args(["status", "--mode", "archive", "--compact"])
    assert (before.mode, before.compact) == (after.mode, after.compact) == ("archive", True)
    plain = parser.parse_args(["status"])
    assert (plain.mode, plain.compact) == (None, False)


async def test_search_dispatches_to_the_backend(settings) -> None:
    app = PinterestApp(settings)
    args = build_parser().parse_args(["search", "brutalist architecture", "-n", "2"])
    result = await run(args, app)
    assert result["count"] == 2
    await app.aclose()


async def test_pin_url_reaches_get_pin(settings) -> None:
    app = PinterestApp(settings)
    args = build_parser().parse_args(["pin", f"https://nl.pinterest.com/pin/{PIN_ID}/"])
    result = await run(args, app)
    assert result["id"] == PIN_ID
    await app.aclose()


def test_no_account_ignores_a_saved_cookie(monkeypatch, capsys) -> None:
    monkeypatch.setenv("PINTEREST_COOKIE", "csrftoken=secret; _auth=1")
    monkeypatch.setenv("PINTEREST_MODE", "archive")
    assert main(["search", "zzzqqqxx", "--no-account"]) == 1
    captured = capsys.readouterr()
    assert "secret" not in captured.out
    assert "secret" not in captured.err
    assert "search 'zzzqqqxx'" in captured.err


def test_login_no_browser_saves_the_environment_cookie(monkeypatch, tmp_path, capsys) -> None:
    from pinterest_mcp.config import Settings

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.delenv("PINTEREST_COOKIE_FILE", raising=False)
    monkeypatch.setenv("PINTEREST_COOKIE", "csrftoken=tok; _auth=1; _pinterest_sess=sess")

    def fail(*args, **kwargs):
        raise AssertionError("browser opened")

    monkeypatch.setattr("webbrowser.open", fail)
    assert main(["login", "--no-browser"]) == 0
    saved = tmp_path / "pinterest-mcp" / "cookie"
    assert saved.read_text(encoding="utf-8").strip() == "csrftoken=tok; _auth=1; _pinterest_sess=sess"
    assert saved.stat().st_mode & 0o777 == 0o600
    captured = capsys.readouterr()
    assert "_pinterest_sess=sess" not in captured.out
    assert "_pinterest_sess=sess" not in captured.err
    assert "Saved session cookie" in captured.err
    monkeypatch.delenv("PINTEREST_COOKIE", raising=False)
    assert Settings.from_env().authenticated


def test_login_uses_headless_when_email_and_password_are_set(monkeypatch, tmp_path, capsys) -> None:
    from pinterest_mcp import login

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.delenv("PINTEREST_COOKIE", raising=False)
    monkeypatch.delenv("PINTEREST_COOKIE_FILE", raising=False)
    monkeypatch.setenv("PINTEREST_EMAIL", "person@example.com")
    monkeypatch.setenv("PINTEREST_PASSWORD", "secret-password")
    seen = {}

    def capture(base_url, email, password, timeout):
        seen["email"] = email
        seen["password"] = password
        return "csrftoken=tok; _auth=1; _pinterest_sess=sess"

    def fail(*args, **kwargs):
        raise AssertionError("browser opened")

    monkeypatch.setattr(login, "capture_with_credentials", capture)
    monkeypatch.setattr(login, "capture_from_default_browser", fail)
    monkeypatch.setattr("webbrowser.open", fail)
    assert main(["login"]) == 0
    assert seen == {"email": "person@example.com", "password": "secret-password"}
    captured = capsys.readouterr()
    assert "secret-password" not in captured.out
    assert "secret-password" not in captured.err
    assert "headless" in captured.err
    assert (tmp_path / "pinterest-mcp" / "cookie").is_file()


def test_login_no_browser_rejects_a_missing_cookie(monkeypatch, tmp_path, capsys) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.delenv("PINTEREST_COOKIE", raising=False)
    monkeypatch.delenv("PINTEREST_COOKIE_FILE", raising=False)
    assert main(["login", "--no-browser"]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "PINTEREST_COOKIE" in captured.err


def test_logout_fails_when_nothing_is_saved(monkeypatch, tmp_path, capsys) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.delenv("PINTEREST_COOKIE", raising=False)
    assert main(["logout"]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "no saved session" in captured.err


def test_errors_go_to_stderr_with_exit_1(capsys, monkeypatch) -> None:
    monkeypatch.setenv("PINTEREST_MODE", "live")
    assert main(["image", "https://example.com/x.jpg"]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "only Pinterest image hosts" in captured.err


def test_cookie_saved_by_login_is_picked_up(monkeypatch, tmp_path) -> None:
    from pinterest_mcp import login

    for name in ("PINTEREST_COOKIE", "PINTEREST_COOKIE_FILE", "PINTEREST_CSRF_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    header = login.cookie_header(
        [
            {"name": "csrftoken", "value": "tok"},
            {"name": "_auth", "value": "1"},
            {"name": "_pinterest_sess", "value": "sess"},
        ]
    )
    path = login.write_cookie(header)
    assert path == tmp_path / "pinterest-mcp" / "cookie"
    assert path.stat().st_mode & 0o777 == 0o600

    from pinterest_mcp.config import Settings

    settings = Settings.from_env()
    assert settings.authenticated
    assert settings.csrf_token == "tok"

    assert main(["logout"]) == 0
    assert not path.exists()
    assert not Settings.from_env().authenticated


def test_signed_in_needs_auth_and_session() -> None:
    from pinterest_mcp.login import _signed_in

    assert not _signed_in([{"name": "_auth", "value": "0"}])
    assert not _signed_in([{"name": "_auth", "value": "1"}])
    assert _signed_in(
        [{"name": "_auth", "value": "1"}, {"name": "_pinterest_sess", "value": "x"}]
    )


def test_decrypts_macos_chromium_cookie_values() -> None:
    import hashlib

    from cryptography.hazmat.primitives import padding
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    from pinterest_mcp.login import decrypt_cookie

    key = hashlib.pbkdf2_hmac("sha1", b"secret", b"saltysalt", 1003, dklen=16)

    def encrypt(plain: bytes) -> bytes:
        padder = padding.PKCS7(128).padder()
        data = padder.update(plain) + padder.finalize()
        enc = Cipher(algorithms.AES(key), modes.CBC(b" " * 16)).encryptor()
        return b"v10" + enc.update(data) + enc.finalize()

    host = ".pinterest.com"
    assert decrypt_cookie(encrypt(b"1"), key, host) == "1"
    prefixed = hashlib.sha256(host.encode()).digest() + b"sess-value"
    assert decrypt_cookie(encrypt(prefixed), key, host) == "sess-value"
