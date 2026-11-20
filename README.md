# Pinterest

Search and retrieve Pinterest pins from the command line. Agents should run `pinterest skills get core` and follow that text. It matches this install.

```bash
uv tool install git+https://github.com/lmist/pinterest.com
pinterest search "brutalist interiors" -n 10
pinterest search "brutalist interiors" --no-account
pinterest search "brutalist interiors" -b <bookmark>
pinterest suggest brutal
pinterest pin https://www.pinterest.com/pin/914862421434174/
pinterest login                 # opens the browser, or signs in headlessly when email and password are set
pinterest login --no-browser    # reads PINTEREST_COOKIE or PINTEREST_COOKIE_FILE
pinterest logout
pinterest skills get core
```

Public search needs no account. A saved cookie is still sent when one exists, so the signed-in account can change ranking. `--no-account` sends no cookie. Do not reuse a bookmark across those two modes. `feed` is the signed-in home feed and rejects `--no-account`.

`login` writes the session cookie to `~/.config/pinterest-mcp/cookie` (mode 0600) and does not print it. `PINTEREST_COOKIE` overrides that file. `logout` exits 1 when no cookie file and no browser profile were removed.

The same commands are MCP tools. `uv run pinterest-mcp` speaks MCP on stdin and stdout.

## Request contract

The request contract was reverse engineered from a captured browser session
(`nl.pinterest.com.har`) and is byte-for-byte identical to the traffic the web
app sends — see `tests/test_api_contract.py`, which replays all 270 captured
`GET /resource/.../get/` URLs through the request builder.

## Two modes

| Mode | Selected by | Answers from | Credentials |
| --- | --- | --- | --- |
| `live` (default) | — | pinterest.com | none needed for public data; cookie unlocks signed-in surfaces |
| `archive` | `PINTEREST_MODE=archive` | the captured HAR file | none |

Public search and retrieval work anonymously: `search_pins`, `get_pin`,
`get_user`, `get_user_boards`, `get_board`, `get_board_pins`,
`get_related_pins`, `get_pin_comments`, `search_suggestions` and `fetch_image`
were all verified against the live site without any cookie. The signed-in
surfaces (`scope="my_pins"`, `get_home_feed`, private boards) answer `401`
until you set `PINTEREST_COOKIE`.

`archive` mode is for offline or deterministic use: it indexes every response in
the HAR (391 pins, 14 boards, captured searches, related modules, feeds,
comments) and falls back to a text search over those pins when the exact query
was never captured.

## Tools

| Tool | Purpose |
| --- | --- |
| `search_pins` | keyword search (`scope="pins"` or `"my_pins"`), paginated via `bookmark` |
| `search_suggestions` | Pinterest autocomplete, to expand or disambiguate a query |
| `get_pin` | full detail for one pin: description, alt text, link, pinner, board, all image sizes, video URL |
| `get_related_pins` | related pins grouped into modules by originating query |
| `get_user` | profile: display name, bio, follower/pin/board counts |
| `get_user_boards` | a user's boards (each with the board id needed by `get_board_pins`) |
| `get_board` | one board's metadata by `username` + `slug` |
| `get_board_pins` | the pins on a board, paginated |
| `get_pin_comments` | comments with author and reply counts |
| `get_home_feed` | the signed-in account's feed (needs a cookie) |
| `fetch_image` | downloads a pin image and returns it as MCP image content |
| `pinterest_status` | which mode is active and what the archive contains |

List results carry a `bookmark`; pass it back to get the next page. Pin records
are compact (`id`, `url`, `description`, `alt_text`, `link`, `domain`,
`pinned`/`image`/`images`/`video`, `pinner`, `board`) so tool output stays
readable.

## CLI

Every tool is also a `pinterest` subcommand that prints JSON, using the same
backend and `PINTEREST_*` configuration as the server. The commands are at the
top of this file. `--compact` prints single-line JSON. `--mode archive`
overrides `PINTEREST_MODE`. Errors go to stderr and start with `pinterest:`.

## Setup

```bash
git clone https://github.com/lmist/pinterest.com && cd pinterest.com
uv sync
cp mcp.example.json mcp.json     # then point your client at `uv run pinterest-mcp`
uv run pinterest-mcp --check     # prints the resolved config and runs one search
```

Register it with any MCP client (Claude Code, Claude Desktop, Cursor, omp…):

```json
{
  "mcpServers": {
    "pinterest": {
      "command": "uv",
      "args": ["--directory", "/absolute/path/to/pinterest.com", "run", "pinterest-mcp"]
    }
  }
}
```

### Credentials (only for signed-in surfaces)

Easiest: let the CLI capture them.

```bash
uv tool install --editable '.[login]'   # adds Playwright; from a clone of this repo
pinterest login                         # opens the default browser and saves the cookie
pinterest login --no-browser            # reads PINTEREST_COOKIE or PINTEREST_COOKIE_FILE
```

If `PINTEREST_EMAIL` and `PINTEREST_PASSWORD` are both set, `pinterest login` does not open a window. It fills the login form in a headless browser and saves the cookie. Do not put the password on the command line. A rejected password or a challenge stops there.

Sign in in the window; once Pinterest sets the session cookie it is saved to
`~/.config/pinterest-mcp/cookie` (mode 600), checked with one home-feed request,
and the window closes. Both the CLI and the MCP server read that file when
`PINTEREST_COOKIE` / `PINTEREST_COOKIE_FILE` are unset. The browser profile is
kept next to it, so re-running `login` after the session expires is usually
instant. `pinterest logout` deletes both.

Or copy the header by hand:

1. Open <https://www.pinterest.com> in a logged-in browser.
2. DevTools → Network → any `/resource/.../get/` request → copy the whole
   `cookie` request header.
3. Export it before starting the server:

```bash
export PINTEREST_COOKIE='csrftoken=…; _auth=…; …'
```

`PINTEREST_COOKIE_FILE` does the same with the value in a file (keeps secrets
out of shell history and config files). `x-csrftoken` is derived from the
`csrftoken` cookie automatically.

### Configuration

| Variable | Default | Meaning |
| --- | --- | --- |
| `PINTEREST_MODE` | `live` | `live` or `archive` |
| `PINTEREST_COOKIE` | — | Cookie header from a logged-in session |
| `PINTEREST_COOKIE_FILE` | `~/.config/pinterest-mcp/cookie` if present | Path to a file containing that cookie header |
| `PINTEREST_BASE_URL` | `https://nl.pinterest.com` | Any Pinterest locale, e.g. `https://www.pinterest.co.uk` |
| `PINTEREST_APP_VERSION` | `2b09e40` | Value for the `x-app-version` header |
| `PINTEREST_HAR` | `har/nl.pinterest.com.har`, `nl.pinterest.com.har` | Capture used by `archive` mode |
| `PINTEREST_TIMEOUT` | `30` | Per-request timeout, seconds |
| `PINTEREST_MIN_INTERVAL` | `0.5` | Minimum spacing between requests, seconds |
| `PINTEREST_MAX_IMAGE_BYTES` | `8388608` | Refuse larger image downloads |

## Behaviour worth knowing

* **Rate limiting.** Requests are serialised with `PINTEREST_MIN_INTERVAL`
  between them (default 0.5 s).
* **Transient failures.** Pinterest's edge answers `503 upstream connect error`
  for cold resources fairly often; those, plus `429`/`5xx`, are retried up to
  three times with backoff, honouring `Retry-After`. Auth failures are never
  retried.
* **Images.** `fetch_image` only accepts Pinterest image hosts
  (`i.pinimg.com`, `s.pinimg.com`, `v1.pinimg.com`) and enforces
  `PINTEREST_MAX_IMAGE_BYTES`, so a model cannot be talked into fetching
  arbitrary URLs through it.
* **Archive honesty.** Archive answers say how they were produced: `match` is
  `captured_search` (a recorded query) or `pin_text_search` (ranked text match
  over the captured pins), bare pages include `captured_bookmark`, and users
  that only appear as a pin's author are flagged `partial: true`. Archive mode
  refuses `bookmark` pagination instead of pretending it has more pages.
* **Unofficial API.** These are Pinterest's internal web endpoints, not a
  supported public API. Scrape politely, and expect the contract to drift —
  `tests/test_api_contract.py` is the tripwire for that.

## Development

```bash
uv run pytest                 # 118 tests; HAR-dependent ones skip if the capture is absent
uv run pytest tests/test_api_contract.py -q   # byte-exact replay of captured requests
uv run pinterest-mcp --check  # one live search + config summary
```

Layout: `api.py` (request contract), `live.py` (network backend), `archive.py`
(HAR backend), `normalize.py` (payload → compact records), `server.py` (tools), `cli.py` (command line), `login.py` (browser sign-in),
`backend.py` (the interface both backends satisfy).
