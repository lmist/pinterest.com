"""Turn Pinterest's very large pin/board/user payloads into compact records.

Only fields an agent can act on are kept; unknown keys are dropped so tool
output stays small enough to reason about.
"""

from __future__ import annotations

from typing import Any

# Preferred image sizes, largest first. ``orig`` is not present on every pin.
IMAGE_SIZES = ("orig", "1200x", "736x", "564x", "474x", "236x", "170x", "136x136")
PIN_URL = "https://www.pinterest.com/pin/{pin_id}/"
BOARD_URL = "https://www.pinterest.com{path}"


def _as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def image_urls(pin: dict[str, Any]) -> dict[str, str]:
    """Available image URLs keyed by size, largest first."""
    out: dict[str, str] = {}
    images = _as_dict(pin.get("images"))
    for size in IMAGE_SIZES:
        entry = _as_dict(images.get(size))
        url = entry.get("url")
        if isinstance(url, str) and url:
            out[size] = url
    return out


def best_image(pin: dict[str, Any], size: str | None = None) -> str | None:
    """URL of the requested size, else the largest available."""
    urls = image_urls(pin)
    if size and size in urls:
        return urls[size]
    for candidate in IMAGE_SIZES:
        if candidate in urls:
            return urls[candidate]
    return None


def _user_ref(pinner: dict[str, Any]) -> dict[str, Any] | None:
    if not pinner:
        return None
    record: dict[str, Any] = {}
    for key in ("id", "username", "full_name", "follower_count"):
        value = pinner.get(key)
        if value not in (None, ""):
            record[key] = value
    if not record:
        return None
    if "username" in record:
        record["profile_url"] = f"https://www.pinterest.com/{record['username']}/"
    return record


def _board_ref(board: dict[str, Any]) -> dict[str, Any] | None:
    if not board:
        return None
    record: dict[str, Any] = {}
    if board.get("id"):
        record["id"] = board["id"]
    if board.get("name"):
        record["name"] = board["name"]
    if board.get("url"):
        record["url"] = BOARD_URL.format(path=board["url"])
    owner = _user_ref(_as_dict(board.get("owner")))
    if owner:
        record["owner"] = owner.get("username") or owner.get("id")
    return record or None


def _video(pin: dict[str, Any]) -> dict[str, Any] | None:
    videos = _as_dict(pin.get("videos"))
    video_list = _as_dict(videos.get("video_list"))
    if not video_list:
        return None
    best = None
    for quality in ("V_HLSV4", "V_HLSV3_MOBILE", "V_720P", "V_EXP7"):
        entry = _as_dict(video_list.get(quality))
        if entry.get("url"):
            best = entry
            break
    if best is None:
        for entry in video_list.values():
            if isinstance(entry, dict) and entry.get("url"):
                best = entry
                break
    if best is None:
        return None
    record: dict[str, Any] = {"url": best["url"]}
    if best.get("duration"):
        record["duration_ms"] = best["duration"]
    if best.get("thumbnail"):
        record["thumbnail"] = best["thumbnail"]
    return record


def pin_record(pin: dict[str, Any], *, detailed: bool = False) -> dict[str, Any]:
    """Compact record for a pin, from a search result, feed or pin detail payload."""
    if not isinstance(pin, dict) or not pin.get("id"):
        return {}
    pin_id = str(pin["id"])
    record: dict[str, Any] = {"id": pin_id, "url": PIN_URL.format(pin_id=pin_id)}

    for key, out_key in (
        ("title", "title"),
        ("grid_title", "grid_title"),
        ("description", "description"),
        ("closeup_description", "closeup_description"),
        ("alt_text", "alt_text"),
        ("auto_alt_text", "auto_alt_text"),
        ("link", "link"),
        ("domain", "domain"),
        ("dominant_color", "dominant_color"),
        ("created_at", "created_at"),
    ):
        value = pin.get(key)
        if isinstance(value, str) and value.strip():
            record[out_key] = value.strip()

    if pin.get("is_video") or pin.get("videos"):
        record["is_video"] = True

    if isinstance(pin.get("reaction_counts"), dict) and pin["reaction_counts"]:
        record["reactions"] = pin["reaction_counts"]
    if isinstance(pin.get("favorite_user_count"), int):
        record["saves"] = pin["favorite_user_count"]
    if isinstance(pin.get("comment_count"), int):
        record["comments"] = pin["comment_count"]

    images = image_urls(pin)
    if images:
        record["image"] = best_image(pin)
        record["images"] = images

    video = _video(pin)
    if video:
        record["video"] = video

    pinner = _user_ref(_as_dict(pin.get("pinner")))
    if pinner:
        record["pinner"] = pinner
    board = _board_ref(_as_dict(pin.get("board")))
    if board:
        record["board"] = board

    if detailed:
        for key in (
            "category",
            "section",
            "is_repin",
            "is_promoted",
            "is_story_pin",
            "rich_summary",
        ):
            value = pin.get(key)
            if value not in (None, "", [], {}):
                record[key] = value
        if pin.get("did_its"):
            record["did_its_count"] = len(_as_list(pin["did_its"]))
    return record


def pin_list(items: Any) -> list[dict[str, Any]]:
    """Normalise a list of pins, dropping anything that is not a pin."""
    out = []
    for item in _as_list(items):
        record = pin_record(item)
        if record:
            out.append(record)
    return out


def user_record(user: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(user, dict) or not user.get("id"):
        return {}
    username = user.get("username")
    record: dict[str, Any] = {"id": str(user["id"])}
    if username:
        record["username"] = username
        record["url"] = f"https://www.pinterest.com/{username}/"
    for key in (
        "full_name",
        "first_name",
        "about",
        "location",
        "website_url",
        "domain_url",
        "country",
    ):
        value = user.get(key)
        if isinstance(value, str) and value.strip():
            record[key] = value.strip()
    if isinstance(user.get("follower_count"), int):
        record["followers"] = user["follower_count"]
    if isinstance(user.get("following_count"), int):
        record["following"] = user["following_count"]
    if isinstance(user.get("pin_count"), int):
        record["pins"] = user["pin_count"]
    if isinstance(user.get("board_count"), int):
        record["boards"] = user["board_count"]
    image = (
        user.get("image_xlarge_url")
        or user.get("image_large_url")
        or user.get("image_medium_url")
    )
    if isinstance(image, str) and image:
        record["image"] = image
    if user.get("is_verified_merchant"):
        record["is_verified_merchant"] = True
    return record


def board_record(board: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(board, dict) or not board.get("id"):
        return {}
    record: dict[str, Any] = {"id": str(board["id"])}
    if board.get("name"):
        record["name"] = board["name"]
    if board.get("description"):
        record["description"] = board["description"]
    if board.get("url"):
        record["url"] = BOARD_URL.format(path=board["url"])
    for key, out_key in (
        ("privacy", "privacy"),
        ("pin_count", "pin_count"),
        ("follower_count", "followers"),
        ("section_count", "sections"),
        ("created_at", "created_at"),
    ):
        value = board.get(key)
        if value not in (None, ""):
            record[out_key] = value
    if board.get("is_collaborative"):
        record["is_collaborative"] = True
    owner = _user_ref(_as_dict(board.get("owner")))
    if owner:
        record["owner"] = owner
    image = (
        board.get("image_cover_url")
        or board.get("image_thumbnail_url")
        or board.get("image_cover_hd_url")
    )
    if isinstance(image, str) and image:
        record["image"] = image
    return record


def board_list(items: Any) -> list[dict[str, Any]]:
    out = []
    for item in _as_list(items):
        record = board_record(item)
        if record:
            out.append(record)
    return out


def comment_record(comment: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(comment, dict) or not comment.get("id"):
        return {}
    record: dict[str, Any] = {"id": str(comment["id"])}
    text = comment.get("text")
    if isinstance(text, str):
        record["text"] = text
    if comment.get("created_at"):
        record["created_at"] = comment["created_at"]
    user = _user_ref(_as_dict(comment.get("user")))
    if user:
        record["user"] = user
    if isinstance(comment.get("comment_count"), int):
        record["replies"] = comment["comment_count"]
    if isinstance(comment.get("reaction_counts"), dict) and comment["reaction_counts"]:
        record["reactions"] = comment["reaction_counts"]
    return record


def comment_list(items: Any) -> list[dict[str, Any]]:
    out = []
    for item in _as_list(items):
        record = comment_record(item)
        if record:
            out.append(record)
    return out


def suggestion_record(item: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(item, dict):
        return {}
    query = item.get("query")
    if not isinstance(query, str) or not query:
        return {}
    record: dict[str, Any] = {"query": query}
    if item.get("id"):
        record["id"] = item["id"]
    if isinstance(item.get("pin_count"), int) and item["pin_count"]:
        record["pin_count"] = item["pin_count"]
    display = item.get("display_query")
    if isinstance(display, str) and display and display != query:
        record["display_query"] = display
    return record


def related_modules(payload: Any) -> list[dict[str, Any]]:
    """Group related-module payload entries into ``{title, pins}`` buckets.

    The payload is a flat list: the first entry is a header story, every later
    entry is a pin whose ``title`` names the query bucket it belongs to.
    """
    modules: list[dict[str, Any]] = []
    index: dict[str, dict[str, Any]] = {}
    for item in _as_list(payload):
        if not isinstance(item, dict) or item.get("type") != "pin":
            continue
        record = pin_record(item)
        if not record:
            continue
        title = item.get("title") or item.get("grid_title") or ""
        bucket = index.get(title)
        if bucket is None:
            bucket = {"title": title, "pins": []}
            index[title] = bucket
            modules.append(bucket)
        bucket["pins"].append(record)
    return modules


def page(items: Any, *, bookmark: str | None = None) -> dict[str, Any]:
    """Standard envelope for list-returning tools."""
    records = pin_list(items)
    out: dict[str, Any] = {"count": len(records), "results": records}
    if bookmark:
        out["bookmark"] = bookmark
    return out
