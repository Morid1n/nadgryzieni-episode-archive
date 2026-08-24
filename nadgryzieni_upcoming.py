#!/usr/bin/env python3
"""Discover and publish the next Nadgryzieni YouTube live stream.

The site-facing upcoming event is deliberately separate from the archived
episode dataset. The scheduled job runs frequently enough to observe the
04:30 UTC window on both sides of DST, but performs network discovery only in
that UTC window and only once per UTC day. Once an event is found, discovery is
held until the next Saturday 04:30 UTC probe.
"""

from __future__ import annotations

import argparse
import html
import json
import os
import re
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

REPO_DIR = Path(os.environ.get("NADGRYZIENI_REPO_DIR", Path(__file__).resolve().parent))
STATE_DIR = Path(os.environ.get(
    "NADGRYZIENI_STATE_DIR",
    str(Path.home() / ".hermes" / "profiles" / "r2-d2" / "state"),
))
STATE_PATH = STATE_DIR / "nadgryzieni-upcoming-state.json"
ARTIFACT_PATH = REPO_DIR / "upcoming.json"
YOUTUBE_LIVE_URL = "https://www.youtube.com/@imagazinepl/live"
MAX_RESPONSE_BYTES = 4 * 1024 * 1024
PROBE_HOUR_UTC = 4
PROBE_MINUTE_UTC = 30
PROBE_WINDOW_MINUTES = 5
STATE_SCHEMA_VERSION = 1
ARTIFACT_SCHEMA_VERSION = 1


class SkipRun(Exception):
    """A safe no-op: the public source has no suitable upcoming event."""


@dataclass(frozen=True)
class Stream:
    video_id: str
    title: str
    start_utc: datetime

    @property
    def link(self) -> str:
        return f"https://www.youtube.com/watch?v={self.video_id}"


def as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("Datetime must be timezone-aware")
    return value.astimezone(timezone.utc)


def iso_utc(value: datetime) -> str:
    return as_utc(value).isoformat(timespec="seconds").replace("+00:00", "Z")


def parse_iso_utc(value: str) -> datetime:
    if not isinstance(value, str) or not value:
        raise ValueError("UTC timestamp is missing")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return as_utc(parsed)


def extract_page_title(page: str) -> str:
    patterns = (
        r'<meta\s+name=["\']title["\']\s+content=["\']([^"\']+)',
        r'<meta\s+property=["\']og:title["\']\s+content=["\']([^"\']+)',
        r'<title>(.*?)</title>',
    )
    for pattern in patterns:
        match = re.search(pattern, page, re.IGNORECASE | re.DOTALL)
        if match:
            title = html.unescape(match.group(1)).strip()
            title = title.replace(r"\u0026", "&").replace(r"\"", '"')
            return re.sub(r"\s+-\s+YouTube$", "", title).strip()
    return ""


def parse_scheduled_streams(page: str, now: datetime | None = None) -> list[Stream]:
    """Parse YouTube's escaped and unescaped scheduled-player fragments."""
    patterns = (
        re.compile(
            r'eStreamabilityRenderer\\":\{\\"videoId\\":\\"(?P<id>[^\\"]+)\\".*?'
            r'scheduledStartTime\\":\\"(?P<ts>\d+)\\"',
            re.DOTALL,
        ),
        re.compile(
            r'eStreamabilityRenderer":\{"videoId":"(?P<id>[^"]+)".*?'
            r'"scheduledStartTime":"(?P<ts>\d+)"',
            re.DOTALL,
        ),
    )
    current = as_utc(now or datetime.now(timezone.utc))
    title = extract_page_title(page)
    candidates: list[Stream] = []
    seen: set[str] = set()
    for pattern in patterns:
        for match in pattern.finditer(page):
            video_id = match.group("id")
            if not re.fullmatch(r"[A-Za-z0-9_-]{6,20}", video_id) or video_id in seen:
                continue
            seen.add(video_id)
            start_utc = datetime.fromtimestamp(int(match.group("ts")), tz=timezone.utc)
            if start_utc <= current:
                continue
            candidates.append(Stream(video_id=video_id, title=title, start_utc=start_utc))
    return sorted(candidates, key=lambda stream: stream.start_utc)


def fetch_text(url: str) -> str:
    request = Request(url, headers={"User-Agent": "Mozilla/5.0 NadgryzieniUpcoming/1.0"})
    try:
        with urlopen(request, timeout=30) as response:
            content_type = str(response.headers.get("Content-Type") or "").casefold()
            if content_type and "text/html" not in content_type:
                raise RuntimeError(f"YouTube discovery returned unexpected content type: {content_type}")
            body = response.read(MAX_RESPONSE_BYTES + 1)
            if len(body) > MAX_RESPONSE_BYTES:
                raise RuntimeError("YouTube discovery response exceeded the size limit")
            return body.decode("utf-8", "strict")
    except (HTTPError, URLError, TimeoutError, UnicodeDecodeError) as exc:
        raise RuntimeError(f"YouTube discovery failed: {type(exc).__name__}") from exc


def discover_stream(now: datetime | None = None, fetcher: Callable[[str], str] = fetch_text) -> Stream:
    current = as_utc(now or datetime.now(timezone.utc))
    page = fetcher(YOUTUBE_LIVE_URL)
    candidates = parse_scheduled_streams(page, now=current)
    if not candidates:
        raise SkipRun("YouTube has no upcoming scheduled live stream")
    stream = candidates[0]
    if not stream.title:
        raise SkipRun(f"YouTube stream {stream.video_id} has no readable title")
    return stream


def next_saturday_probe(now: datetime) -> datetime:
    """Return the next Saturday 04:30 UTC, never the current probe."""
    current = as_utc(now)
    days_until_saturday = (5 - current.weekday()) % 7
    if days_until_saturday == 0:
        days_until_saturday = 7
    target_date = (current + timedelta(days=days_until_saturday)).date()
    return datetime(
        target_date.year,
        target_date.month,
        target_date.day,
        PROBE_HOUR_UTC,
        PROBE_MINUTE_UTC,
        tzinfo=timezone.utc,
    )


def in_probe_window(now: datetime) -> bool:
    current = as_utc(now)
    return current.hour == PROBE_HOUR_UTC and PROBE_MINUTE_UTC <= current.minute < PROBE_MINUTE_UTC + PROBE_WINDOW_MINUTES


def _read_json(path: Path, default: dict) -> dict:
    if not path.exists():
        return default
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Upcoming state is unreadable: {type(exc).__name__}") from exc
    if not isinstance(payload, dict):
        raise RuntimeError("Upcoming state must be a JSON object")
    return payload


def _write_json_atomic(path: Path, payload: dict) -> bool:
    encoded = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=False) + "\n"
    if path.exists() and path.read_text(encoding="utf-8") == encoded:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(encoded, encoding="utf-8")
    os.replace(temporary, path)
    return True


def artifact_for(stream: Stream | None, now: datetime) -> dict:
    return {
        "schema_version": ARTIFACT_SCHEMA_VERSION,
        "updated_at_utc": iso_utc(now),
        "source_url": YOUTUBE_LIVE_URL,
        "event": None if stream is None else {
            "video_id": stream.video_id,
            "title": stream.title,
            "scheduled_start_utc": iso_utc(stream.start_utc),
            "url": stream.link,
        },
    }


def run_cycle(
    now: datetime,
    state_path: Path = STATE_PATH,
    artifact_path: Path = ARTIFACT_PATH,
    discover: Callable[[datetime], Stream | None] = discover_stream,
    publish: Callable[[], object] | None = None,
    force: bool = False,
) -> dict:
    """Run one gated discovery cycle; dependencies are injectable for tests."""
    current = as_utc(now)
    if not force and not in_probe_window(current):
        return {"status": "outside_probe_window"}

    state = _read_json(state_path, {"schema_version": STATE_SCHEMA_VERSION})
    if state.get("schema_version", STATE_SCHEMA_VERSION) != STATE_SCHEMA_VERSION:
        raise RuntimeError("Upcoming state has an unsupported schema")
    hold_until_raw = state.get("hold_until_utc")
    if hold_until_raw:
        hold_until = parse_iso_utc(hold_until_raw)
        if current < hold_until:
            if state.get("publish_pending") and publish:
                publish()
                state["publish_pending"] = False
                _write_json_atomic(state_path, state)
            return {"status": "paused", "hold_until_utc": iso_utc(hold_until)}

    probe_date = current.date().isoformat()
    if not force and state.get("last_probe_date_utc") == probe_date:
        return {"status": "already_probed", "probe_date_utc": probe_date}

    try:
        stream = discover(current)
    except SkipRun:
        stream = None

    if stream is None:
        artifact_changed = _write_json_atomic(artifact_path, artifact_for(None, current))
        new_state = {
            "schema_version": STATE_SCHEMA_VERSION,
            "last_probe_date_utc": probe_date,
            "hold_until_utc": None,
            "publish_pending": bool(artifact_changed and publish),
        }
        _write_json_atomic(state_path, new_state)
        if artifact_changed and publish:
            publish()
            new_state["publish_pending"] = False
            _write_json_atomic(state_path, new_state)
        return {"status": "not_found", "artifact_changed": artifact_changed}

    resume_at = next_saturday_probe(current)
    artifact_changed = _write_json_atomic(artifact_path, artifact_for(stream, current))
    new_state = {
        "schema_version": STATE_SCHEMA_VERSION,
        "last_probe_date_utc": probe_date,
        "hold_until_utc": iso_utc(resume_at),
        "video_id": stream.video_id,
        "scheduled_start_utc": iso_utc(stream.start_utc),
        "publish_pending": bool(artifact_changed and publish),
    }
    _write_json_atomic(state_path, new_state)
    if artifact_changed and publish:
        publish()
        new_state["publish_pending"] = False
        _write_json_atomic(state_path, new_state)
    return {
        "status": "found",
        "video_id": stream.video_id,
        "title": stream.title,
        "scheduled_start_utc": iso_utc(stream.start_utc),
        "hold_until_utc": iso_utc(resume_at),
        "artifact_changed": artifact_changed,
    }


def publish_git() -> bool:
    """Commit and push only upcoming.json under the shared pipeline lock."""
    sys.path.insert(0, str(REPO_DIR))
    import nadgryzieni_pipeline as pipeline

    if not pipeline.acquire_pipeline_lock():
        raise RuntimeError("Another Nadgryzieni pipeline run is active")
    today = datetime.now(timezone.utc).date().isoformat()
    return pipeline.git_commit_and_push_paths(
        f"Nadgryzieni upcoming event – {today}",
        ["upcoming.json"],
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Discover and publish the next Nadgryzieni live stream")
    parser.add_argument("--run-now", action="store_true", help="bypass the 04:30 UTC probe window")
    parser.add_argument("--no-publish", action="store_true", help="write local artifacts without committing/pushing")
    args = parser.parse_args(argv)
    now = datetime.now(timezone.utc)
    result = run_cycle(
        now=now,
        discover=discover_stream,
        publish=None if args.no_publish else publish_git,
        force=args.run_now,
    )
    if result["status"] in {"found", "not_found"}:
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
