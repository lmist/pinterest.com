"""Runtime configuration for the Pinterest MCP server.

Two operating modes, selected with ``PINTEREST_MODE``:

* ``live`` (default) - call pinterest.com. Public search and retrieval work
  without any credentials; set ``PINTEREST_COOKIE`` to also reach the signed-in
  surfaces (own pins, home feed, private boards).
* ``archive`` - answer from a captured HAR instead of the network, for offline
  or deterministic use.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

MODES = ("live", "archive")
DEFAULT_MODE = "live"
DEFAULT_BASE_URL = "https://nl.pinterest.com"
DEFAULT_APP_VERSION = "2b09e40"
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"
)
DEFAULT_HAR_CANDIDATES = ("har/nl.pinterest.com.har", "nl.pinterest.com.har")


class ConfigError(RuntimeError):
    """Raised when the server is configured in a way that cannot work."""


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError as exc:  # pragma: no cover - operator error
        raise ConfigError(f"{name} must be a number, got {raw!r}") from exc


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError as exc:  # pragma: no cover - operator error
        raise ConfigError(f"{name} must be an integer, got {raw!r}") from exc


def config_dir() -> Path:
    """Where ``pinterest login`` keeps its state (``$XDG_CONFIG_HOME/pinterest-mcp``)."""
    base = os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config"
    return Path(base).expanduser() / "pinterest-mcp"


def default_cookie_file() -> Path:
    return config_dir() / "cookie"


def _read_cookie() -> str:
    if os.environ.get("PINTEREST_NO_ACCOUNT") == "1":
        return ""
    """Cookie header from ``PINTEREST_COOKIE``, ``PINTEREST_COOKIE_FILE`` or the
    file ``pinterest login`` saved, in that order."""
    inline = (os.environ.get("PINTEREST_COOKIE") or "").strip()
    if inline:
        return inline
    path = os.environ.get("PINTEREST_COOKIE_FILE")
    if not path and default_cookie_file().is_file():
        path = str(default_cookie_file())
    if path:
        cookie_file = Path(path).expanduser()
        if not cookie_file.is_file():
            raise ConfigError(f"PINTEREST_COOKIE_FILE does not exist: {cookie_file}")
        text = cookie_file.read_text(encoding="utf-8").strip()
        # Tolerate a pasted "Cookie: ..." header or a Netscape cookie file.
        for line in text.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if line.lower().startswith("cookie:"):
                line = line.split(":", 1)[1].strip()
            if "=" in line and (";" in line or line.count("=") == 1):
                return line
        if "=" in text:
            return text
        raise ConfigError(f"no cookie header found in {cookie_file}")
    return ""


def _resolve_har() -> Path | None:
    explicit = os.environ.get("PINTEREST_HAR")
    if explicit:
        path = Path(explicit).expanduser()
        if not path.is_file():
            raise ConfigError(f"PINTEREST_HAR does not exist: {path}")
        return path
    for candidate in DEFAULT_HAR_CANDIDATES:
        path = Path.cwd() / candidate
        if path.is_file():
            return path
    return None


@dataclass(frozen=True)
class Settings:
    mode: str
    base_url: str
    cookie: str
    csrf_token: str
    app_version: str
    user_agent: str
    timeout: float
    min_interval: float
    max_image_bytes: int
    har_path: Path | None

    @property
    def live(self) -> bool:
        return self.mode == "live"

    @property
    def authenticated(self) -> bool:
        return bool(self.cookie)

    @classmethod
    def from_env(cls) -> Settings:
        mode = (os.environ.get("PINTEREST_MODE") or DEFAULT_MODE).strip().lower()
        if mode not in MODES:
            raise ConfigError(f"PINTEREST_MODE must be one of {MODES}, got {mode!r}")
        cookie = _read_cookie()
        csrf = (
            os.environ.get("PINTEREST_CSRF_TOKEN") or ""
        ).strip() or _csrf_from_cookie(cookie)
        base_url = (os.environ.get("PINTEREST_BASE_URL") or DEFAULT_BASE_URL).rstrip(
            "/"
        )
        if not base_url.startswith(("http://", "https://")):
            raise ConfigError(
                f"PINTEREST_BASE_URL must be an http(s) URL, got {base_url!r}"
            )
        return cls(
            mode=mode,
            base_url=base_url,
            cookie=cookie,
            csrf_token=csrf,
            app_version=os.environ.get("PINTEREST_APP_VERSION") or DEFAULT_APP_VERSION,
            user_agent=os.environ.get("PINTEREST_USER_AGENT") or DEFAULT_USER_AGENT,
            timeout=_env_float("PINTEREST_TIMEOUT", 30.0),
            min_interval=_env_float("PINTEREST_MIN_INTERVAL", 0.5),
            max_image_bytes=_env_int("PINTEREST_MAX_IMAGE_BYTES", 8 * 1024 * 1024),
            har_path=_resolve_har(),
        )


def _csrf_from_cookie(cookie: str) -> str:
    for part in cookie.split(";"):
        name, _, value = part.strip().partition("=")
        if name == "csrftoken":
            return value.strip()
    return ""
