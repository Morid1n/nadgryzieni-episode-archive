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
import hashlib
import html
import json
import os
import re
import ssl
import stat
import sys
import tempfile
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, HTTPSHandler, Request, build_opener, urlopen

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
MIN_UNIX_TIMESTAMP = 946684800  # 2000-01-01 UTC
MAX_UNIX_TIMESTAMP = 4102444800  # 2100-01-01 UTC


class SkipRun(Exception):
    """A safe no-op: the public source has no suitable upcoming event."""


class PublishPendingError(RuntimeError):
    """The upcoming commit exists locally but still needs a push retry."""

    def __init__(self, commit: str):
        if not re.fullmatch(r"[0-9a-f]{40}", commit):
            raise ValueError("Pending publication commit is invalid")
        self.commit = commit
        super().__init__("Git push remains pending for the local upcoming commit")


def _validated_video_id(value: object) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{6,20}", value):
        raise ValueError("Stream video ID is invalid")
    return value


@dataclass(frozen=True)
class Stream:
    video_id: str
    title: str
    start_utc: datetime

    @property
    def link(self) -> str:
        return f"https://www.youtube.com/watch?v={_validated_video_id(self.video_id)}"


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
    if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
        raise ValueError("UTC timestamp must have a zero UTC offset")
    return parsed.astimezone(timezone.utc)


def _validated_stream_fields(stream: Stream) -> tuple[str, str, datetime]:
    if not isinstance(stream, Stream):
        raise ValueError("Stream object is invalid")
    video_id = _validated_video_id(stream.video_id)
    if not isinstance(stream.title, str) or not stream.title.strip():
        raise ValueError("Stream title is invalid")
    start_utc = as_utc(stream.start_utc)
    timestamp = int(start_utc.timestamp())
    if not MIN_UNIX_TIMESTAMP <= timestamp <= MAX_UNIX_TIMESTAMP:
        raise ValueError("Stream scheduled timestamp is out of range")
    return video_id, stream.title, start_utc


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


def _renderer_bodies(page: str):
    marker = "eStreamabilityRenderer"
    start = 0
    while True:
        marker_index = page.find(marker, start)
        if marker_index < 0:
            return
        next_marker = page.find(marker, marker_index + len(marker))
        scan_end = next_marker if next_marker >= 0 else len(page)
        brace_index = page.find("{", marker_index + len(marker), min(scan_end, marker_index + len(marker) + 16))
        if brace_index >= 0:
            depth = 0
            quote = None
            escaped = False
            for index in range(brace_index, scan_end):
                character = page[index]
                if quote is not None:
                    if escaped:
                        escaped = False
                    elif character == "\\":
                        escaped = True
                    elif character == quote:
                        quote = None
                    continue
                if character in {"\"", "'"}:
                    quote = character
                elif character == "{":
                    depth += 1
                elif character == "}":
                    depth -= 1
                    if depth == 0:
                        yield page[brace_index + 1:index]
                        start = index + 1
                        break
            else:
                start = next_marker if next_marker >= 0 else len(page)
                continue
            continue
        start = next_marker if next_marker >= 0 else len(page)


def _renderer_context(body: str, position: int) -> tuple[int, str | None]:
    depth = 0
    quote: str | None = None
    escaped = False
    for character in body[:position]:
        if quote is not None:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == quote:
                quote = None
            continue
        if character in {"\"", "'"}:
            quote = character
        elif character in {"{", "["}:
            depth += 1
        elif character in {"}", "]"}:
            depth = max(0, depth - 1)
    return depth, quote


def _renderer_field(body: str, field: str) -> str | None:
    pattern = re.compile(
        r"(?<![A-Za-z0-9_])\\?[\"']?"
        + re.escape(field)
        + r"\\?[\"']?\s*:\s*\\?[\"']?(?P<value>[^\"'\\,\s}]+)"
    )
    for match in pattern.finditer(body):
        depth, quote = _renderer_context(body, match.start())
        if depth == 0 and quote is None:
            return match.group("value")
    return None


def parse_scheduled_streams(page: str, now: datetime | None = None) -> list[Stream]:
    """Parse scheduled-player fields only when they share one renderer object."""
    current = as_utc(now or datetime.now(timezone.utc))
    title = extract_page_title(page)
    candidates: list[Stream] = []
    seen: set[str] = set()
    for body in _renderer_bodies(page):
        video_id = _renderer_field(body, "videoId")
        timestamp_text = _renderer_field(body, "scheduledStartTime")
        if not video_id or not timestamp_text:
            continue
        if not re.fullmatch(r"[A-Za-z0-9_-]{6,20}", video_id) or video_id in seen:
            continue
        try:
            timestamp = int(timestamp_text)
            if not MIN_UNIX_TIMESTAMP <= timestamp <= MAX_UNIX_TIMESTAMP:
                continue
            start_utc = datetime.fromtimestamp(timestamp, tz=timezone.utc)
        except (ValueError, OverflowError, OSError):
            continue
        if start_utc <= current:
            continue
        seen.add(video_id)
        candidates.append(Stream(video_id=video_id, title=title, start_utc=start_utc))
    return sorted(candidates, key=lambda stream: stream.start_utc)


class _NoRedirectHandler(HTTPRedirectHandler):
    """Reject redirects so the discovered stream remains bound to its source."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _open_https_no_redirect(request: Request, *, timeout: float):
    parsed = urlsplit(request.full_url)
    if parsed.scheme.casefold() != "https":
        raise ValueError("HTTPS URL required")
    opener = build_opener(_NoRedirectHandler(), HTTPSHandler(context=ssl.create_default_context()))
    return opener.open(request, timeout=timeout)


def fetch_text(url: str) -> str:
    request = Request(url, headers={"User-Agent": "Mozilla/5.0 NadgryzieniUpcoming/1.0"})
    try:
        with _open_https_no_redirect(request, timeout=30) as response:
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
    _validated_stream_fields(stream)
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


def _reject_symlink_components(path: Path, stop_at: Path) -> None:
    current = Path(path)
    boundary = Path(stop_at)
    if boundary not in {current, *current.parents}:
        boundary = current.parent.parent
    while True:
        if current.is_symlink():
            raise RuntimeError(f"Refusing unsafe symlink path component: {current}")
        if current == boundary:
            return
        parent = current.parent
        if parent == current:
            return
        current = parent


def _read_json(path: Path, default: dict, *, root: Path | None = None) -> dict:
    try:
        content = _read_bytes_secure(path, root or path.parent)
        if content is None:
            return default
        payload = json.loads(content.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, RuntimeError) as exc:
        raise RuntimeError(f"Upcoming state is unreadable: {type(exc).__name__}") from exc
    if not isinstance(payload, dict):
        raise RuntimeError("Upcoming state must be a JSON object")
    return payload


def _encode_json(payload: dict) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=False) + "\n"


def _payload_digest(payload: dict) -> str:
    return hashlib.sha256(_encode_json(payload).encode("utf-8")).hexdigest()


def _read_bytes_secure(path: Path, root: Path) -> bytes | None:
    path_abs, root_abs = _assert_path_within(path, root)
    _reject_symlink_components(path_abs, root_abs)
    parent_fd = _open_secure_directory_fd(path_abs.parent, root_abs)
    try:
        try:
            descriptor = os.open(
                path_abs.name,
                os.O_RDONLY
                | getattr(os, "O_NOFOLLOW", 0)
                | getattr(os, "O_NONBLOCK", 0),
                dir_fd=parent_fd,
            )
        except FileNotFoundError:
            return None
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            os.close(descriptor)
            raise RuntimeError(f"Upcoming state path is not a regular file: {path_abs}")
        with os.fdopen(descriptor, "rb") as source:
            return source.read()
    finally:
        os.close(parent_fd)


def _artifact_digest(path: Path) -> str | None:
    content = _read_bytes_secure(path, path.parent)
    if content is None:
        return None
    return hashlib.sha256(content).hexdigest()


def _absolute_path(path: Path) -> Path:
    return Path(os.path.abspath(os.fspath(path)))


def _assert_path_within(path: Path, root: Path) -> tuple[Path, Path]:
    path_abs = _absolute_path(path)
    root_abs = _absolute_path(root)
    try:
        path_abs.relative_to(root_abs)
    except ValueError as exc:
        raise RuntimeError(f"Unsafe path outside root: {path_abs}") from exc
    return path_abs, root_abs


def _open_secure_directory_fd(directory: Path, root: Path) -> int:
    directory_abs, root_abs = _assert_path_within(directory, root)
    _reject_symlink_components(directory_abs, root_abs)
    directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    current_fd = os.open(root_abs, directory_flags)
    try:
        for component in directory_abs.relative_to(root_abs).parts:
            if component in {"", "."}:
                continue
            try:
                next_fd = os.open(component, directory_flags, dir_fd=current_fd)
            except FileNotFoundError:
                os.mkdir(component, 0o700, dir_fd=current_fd)
                next_fd = os.open(component, directory_flags, dir_fd=current_fd)
            os.close(current_fd)
            current_fd = next_fd
        return current_fd
    except Exception:
        os.close(current_fd)
        raise


def _ensure_secure_directory(directory: Path, root: Path, mode: int = 0o700) -> None:
    descriptor = _open_secure_directory_fd(directory, root)
    try:
        os.fchmod(descriptor, mode)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _write_json_atomic(path: Path, payload: dict, *, root: Path | None = None, mode: int = 0o600) -> bool:
    encoded = _encode_json(payload).encode("utf-8")
    path_abs, root_abs = _assert_path_within(path, root or path.parent)
    _ensure_secure_directory(path_abs.parent, root_abs)
    parent_fd = _open_secure_directory_fd(path_abs.parent, root_abs)
    temporary_name = None
    try:
        try:
            existing_stat = os.stat(path_abs.name, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            existing_stat = None
        if existing_stat is not None:
            if stat.S_ISLNK(existing_stat.st_mode):
                raise RuntimeError(f"Refusing symlink atomic-write destination: {path_abs}")
            if not stat.S_ISREG(existing_stat.st_mode):
                raise RuntimeError(f"Unsafe atomic-write destination: {path_abs}")
        try:
            existing_fd = os.open(
                path_abs.name,
                os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0),
                dir_fd=parent_fd,
            )
        except FileNotFoundError:
            existing_fd = None
        if existing_fd is not None:
            try:
                if not stat.S_ISREG(os.fstat(existing_fd).st_mode):
                    raise RuntimeError(f"Unsafe atomic-write destination: {path_abs}")
                with os.fdopen(existing_fd, "rb") as existing:
                    existing_fd = None
                    if existing.read() == encoded:
                        return False
            finally:
                if existing_fd is not None:
                    os.close(existing_fd)
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
        for _ in range(10):
            candidate = f".{path_abs.name}.{uuid.uuid4().hex}.tmp"
            try:
                descriptor = os.open(candidate, flags, mode, dir_fd=parent_fd)
                temporary_name = candidate
                break
            except FileExistsError:
                continue
        else:
            raise RuntimeError("Could not allocate a unique atomic-write temporary file")
        with os.fdopen(descriptor, "wb") as temporary:
            temporary.write(encoded)
            temporary.flush()
            os.fchmod(temporary.fileno(), mode)
            os.fsync(temporary.fileno())
        os.replace(
            temporary_name,
            path_abs.name,
            src_dir_fd=parent_fd,
            dst_dir_fd=parent_fd,
        )
        os.fsync(parent_fd)
        temporary_name = None
        return True
    finally:
        if temporary_name is not None:
            try:
                os.unlink(temporary_name, dir_fd=parent_fd)
            except FileNotFoundError:
                pass
        os.close(parent_fd)


def artifact_for(stream: Stream | None, now: datetime) -> dict:
    event = None
    if stream is not None:
        video_id, title, start_utc = _validated_stream_fields(stream)
        event = {
            "video_id": video_id,
            "title": title,
            "scheduled_start_utc": iso_utc(start_utc),
            "url": f"https://www.youtube.com/watch?v={video_id}",
        }
    return {
        "schema_version": ARTIFACT_SCHEMA_VERSION,
        "updated_at_utc": iso_utc(now),
        "source_url": YOUTUBE_LIVE_URL,
        "event": event,
    }


@contextmanager
def _exclusive_cycle_lock(state_path: Path):
    """Serialize discovery, state writes, and artifact publication per state file."""
    import fcntl

    root = state_path.parent.parent
    _ensure_secure_directory(state_path.parent, root)
    lock_path = state_path.with_name(state_path.name + ".lock")
    parent_fd = _open_secure_directory_fd(state_path.parent, root)
    try:
        lock_abs, parent_abs = _assert_path_within(lock_path, state_path.parent)
        if lock_abs.parent != parent_abs:
            raise RuntimeError("Cycle lock path must be directly inside the state directory")
        descriptor = None
        try:
            descriptor = os.open(
                lock_abs.name,
                os.O_RDWR
                | os.O_CREAT
                | getattr(os, "O_NOFOLLOW", 0)
                | getattr(os, "O_NONBLOCK", 0),
                0o600,
                dir_fd=parent_fd,
            )
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                raise RuntimeError("Cycle lock is not a regular file")
            handle = os.fdopen(descriptor, "a+", encoding="utf-8")
            descriptor = None
        finally:
            if descriptor is not None:
                os.close(descriptor)
    finally:
        os.close(parent_fd)
    try:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        yield True
    finally:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()


def run_cycle(
    now: datetime,
    state_path: Path = STATE_PATH,
    artifact_path: Path = ARTIFACT_PATH,
    discover: Callable[[datetime], Stream | None] = discover_stream,
    publish: Callable[[], object] | None = None,
    force: bool = False,
) -> dict:
    """Run one gated discovery cycle under an exclusive per-state lock."""
    current = as_utc(now)
    if not force and not in_probe_window(current):
        return {"status": "outside_probe_window"}
    with _exclusive_cycle_lock(state_path) as acquired:
        if not acquired:
            return {"status": "already_running"}
        return _run_cycle_locked(
            current,
            state_path=state_path,
            artifact_path=artifact_path,
            discover=discover,
            publish=publish,
            force=force,
        )


def _call_publisher(publish: Callable[..., object], pending_commit: str | None = None) -> object:
    if pending_commit is not None and publish is publish_git:
        return publish(pending_commit)
    return publish()


def _retry_pending_publication(
    state: dict,
    state_path: Path,
    artifact_path: Path,
    publish: Callable[[], object] | None,
) -> bool:
    if not state.get("publish_pending") or publish is None:
        return False
    pending_digest = state.get("pending_artifact_sha256")
    if not isinstance(pending_digest, str) or not re.fullmatch(r"[0-9a-f]{64}", pending_digest):
        raise RuntimeError("Pending publication is missing its artifact digest")
    if _artifact_digest(artifact_path) != pending_digest:
        raise RuntimeError("Pending publication artifact digest does not match the recorded artifact")
    pending_commit = state.get("pending_commit")
    try:
        publication_result = _call_publisher(publish, pending_commit)
    except PublishPendingError as exc:
        state["pending_commit"] = exc.commit
        _write_json_atomic(state_path, state, root=state_path.parent.parent)
        raise
    if publication_result is True:
        state["publish_pending"] = False
        state.pop("pending_artifact_sha256", None)
        state.pop("pending_commit", None)
    _write_json_atomic(state_path, state, root=state_path.parent.parent)
    return True


def _validate_state(state: dict) -> None:
    if "schema_version" not in state:
        raise RuntimeError("Upcoming state is missing schema_version")
    schema_version = state["schema_version"]
    if type(schema_version) is not int or schema_version != STATE_SCHEMA_VERSION:
        raise RuntimeError("Upcoming state has an unsupported schema")
    allowed = {
        "schema_version",
        "last_probe_date_utc",
        "hold_until_utc",
        "publish_pending",
        "pending_artifact_sha256",
        "pending_commit",
        "video_id",
        "scheduled_start_utc",
    }
    if set(state) - allowed:
        raise RuntimeError("Upcoming state contains unknown fields")
    if "last_probe_date_utc" in state:
        probe_date = state["last_probe_date_utc"]
        if not isinstance(probe_date, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", probe_date):
            raise RuntimeError("Upcoming state has an invalid UTC probe date")
        try:
            datetime.strptime(probe_date, "%Y-%m-%d")
        except ValueError as exc:
            raise RuntimeError("Upcoming state has an invalid UTC probe date") from exc
    if "hold_until_utc" in state and state["hold_until_utc"] is not None:
        parse_iso_utc(state["hold_until_utc"])
    if "publish_pending" in state and type(state["publish_pending"]) is not bool:
        raise RuntimeError("Upcoming state publish_pending must be boolean")
    pending_digest = state.get("pending_artifact_sha256")
    if state.get("publish_pending") is True and pending_digest is None:
        raise RuntimeError("Upcoming state publish_pending requires an artifact digest")
    if pending_digest is not None:
        if not isinstance(pending_digest, str) or not re.fullmatch(r"[0-9a-f]{64}", pending_digest):
            raise RuntimeError("Upcoming state has an invalid pending artifact digest")
        if state.get("publish_pending") is not True:
            raise RuntimeError("Upcoming state pending artifact digest requires publish_pending")
    pending_commit = state.get("pending_commit")
    if pending_commit is not None:
        if not isinstance(pending_commit, str) or not re.fullmatch(r"[0-9a-f]{40}", pending_commit):
            raise RuntimeError("Upcoming state has an invalid pending commit")
        if state.get("publish_pending") is not True:
            raise RuntimeError("Upcoming state pending commit requires publish_pending")
    if "video_id" in state:
        if not isinstance(state["video_id"], str) or not re.fullmatch(r"[A-Za-z0-9_-]{6,20}", state["video_id"]):
            raise RuntimeError("Upcoming state has an invalid video ID")
    if "scheduled_start_utc" in state:
        scheduled = parse_iso_utc(state["scheduled_start_utc"])
        if not MIN_UNIX_TIMESTAMP <= int(scheduled.timestamp()) <= MAX_UNIX_TIMESTAMP:
            raise RuntimeError("Upcoming state has an out-of-range scheduled timestamp")


def _publish_and_persist_state(
    publish: Callable[..., object],
    new_state: dict,
    state_path: Path,
    artifact_digest: str,
) -> object:
    try:
        publication_result = _call_publisher(publish)
    except PublishPendingError as exc:
        new_state["publish_pending"] = True
        new_state["pending_artifact_sha256"] = artifact_digest
        new_state["pending_commit"] = exc.commit
        _write_json_atomic(state_path, new_state, root=state_path.parent.parent)
        raise
    if publication_result is True:
        new_state["publish_pending"] = False
        new_state.pop("pending_artifact_sha256", None)
        new_state.pop("pending_commit", None)
    _write_json_atomic(state_path, new_state, root=state_path.parent.parent)
    return publication_result


def _run_cycle_locked(
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

    state = _read_json(
        state_path,
        {"schema_version": STATE_SCHEMA_VERSION},
        root=state_path.parent.parent,
    )
    _validate_state(state)
    publication_retried = _retry_pending_publication(state, state_path, artifact_path, publish)
    pending_before_probe = bool(state.get("publish_pending"))
    hold_until_raw = state.get("hold_until_utc")
    if hold_until_raw:
        hold_until = parse_iso_utc(hold_until_raw)
        if current < hold_until:
            result: dict[str, object] = {"status": "paused", "hold_until_utc": iso_utc(hold_until)}
            if publication_retried:
                result["publication_retried"] = True
            return result

    probe_date = current.date().isoformat()
    if not force and state.get("last_probe_date_utc") == probe_date:
        result: dict[str, object] = {"status": "already_probed", "probe_date_utc": probe_date}
        if publication_retried:
            result["publication_retried"] = True
        return result

    if state.get("publish_pending") is True:
        result = {"status": "publication_pending"}
        if publication_retried:
            result["publication_retried"] = True
        return result
    if publication_retried:
        return {"status": "publication_retried", "publication_retried": True}
    try:
        stream = discover(current)
    except SkipRun:
        stream = None

    if stream is None:
        artifact_payload = artifact_for(None, current)
        artifact_digest = _payload_digest(artifact_payload)
        if publish:
            provisional_state = dict(state)
            provisional_state["publish_pending"] = True
            provisional_state["pending_artifact_sha256"] = artifact_digest
            _write_json_atomic(state_path, provisional_state, root=state_path.parent.parent)
        artifact_changed = _write_json_atomic(
            artifact_path,
            artifact_payload,
            root=artifact_path.parent,
            mode=0o644,
        )
        new_state = {
            "schema_version": STATE_SCHEMA_VERSION,
            "last_probe_date_utc": probe_date,
            "hold_until_utc": None,
            "publish_pending": pending_before_probe or bool(artifact_changed and publish),
        }
        if artifact_changed and publish:
            new_state["pending_artifact_sha256"] = artifact_digest
        elif pending_before_probe and state.get("pending_artifact_sha256"):
            new_state["pending_artifact_sha256"] = state["pending_artifact_sha256"]
        _write_json_atomic(state_path, new_state, root=state_path.parent.parent)
        if artifact_changed and publish:
            _publish_and_persist_state(publish, new_state, state_path, artifact_digest)
        return {"status": "not_found", "artifact_changed": artifact_changed}

    resume_at = next_saturday_probe(current)
    artifact_payload = artifact_for(stream, current)
    artifact_digest = _payload_digest(artifact_payload)
    if publish:
        provisional_state = dict(state)
        provisional_state["publish_pending"] = True
        provisional_state["pending_artifact_sha256"] = artifact_digest
        _write_json_atomic(state_path, provisional_state, root=state_path.parent.parent)
    artifact_changed = _write_json_atomic(
        artifact_path,
        artifact_payload,
        root=artifact_path.parent,
        mode=0o644,
    )
    new_state = {
        "schema_version": STATE_SCHEMA_VERSION,
        "last_probe_date_utc": probe_date,
        "hold_until_utc": iso_utc(resume_at),
        "video_id": stream.video_id,
        "scheduled_start_utc": iso_utc(stream.start_utc),
        "publish_pending": pending_before_probe or bool(artifact_changed and publish),
    }
    if artifact_changed and publish:
        new_state["pending_artifact_sha256"] = artifact_digest
    elif pending_before_probe and state.get("pending_artifact_sha256"):
        new_state["pending_artifact_sha256"] = state["pending_artifact_sha256"]
    _write_json_atomic(state_path, new_state, root=state_path.parent.parent)
    if artifact_changed and publish:
        _publish_and_persist_state(publish, new_state, state_path, artifact_digest)
    return {
        "status": "found",
        "video_id": stream.video_id,
        "title": stream.title,
        "scheduled_start_utc": iso_utc(stream.start_utc),
        "hold_until_utc": iso_utc(resume_at),
        "artifact_changed": artifact_changed,
    }


def publish_git(pending_commit: str | None = None) -> bool:
    """Commit and push only upcoming.json under the shared pipeline lock."""
    if pending_commit is not None and not re.fullmatch(r"[0-9a-f]{40}", pending_commit):
        raise ValueError("Pending publication commit is invalid")
    sys.path.insert(0, str(REPO_DIR))
    import nadgryzieni_pipeline as pipeline

    if not pipeline.acquire_pipeline_lock():
        raise RuntimeError("Another Nadgryzieni pipeline run is active")
    try:
        repo = str(REPO_DIR)
        if pending_commit is not None:
            ahead_of_origin = pipeline._local_commits_ahead_of_origin(repo)
            current_commit = pipeline._git_head_sha(repo)
            if ahead_of_origin is not True or current_commit != pending_commit:
                raise RuntimeError("Pending upcoming commit no longer matches local main")
            verified_commit = pending_commit
            pipeline._ensure_main_branch(repo)
            pipeline._ensure_ahead_commits_are_allowed(repo, {"upcoming.json"})
            try:
                pipeline._push_with_retries(repo)
            except pipeline.GitPushPendingError as exc:
                raise PublishPendingError(verified_commit) from exc
            return True

        today = datetime.now(timezone.utc).date().isoformat()
        try:
            return pipeline.git_commit_and_push_paths(
                f"Nadgryzieni upcoming event – {today}",
                ["upcoming.json"],
            )
        except pipeline.GitPushPendingError as exc:
            current_commit = pipeline._git_head_sha(repo)
            if current_commit is None:
                raise RuntimeError("Git push failed and local upcoming commit could not be identified") from exc
            raise PublishPendingError(current_commit) from exc
    finally:
        pipeline.release_pipeline_lock()


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
