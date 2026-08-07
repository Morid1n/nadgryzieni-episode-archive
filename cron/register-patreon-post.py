#!/usr/bin/env python3
"""Register metadata collected from the rendered Patreon page.

This helper is intended for the browser-assisted Hermes cron job. The cron
agent discovers a public post with the browser tool, then passes the visible
post metadata here. The pipeline remains responsible for archive generation,
validation, Obsidian synchronization, and Git publication.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import tempfile
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse


SLUG_TEMPLATE = r"{episode}-afterparty(?:-[a-z0-9]+)*-\d{{6,12}}"


def normalize_duration(value: str) -> str:
    parts = value.strip().split(":")
    if len(parts) == 2:
        hours, minutes, seconds = 0, int(parts[0]), int(parts[1])
    elif len(parts) == 3:
        hours, minutes, seconds = (int(part) for part in parts)
    else:
        raise ValueError("duration must be MM:SS or H:MM:SS")
    if minutes >= 60 or seconds >= 60 or min(hours, minutes, seconds) < 0:
        raise ValueError("duration contains invalid minute or second values")
    return f"{hours}:{minutes:02d}:{seconds:02d}"


def build_entry(args: argparse.Namespace) -> dict:
    if args.episode <= 0:
        raise ValueError("episode must be positive")
    slug = args.slug.strip()
    if not re.fullmatch(SLUG_TEMPLATE.format(episode=args.episode), slug):
        raise ValueError("slug does not match the expected Patreon Afterparty form")

    parsed_url = urlparse(args.url.strip())
    if parsed_url.scheme != "https" or parsed_url.netloc != "www.patreon.com":
        raise ValueError("url must be an HTTPS www.patreon.com URL")
    expected_path = f"/iMagazinePL/posts/{slug}"
    if parsed_url.path != expected_path:
        raise ValueError("url path must match the supplied slug")

    title = args.title.strip()
    if not title or "(Afterparty)" not in title or "\n" in title:
        raise ValueError("title must be a single-line Afterparty title")
    try:
        datetime.strptime(args.date.strip(), "%Y-%m-%d")
    except ValueError as exc:
        raise ValueError("date must be YYYY-MM-DD") from exc

    return {
        "episode": args.episode,
        "slug": slug,
        "url": parsed_url.geturl(),
        "title": title,
        "date": args.date.strip(),
        "duration": normalize_duration(args.duration),
    }


def update_manifest(path: Path, entry: dict) -> None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"could not read manifest: {type(exc).__name__}") from exc

    posts = payload.get("posts")
    if not isinstance(posts, list):
        raise RuntimeError("manifest posts must be a list")

    for index, existing in enumerate(posts):
        try:
            existing_episode = int(existing["episode"])
        except (KeyError, TypeError, ValueError):
            continue
        if existing_episode != entry["episode"]:
            continue
        if existing.get("slug") != entry["slug"] or existing.get("url") != entry["url"]:
            raise RuntimeError(
                f"episode {entry['episode']} already has a different Patreon post; refusing to replace it"
            )
        posts[index] = {**existing, **entry}
        break
    else:
        posts.append(entry)

    posts.sort(key=lambda post: int(post["episode"]))
    payload["version"] = max(int(payload.get("version", 1)), 2)
    payload["posts"] = posts
    serialized = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"

    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent, text=True)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(serialized)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=Path("patreon_posts.json"))
    parser.add_argument("--episode", type=int, required=True)
    parser.add_argument("--slug", required=True)
    parser.add_argument("--url", required=True)
    parser.add_argument("--title", required=True)
    parser.add_argument("--date", required=True)
    parser.add_argument("--duration", required=True)
    args = parser.parse_args()

    try:
        entry = build_entry(args)
        update_manifest(args.manifest, entry)
    except (OSError, RuntimeError, ValueError) as exc:
        parser.error(str(exc))
    print(json.dumps({"registered": entry["episode"], "url": entry["url"]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
