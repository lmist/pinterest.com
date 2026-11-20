"""Agent skill text served by `pinterest skills`.

Keep this in a module so an editable install and a wheel both serve the same
text. Do not tell the agent to reconstruct commands from the README's MCP tables.
"""

SKILL = """\
---
name: pinterest
description: Search and retrieve Pinterest pins, boards, and profiles from the command line. Use when the user asks to find a pin, a visual reference, a board, or a Pinterest profile, or to page through Pinterest results.
---

# pinterest

Search public Pinterest pins and read pins, boards, and profiles. Results are JSON. Keep `url` and image URLs.

Start here, then follow this file. `pinterest --help` lists every command.

## Search

```bash
pinterest search "brutalist interiors" -n 10
pinterest search "brutalist interiors" --no-account
pinterest search "brutalist interiors" -b '<bookmark>'
pinterest suggest brutal
```

Search prints JSON. A page has `results` and, when another page exists, `bookmark`. Pass that value to `-b`, quoted. Stop when `bookmark` is absent.

`--no-account` does not send `PINTEREST_COOKIE`, `PINTEREST_COOKIE_FILE`, or the cookie saved by `pinterest login`. Use it when the signed-in account must not influence results. A bookmark from a signed-in search is not a public bookmark: do not pass it to a later `--no-account` command, and do not pass a public bookmark to a signed-in command.

`search`, `suggest`, `pin`, `related`, `comments`, `user`, `boards`, `board`, and `board-pins` accept `--no-account`. `feed` does not: the home feed is the signed-in account.

Public search needs no account. Without `--no-account`, a saved cookie is still sent when one exists.

## Other reads

```bash
pinterest pin https://www.pinterest.com/pin/914862421434174/
pinterest related 914862421434174 -q concrete
pinterest user someuser
pinterest boards someuser
pinterest board someuser brutalism
pinterest board-pins <board_id>
pinterest image <image-url> -o pin.jpg
```

`pin` accepts a pin id or a pin URL. `image` only downloads Pinterest image hosts.

## Account

```bash
pinterest login
pinterest login --no-browser
pinterest logout
pinterest status
```

`login` opens the default browser and writes the session cookie to `~/.config/pinterest-mcp/cookie` (mode 0600). It does not print the cookie. If `PINTEREST_EMAIL` and `PINTEREST_PASSWORD` are both set, `login` does not open a window: it signs in with a headless browser and saves the cookie. Do not put the password on the command line. `login --no-browser` does not sign in either: it reads `PINTEREST_COOKIE` or `PINTEREST_COOKIE_FILE` and writes that same file. The cookie must contain `_auth=1` and `_pinterest_sess`. `PINTEREST_COOKIE` overrides the saved file. `logout` exits 1 when no cookie file and no browser profile were removed. If `PINTEREST_COOKIE` or `PINTEREST_COOKIE_FILE` is set, `logout` still exits 0 after removing saved state and says that variable is still in use.

## Failures

Errors go to stderr and start with `pinterest:`. A usage error exits 2. A Pinterest or input error exits 1.
"""
