"""Command-line interface: every MCP tool as a subcommand that prints JSON.

Shares ``PinterestApp`` with the MCP server, so the CLI and the tools answer
from the same backend with the same configuration (``PINTEREST_*`` variables).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from .api import PinterestError
from .backend import BackendError
from . import login
from .config import MODES, ConfigError, Settings, default_cookie_file
from .skill import SKILL
from .live import fetch_image
from .server import PinterestApp

_PIN_URL = re.compile(r"/pin/(\d+)")


def pin_id(value: str) -> str:
    """Accept a bare pin id or a pin URL such as ``https://pin.it/.../pin/123/``."""
    value = value.strip()
    if value.isdigit():
        return value
    match = _PIN_URL.search(value)
    if match:
        return match.group(1)
    raise argparse.ArgumentTypeError(f"not a pin id or pin URL: {value!r}")


def package_version() -> str:
    try:
        return version("pinterest-mcp")
    except PackageNotFoundError:
        return "0.1.0"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pinterest",
        description=(
            "Pinterest search and retrieval from the command line. Prints JSON. "
            "Agents: pinterest skills get core"
        ),
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {package_version()}")
    # Global flags live on a parent so they work before or after the command;
    # SUPPRESS keeps a subcommand's unset copy from clobbering the top-level one.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--mode",
        choices=MODES,
        default=argparse.SUPPRESS,
        help="override PINTEREST_MODE (live or archive)",
    )
    common.add_argument(
        "--compact",
        action="store_true",
        default=argparse.SUPPRESS,
        help="print single-line JSON",
    )
    common.add_argument(
        "--no-account",
        action="store_true",
        default=argparse.SUPPRESS,
        help="do not send a saved cookie; results are not ranked for the signed-in account",
    )
    parser.set_defaults(mode=None, compact=False, no_account=False)
    for action in common._actions:
        parser._add_action(action)
    sub = parser.add_subparsers(dest="command", required=True, metavar="COMMAND")

    def add(name: str, help: str) -> argparse.ArgumentParser:
        return sub.add_parser(name, help=help, description=help, parents=[common])

    def paging(p: argparse.ArgumentParser, default: int | None) -> None:
        p.add_argument("-n", "--page-size", type=int, default=default)
        p.add_argument("-b", "--bookmark", help="bookmark from a previous page")

    p = add("search", "search pins")
    p.add_argument("query")
    p.add_argument("--scope", choices=("pins", "my_pins"), default="pins")
    paging(p, None)

    p = add("suggest", "Pinterest autocomplete for a term")
    p.add_argument("term")
    p.add_argument("-n", "--count", type=int, default=10)

    p = add("pin", "full detail for one pin")
    p.add_argument("pin", type=pin_id, help="pin id or pin URL")

    p = add("related", "pins related to a pin")
    p.add_argument("pin", type=pin_id, help="pin id or pin URL")
    p.add_argument("-q", "--query", help="bias modules toward a topic")
    p.add_argument("-n", "--page-size", type=int, default=12)

    p = add("comments", "comments on a pin")
    p.add_argument("pin", type=pin_id, help="pin id or pin URL")
    paging(p, 10)

    p = add("user", "a user's profile")
    p.add_argument("username")

    p = add("boards", "a user's boards")
    p.add_argument("username")
    p.add_argument("-n", "--page-size", type=int, default=25)

    p = add("board", "one board's metadata")
    p.add_argument("username")
    p.add_argument("slug")

    p = add("board-pins", "the pins on a board (board id from `board` or `boards`)")
    p.add_argument("board_id")
    p.add_argument("--url", dest="board_url", help="board url path, e.g. /user/slug/")
    paging(p, 25)

    feed_flags = argparse.ArgumentParser(add_help=False)
    feed_flags.add_argument(
        "--mode", choices=MODES, default=argparse.SUPPRESS, help="override PINTEREST_MODE (live or archive)"
    )
    feed_flags.add_argument(
        "--compact", action="store_true", default=argparse.SUPPRESS, help="print single-line JSON"
    )
    feed = sub.add_parser(
        "feed",
        help="the signed-in home feed (needs PINTEREST_COOKIE)",
        description="the signed-in home feed (needs PINTEREST_COOKIE)",
        parents=[feed_flags],
    )
    paging(feed, None)

    p = add("image", "download a pin image")
    p.add_argument("url", help="an 'image' or 'images' URL from a pin record")
    p.add_argument(
        "-o", "--output", help="file to write, '-' for stdout (default: name from URL)"
    )

    add("status", "which mode is active and what the archive contains")

    p = add(
        "login",
        "sign in with your default browser and save the session cookie "
        "(already signed in there? it is picked up immediately)",
    )
    p.add_argument(
        "--automated",
        action="store_true",
        help="drive a separate Chrome window instead (needs the `login` extra)",
    )
    p.add_argument(
        "--no-browser",
        action="store_true",
        help="save PINTEREST_COOKIE or PINTEREST_COOKIE_FILE; do not open a browser",
    )
    p.add_argument(
        "--timeout", type=float, default=600, help="seconds to wait for sign-in"
    )
    add("logout", "delete the saved session cookie and browser profile")

    skills = sub.add_parser("skills", help="print the agent skill for this CLI")
    skills.add_argument("action", nargs="?")
    skills.add_argument("name", nargs="?")
    skills.set_defaults(mode=None, compact=False, no_account=False)
    return parser


async def run(args: argparse.Namespace, app: PinterestApp) -> Any:
    """Execute one parsed command; returns the JSON-able result."""
    match args.command:
        case "search":
            return await app.search_pins(
                query=args.query,
                scope=args.scope,
                page_size=args.page_size,
                bookmark=args.bookmark,
            )
        case "suggest":
            return await app.search_suggestions(term=args.term, count=args.count)
        case "pin":
            return await app.get_pin(pin_id=args.pin)
        case "related":
            return await app.get_related_pins(
                pin_id=args.pin, query=args.query, page_size=args.page_size
            )
        case "comments":
            return await app.get_pin_comments(
                pin_id=args.pin, page_size=args.page_size, bookmark=args.bookmark
            )
        case "user":
            return await app.get_user(username=args.username)
        case "boards":
            return await app.get_user_boards(
                username=args.username, page_size=args.page_size
            )
        case "board":
            return await app.get_board(username=args.username, slug=args.slug)
        case "board-pins":
            return await app.get_board_pins(
                board_id=args.board_id,
                board_url=args.board_url,
                page_size=args.page_size,
                bookmark=args.bookmark,
            )
        case "feed":
            return await app.get_home_feed(
                page_size=args.page_size, bookmark=args.bookmark
            )
        case "image":
            return await _save_image(args, app)
        case "status":
            return app.pinterest_status()
        case "login":
            # The cookie was saved before the app was built; prove it works.
            feed = await app.get_home_feed(page_size=1)
            return {
                "signed_in": True,
                "cookie_file": str(default_cookie_file()),
                "home_feed_pins": feed.get("count"),
            }
    raise AssertionError(f"unhandled command {args.command!r}")


async def _save_image(args: argparse.Namespace, app: PinterestApp) -> dict[str, Any] | None:
    data, fmt = await fetch_image(app.settings, app._http, args.url)
    if args.output == "-":
        sys.stdout.buffer.write(data)
        sys.stdout.buffer.flush()
        return None
    target = Path(args.output or Path(urlparse(args.url).path).name or f"pin.{fmt}")
    target.write_bytes(data)
    return {"path": str(target), "bytes": len(data), "format": fmt}


def skills_command(argv: list[str]) -> None:
    if argv == ["get", "core"] or argv == ["get"]:
        print(SKILL, end="")
        return
    if argv in ([], ["list"]):
        print("core")
        return
    raise SystemExit("Usage: pinterest skills get core")


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "skills":
        skills_argv = [part for part in (args.action, args.name) if part]
        try:
            skills_command(skills_argv)
        except SystemExit as exc:
            print(exc, file=sys.stderr)
            return 1
        return 0
    saved_env = {
        name: os.environ.get(name)
        for name in ("PINTEREST_COOKIE", "PINTEREST_COOKIE_FILE", "PINTEREST_NO_ACCOUNT")
    }
    if args.no_account:
        os.environ.pop("PINTEREST_COOKIE", None)
        os.environ.pop("PINTEREST_COOKIE_FILE", None)
        os.environ["PINTEREST_NO_ACCOUNT"] = "1"
    if args.mode:
        os.environ["PINTEREST_MODE"] = args.mode
    if args.command == "login":
        args.mode = "live"
        os.environ["PINTEREST_MODE"] = "live"

    def emit(result: Any) -> None:
        print(json.dumps(result, indent=None if args.compact else 2, ensure_ascii=False))

    async def go() -> int:
        app = PinterestApp(Settings.from_env())
        try:
            result = await run(args, app)
        finally:
            await app.aclose()
        if result is not None:
            emit(result)
        return 0

    try:
        if args.command == "logout":
            emit({"removed": [str(path) for path in login.logout()]})
            if os.environ.get("PINTEREST_COOKIE") or os.environ.get("PINTEREST_COOKIE_FILE"):
                print(
                    "pinterest: PINTEREST_COOKIE / PINTEREST_COOKIE_FILE is still set and will be used instead.",
                    file=sys.stderr,
                )
            return 0
        if args.command == "login":
            if args.no_browser and args.automated:
                print("pinterest: --no-browser and --automated cannot be combined", file=sys.stderr)
                return 1
            if args.no_browser:
                path = login.write_cookie(login.cookie_from_env())
                print(f"Saved session cookie to {path}", file=sys.stderr)
                return 0
            base_url = Settings.from_env().base_url
            credentials = login.credentials_from_env()
            if credentials and not args.automated:
                email, password = credentials
                print("pinterest: signing in with a headless browser…", file=sys.stderr)
                path = login.write_cookie(
                    login.capture_with_credentials(base_url, email, password, timeout=args.timeout)
                )
                print(f"Saved session cookie to {path}", file=sys.stderr)
                return 0
            if os.environ.get("PINTEREST_COOKIE") or os.environ.get("PINTEREST_COOKIE_FILE"):
                print(
                    "pinterest: note: PINTEREST_COOKIE / PINTEREST_COOKIE_FILE is set and "
                    "takes precedence over the cookie saved here",
                    file=sys.stderr,
                )
            print(
                "pinterest: sign in to Pinterest in the browser window that opens…",
                file=sys.stderr,
            )
            capture = login.capture_cookie if args.automated else login.capture_from_default_browser
            login.write_cookie(capture(base_url, timeout=args.timeout))
        return asyncio.run(go())
    except (PinterestError, BackendError, ConfigError, ValueError, OSError) as exc:
        print(f"pinterest: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
    finally:
        if args.no_account:
            for name, value in saved_env.items():
                if value is None:
                    os.environ.pop(name, None)
                else:
                    os.environ[name] = value


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
