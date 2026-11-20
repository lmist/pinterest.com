"""Interactive sign-in: get a signed-in Pinterest session cookie and save it to
the default cookie file.

Two ways in:

* default browser (macOS, Chromium-based browsers) - open the login page in the
  user's own browser, then read the session out of that browser's cookie store
  once it shows up. Decrypting it needs the browser's "Safe Storage" key, so
  macOS asks once for Keychain access.
* automated - drive a fresh Chrome window with Playwright
  (``uv sync --extra login``) and watch its cookies directly.
"""

from __future__ import annotations

import hashlib
import os
import plistlib
import shutil
import sqlite3
import subprocess
import tempfile
import time
import webbrowser
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

from .config import ConfigError, config_dir, default_cookie_file

LOGIN_TIMEOUT = 600.0
POLL_INTERVAL = 1.0
# Chromium flushes cookies to disk lazily, so the store lags the browser a bit.
STORE_POLL_INTERVAL = 2.0


@dataclass(frozen=True)
class ChromiumBrowser:
    name: str
    data_dir: str  # under ~/Library/Application Support
    keychain_service: str


# macOS bundle id -> where that browser keeps its profiles and cookie key.
CHROMIUM_BROWSERS = {
    "company.thebrowser.dia": ChromiumBrowser("Dia", "Dia/User Data", "Dia Safe Storage"),
    "company.thebrowser.browser": ChromiumBrowser("Arc", "Arc/User Data", "Arc Safe Storage"),
    "com.google.chrome": ChromiumBrowser("Chrome", "Google/Chrome", "Chrome Safe Storage"),
    "com.brave.browser": ChromiumBrowser(
        "Brave", "BraveSoftware/Brave-Browser", "Brave Safe Storage"
    ),
    "com.microsoft.edgemac": ChromiumBrowser(
        "Edge", "Microsoft Edge", "Microsoft Edge Safe Storage"
    ),
    "com.vivaldi.vivaldi": ChromiumBrowser("Vivaldi", "Vivaldi", "Vivaldi Safe Storage"),
}


def browser_profile_dir() -> Path:
    """Persistent profile, so a later ``login`` is usually already signed in."""
    return config_dir() / "browser"


def _signed_in(cookies: list[dict]) -> bool:
    values = {c["name"]: c["value"] for c in cookies}
    return values.get("_auth") == "1" and bool(values.get("_pinterest_sess"))


def cookie_header(cookies: list[dict]) -> str:
    return "; ".join(f"{c['name']}={c['value']}" for c in cookies)


def cookie_from_env() -> str:
    """Cookie header from ``PINTEREST_COOKIE`` or ``PINTEREST_COOKIE_FILE``.

    Does not read the file ``login`` itself writes, and does not open a browser.
    """
    inline = (os.environ.get("PINTEREST_COOKIE") or "").strip()
    path = (os.environ.get("PINTEREST_COOKIE_FILE") or "").strip()
    if inline and path:
        raise ConfigError("set PINTEREST_COOKIE or PINTEREST_COOKIE_FILE, not both")
    if inline:
        header = inline
    elif path:
        cookie_file = Path(path).expanduser()
        if not cookie_file.is_file():
            raise ConfigError(f"PINTEREST_COOKIE_FILE does not exist: {cookie_file}")
        header = cookie_file.read_text(encoding="utf-8").strip()
        if header.lower().startswith("cookie:"):
            header = header.split(":", 1)[1].strip()
    else:
        raise ConfigError(
            "set PINTEREST_COOKIE or PINTEREST_COOKIE_FILE, or run `pinterest login` to open a browser"
        )
    if "_auth=1" not in header or "_pinterest_sess=" not in header:
        raise ConfigError("cookie is not a signed-in Pinterest session")
    return header


def write_cookie(header: str, path: Path | None = None) -> Path:
    """Write the cookie header readable by the owner only."""
    path = path or default_cookie_file()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(header + "\n")
    os.chmod(path, 0o600)
    return path


# ------------------------------------------------------------ default browser


def default_browser_id() -> str | None:
    """Bundle id of the macOS default https handler, e.g. ``company.thebrowser.dia``."""
    prefs = (
        Path.home()
        / "Library/Preferences/com.apple.LaunchServices/com.apple.launchservices.secure.plist"
    )
    try:
        handlers = plistlib.loads(prefs.read_bytes()).get("LSHandlers", [])
    except (OSError, plistlib.InvalidFileException):
        return None
    for handler in handlers:
        if handler.get("LSHandlerURLScheme") == "https":
            return (handler.get("LSHandlerRoleAll") or "").lower() or None
    return None


def _keychain_key(service: str) -> bytes:
    result = subprocess.run(
        ["security", "find-generic-password", "-w", "-s", service],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise ConfigError(f"could not read {service!r} from the Keychain (access denied?)")
    return hashlib.pbkdf2_hmac(
        "sha1", result.stdout.strip().encode(), b"saltysalt", 1003, dklen=16
    )


def decrypt_cookie(encrypted: bytes, key: bytes, host_key: str) -> str:
    """Decrypt a macOS Chromium ``v10`` cookie value."""
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    if not encrypted.startswith(b"v10"):
        return encrypted.decode("utf-8", "replace")
    decryptor = Cipher(algorithms.AES(key), modes.CBC(b" " * 16)).decryptor()
    plain = decryptor.update(encrypted[3:]) + decryptor.finalize()
    plain = plain[: -plain[-1]]  # PKCS#7
    # Cookie DB schema 24+ prefixes the value with sha256(host_key).
    digest = hashlib.sha256(host_key.encode()).digest()
    if plain.startswith(digest):
        plain = plain[len(digest) :]
    return plain.decode("utf-8")


def _read_store(db: Path, key: bytes, host: str) -> list[dict]:
    """Cookies that apply to ``host`` from one profile's store, newest first.

    The browser holds the database open, so read a private copy.
    """
    with tempfile.TemporaryDirectory() as tmp:
        copy = Path(tmp) / "Cookies"
        shutil.copy2(db, copy)
        for suffix in ("-wal", "-journal"):
            side = db.with_name(db.name + suffix)
            if side.exists():
                shutil.copy2(side, copy.with_name(copy.name + suffix))
        conn = sqlite3.connect(copy)
        try:
            rows = conn.execute(
                "SELECT host_key, name, value, encrypted_value, last_access_utc "
                "FROM cookies WHERE host_key LIKE '%pinterest.com' "
                "ORDER BY last_access_utc DESC"
            ).fetchall()
        finally:
            conn.close()
    cookies, seen = [], set()
    for host_key, name, value, encrypted, accessed in rows:
        domain = host_key.lstrip(".")
        if not (host == domain or host.endswith("." + domain)) or name in seen:
            continue
        seen.add(name)
        cookies.append(
            {
                "name": name,
                "value": value or decrypt_cookie(encrypted, key, host_key),
                "accessed": accessed,
            }
        )
    return cookies


def signed_in_cookies(browser: ChromiumBrowser, key: bytes, host: str) -> list[dict] | None:
    """The most recently used signed-in Pinterest session across all profiles."""
    root = Path.home() / "Library/Application Support" / browser.data_dir
    best: tuple[int, list[dict]] | None = None
    for db in root.glob("*/Cookies"):
        try:
            cookies = _read_store(db, key, host)
        except (OSError, sqlite3.Error, ValueError):
            continue
        if _signed_in(cookies):
            accessed = max(c["accessed"] for c in cookies)
            if best is None or accessed > best[0]:
                best = (accessed, cookies)
    return best[1] if best else None


def capture_from_default_browser(base_url: str, timeout: float = LOGIN_TIMEOUT) -> str:
    """Open the login page in the default browser and wait for its session."""
    bundle = default_browser_id()
    browser = CHROMIUM_BROWSERS.get(bundle or "")
    if browser is None:
        raise ConfigError(
            f"can't read cookies from the default browser ({bundle or 'unknown'}); "
            "use `pinterest login --automated` instead"
        )
    host = urlparse(base_url).hostname or "www.pinterest.com"
    key = _keychain_key(browser.keychain_service)
    cookies = signed_in_cookies(browser, key, host)
    if cookies is None:
        webbrowser.open(f"{base_url}/login/")
        deadline = time.monotonic() + timeout
        while cookies is None:
            if time.monotonic() > deadline:
                raise ConfigError(f"not signed in to Pinterest in {browser.name} after {int(timeout)}s")
            time.sleep(STORE_POLL_INTERVAL)
            cookies = signed_in_cookies(browser, key, host)
    return cookie_header(cookies)


# ------------------------------------------------------------------ automated


def credentials_from_env() -> tuple[str, str] | None:
    """Email and password from the environment, or None when neither is set.

    The password is read here and must not be printed by the caller.
    """
    email = (os.environ.get("PINTEREST_EMAIL") or "").strip()
    password = os.environ.get("PINTEREST_PASSWORD") or ""
    if not email and not password:
        return None
    if not email or not password:
        raise ConfigError("set both PINTEREST_EMAIL and PINTEREST_PASSWORD")
    return email, password


def capture_with_credentials(
    base_url: str, email: str, password: str, timeout: float = LOGIN_TIMEOUT
) -> str:
    """Sign in through a headless browser and return the session cookie.

    Pinterest renders the login form in the browser, so this submits that form
    instead of posting a guessed API body. A challenge or a rejected password
    stops here; it does not open a window.
    """
    pw, PlaywrightError = _playwright()
    host = urlparse(base_url).hostname or "www.pinterest.com"
    with pw:
        try:
            browser = pw.chromium.launch(channel="chrome", headless=True)
        except PlaywrightError:
            browser = pw.chromium.launch(headless=True)
        try:
            context = browser.new_context()
            page = context.new_page()
            page.goto(f"{base_url}/login/", wait_until="domcontentloaded")
            email_box = page.locator("input[type='email'], input[name='id'], input[autocomplete='username']").first
            password_box = page.locator("input[type='password']").first
            email_box.wait_for(timeout=15000)
            email_box.fill(email)
            password_box.fill(password)
            password_box.press("Enter")
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                cookies = context.cookies(f"https://{host}/")
                if _signed_in(cookies):
                    return cookie_header(cookies)
                time.sleep(POLL_INTERVAL)
            raise ConfigError(
                "headless sign-in did not get a session; the password was rejected or Pinterest asked for a challenge"
            )
        finally:
            browser.close()


def _playwright():
    try:
        from playwright.sync_api import Error as PlaywrightError
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise ConfigError(
            "login needs Playwright: run `uv sync --extra login` (or "
            "`uv tool install --editable '.[login]'`)"
        ) from exc
    return sync_playwright(), PlaywrightError


def capture_cookie(base_url: str, timeout: float = LOGIN_TIMEOUT) -> str:
    """Open the login page in a Playwright-driven Chrome and block until signed
    in; returns the cookie header."""
    pw, PlaywrightError = _playwright()

    host = urlparse(base_url).hostname or "www.pinterest.com"
    profile = browser_profile_dir()
    profile.mkdir(parents=True, exist_ok=True, mode=0o700)

    with pw:
        options = dict(
            headless=False,
            no_viewport=True,
            ignore_default_args=["--enable-automation"],
            args=["--disable-blink-features=AutomationControlled"],
        )
        try:
            context = pw.chromium.launch_persistent_context(
                str(profile), channel="chrome", **options
            )
        except PlaywrightError:
            context = pw.chromium.launch_persistent_context(str(profile), **options)
        try:
            page = context.pages[0] if context.pages else context.new_page()
            page.goto(f"{base_url}/login/")
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                cookies = context.cookies(f"https://{host}/")
                if _signed_in(cookies):
                    return cookie_header(cookies)
                if not context.pages:
                    raise ConfigError("browser closed before sign-in finished")
                time.sleep(POLL_INTERVAL)
            raise ConfigError(f"not signed in after {int(timeout)}s; giving up")
        finally:
            try:
                context.close()
            except PlaywrightError:
                pass


def logout() -> list[Path]:
    """Forget the saved cookie and the browser profile. Returns what was removed."""
    removed = []
    cookie = default_cookie_file()
    if cookie.exists():
        cookie.unlink()
        removed.append(cookie)
    profile = browser_profile_dir()
    if profile.exists():
        shutil.rmtree(profile)
        removed.append(profile)
    if not removed:
        raise ConfigError(f"no saved session at {cookie}")
    return removed
