#!/usr/bin/env python3
"""
nadgryzieni_pipeline.py — Weekly automation pipeline for the Nadgryzieni episode archive.

Runs every Friday at 17:00 local time (scheduled via Hermes cronjob). Conditional retries run on Sunday and Tuesday at 04:00 local time. Does the following:

1. Fetches the RSS feed and parses episode items.
2. Compares against the existing repo archive to find new episodes.
3. Appends new episodes to `Nadgryzieni Episode Archive.md` (column-padded markdown table).
4. Regenerates `data.json` (Chart.js scatter plot data) from the archive.
5. Regenerates `Nadgryzieni Statistics.md` with comprehensive stats.
6. Bumps cache-busting version (?v=N) in index.html, script.js, data.json references.
7. Commits and pushes to Git (triggers GitHub Pages rebuild).
8. Syncs archive and statistics to Obsidian vault directory.

If no new episodes are found, the script exits silently (no output, no commit).

Usage:
    python3 nadgryzieni_pipeline.py           # full run
    python3 nadgryzieni_pipeline.py --dry     # dry run (no git commit/push)
    python3 nadgryzieni_pipeline.py --force   # force regeneration even if no new episodes
"""

from __future__ import annotations

import functools
import json
import hashlib
import os
import re
import stat
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
import uuid
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict
from datetime import datetime, date, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import urlparse
import unicodedata

from nadgryzieni_hosts import (
    _sanitize_public_value as _sanitize_host_public_value,
    apply_record_host_corrections,
    canonical_url,
    normalize_host_name,
)

# ── Configuration ────────────────────────────────────────────────────────────
# Resolve from the script location so manual and cron invocations use the same checkout.
REPO_DIR = Path(os.environ.get("NADGRYZIENI_REPO_DIR", Path(__file__).resolve().parent))
ARCHIVE_PATH = REPO_DIR / "Nadgryzieni Episode Archive.md"
STATS_PATH = REPO_DIR / "Nadgryzieni Statistics.md"
DATA_JSON_PATH = REPO_DIR / "data.json"
HOST_METADATA_PATH = REPO_DIR / "host_metadata.json"
INDEX_HTML_PATH = REPO_DIR / "index.html"
SCRIPT_JS_PATH = REPO_DIR / "script.js"
README_PATH = REPO_DIR / "README.md"

# Obsidian vault directory for syncing archive and statistics
VAULT_DIR = Path("/Users/tarkin/Library/Mobile Documents/com~apple~CloudDocs/! Hermes !/Scarif Vault/20-Podcast")

RSS_URL = "https://retrorocketnetwork.pl/category/nadgryzieni-rss/feed/"

# Patreon creator page (public posts are scraped; private RSS requires auth)
PATREON_URL = "https://www.patreon.com/iMagazinePL/posts"
PATREON_MANIFEST_PATH = REPO_DIR / "patreon_posts.json"

# Content widths for the padded markdown table (content is left-justified,
# with 1 space padding on each side between | characters)
# Derived from the existing archive; Hosts is appended so the legacy column
# order remains stable: counter, episode, title, date, duration, hosts.
COL_CONTENT_WIDTHS = [3, 5, 108, 12, 8, 34]

# Durable operational state. These files are deliberately outside the Git checkout.
STATE_DIR = Path(os.environ.get(
    "NADGRYZIENI_STATE_DIR",
    str(Path.home() / ".hermes" / "profiles" / "r2-d2" / "state"),
))
RETRY_STATE_PATH = STATE_DIR / "nadgryzieni-retry-state.json"
LOCK_PATH = STATE_DIR / "nadgryzieni-pipeline.lock"
_LOCK_HANDLE = None
_LOCK_OWNER: int | None = None
_LOCK_DEPTH = 0
_LOCK_STATE_GUARD = threading.Lock()
PUBLICATION_JOURNAL_PATH = STATE_DIR / "nadgryzieni-publication-journal.json"


class GitPushPendingError(RuntimeError):
    """A local commit exists but still needs a later push retry."""


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(
        path,
        os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
    )
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _fsync_file(path: Path) -> None:
    descriptor = os.open(
        path,
        os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0),
    )
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise RuntimeError(f"Unsafe non-regular file for fsync: {path}")
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


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
        relative_parts = directory_abs.relative_to(root_abs).parts
        for component in relative_parts:
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
            raise RuntimeError(f"Unsafe regular-file path: {path_abs}")
        with os.fdopen(descriptor, "rb") as source:
            return source.read()
    finally:
        os.close(parent_fd)


def _read_text_secure(path: Path, root: Path) -> str:
    content = _read_bytes_secure(path, root)
    if content is None:
        raise FileNotFoundError(path)
    return content.decode("utf-8")


def _atomic_write_bytes(path: Path, content: bytes, *, root: Path, mode: int = 0o600) -> None:
    path_abs, root_abs = _assert_path_within(path, root)
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
            temporary.write(content)
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
    finally:
        if temporary_name is not None:
            try:
                os.unlink(temporary_name, dir_fd=parent_fd)
            except FileNotFoundError:
                pass
        os.close(parent_fd)


def _atomic_write_text(path: Path, content: str, *, root: Path, mode: int = 0o600) -> None:
    _atomic_write_bytes(path, content.encode("utf-8"), root=root, mode=mode)


def _write_publication_journal(payload: dict) -> None:
    _ensure_secure_directory(STATE_DIR, STATE_DIR.parent)
    _atomic_write_text(
        PUBLICATION_JOURNAL_PATH,
        json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n",
        root=STATE_DIR.parent,
    )


def _path_is_within(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def _has_parent_component(path: Path) -> bool:
    return ".." in path.parts


def _approved_publication_target(target: Path) -> bool:
    allowed = set(globals().get("PUBLISH_PATHS", ()))
    return target.name in allowed


def _validate_replacement_paths(replacements: list[tuple[Path, Path]]) -> list[tuple[Path, Path]]:
    normalized = [(Path(target), Path(staged)) for target, staged in replacements]
    seen_targets = set()
    seen_staged = set()
    for target, staged in normalized:
        if (
            not target.is_absolute()
            or not staged.is_absolute()
            or not _path_is_within(target, REPO_DIR)
            or _has_parent_component(target)
            or not _approved_publication_target(target)
            or target.parent.resolve() != REPO_DIR.resolve()
            or target.is_symlink()
            or staged.is_symlink()
        ):
            raise ValueError("publication replacement path is unsafe")
        _reject_symlink_components(target, REPO_DIR)
        _reject_symlink_components(staged, REPO_DIR.parent)
        if target.exists() and not target.is_file():
            raise ValueError("publication target must be a regular file")
        stage_root = staged.parent
        if (
            stage_root.is_symlink()
            or _has_parent_component(stage_root)
            or stage_root.parent.resolve() != REPO_DIR.parent.resolve()
            or not stage_root.name.startswith((".nadgryzieni-pipeline-stage-", ".nadgryzieni-hosts-stage-"))
        ):
            raise ValueError("publication staging path is unsafe")
        if staged.exists() and not staged.is_file():
            raise ValueError("publication staged path must be a regular file")
        target_key = target.resolve()
        staged_key = staged.resolve()
        if target_key in seen_targets or staged_key in seen_staged or target_key == staged_key:
            raise ValueError("publication replacement paths must be unique and disjoint")
        seen_targets.add(target_key)
        seen_staged.add(staged_key)
    return normalized


def _validate_journal_identity(value: object, field_name: str) -> None:
    if value is None:
        return
    if (
        not isinstance(value, dict)
        or set(value) != {"dev", "ino"}
        or type(value["dev"]) is not int
        or type(value["ino"]) is not int
        or value["dev"] < 0
        or value["ino"] < 0
    ):
        raise ValueError(f"publication journal {field_name} is invalid")


def _validate_publication_journal(journal: dict) -> tuple[str, list[dict], Path]:
    if not isinstance(journal, dict):
        raise ValueError("publication journal must be an object")
    if set(journal) != {"phase", "entries", "backup_dir"}:
        raise ValueError("publication journal has an unsupported schema")
    phase = journal.get("phase")
    entries = journal.get("entries")
    backup_dir_value = journal.get("backup_dir")
    if not isinstance(backup_dir_value, str):
        raise ValueError("publication journal backup directory is invalid")
    backup_dir = Path(backup_dir_value)
    if phase not in {"prepared", "committed"} or not isinstance(entries, list) or not entries:
        raise ValueError("publication journal has invalid metadata")
    if (
        not backup_dir.is_absolute()
        or _has_parent_component(backup_dir)
        or not backup_dir.name.startswith(".nadgryzieni-publication-backup-")
    ):
        raise ValueError("publication journal backup directory is unsafe")
    _reject_symlink_components(backup_dir, REPO_DIR)
    if backup_dir.parent.resolve() != REPO_DIR.resolve() or backup_dir.is_symlink():
        raise ValueError("publication journal backup directory is outside the repository")
    if backup_dir.exists() and not backup_dir.is_dir():
        raise ValueError("publication journal backup directory is not a directory")
    if phase == "prepared" and not backup_dir.is_dir():
        raise ValueError("publication journal backup directory is missing")

    seen_targets = set()
    seen_staged = set()
    seen_backups = set()
    expected_backups = set()
    expected_stage_files: dict[Path, set[str]] = {}
    for entry in entries:
        if not isinstance(entry, dict):
            raise ValueError("publication journal entry is not an object")
        if set(entry) != {
            "target",
            "staged",
            "backup",
            "target_existed",
            "target_identity",
            "backup_identity",
            "staged_identity",
        }:
            raise ValueError("publication journal entry has an unsupported schema")
        target_value = entry.get("target")
        staged_value = entry.get("staged")
        backup_value = entry.get("backup")
        target_identity = entry.get("target_identity")
        backup_identity = entry.get("backup_identity")
        staged_identity = entry.get("staged_identity")
        _validate_journal_identity(target_identity, "target identity")
        _validate_journal_identity(backup_identity, "backup identity")
        _validate_journal_identity(staged_identity, "staged identity")
        if staged_identity is None:
            raise ValueError("publication journal staged identity is invalid")
        if not isinstance(target_value, str) or not isinstance(staged_value, str):
            raise ValueError("publication journal entry paths are invalid")
        if backup_value is not None and not isinstance(backup_value, str):
            raise ValueError("publication journal backup path is invalid")
        target = Path(target_value)
        staged = Path(staged_value)
        backup = Path(backup_value) if backup_value is not None else None
        _reject_symlink_components(target, REPO_DIR)
        _reject_symlink_components(staged, REPO_DIR.parent)
        if (
            not target.is_absolute()
            or not staged.is_absolute()
            or not _path_is_within(target, REPO_DIR)
            or _has_parent_component(target)
            or not _approved_publication_target(target)
            or target.parent.resolve() != REPO_DIR.resolve()
            or target.is_symlink()
            or staged.is_symlink()
            or (target.exists() and not target.is_file())
            or (staged.exists() and not staged.is_file())
        ):
            raise ValueError("publication journal entry path is outside the repository")
        target_key = target.resolve()
        if target_key in seen_targets or target.parent.resolve() != backup_dir.parent.resolve():
            raise ValueError("publication journal has duplicate or misplaced targets")
        seen_targets.add(target_key)
        stage_root = staged.parent
        if (
            stage_root.is_symlink()
            or _has_parent_component(stage_root)
            or stage_root.parent.resolve() != REPO_DIR.parent.resolve()
            or not stage_root.name.startswith((".nadgryzieni-pipeline-stage-", ".nadgryzieni-hosts-stage-"))
        ):
            raise ValueError("publication journal staging path is unsafe")
        expected_stage_files.setdefault(stage_root, set()).add(staged.name)
        staged_key = staged.resolve()
        if staged_key in seen_staged or target_key == staged_key:
            raise ValueError("publication journal has duplicate staged paths")
        seen_staged.add(staged_key)
        if backup is not None:
            _reject_symlink_components(backup, backup_dir)
            if (
                _has_parent_component(backup)
                or backup.parent.resolve() != backup_dir.resolve()
                or backup.is_symlink()
                or (backup.exists() and not backup.is_file())
            ):
                raise ValueError("publication journal backup path is unsafe")
            backup_key = backup.resolve()
            if backup_key in seen_backups:
                raise ValueError("publication journal has duplicate backup paths")
            seen_backups.add(backup_key)
            expected_backups.add(backup.name)
        target_existed = entry.get("target_existed")
        if not isinstance(target_existed, bool):
            raise ValueError("publication journal target identity is invalid")
        if target_existed != (target_identity is not None):
            raise ValueError("publication journal target identity does not match target_existed")
        if target_existed and backup is None:
            raise ValueError("publication journal target identity has no backup")
        if not target_existed and backup is not None:
            raise ValueError("publication journal backup exists for a new target")
        if backup is None and backup_identity is not None:
            raise ValueError("publication journal backup identity has no backup")
        if phase == "committed" and not target.exists():
            raise ValueError("publication journal committed target is missing")
        if backup is not None and backup.exists() and backup_identity is None:
            raise ValueError("publication journal backup identity is missing")
        if phase == "committed" and backup is not None and not backup.exists():
            raise ValueError("publication journal backup is missing for a committed target")
    if backup_dir.exists():
        for child in backup_dir.iterdir():
            if child.name not in expected_backups or child.is_symlink() or not child.is_file():
                raise ValueError("publication journal backup directory contains unsafe entries")
    for stage_root, expected_names in expected_stage_files.items():
        if not stage_root.exists():
            continue
        if stage_root.is_symlink() or not stage_root.is_dir():
            raise ValueError("publication journal staging directory is unsafe")
        for child in stage_root.iterdir():
            if child.name not in expected_names or child.is_symlink() or not child.is_file():
                raise ValueError("publication journal staging directory contains unsafe entries")
    for entry in entries:
        staged = Path(entry["staged"])
        target = Path(entry["target"])
        backup_value = entry.get("backup")
        backup = Path(backup_value) if backup_value else None
        target_identity = entry["target_identity"]
        backup_identity = entry["backup_identity"]
        staged_identity = entry["staged_identity"]
        if staged.exists() and _file_identity(staged, root=REPO_DIR.parent) != staged_identity:
            raise ValueError("publication journal staged identity changed")
        target_present = target.exists()
        current_target_identity = _file_identity(target, root=REPO_DIR) if target_present else None
        backup_present = backup is not None and backup.exists()
        if backup_present and backup is not None:
            if backup_identity is None:
                raise ValueError("publication journal backup identity is missing")
            if _file_identity(backup, root=REPO_DIR) != backup_identity:
                raise ValueError("publication journal backup identity changed")
        if phase == "committed":
            if current_target_identity != staged_identity:
                raise ValueError("publication journal committed target identity changed")
        elif entry["target_existed"]:
            allowed_target_identities = {
                (target_identity["dev"], target_identity["ino"]),
                (staged_identity["dev"], staged_identity["ino"]),
            }
            if current_target_identity is not None and (
                current_target_identity["dev"], current_target_identity["ino"]
            ) not in allowed_target_identities:
                raise ValueError("publication journal target identity changed")
            if current_target_identity is None and not backup_present:
                raise ValueError("publication journal target and backup are both missing")
        elif current_target_identity is not None and current_target_identity != staged_identity:
            raise ValueError("publication journal new target already exists")
    return phase, entries, backup_dir


def _create_secure_directory(parent: Path, root: Path, prefix: str) -> Path:
    parent_fd = _open_secure_directory_fd(parent, root)
    try:
        for _ in range(20):
            name = f"{prefix}{uuid.uuid4().hex}"
            try:
                os.mkdir(name, 0o700, dir_fd=parent_fd)
            except FileExistsError:
                continue
            os.fsync(parent_fd)
            return _absolute_path(parent) / name
        raise RuntimeError("Could not allocate a secure temporary directory")
    finally:
        os.close(parent_fd)


def _entry_exists_verified(path: Path, *, root: Path) -> bool:
    parent_fd = _open_secure_directory_fd(path.parent, root)
    try:
        try:
            path_stat = os.stat(path.name, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            return False
        if not stat.S_ISREG(path_stat.st_mode):
            raise RuntimeError(f"Unsafe regular-file path: {path}")
        return True
    finally:
        os.close(parent_fd)


def _file_identity(path: Path, *, root: Path) -> dict[str, int]:
    parent_fd = _open_secure_directory_fd(path.parent, root)
    descriptor = None
    try:
        descriptor = os.open(
            path.name,
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0),
            dir_fd=parent_fd,
        )
        file_stat = os.fstat(descriptor)
        if not stat.S_ISREG(file_stat.st_mode):
            raise RuntimeError(f"Unsafe non-regular file for identity: {path}")
        return {"dev": int(file_stat.st_dev), "ino": int(file_stat.st_ino)}
    finally:
        if descriptor is not None:
            os.close(descriptor)
        os.close(parent_fd)


def _fsync_file_verified(path: Path, *, root: Path) -> None:
    parent_fd = _open_secure_directory_fd(path.parent, root)
    try:
        descriptor = os.open(
            path.name,
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0),
            dir_fd=parent_fd,
        )
        try:
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                raise RuntimeError(f"Unsafe non-regular file for fsync: {path}")
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    finally:
        os.close(parent_fd)


def _remove_tree_fd(directory_fd: int) -> None:
    directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    for name in os.listdir(directory_fd):
        try:
            child_fd = os.open(name, directory_flags, dir_fd=directory_fd)
        except NotADirectoryError:
            child_stat = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
            if not stat.S_ISREG(child_stat.st_mode):
                raise RuntimeError(f"Unsafe cleanup entry: {name}")
            os.unlink(name, dir_fd=directory_fd)
            continue
        try:
            _remove_tree_fd(child_fd)
        finally:
            os.close(child_fd)
        os.rmdir(name, dir_fd=directory_fd)


def _remove_directory_verified(path: Path, *, root: Path) -> None:
    parent_fd = _open_secure_directory_fd(path.parent, root)
    directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        try:
            directory_fd = os.open(path.name, directory_flags, dir_fd=parent_fd)
        except FileNotFoundError:
            return
        try:
            _remove_tree_fd(directory_fd)
        finally:
            os.close(directory_fd)
        os.rmdir(path.name, dir_fd=parent_fd)
        os.fsync(parent_fd)
    finally:
        os.close(parent_fd)


def _unlink_verified(path: Path, *, root: Path, expected_identity: dict[str, int] | None = None) -> None:
    parent_fd = _open_secure_directory_fd(path.parent, root)
    try:
        try:
            path_stat = os.stat(path.name, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            return
        if not stat.S_ISREG(path_stat.st_mode):
            raise RuntimeError(f"Unsafe cleanup file: {path}")
        if expected_identity is not None and (
            path_stat.st_dev != expected_identity["dev"]
            or path_stat.st_ino != expected_identity["ino"]
        ):
            raise RuntimeError(f"Cleanup file identity changed: {path}")
        os.unlink(path.name, dir_fd=parent_fd)
        os.fsync(parent_fd)
    finally:
        os.close(parent_fd)


def _install_file_no_replace(source: Path, target: Path, *, root: Path, remove_source: bool = True) -> None:
    """Install a staged regular file without replacing a concurrently-created target."""
    source_abs, root_abs = _assert_path_within(source, root)
    target_abs, _ = _assert_path_within(target, root)
    if source_abs == target_abs:
        raise RuntimeError("Replacement source and target must be different files")
    source_parent_fd = _open_secure_directory_fd(source_abs.parent, root_abs)
    target_parent_fd = _open_secure_directory_fd(target_abs.parent, root_abs)
    source_fd = None
    try:
        source_fd = os.open(
            source_abs.name,
            os.O_RDONLY
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_NONBLOCK", 0),
            dir_fd=source_parent_fd,
        )
        source_stat = os.fstat(source_fd)
        if not stat.S_ISREG(source_stat.st_mode):
            raise RuntimeError(f"Unsafe replacement source: {source_abs}")
        try:
            os.link(
                source_abs.name,
                target_abs.name,
                src_dir_fd=source_parent_fd,
                dst_dir_fd=target_parent_fd,
                follow_symlinks=False,
            )
        except FileExistsError as exc:
            raise RuntimeError("Publication target appeared during replacement") from exc
        os.fsync(target_parent_fd)
        target_stat = os.stat(target_abs.name, dir_fd=target_parent_fd, follow_symlinks=False)
        if (
            not stat.S_ISREG(target_stat.st_mode)
            or target_stat.st_dev != source_stat.st_dev
            or target_stat.st_ino != source_stat.st_ino
        ):
            raise RuntimeError(f"Replacement target identity changed during publication: {target_abs}")
        if remove_source:
            source_check = os.stat(source_abs.name, dir_fd=source_parent_fd, follow_symlinks=False)
            if (
                not stat.S_ISREG(source_check.st_mode)
                or source_check.st_dev != source_stat.st_dev
                or source_check.st_ino != source_stat.st_ino
            ):
                raise RuntimeError(f"Replacement source changed during publication: {source_abs}")
            os.unlink(source_abs.name, dir_fd=source_parent_fd)
            os.fsync(source_parent_fd)
    finally:
        if source_fd is not None:
            os.close(source_fd)
        os.close(source_parent_fd)
        os.close(target_parent_fd)


def _replace_file_verified(
    source: Path,
    target: Path,
    *,
    root: Path,
    remove_source: bool = True,
    expected_target_identity: dict[str, int] | None = None,
    expected_target_absent: bool = False,
) -> None:
    source_abs, root_abs = _assert_path_within(source, root)
    target_abs, _ = _assert_path_within(target, root)
    if source_abs == target_abs:
        raise RuntimeError("Replacement source and target must be different files")
    source_parent_fd = _open_secure_directory_fd(source_abs.parent, root_abs)
    target_parent_fd = _open_secure_directory_fd(target_abs.parent, root_abs)
    temporary_name = None
    try:
        source_fd = os.open(
            source_abs.name,
            os.O_RDONLY
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_NONBLOCK", 0),
            dir_fd=source_parent_fd,
        )
        try:
            source_stat = os.fstat(source_fd)
            if not stat.S_ISREG(source_stat.st_mode):
                raise RuntimeError(f"Unsafe replacement source: {source_abs}")
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
            for _ in range(10):
                candidate = f".{target_abs.name}.{uuid.uuid4().hex}.replace"
                try:
                    temporary_fd = os.open(candidate, flags, source_stat.st_mode & 0o777, dir_fd=target_parent_fd)
                    temporary_name = candidate
                    break
                except FileExistsError:
                    continue
            else:
                raise RuntimeError("Could not allocate a unique replacement temporary file")
            try:
                while True:
                    chunk = os.read(source_fd, 1024 * 1024)
                    if not chunk:
                        break
                    view = memoryview(chunk)
                    while view:
                        written = os.write(temporary_fd, view)
                        if written <= 0:
                            raise OSError("Replacement temporary write made no progress")
                        view = view[written:]
                os.fchmod(temporary_fd, source_stat.st_mode & 0o777)
                os.fsync(temporary_fd)
            finally:
                os.close(temporary_fd)
        finally:
            os.close(source_fd)
        try:
            current_target_stat = os.stat(target_abs.name, dir_fd=target_parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            current_target_stat = None
        if expected_target_absent and current_target_stat is not None:
            raise RuntimeError(f"Replacement target appeared before conditional replacement: {target_abs}")
        if expected_target_identity is not None:
            if current_target_stat is None or (
                not stat.S_ISREG(current_target_stat.st_mode)
                or current_target_stat.st_dev != expected_target_identity["dev"]
                or current_target_stat.st_ino != expected_target_identity["ino"]
            ):
                raise RuntimeError(f"Replacement target identity changed before replacement: {target_abs}")
        os.replace(
            temporary_name,
            target_abs.name,
            src_dir_fd=target_parent_fd,
            dst_dir_fd=target_parent_fd,
        )
        os.fsync(target_parent_fd)
        target_stat = os.stat(target_abs.name, dir_fd=target_parent_fd, follow_symlinks=False)
        if not stat.S_ISREG(target_stat.st_mode):
            raise RuntimeError(f"Replacement target is not a regular file: {target_abs}")
        temporary_name = None
        if remove_source:
            source_check = os.stat(source_abs.name, dir_fd=source_parent_fd, follow_symlinks=False)
            if (
                not stat.S_ISREG(source_check.st_mode)
                or source_check.st_dev != source_stat.st_dev
                or source_check.st_ino != source_stat.st_ino
            ):
                raise RuntimeError(f"Replacement source changed during publication: {source_abs}")
            os.unlink(source_abs.name, dir_fd=source_parent_fd)
            os.fsync(source_parent_fd)
    finally:
        if temporary_name is not None:
            try:
                os.unlink(temporary_name, dir_fd=target_parent_fd)
            except FileNotFoundError:
                pass
        os.close(source_parent_fd)
        os.close(target_parent_fd)


def _recover_publication_journal() -> None:
    _reject_symlink_components(STATE_DIR, STATE_DIR.parent)
    _reject_symlink_components(PUBLICATION_JOURNAL_PATH, STATE_DIR.parent)
    if PUBLICATION_JOURNAL_PATH.is_symlink():
        raise RuntimeError("Publication journal is a symlink; refusing to publish")
    if not PUBLICATION_JOURNAL_PATH.exists():
        return
    if not PUBLICATION_JOURNAL_PATH.is_file():
        raise RuntimeError("Publication journal is not a regular file; refusing to publish")
    try:
        content = _read_bytes_secure(PUBLICATION_JOURNAL_PATH, STATE_DIR.parent)
        if content is None:
            return
        journal = json.loads(content.decode("utf-8"))
        phase, entries, backup_dir = _validate_publication_journal(journal)
    except (OSError, UnicodeDecodeError, TypeError, ValueError, json.JSONDecodeError, RuntimeError) as exc:
        raise RuntimeError("Publication journal is unreadable or unsafe; refusing to publish") from exc

    if phase == "committed":
        try:
            staging_dirs = {Path(entry["staged"]).parent for entry in entries}
            for staging_dir in staging_dirs:
                if (
                    staging_dir.parent.resolve() == REPO_DIR.parent.resolve()
                    and staging_dir.name.startswith(
                        (".nadgryzieni-pipeline-stage-", ".nadgryzieni-hosts-stage-")
                    )
                ):
                    _remove_directory_verified(staging_dir, root=REPO_DIR.parent)
            _remove_directory_verified(backup_dir, root=REPO_DIR)
            _unlink_verified(PUBLICATION_JOURNAL_PATH, root=STATE_DIR.parent)
            _fsync_directory(STATE_DIR)
        except Exception as exc:
            raise RuntimeError("Committed publication cleanup failed; journal retained") from exc
        return
    if phase != "prepared" or not isinstance(entries, list):
        raise RuntimeError("Publication journal has an unsupported phase")

    touched_dirs = set()
    staging_dirs = set()
    for entry in reversed(entries):
        target = Path(entry["target"])
        staged = Path(entry["staged"])
        staging_dirs.add(staged.parent)
        backup_value = entry.get("backup")
        backup = Path(backup_value) if backup_value else None
        target_existed = bool(entry["target_existed"])
        touched_dirs.add(target.parent)
        target_identity = entry["target_identity"]
        staged_identity = entry["staged_identity"]
        target_current_identity = _file_identity(target, root=REPO_DIR) if target.exists() else None
        if target_existed:
            if target_current_identity == staged_identity:
                if backup is None:
                    raise RuntimeError("Prepared publication backup is missing")
                _replace_file_verified(
                    backup,
                    target,
                    root=REPO_DIR,
                    remove_source=False,
                    expected_target_identity=staged_identity,
                )
            elif target_current_identity == target_identity:
                pass
            elif target_current_identity is None:
                if backup is None:
                    raise RuntimeError("Prepared publication backup is missing")
                _replace_file_verified(
                    backup,
                    target,
                    root=REPO_DIR,
                    remove_source=False,
                    expected_target_absent=True,
                )
            else:
                raise RuntimeError("Prepared publication target identity changed")
        elif target_current_identity == staged_identity:
            _unlink_verified(target, root=REPO_DIR, expected_identity=staged_identity)
        elif target_current_identity is not None:
            raise RuntimeError("Prepared publication new target identity changed")
        _unlink_verified(staged, root=REPO_DIR.parent, expected_identity=staged_identity)

    try:
        _write_publication_journal({**journal, "phase": "committed"})
        for staging_dir in staging_dirs:
            if (
                staging_dir.parent.resolve() == REPO_DIR.parent.resolve()
                and staging_dir.name.startswith(
                    (".nadgryzieni-pipeline-stage-", ".nadgryzieni-hosts-stage-")
                )
            ):
                _remove_directory_verified(staging_dir, root=REPO_DIR.parent)
        _remove_directory_verified(backup_dir, root=REPO_DIR)
        _unlink_verified(PUBLICATION_JOURNAL_PATH, root=STATE_DIR.parent)
    except Exception as exc:
        raise RuntimeError("Publication recovery cleanup failed; journal retained") from exc
    for directory in touched_dirs:
        _fsync_directory(directory)
    _fsync_directory(STATE_DIR)


def atomic_replace_group(replacements: list[tuple[Path, Path]]) -> None:
    """Publish a multi-file generation with crash recovery and durable journaling."""
    if not replacements:
        return
    lock_owned_here = not _pipeline_lock_owned_by_current_thread()
    if lock_owned_here and not acquire_pipeline_lock():
        raise RuntimeError("Could not acquire publication lock")
    try:
        _atomic_replace_group_locked(replacements)
    finally:
        if lock_owned_here:
            release_pipeline_lock()


def _atomic_replace_group_locked(replacements: list[tuple[Path, Path]]) -> None:
    if not replacements:
        return
    _recover_publication_journal()
    normalized = _validate_replacement_paths(replacements)
    for _, staged in normalized:
        if not _entry_exists_verified(staged, root=REPO_DIR.parent):
            raise FileNotFoundError(f"Staged publication file is missing: {staged}")
        _fsync_file_verified(staged, root=REPO_DIR.parent)
    backup_dir = _create_secure_directory(normalized[0][0].parent, REPO_DIR, ".nadgryzieni-publication-backup-")
    entries = []
    touched_dirs = {target.parent for target, _ in normalized}
    for index, (target, staged) in enumerate(normalized):
        if not _entry_exists_verified(staged, root=REPO_DIR.parent):
            raise FileNotFoundError(f"Staged publication file is missing: {staged}")
        target_existed = _entry_exists_verified(target, root=REPO_DIR)
        target_identity = _file_identity(target, root=REPO_DIR) if target_existed else None
        entries.append({
            "target": str(target),
            "staged": str(staged),
            "backup": str(backup_dir / f"{index}.bak") if target_existed else None,
            "target_existed": target_existed,
            "target_identity": target_identity,
            "backup_identity": None,
            "staged_identity": _file_identity(staged, root=REPO_DIR.parent),
        })
    journal = {"phase": "prepared", "backup_dir": str(backup_dir), "entries": entries}
    journal_written = False
    try:
        _write_publication_journal(journal)
        journal_written = True
        for entry in entries:
            target = Path(entry["target"])
            staged = Path(entry["staged"])
            backup_value = entry.get("backup")
            if backup_value:
                backup = Path(backup_value)
                _replace_file_verified(target, backup, root=REPO_DIR)
                entry["backup_identity"] = _file_identity(backup, root=REPO_DIR)
                _write_publication_journal(journal)
            _install_file_no_replace(staged, target, root=REPO_DIR.parent)
            _fsync_directory(target.parent)
        _write_publication_journal({**journal, "phase": "committed"})
        for directory in touched_dirs:
            _fsync_directory(directory)
    except Exception:
        journal_present = False
        try:
            journal_present = _entry_exists_verified(PUBLICATION_JOURNAL_PATH, root=STATE_DIR.parent)
        except Exception as journal_error:
            raise RuntimeError("Publication failed and journal state is uncertain") from journal_error
        if journal_written or journal_present:
            try:
                _recover_publication_journal()
            except Exception as recovery_error:
                raise RuntimeError("Publication failed and recovery also failed") from recovery_error
        else:
            try:
                _remove_directory_verified(backup_dir, root=REPO_DIR)
            except Exception as cleanup_error:
                raise RuntimeError("Publication failed and backup cleanup also failed") from cleanup_error
        raise
    else:
        try:
            _remove_directory_verified(backup_dir, root=REPO_DIR)
            _unlink_verified(PUBLICATION_JOURNAL_PATH, root=STATE_DIR.parent)
            _fsync_directory(STATE_DIR)
        except Exception as exc:
            raise RuntimeError("Publication cleanup failed; journal retained") from exc

# Cache-busting version is committed with the generated site, not kept in /tmp.
CACHE_VERSION_FILE = REPO_DIR / ".cache-version"

# Only generated/published files may be staged by an automated run.
PUBLISH_PATHS = [
    "Nadgryzieni Episode Archive.md",
    "Nadgryzieni Statistics.md",
    "README.md",
    "data.json",
    "host_metadata.json",
    "patreon_posts.json",
    "index.html",
    "script.js",
    ".cache-version",
]

# Logging
import logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stderr)],
)
log = logging.getLogger("nadgryzieni-pipeline")

# RSS namespace
RSS_NS = {"itunes": "http://www.itunes.com/dtds/podcast-1.0.dtd"}


def create_ssl_context() -> ssl.SSLContext:
    """Return a certificate-validating HTTPS context."""
    return ssl.create_default_context()


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Reject redirects so source identity cannot silently change."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _open_https_no_redirect(request, *, timeout: float, context: ssl.SSLContext):
    if urlparse(request.full_url).scheme.casefold() != "https":
        raise ValueError("HTTPS URL required")
    opener = urllib.request.build_opener(
        _NoRedirectHandler(),
        urllib.request.HTTPSHandler(context=context),
    )
    return opener.open(request, timeout=timeout)


def parse_publish_date(value: str) -> str:
    """Normalize an RSS/Patreon date to ISO YYYY-MM-DD where possible."""
    if not value:
        return ""
    try:
        return parsedate_to_datetime(value).date().isoformat()
    except (TypeError, ValueError, IndexError, OverflowError):
        return value[:10] if len(value) >= 10 else ""


def _canonical_episode_source_url(value: str) -> str:
    """Accept only canonical URLs from the reviewed archive source origins."""
    canonical = canonical_url(value)
    parsed = urlparse(canonical)
    if parsed.netloc == "retrorocketnetwork.pl" and parsed.path != "/":
        return canonical
    if parsed.netloc == "www.patreon.com" and re.fullmatch(
        r"/iMagazinePL/posts/[A-Za-z0-9][A-Za-z0-9-]*", parsed.path
    ):
        return canonical
    raise ValueError("source URL origin/path is not allowed")


# ── RSS Fetching ─────────────────────────────────────────────────────────────

def fetch_rss(max_retries: int = 3) -> bytes:
    """Fetch the RSS feed with retries. Returns raw XML bytes."""
    ctx = create_ssl_context()
    for attempt in range(1, max_retries + 1):
        try:
            req = urllib.request.Request(RSS_URL, headers={
                "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
            })
            with _open_https_no_redirect(req, timeout=30, context=ctx) as resp:
                return resp.read()
        except Exception as exc:
            log.warning("RSS fetch attempt %s/%s failed: %s", attempt, max_retries, _safe_log_text(exc))
            if attempt < max_retries:
                time.sleep(2 ** attempt)
    raise RuntimeError(f"RSS fetch failed after {max_retries} attempts")


# ── RSS Parsing ──────────────────────────────────────────────────────────────

def parse_rss_items(xml_bytes: bytes) -> list[dict]:
    """Parse RSS XML and return list of episode dicts with title, date, duration, url."""
    root = ET.fromstring(xml_bytes)
    items = []
    for entry in root.findall(".//item"):
        title_el = entry.find("title")
        pubdate_el = entry.find("pubDate")
        duration_el = entry.find(".//itunes:duration", RSS_NS)
        guid_el = entry.find("guid")
        link_el = entry.find("link")

        if title_el is None or pubdate_el is None:
            continue

        title = title_el.text.strip() if title_el.text else ""
        pubdate = pubdate_el.text.strip() if pubdate_el.text else ""
        duration = duration_el.text.strip() if duration_el is not None and duration_el.text else ""
        guid = guid_el.text.strip() if guid_el is not None and guid_el.text else ""
        link = link_el.text.strip() if link_el is not None and link_el.text else ""
        if not link and guid.startswith("http"):
            link = guid
        if not link:
            continue
        try:
            link = _canonical_episode_source_url(link)
        except ValueError:
            continue

        # Parse pubDate (RFC 822)
        iso_date = parse_publish_date(pubdate)

        # Extract episode number from title
        ep_num = extract_episode_number(title)

        items.append({
            "title": title,
            "date": iso_date,
            "duration": duration,
            "guid": guid,
            "url": link,
            "episode_number": ep_num,
        })
    return items


# ── Patreon Scraping ──────────────────────────────────────────────────────────

def load_patreon_manifest(path: Path = PATREON_MANIFEST_PATH) -> list[dict]:
    """Load the tracked Patreon fallback manifest without exposing credentials.

    Browser-assisted cron runs may include verified title/date/duration metadata
    because Patreon can block the pipeline's non-browser post-page fetch. Those
    fields are optional for backwards compatibility with older manifest entries.
    """
    try:
        content = _read_bytes_secure(path, path.parent.parent)
        if content is None:
            return []
        payload = json.loads(content.decode("utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        log.warning("Could not read Patreon manifest: %s", type(exc).__name__)
        return []

    if not isinstance(payload, dict) or not isinstance(payload.get("posts"), list):
        log.warning("Could not read Patreon manifest: invalid JSON shape")
        return []

    posts = []
    for post in payload.get("posts", []):
        try:
            episode = int(post["episode"])
            slug = str(post["slug"]).strip()
            url = str(post["url"]).strip()
        except (KeyError, TypeError, ValueError):
            continue
        parsed_url = urlparse(url)
        expected_url = f"https://www.patreon.com/iMagazinePL/posts/{slug}"
        if (
            parsed_url.scheme != "https"
            or parsed_url.netloc != "www.patreon.com"
            or parsed_url.path != f"/iMagazinePL/posts/{slug}"
            or parsed_url.query
            or parsed_url.fragment
            or not re.fullmatch(rf"{episode}-afterparty(?:-[a-z0-9]+)*-\d{{6,12}}", slug)
            or url != expected_url
        ):
            continue

        entry = {"episode": episode, "slug": slug, "url": expected_url}
        title = post.get("title")
        if title is not None:
            title = str(title).strip()
            if not title or "(Afterparty)" not in title:
                continue
            entry["title"] = title

        pub_date = post.get("date")
        if pub_date is not None:
            pub_date = str(pub_date).strip()
            if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", pub_date):
                continue
            entry["date"] = pub_date

        duration = post.get("duration")
        if duration is not None:
            duration = str(duration).strip()
            duration_parts = duration.split(":")
            if len(duration_parts) not in (2, 3) or any(
                not part.isdigit() for part in duration_parts
            ):
                continue
            entry["duration"] = duration

        posts.append(entry)
    return posts


PATREON_MANIFEST = load_patreon_manifest()
PATREON_KNOWN_POSTS = [(post["episode"], post["slug"]) for post in PATREON_MANIFEST]
PATREON_POST_URLS = {f"{post['episode']}.5": post["url"] for post in PATREON_MANIFEST}


def _fetch_patreon_post_page(slug: str) -> str | None:
    """Fetch a single Patreon post page and return its HTML."""
    ctx = create_ssl_context()
    url = f"https://www.patreon.com/iMagazinePL/posts/{slug}"
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
        "Accept": "text/html",
    })
    try:
        with _open_https_no_redirect(req, timeout=15, context=ctx) as resp:
            return resp.read().decode("utf-8", errors="replace")
    except Exception:
        return None


def _parse_patreon_post(html: str, source_url: str = "") -> dict | None:
    """Extract title, duration, and date from a Patreon post page HTML."""
    # Extract title from og:title meta tag
    title_match = re.search(r'og:title" content="([^"]+)"', html)
    if not title_match:
        return None
    title = title_match.group(1)
    # Strip the " | iMagazinePL x Nadgryzieni" suffix (and any other " | ..." suffixes)
    if " | " in title:
        title = title.split(" | ")[0].strip()

    # Extract duration
    duration = None
    # Try: "duration":"HH:MM:SS" format
    dur_match = re.search(r'"duration":"(\d{1,2}:\d{2}:\d{2})"', html)
    if dur_match:
        duration = dur_match.group(1)
    else:
        # Try: duration in seconds (integer)
        dur_sec = re.search(r'"duration":(\d+)', html)
        if dur_sec:
            secs = int(dur_sec.group(1))
            h = secs // 3600
            m = (secs % 3600) // 60
            s = secs % 60
            duration = f"{h}:{m:02d}:{s:02d}"

    # Extract published date
    date_match = re.search(r'"published_at":"([^"]+)"', html)
    pub_date = parse_publish_date(date_match.group(1)) if date_match else ""

    return {
        "title": title,
        "duration": duration,
        "date": pub_date,
        "url": source_url,
    }


def _fetch_patreon_posts_from_list() -> list[dict]:
    """Fetch known Patreon posts, using browser-verified manifest metadata when present."""
    posts = []
    for post in PATREON_MANIFEST:
        ep_num = post["episode"]
        slug = post["slug"]
        if post.get("title"):
            parsed = {
                "title": post["title"],
                "duration": post.get("duration"),
                "date": post.get("date", ""),
                "url": post["url"],
            }
        else:
            html = _fetch_patreon_post_page(slug)
            if not html:
                continue
            parsed = _parse_patreon_post(html, post["url"])
            if not parsed:
                continue
        parsed["episode_number"] = ep_num
        posts.append(parsed)
    return posts


def _merge_patreon_posts(*sources: list[dict]) -> list[dict]:
    """Merge Patreon source results by episode, preferring verified metadata."""
    merged: dict[str, dict] = {}
    for source in sources:
        for post in source:
            key = str(post.get("episode_number") or post.get("episode") or post.get("url") or "").strip()
            if not key:
                continue
            current = merged.setdefault(key, {})
            for field, value in post.items():
                if value not in (None, ""):
                    current[field] = value
    return list(merged.values())


def fetch_patreon_posts() -> list[dict]:
    """Fetch and merge Patreon Afterparty episodes from all available sources.

    1. If PATREON_RSS_URL env var is set, fetch authenticated RSS XML.
    2. Try scraping the public posts page for post slugs (JS-rendered, may fail).
    3. Merge the tracked verified post manifest, including browser-verified metadata.

    Returns a deduplicated list of dicts with keys: title, episode_number,
    duration, date, and url where available.
    """
    source_results: list[list[dict]] = []

    # 1. Try authenticated RSS
    rss_url = os.environ.get("PATREON_RSS_URL")
    if rss_url:
        try:
            parsed = urlparse(rss_url)
            if parsed.scheme != "https" or not parsed.netloc:
                raise ValueError("PATREON_RSS_URL must use HTTPS")
            ctx = create_ssl_context()
            req = urllib.request.Request(rss_url, headers={
                "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
                "Accept": "application/xml",
            })
            with _open_https_no_redirect(req, timeout=30, context=ctx) as resp:
                xml_bytes = resp.read()
            rss_posts = _parse_patreon_rss(xml_bytes)
            if rss_posts:
                source_results.append(rss_posts)
            else:
                log.warning("Patreon RSS returned no Afterparty records")
        except Exception as exc:
            log.warning("Patreon RSS fetch failed: %s", type(exc).__name__)

    # 2. Try scraping the public posts page
    public_posts = _scrape_patreon_posts_page()
    if public_posts:
        source_results.append(public_posts)

    # 3. Merge the verified post manifest. This is intentionally consulted even
    # when another source returned records so browser-discovered posts cannot be
    # lost when authenticated RSS is incomplete.
    log.info("Loading the verified Patreon post manifest.")
    manifest_posts = _fetch_patreon_posts_from_list()
    if manifest_posts:
        source_results.append(manifest_posts)

    posts = _merge_patreon_posts(*source_results)
    if posts:
        log.info("Found %d Patreon Afterparty record(s) across configured sources.", len(posts))
    else:
        log.info("No Patreon Afterparty posts found.")
    return posts


def _scrape_patreon_posts_page() -> list[dict]:
    """Scrape the Patreon posts page to find Afterparty episode links.

    The page is JS-rendered, so this may not find any posts.
    Returns empty list if posts are not in the HTML.
    """
    try:
        ctx = create_ssl_context()
        patreon_url = PATREON_URL
        req = urllib.request.Request(patreon_url, headers={
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
            "Accept": "text/html",
        })
        with _open_https_no_redirect(req, timeout=30, context=ctx) as resp:
            html = resp.read().decode("utf-8", errors="replace")
    except Exception as exc:
        log.warning("Patreon page fetch failed: %s", type(exc).__name__)
        return []

    # Extract Next.js data chunks and concatenate
    all_data = ""
    for push in re.finditer(r'self\.__next_f\.push\(\[\d+,\s*"((?:[^"\\]|\\.)*)"\]\)', html):
        try:
            chunk = json.loads('"' + push.group(1) + '"')
            all_data += chunk
        except json.JSONDecodeError:
            continue

    # Look for post URL slugs in the data. Both `afterparty-ID` and
    # `afterparty-z-ID` forms occur in Patreon URLs.
    slug_pattern = re.compile(r'/iMagazinePL/posts/(\d+-afterparty(?:-[a-z]+)?-\d{6,12})')
    all_slugs = slug_pattern.findall(all_data) + slug_pattern.findall(html)

    # Deduplicate and convert to int
    seen = set()
    posts = []
    for slug in all_slugs:
        ep_num = int(slug.split("-", 1)[0])
        if ep_num in seen:
            continue
        seen.add(ep_num)
        page_html = _fetch_patreon_post_page(slug)
        if page_html:
            source_url = f"https://www.patreon.com/iMagazinePL/posts/{slug}"
            parsed = _parse_patreon_post(page_html, source_url)
            if parsed:
                parsed["episode_number"] = str(ep_num)
                posts.append(parsed)

    return posts


def _canonical_patreon_rss_url(value: str, episode_number: str) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = urlparse(value)
        if (
            parsed.scheme != "https"
            or parsed.netloc != "www.patreon.com"
            or parsed.username is not None
            or parsed.password is not None
            or parsed.port is not None
            or parsed.query
            or parsed.fragment
        ):
            return None
    except ValueError:
        return None
    prefix = "/iMagazinePL/posts/"
    if not parsed.path.startswith(prefix):
        return None
    slug = parsed.path[len(prefix):]
    if (
        not re.fullmatch(r"\d+-afterparty(?:-[a-z0-9]+)*-\d{6,12}", slug)
        or slug.split("-", 1)[0] != str(episode_number)
    ):
        return None
    return f"https://www.patreon.com{prefix}{slug}"


def _parse_patreon_rss(xml_bytes: bytes) -> list[dict]:
    """Parse Patreon RSS XML to extract afterparty episodes."""
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError:
        return []
    posts = []
    for item in root.findall(".//item"):
        title_el = item.find("title")
        if title_el is None or title_el.text is None:
            continue
        title = title_el.text.strip()
        if "(Afterparty)" not in title:
            continue
        ep_match = re.match(r"^(\d+):\s*\(Afterparty\)", title)
        if ep_match:
            # Try to get duration from either common iTunes namespace.
            duration = None
            itunes_dur = item.find(".//{http://www.itunes.com/dtds/rss-2.0.dtd}duration")
            if itunes_dur is None:
                itunes_dur = item.find(".//{http://www.itunes.com/dtds/podcast-1.0.dtd}duration")
            if itunes_dur is not None and itunes_dur.text:
                try:
                    secs = int(itunes_dur.text)
                    h = secs // 3600
                    m = (secs % 3600) // 60
                    s = secs % 60
                    duration = f"{h}:{m:02d}:{s:02d}"
                except (ValueError, TypeError):
                    duration = itunes_dur.text
            # Try to get a canonical Patreon source URL. Invalid external or
            # credential-bearing RSS links are discarded rather than published.
            date_str = ""
            pub_date = item.find("pubDate")
            if pub_date is not None and pub_date.text:
                date_str = parse_publish_date(pub_date.text.strip())
            link_el = item.find("link")
            guid_el = item.find("guid")
            raw_urls = []
            if link_el is not None and link_el.text:
                raw_urls.append(link_el.text.strip())
            if guid_el is not None and guid_el.text:
                raw_urls.append(guid_el.text.strip())
            source_url = next(
                (
                    candidate
                    for candidate in (
                        _canonical_patreon_rss_url(raw, ep_match.group(1))
                        for raw in raw_urls
                    )
                    if candidate is not None
                ),
                None,
            )
            if source_url is None:
                continue
            posts.append({
                "title": title,
                "episode_number": ep_match.group(1),
                "duration": duration,
                "date": date_str,
                "url": source_url,
            })
    return posts


def merge_patreon_episodes(existing_ep_numbers: set, new_episodes: list) -> list:
    """Check Patreon for afterparty episodes not in the regular feed.

    Patreon Afterparty episodes share episode numbers with main episodes
    (e.g., Afterparty 595 accompanies main episode 595). To avoid collisions,
    we use fractional numbering: Afterparty 595 → 595.5, 596 → 596.5, etc.

    Only adds episodes that are not already in the archive or new_episodes list.
    Returns a list of new Patreon episode dicts.
    """
    patreon_posts = fetch_patreon_posts()
    if not patreon_posts:
        return []

    all_known_numbers = existing_ep_numbers | {e["episode"] for e in new_episodes}
    patreon_new = []

    for post in patreon_posts:
        ep_num = post["episode_number"]
        patreon_ep_id = f"{ep_num}.5"  # Fractional to avoid collision with main episode
        if patreon_ep_id in all_known_numbers:
            continue
        patreon_new.append({
            "counter": "",  # Will be assigned after sorting
            "episode": patreon_ep_id,
            "title": post["title"],
            "date": post.get("date", ""),
            "duration": post.get("duration") or "?",
            "url": post.get("url") or PATREON_POST_URLS.get(patreon_ep_id, ""),
        })
        all_known_numbers.add(patreon_ep_id)

    return patreon_new


def extract_episode_number(title: str) -> str:
    """Extract episode number from title. Returns 'SP' for specials, or a number string."""
    # Try "598: Title" format
    m = re.match(r"^(\d+)([½⅓⅔]?)[\s:]", title.strip())
    if m:
        base = int(m.group(1))
        frac = m.group(2)
        if frac == '½':
            return f"{base}.5"
        elif frac == '⅓':
            return f"{base}.3"
        elif frac == '⅔':
            return f"{base}.6"
        else:
            return str(base)
    # Try old format "Nadgryzieni – 05 – Title"
    m = re.search(r"Nadgryzieni\s*(?:&#8211;|[-–])\s*(\d+)", title, re.I)
    if m:
        return m.group(1)
    # Try "Nadgryzieni – 05 – Title" with different dash
    m = re.search(r"Nadgryzieni\s*[-–]\s*(\d+)\s*[-–]", title, re.I)
    if m:
        return m.group(1)
    return "SP"


# ── Host metadata and record identity ────────────────────────────────────────

HOST_SCHEMA_VERSION = 1
HOST_STATUSES = {"verified", "not_listed", "unavailable", "ambiguous", "manual_review"}
HOST_UNRESOLVED_STATUSES = {"unavailable", "ambiguous", "manual_review"}
HOST_SOURCES = {"rrn", "patreon", "paired_rrn", "manual"}
HOST_ALIAS_POLICY = {
    "mode": "conservative",
    "description": "Only NFKC, whitespace and case-insensitive deduplication are automatic; reviewed legacy aliases are normalized to the public canonical names NPC and Thomas Voland on every parser and update path.",
    "aliases": {
        "legacy_norbert_alias": "NPC",
        "legacy_thomas_alias": "Thomas Voland",
    },
}
HOST_SENTINEL = "Brak danych"


def effective_host_alias_policy(stored: dict | None = None) -> dict:
    """Return the policy with the code-defined aliases always enforced."""
    policy = dict(stored) if isinstance(stored, dict) else {}
    stored_aliases = policy.get("aliases", {})
    if not isinstance(stored_aliases, dict):
        stored_aliases = {}
    aliases = {
        str(key): str(value)
        for key, value in stored_aliases.items()
        if not re.search(r"(?iu)\bnorbert\s+cała\b", f"{key} {value}")
        and not re.search(r"(?iu)\b(?:tomek\s+pluszczyk|thomas\s+voland)\b", f"{key} {value}")
    }
    aliases.update(HOST_ALIAS_POLICY["aliases"])
    policy.update({
        "mode": HOST_ALIAS_POLICY["mode"],
        "description": HOST_ALIAS_POLICY["description"],
        "aliases": aliases,
    })
    return policy


def normalize_host_display_name(value: str) -> str:
    """Normalize a stored/displayed host through the shared alias policy."""
    return normalize_host_name(value)


def normalize_identity_text(value: str) -> str:
    """Normalize text used only for deterministic identity/fingerprint keys."""
    return " ".join(unicodedata.normalize("NFKC", str(value or "")).replace("\u00a0", " ").split()).strip()


_SECRET_KEY_PATTERN = (
    r"authorization|api[_-]?key|access[_-]?token|client[_-]?secret|"
    r"refresh[_-]?token|private[_-]?key|password|passwd|secret|token|"
    r"credential|credentials"
)


def _sanitize_public_text(value: str) -> str:
    redacted = re.sub(
        r"(?i)(?:https?|ssh|git(?:\+ssh)?)\s*:(?:\\?/){2}[^\s<>\"']+",
        "[REDACTED_URL]",
        str(value),
    )
    redacted = re.sub(
        r"(?i)\b[^/\s@]+@[^/\s:]+:[^\s]+",
        "[REDACTED_REMOTE]",
        redacted,
    )
    redacted = re.sub(
        r"(?im)(\bauthorization\b(?:\s*[:=]\s*|\s+))[^\r\n]*",
        r"\1[REDACTED]",
        redacted,
    )
    redacted = re.sub(
        r"(?im)(\bpassword\s+for\b)[^\r\n]*",
        r"\1 [REDACTED]",
        redacted,
    )
    redacted = re.sub(
        r'''(?im)(\\*["']?\b(?:''' + _SECRET_KEY_PATTERN + r''')[A-Za-z0-9_-]*\\*["']?\s*[:=]\s*)(?:\\*"(?:\\.|[^"\\])*"\\*|\\*'(?:\\.|[^'\\])*'\\*|\\*\[[^\r\n\]]*\]\\*|[^\s,;}\]]+)''',
        r"\1[REDACTED]",
        redacted,
    )
    redacted = re.sub(
        r'''(?im)(\b(?:bearer|basic)\s+)(?:\\*"(?:\\.|[^"\\])*"\\*|\\*'(?:\\.|[^'\\])*'\\*|[A-Za-z0-9._~+/=-]+)''',
        r"\1[REDACTED]",
        redacted,
    )
    return redacted[:500]


def _safe_log_text(value: object) -> str:
    """Redact secrets and collapse controls before interpolating untrusted text in logs."""
    return " ".join(_sanitize_public_text(str(value)).split())[:300]


_SECRET_KEY_NAME_RE = re.compile(
    rf"(?i)(?:^|_)(?:{_SECRET_KEY_PATTERN})(?:_|$)"
)


def _is_secret_public_key(value: object) -> bool:
    text = unicodedata.normalize("NFKC", str(value))
    text = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", text)
    text = re.sub(r"[^A-Za-z0-9]+", "_", text).strip("_")
    return bool(_SECRET_KEY_NAME_RE.search(text))


def _sanitize_public_key(value: object) -> str:
    text = str(value)
    if _is_secret_public_key(text):
        return "[REDACTED_KEY]"
    return _sanitize_public_text(text)


def _sanitize_provenance(value: object) -> object:
    if isinstance(value, dict):
        sanitized = {}
        for child_key, child_value in value.items():
            safe_key = _sanitize_public_key(child_key)
            sanitized[safe_key] = (
                "[REDACTED]" if safe_key == "[REDACTED_KEY]" else _sanitize_provenance(child_value)
            )
        return sanitized
    if isinstance(value, list):
        return [_sanitize_provenance(item) for item in value]
    if isinstance(value, str):
        return _sanitize_public_text(value)
    return value


def _public_provenance(value: object, source_url: str = "") -> object:
    """Return the fail-closed provenance representation allowed in public JSON."""
    if value is None:
        return None
    sanitized = _sanitize_provenance(value)
    if not isinstance(sanitized, dict):
        raise ValueError("Host provenance must be an object")
    if "source_url" in sanitized:
        sanitized["source_url"] = source_url
    return sanitized


def canonical_source_url(value: str) -> str:
    """Canonicalize a public source URL for identity comparisons."""
    value = str(value or "").strip()
    if not value:
        return ""
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return value
    path = parsed.path or "/"
    if path != "/":
        path = path.rstrip("/") + "/"
    return parsed._replace(
        scheme=parsed.scheme.lower(),
        netloc=parsed.netloc.lower(),
        path=path,
        fragment="",
    ).geturl()


def _canonical_public_url(value: object) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    return canonical_url(text)


def build_record_key(row: dict) -> str:
    """Return a stable record key that does not rely on episode number alone or duration."""
    identity = {
        "source_url": canonical_source_url(row.get("url") or row.get("source_url", "")),
        "date": normalize_identity_text(row.get("date", "")),
        "episode": normalize_identity_text(row.get("episode", "")),
        "title": normalize_identity_text(row.get("title", "")),
    }
    digest = hashlib.sha256(
        json.dumps(identity, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()[:24]
    return f"rk_{digest}"


def dataset_fingerprint(rows: list[dict]) -> str:
    """Fingerprint non-host record identity fields for audit/apply reconciliation."""
    canonical_rows = []
    for row in rows:
        canonical_rows.append({
            "record_key": build_record_key(row),
            "source_url": canonical_source_url(row.get("url") or row.get("source_url", "")),
            "episode": normalize_identity_text(row.get("episode", "")),
            "title": normalize_identity_text(row.get("title", "")),
            "date": normalize_identity_text(row.get("date", "")),
            "duration": normalize_identity_text(row.get("duration", "")),
        })
    canonical_rows.sort(key=lambda value: value["record_key"])
    payload = json.dumps(canonical_rows, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _host_dedupe_key(value: str) -> str:
    return normalize_identity_text(value).casefold()


def validate_host_entry(
    entry: dict,
    record_key: str = "",
    expected_source_url: str = "",
) -> dict:
    """Validate and normalize one manifest entry without changing host spelling."""
    if not isinstance(entry, dict):
        raise ValueError(f"Host metadata for {record_key or 'record'} is not an object")
    unknown_fields = set(entry) - {
        "record_key",
        "hosts",
        "hosts_status",
        "hosts_source",
        "hosts_source_url",
        "provenance",
        "diagnostics",
        "audit",
    }
    if unknown_fields:
        raise ValueError(
            f"Host metadata for {record_key or 'record'} has unknown fields: {sorted(unknown_fields)}"
        )
    if record_key and entry.get("record_key") not in (None, "", record_key):
        raise ValueError(f"Host metadata record-key mismatch for {record_key}")
    entry = apply_record_host_corrections(record_key, entry)
    hosts = entry.get("hosts")
    if not isinstance(hosts, list):
        raise ValueError(f"Host metadata for {record_key or 'record'} has no hosts list")
    normalized_hosts = []
    seen = set()
    for host in hosts:
        if not isinstance(host, str) or not host.strip():
            raise ValueError(f"Host metadata for {record_key or 'record'} has an invalid host name")
        display = normalize_host_display_name(host)
        if ";" in display or "|" in display:
            raise ValueError(f"Host metadata for {record_key or 'record'} contains a table delimiter")
        key = _host_dedupe_key(display)
        if key in seen:
            raise ValueError(f"Host metadata for {record_key or 'record'} contains duplicate host names")
        seen.add(key)
        normalized_hosts.append(display)
    normalized_hosts.sort(key=_host_dedupe_key)

    status = str(entry.get("hosts_status") or "")
    source = str(entry.get("hosts_source") or "")
    source_url = str(entry.get("hosts_source_url") or "").strip()
    if status not in HOST_STATUSES:
        raise ValueError(f"Host metadata for {record_key or 'record'} has invalid status: {status!r}")
    if source not in HOST_SOURCES:
        raise ValueError(f"Host metadata for {record_key or 'record'} has invalid source: {source!r}")
    if status == "verified" and not normalized_hosts:
        raise ValueError(f"Verified host metadata for {record_key or 'record'} must contain a host")
    if status == "not_listed" and normalized_hosts:
        raise ValueError(f"Not-listed host metadata for {record_key or 'record'} cannot contain hosts")
    try:
        strict_source_url = canonical_url(source_url)
    except ValueError as exc:
        raise ValueError(f"Host metadata for {record_key or 'record'} has no valid source URL: {exc}") from exc
    if expected_source_url and source != "paired_rrn":
        try:
            expected_canonical = canonical_url(expected_source_url)
        except ValueError as exc:
            raise ValueError(f"Expected source URL for {record_key or 'record'} is invalid: {exc}") from exc
        if strict_source_url != expected_canonical:
            raise ValueError(f"Host metadata source URL mismatch for {record_key or 'record'}")

    provenance = entry.get("provenance")
    if provenance is None:
        provenance = {"kind": "direct_source", "source_url": source_url}
    if not isinstance(provenance, dict) or not provenance.get("kind"):
        raise ValueError(f"Host metadata for {record_key or 'record'} has invalid provenance")
    provenance = dict(provenance)
    raw_provenance_source_url = provenance.get("source_url")
    sanitized_provenance = _sanitize_provenance(provenance)
    if not isinstance(sanitized_provenance, dict):
        raise ValueError(f"Host metadata for {record_key or 'record'} has invalid provenance")
    provenance = sanitized_provenance
    if raw_provenance_source_url is not None:
        provenance["source_url"] = raw_provenance_source_url
    if provenance.get("source_url"):
        try:
            strict_provenance_url = canonical_url(str(provenance["source_url"]))
        except ValueError as exc:
            raise ValueError(f"Host metadata provenance URL for {record_key or 'record'} is invalid: {exc}") from exc
        if strict_provenance_url != strict_source_url:
            raise ValueError(f"Host metadata provenance/source URL mismatch for {record_key or 'record'}")
        provenance["source_url"] = strict_source_url
    elif source != "paired_rrn":
        raise ValueError(f"Host metadata for {record_key or 'record'} lacks provenance source URL")
    if source == "paired_rrn":
        if provenance.get("kind") != "paired_rrn" or not provenance.get("paired_record_key"):
            raise ValueError(f"Paired host metadata for {record_key or 'record'} lacks its paired record")
        if provenance.get("rule") != "afterparty_same_hosts_from_main":
            raise ValueError(f"Paired host metadata for {record_key or 'record'} lacks the approved rule")
    result = {
        "record_key": record_key,
        "hosts": normalized_hosts,
        "hosts_status": status,
        "hosts_source": source,
        "hosts_source_url": strict_source_url,
        "provenance": provenance,
    }
    if isinstance(entry.get("diagnostics"), list):
        result["diagnostics"] = [
            _sanitize_host_public_value(str(item))
            for item in entry["diagnostics"]
        ]
    if isinstance(entry.get("audit"), dict):
        sanitized_audit = _sanitize_host_public_value(entry["audit"])
        if not isinstance(sanitized_audit, dict):
            raise ValueError(f"Host metadata audit for {record_key or 'record'} is not an object")
        result["audit"] = sanitized_audit
    return result


def _record_rows_by_key(record_rows):
    if record_rows is None:
        return None
    if isinstance(record_rows, dict):
        return {str(key): value for key, value in record_rows.items() if isinstance(value, dict)}
    return {
        build_record_key(row): row
        for row in record_rows
        if isinstance(row, dict)
    }


def validate_manifest_integrity(manifest: dict, record_rows=None) -> dict:
    """Validate entries and bind every Afterparty pair to real archive identities."""
    if not isinstance(manifest, dict) or not isinstance(manifest.get("records"), dict):
        raise ValueError("Host manifest has no records object")
    unknown_fields = set(manifest) - {"schema_version", "alias_policy", "records"}
    if unknown_fields:
        raise ValueError(f"Host manifest has unknown fields: {sorted(unknown_fields)}")
    normalized_records = {
        str(record_key): validate_host_entry(manifest["records"][record_key], str(record_key))
        for record_key in sorted(manifest["records"], key=str)
    }
    identity_rows = _record_rows_by_key(record_rows)
    if identity_rows is None and any(entry["hosts_source"] == "paired_rrn" for entry in normalized_records.values()):
        raise ValueError("Paired host metadata requires archive identity context")
    bound_rows = identity_rows or {}
    if identity_rows is not None:
        expected_keys = set(identity_rows)
        actual_keys = set(normalized_records)
        if actual_keys != expected_keys:
            missing = sorted(expected_keys - actual_keys)
            orphaned = sorted(actual_keys - expected_keys)
            raise ValueError(
                f"Host manifest archive record key set mismatch (missing={missing[:3]}, orphaned={orphaned[:3]})"
            )
        for record_key, entry in normalized_records.items():
            if entry["hosts_source"] == "paired_rrn":
                continue
            row = bound_rows.get(record_key)
            if not isinstance(row, dict):
                continue
            raw_row_url = row.get("url") or row.get("source_url") or ""
            try:
                expected_row_url = canonical_url(str(raw_row_url))
            except ValueError as exc:
                raise ValueError(f"Host metadata for {record_key} has no canonical archive source URL") from exc
            if entry["hosts_source_url"] != expected_row_url:
                raise ValueError(f"Host metadata source URL mismatch for {record_key}")
    for record_key, entry in normalized_records.items():
        if entry["hosts_source"] != "paired_rrn":
            continue
        provenance = entry["provenance"]
        paired_key = str(provenance.get("paired_record_key") or "")
        if not paired_key or paired_key == record_key or paired_key not in normalized_records:
            raise ValueError(f"Paired host metadata for {record_key} references a missing or self pair")
        paired = normalized_records[paired_key]
        if paired["hosts_source"] != "rrn":
            raise ValueError(f"Paired host metadata for {record_key} does not reference an RRN record")
        if entry["hosts"] != paired["hosts"]:
            raise ValueError(f"Paired host metadata for {record_key} has different hosts from its pair")
        if entry["hosts_status"] != paired["hosts_status"]:
            raise ValueError(f"Paired host metadata for {record_key} has a different status from its pair")
        if entry["hosts_source_url"] != paired["hosts_source_url"]:
            raise ValueError(f"Paired host metadata for {record_key} has a different source URL from its pair")
        current_row = bound_rows.get(record_key)
        paired_row = bound_rows.get(paired_key)
        if not isinstance(current_row, dict) or not isinstance(paired_row, dict):
            raise ValueError(f"Paired host metadata for {record_key} has no bound archive pair")
        try:
            if build_record_key(current_row) != record_key or build_record_key(paired_row) != paired_key:
                raise ValueError(f"Paired host metadata for {record_key} has an identity-key mismatch")
        except (KeyError, TypeError, ValueError) as exc:
            if isinstance(exc, ValueError) and "identity-key mismatch" in str(exc):
                raise
            raise ValueError(f"Paired host metadata for {record_key} has invalid archive identity") from exc
        raw_paired_url = paired_row.get("url") or paired_row.get("source_url") or ""
        try:
            expected_paired_url = canonical_url(str(raw_paired_url))
        except ValueError as exc:
            raise ValueError(f"Paired host metadata for {record_key} has no canonical paired source URL") from exc
        if paired["hosts_source_url"] != expected_paired_url:
            raise ValueError(f"Paired host metadata for {record_key} has a mismatched base source URL")
        if entry["hosts_source_url"] != expected_paired_url:
            raise ValueError(f"Paired host metadata for {record_key} has a mismatched source URL")
        paired_episode = normalize_identity_text(provenance.get("paired_episode", ""))
        expected_paired_episode = normalize_identity_text(paired_row.get("episode", ""))
        if not paired_episode or paired_episode != expected_paired_episode:
            raise ValueError(f"Paired host metadata for {record_key} has an invalid paired episode")
        current_title = normalize_identity_text(current_row.get("title", "")).casefold()
        paired_title = normalize_identity_text(paired_row.get("title", "")).casefold()
        try:
            current_episode = float(normalize_identity_text(current_row.get("episode", "")))
            paired_episode_number = float(expected_paired_episode)
        except (TypeError, ValueError):
            current_episode = None
            paired_episode_number = None
        if (
            current_episode is None
            or paired_episode_number is None
            or current_episode.is_integer()
            or "(afterparty)" not in current_title
            or not paired_episode_number.is_integer()
            or "(afterparty)" in paired_title
        ):
            raise ValueError(f"Paired host metadata for {record_key} is not bound to a main Afterparty base")
        if str(int(current_episode)) != paired_episode:
            raise ValueError(f"Paired host metadata for {record_key} has an invalid base episode")
    return {
        "schema_version": HOST_SCHEMA_VERSION,
        "alias_policy": effective_host_alias_policy(manifest.get("alias_policy")),
        "records": normalized_records,
    }


def load_host_metadata(path: Path = HOST_METADATA_PATH, record_rows=None) -> dict:
    """Load and validate the tracked host manifest against archive identities."""
    content = _read_bytes_secure(path, path.parent.parent)
    if content is None:
        return {"schema_version": HOST_SCHEMA_VERSION, "alias_policy": effective_host_alias_policy(), "records": {}}
    payload = json.loads(content.decode("utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("host_metadata.json has an invalid top-level object")
    unknown_fields = set(payload) - {"schema_version", "alias_policy", "records"}
    if unknown_fields:
        raise ValueError(f"host_metadata.json has unknown fields: {sorted(unknown_fields)}")
    if payload.get("schema_version") != HOST_SCHEMA_VERSION or not isinstance(payload.get("records"), dict):
        raise ValueError("host_metadata.json has an unsupported schema")
    stored_alias_policy = payload.get("alias_policy", HOST_ALIAS_POLICY)
    if not isinstance(stored_alias_policy, dict) or stored_alias_policy.get("mode") != "conservative" or not isinstance(stored_alias_policy.get("aliases", {}), dict):
        raise ValueError("host_metadata.json has an invalid alias policy")
    return validate_manifest_integrity({
        "schema_version": HOST_SCHEMA_VERSION,
        "alias_policy": effective_host_alias_policy(stored_alias_policy),
        "records": payload["records"],
    }, record_rows=record_rows)


def write_host_metadata(manifest: dict, path: Path = HOST_METADATA_PATH, dry: bool = False, record_rows=None) -> None:
    """Write a deterministic manifest, or a .dry artifact when requested."""
    normalized = validate_manifest_integrity(manifest, record_rows=record_rows)
    target = path if not dry else path.with_name(path.name.replace(".json", ".dry.json"))
    _atomic_write_text(
        target,
        json.dumps(normalized, ensure_ascii=False, indent=2) + "\n",
        root=target.parent.parent,
        mode=0o644,
    )
    log.info("Host metadata written: %s (%d records)", target, len(normalized["records"]))


def parse_hosts_cell(value: str) -> list[str]:
    """Parse a six-column Hosts cell while accepting the legacy blank value."""
    value = normalize_identity_text(value)
    if not value or value == HOST_SENTINEL:
        return []
    return [normalize_host_display_name(part) for part in value.split(";") if normalize_identity_text(part)]


def hosts_cell(row: dict) -> str:
    """Serialize host metadata into the human-readable Markdown cell."""
    hosts = row.get("hosts") or []
    if row.get("hosts_status") in HOST_UNRESOLVED_STATUSES:
        return "Do weryfikacji"
    if row.get("hosts_status") == "not_listed" or not hosts:
        return HOST_SENTINEL
    validated = validate_host_entry({
        "hosts": hosts,
        "hosts_status": row.get("hosts_status", "verified"),
        "hosts_source": row.get("hosts_source", "rrn"),
        "hosts_source_url": row.get("hosts_source_url") or row.get("url", "https://example.invalid/"),
        "provenance": row.get("hosts_provenance") or row.get("provenance"),
    })
    return "; ".join(validated["hosts"])


def apply_host_metadata(rows: list[dict], manifest: dict, strict: bool = False) -> list[dict]:
    """Join manifest entries onto rows by record_key."""
    records = manifest.get("records", {})
    if not isinstance(records, dict):
        raise ValueError("Host manifest records must be an object")
    if strict:
        expected_keys = {build_record_key(row) for row in rows}
        actual_keys = {str(key) for key in records}
        if actual_keys != expected_keys:
            missing = sorted(expected_keys - actual_keys)
            orphaned = sorted(actual_keys - expected_keys)
            raise ValueError(f"Host manifest row binding mismatch (missing={missing[:3]}, orphaned={orphaned[:3]})")
        validate_manifest_integrity(manifest, record_rows=rows)
    for row in rows:
        row["record_key"] = build_record_key(row)
        entry = records.get(row["record_key"])
        if entry:
            expected_source_url = None
            if entry.get("hosts_source") != "paired_rrn":
                expected_source_url = row.get("hosts_source_url") or row.get("url")
            normalized = validate_host_entry(entry, row["record_key"], expected_source_url=expected_source_url or "")
            row.update({key: value for key, value in normalized.items() if key != "provenance"})
            row["hosts_provenance"] = normalized["provenance"]
        elif strict:
            raise ValueError(f"No host metadata for record {row['record_key']}")
    return rows


def manifest_from_rows(rows: list[dict], base: dict | None = None, strict: bool = True) -> dict:
    """Build the tracked manifest from the current rows, never preserving orphans."""
    records = {}
    base_records = (base or {}).get("records", {})
    for row in rows:
        row["record_key"] = build_record_key(row)
        if not row.get("hosts_status"):
            if strict:
                raise ValueError(f"Record {row['record_key']} has no host status")
            continue
        entry_input = {
            "hosts": row.get("hosts", []),
            "hosts_status": row.get("hosts_status"),
            "hosts_source": row.get("hosts_source"),
            "hosts_source_url": row.get("hosts_source_url") or row.get("url"),
            "provenance": row.get("hosts_provenance"),
        }
        previous = base_records.get(row["record_key"], {}) if isinstance(base_records, dict) else {}
        for optional_field in ("diagnostics", "audit"):
            if row.get(optional_field) is not None:
                entry_input[optional_field] = row[optional_field]
            elif isinstance(previous, dict) and previous.get(optional_field) is not None:
                entry_input[optional_field] = previous[optional_field]
        entry = validate_host_entry(
            entry_input,
            row["record_key"],
            expected_source_url=(row.get("hosts_source_url") or row.get("url")) if row.get("hosts_source") != "paired_rrn" else "",
        )
        records[row["record_key"]] = entry
    return validate_manifest_integrity({
        "schema_version": HOST_SCHEMA_VERSION,
        "alias_policy": effective_host_alias_policy((base or {}).get("alias_policy")),
        "records": records,
    }, record_rows=rows)


def enrich_host_rows(rows: list[dict], manifest: dict, refresh_keys: set[str] | None = None) -> dict:
    """Fetch only missing/refreshed rows and explicitly pair approved Afterparty rows."""
    import nadgryzieni_hosts as host_tools

    refresh_keys = refresh_keys or set()
    apply_host_metadata(rows, manifest, strict=False)
    rows_by_episode = {
        str(row.get("episode", "")): row
        for row in rows
        if "(afterparty)" not in normalize_identity_text(row.get("title", "")).casefold()
    }
    fetch_cache = {}
    robots_cache = {}
    last_fetch = [0.0]
    pending_pairs = []
    for row in rows:
        row["record_key"] = build_record_key(row)
        needs_refresh = row["record_key"] in refresh_keys or not row.get("hosts_status")
        if not needs_refresh:
            continue
        episode = str(row.get("episode", ""))
        try:
            numeric_episode = float(episode)
        except ValueError:
            numeric_episode = None
        is_afterparty = "(afterparty)" in normalize_identity_text(row.get("title", "")).casefold()
        base_episode = str(int(numeric_episode)) if numeric_episode is not None else ""
        main = rows_by_episode.get(base_episode)
        if is_afterparty and numeric_episode is not None and numeric_episode >= 550 and main is not None:
            pending_pairs.append((row, main))
            continue
        result = host_tools._direct_audit_entry(row, fetch_cache, robots_cache, last_fetch, 0.25)
        row.update({
            "hosts": list(result.get("hosts", [])),
            "hosts_status": result.get("hosts_status", "manual_review"),
            "hosts_source": result.get("hosts_source", "manual"),
            "hosts_source_url": result.get("hosts_source_url", ""),
            "hosts_provenance": result.get("provenance"),
        })
        if result.get("diagnostics"):
            row["diagnostics"] = list(result["diagnostics"])
    for row, main in pending_pairs:
        if main.get("hosts_status") not in {"verified", "not_listed"}:
            raise ValueError(
                f"Cannot pair host metadata for {row.get('episode')}: main record is unresolved"
            )
        row.update({
            "hosts": list(main.get("hosts", [])),
            "hosts_status": main["hosts_status"],
            "hosts_source": "paired_rrn",
            "hosts_source_url": main.get("hosts_source_url") or main.get("url", ""),
            "hosts_provenance": {
                "kind": "paired_rrn",
                "rule": "afterparty_same_hosts_from_main",
                "paired_record_key": main["record_key"],
                "paired_episode": str(main.get("episode", "")),
            },
        })
    return manifest_from_rows(rows, base=manifest, strict=True)


# ── Archive Reading ──────────────────────────────────────────────────────────

def parse_archive(path: Path) -> tuple[list[dict], str]:
    """Parse either the legacy five-column or enriched six-column archive table."""
    content = _read_text_secure(path, path.parent.parent)
    lines = content.splitlines()

    table_start = None
    for i, line in enumerate(lines):
        if line.strip().startswith("|") and "Episode title" in line and "Publish date" in line:
            table_start = i
            break

    if table_start is None:
        raise ValueError("Could not find table in archive")

    header_lines = lines[table_start:table_start + 2]
    header_parts = [part.strip() for part in header_lines[0].strip().split("|")[1:-1]]
    try:
        counter_index = header_parts.index("#")
        episode_index = header_parts.index("Ep.")
        title_index = header_parts.index("Episode title")
        date_index = header_parts.index("Publish date")
        duration_index = header_parts.index("Duration")
    except ValueError as exc:
        raise ValueError("Archive header is missing a required column") from exc
    hosts_index = header_parts.index("Hosts") if "Hosts" in header_parts else None

    # Find table end (blank line after table)
    table_end = table_start + 2
    for i in range(table_start + 2, len(lines)):
        if lines[i].strip() == "":
            table_end = i
            break
    else:
        table_end = len(lines)

    rows = []
    for line in lines[table_start + 2:table_end]:
        stripped = line.strip()
        if not stripped.startswith("|"):
            continue
        parts = _split_markdown_row(stripped)
        if len(parts) < len(header_parts) or not parts[counter_index].isdigit():
            continue
        row = {
            "counter": parts[counter_index],
            "episode": parts[episode_index],
            "title": parts[title_index],
            "date": parts[date_index],
            "duration": parts[duration_index],
            "hosts": parse_hosts_cell(parts[hosts_index]) if hosts_index is not None else [],
        }
        if hosts_index is not None and parts[hosts_index] == HOST_SENTINEL:
            row["hosts_status"] = "not_listed"
        elif hosts_index is not None and parts[hosts_index] == "Do weryfikacji":
            row["hosts"] = []
            row["hosts_status"] = "manual_review"
        rows.append(row)

    return rows, "\n".join(header_lines)


def _row_match_key(row: dict) -> tuple[str, str, str, str, str]:
    return (
        normalize_identity_text(row.get("episode", "")),
        normalize_identity_text(row.get("title", "")),
        normalize_identity_text(row.get("date", "")),
        normalize_identity_text(row.get("duration", "")),
        normalize_identity_text(row.get("counter", "")),
    )


def attach_existing_data(rows: list[dict], data: dict) -> list[dict]:
    """Restore source URLs and durable fields when reading a legacy archive table."""
    episodes = data.get("episodes", []) if isinstance(data, dict) else []
    by_exact: dict[tuple[str, str, str, str], list[dict]] = defaultdict(list)
    by_title: dict[str, list[dict]] = defaultdict(list)
    for episode in episodes:
        key = (
            normalize_identity_text(episode.get("episode", "")),
            normalize_identity_text(episode.get("title", "")),
            normalize_identity_text(episode.get("date", "")),
            normalize_identity_text(episode.get("duration", "")),
        )
        by_exact[key].append(episode)
        by_title[normalize_lookup_title(episode.get("title", ""))].append(episode)

    for row in rows:
        exact_key = _row_match_key(row)[:4]
        matches = by_exact.get(exact_key, [])
        if len(matches) == 1:
            source = matches[0]
        else:
            title_matches = by_title.get(normalize_lookup_title(row.get("title", "")), [])
            source = title_matches[0] if len(title_matches) == 1 else None
        if source:
            for field in ("url", "record_key", "hosts", "hosts_status", "hosts_source", "hosts_source_url", "hosts_provenance"):
                if source.get(field) not in (None, ""):
                    row[field] = source[field]
        row["record_key"] = build_record_key(row)
    return rows


# ── Archive Writing ──────────────────────────────────────────────────────────

def _split_markdown_row(line: str) -> list[str]:
    """Split one Markdown row while honoring escaped pipes and backslashes."""
    stripped = line.strip()
    if stripped.startswith("|"):
        stripped = stripped[1:]
    if stripped.endswith("|") and not stripped.endswith("\\|"):
        stripped = stripped[:-1]
    fields = []
    current = []
    escaped = False
    for char in stripped:
        if escaped:
            current.append(char if char in {"|", "\\"} else "\\" + char)
            escaped = False
        elif char == "\\":
            escaped = True
        elif char == "|":
            fields.append("".join(current).strip())
            current = []
        else:
            current.append(char)
    if escaped:
        current.append("\\")
    fields.append("".join(current).strip())
    return fields


def _escape_markdown_cell(value: object) -> str:
    """Keep untrusted text inside one Markdown table cell."""
    text = " ".join(str(value).splitlines())
    text = text.replace("\\", "\\\\").replace("|", "\\|")
    return text


def pad_field(text: str, width: int) -> str:
    """Left-pad text to width with spaces (markdown table padding)."""
    return text.ljust(width)


def write_archive(rows: list[dict], dry: bool = False, target_path: Path | None = None) -> None:
    """Write the archive markdown table with column padding."""
    cw = COL_CONTENT_WIDTHS

    header_values = ["#", "Ep.", "Episode title", "Publish date", "Duration", "Hosts"]
    header = "| " + " | ".join(pad_field(value, width) for value, width in zip(header_values, cw)) + " |"
    separator = "| " + " | ".join("-" * width for width in cw) + " |"

    lines = [header, separator]
    for r in rows:
        host_value = hosts_cell(r)
        values = [
            _escape_markdown_cell(r["counter"]),
            _escape_markdown_cell(r["episode"]),
            _escape_markdown_cell(r["title"]),
            _escape_markdown_cell(r["date"]),
            _escape_markdown_cell(r["duration"]),
            _escape_markdown_cell(host_value),
        ]
        line = "| " + " | ".join(pad_field(value, width) for value, width in zip(values, cw)) + " |"
        lines.append(line)

    content = "\n".join(lines) + "\n"

    target = target_path or (ARCHIVE_PATH if not dry else ARCHIVE_PATH.with_name(
        ARCHIVE_PATH.name.replace(".md", ".dry.md")
    ))
    _atomic_write_text(target, content, root=target.parent.parent, mode=0o644)
    log.info(f"Archive written: {target} ({len(rows)} rows)")


# ── data.json Generation ─────────────────────────────────────────────────────

def duration_to_minutes(duration: str) -> float | None:
    """Convert HH:MM:SS or MM:SS to minutes (float)."""
    if not duration or duration == "?":
        return None
    parts = duration.split(":")
    try:
        if len(parts) == 3:
            h, m, s = int(parts[0]), int(parts[1]), int(parts[2])
            return h * 60 + m + s / 60
        elif len(parts) == 2:
            m, s = int(parts[0]), int(parts[1])
            return m + s / 60
        else:
            return int(parts[0])
    except (ValueError, IndexError):
        return None


def detect_category(title: str, episode: str) -> str:
    """Detect episode sub-series category from title and episode number.
    
    Priority order:
    1. SP episode number → sp
    2. (Live) → live
    3. (Po Godzinach) → po_godzinach
    4. ½ fraction → half (only ½, not ⅓ or ⅔)
    5. (Special) → special
    6. (Afterparty) → afterparty
    7. (Prawie) → prawie
    8. (Na Placu Budowy) → na_placu_budowy
    9. (Na Spacerze) → na_spacerze
    10. (Video) → video
    11. (W Biegu) → w_biegu
    12. default → main
    """
    t = title.lower()

    # Check for SP episode numbers
    if episode == "SP" or title.startswith("SP") or title.startswith("#SP"):
        return "sp"

    # Check for Live sub-series
    if "(live)" in t or "(live" in t:
        return "live"

    # Check for Po Godzinach sub-series
    if "(po godzinach)" in t or "po godzinach" in t:
        return "po_godzinach"

    # Check for ½ fraction (only ½, not ⅓ or ⅔)
    if "½" in title:
        return "half"

    # Check remaining sub-series tags
    if "(special)" in t:
        return "special"
    elif "(afterparty)" in t:
        return "afterparty"
    elif "(prawie)" in t:
        return "prawie"
    elif "(na placu budowy)" in t:
        return "na_placu_budowy"
    elif "(na spacerze)" in t:
        return "na_spacerze"
    elif "(video)" in t or "video" in t:
        return "video"
    elif "(w biegu)" in t:
        return "w_biegu"
    return "main"


def minutes_to_duration_str(minutes: float) -> str:
    """Convert minutes float to H:MM:SS or MM:SS string."""
    total_seconds = int(round(minutes * 60))
    h = total_seconds // 3600
    m = (total_seconds % 3600) // 60
    s = total_seconds % 60
    if h:
        return f"{h}:{m:02d}:{s:02d}"
    else:
        return f"{m}:{s:02d}"


def normalize_lookup_title(title: str) -> str:
    """Normalize title whitespace for matching RSS and archive records."""
    import unicodedata
    return " ".join(unicodedata.normalize("NFKC", title).replace("\u00a0", " ").split()).strip()


def resolve_existing_source_url(item: dict, rows: list[dict]) -> str:
    """Prefer an existing canonical URL when RSS misassigns a historical link."""
    title = normalize_lookup_title(item.get("title", ""))
    episode = normalize_identity_text(item.get("episode_number", ""))
    date_value = normalize_identity_text(item.get("date", ""))
    duration = normalize_identity_text(item.get("duration", ""))
    matches = [
        row for row in rows
        if normalize_identity_text(row.get("episode", "")) == episode
        and normalize_lookup_title(row.get("title", "")) == title
        and normalize_identity_text(row.get("date", "")) == date_value
        and (not duration or normalize_identity_text(row.get("duration", "")) == duration)
        and row.get("url")
    ]
    if len(matches) == 1:
        return str(matches[0]["url"])
    return str(item.get("url") or "")


def generate_data_json(rows: list[dict], url_by_title: dict[str, str | list[str]] | None = None) -> dict:
    """Generate the data.json structure for Chart.js."""
    episodes = []
    durations_min = []
    year_counts = Counter()
    url_by_title = url_by_title or {}

    for r in rows:
        minutes = duration_to_minutes(r["duration"])
        year = ""
        if r["date"] and len(r["date"]) >= 4:
            year = r["date"][:4]
            try:
                year_counts[int(year)] += 1
            except ValueError:
                pass

        if minutes is not None:
            durations_min.append(minutes)

        category = detect_category(r["title"], r["episode"])
        dur_str = r["duration"] if r["duration"] else "?"
        raw_source_url = r.get("url", "")
        if not raw_source_url:
            fallback = url_by_title.get(normalize_lookup_title(r["title"]))
            if isinstance(fallback, list):
                raw_source_url = fallback[0] if len(set(fallback)) == 1 else ""
            elif isinstance(fallback, str):
                raw_source_url = fallback

        if raw_source_url:
            source_url = _canonical_episode_source_url(str(raw_source_url))
        else:
            source_url = ""

        hosts = sorted(
            [normalize_host_display_name(host) for host in (r.get("hosts") or [])],
            key=_host_dedupe_key,
        )
        hosts_status = r.get("hosts_status") or "not_listed"
        hosts_source = r.get("hosts_source") or ("rrn" if "retrorocketnetwork.pl" in source_url else "manual")
        raw_hosts_source_url = r.get("hosts_source_url") or source_url
        hosts_source_url = _canonical_episode_source_url(str(raw_hosts_source_url)) if raw_hosts_source_url else ""
        public_provenance = _public_provenance(r.get("hosts_provenance"), hosts_source_url)
        record_key = build_record_key(r)

        episodes.append({
            "record_key": record_key,
            "episode": r["episode"],
            "title": r["title"],
            "date": r["date"],
            "duration": dur_str,
            "minutes": round(minutes, 2) if minutes is not None else None,
            "category": category,
            "year": int(year) if year else None,
            "url": source_url,
            "hosts": hosts,
            "hosts_status": hosts_status,
            "hosts_source": hosts_source,
            "hosts_source_url": hosts_source_url,
            "hosts_provenance": public_provenance,
        })

    # Stats
    total_episodes = len(episodes)
    total_seconds = sum(m * 60 for m in durations_min) if durations_min else 0
    total_hours = round(total_seconds / 3600, 1) if total_seconds else 0
    avg_duration = round(sum(durations_min) / len(durations_min), 1) if durations_min else 0
    max_duration = round(max(durations_min), 2) if durations_min else 0
    min_duration = round(min(durations_min), 2) if durations_min else 0

    categories = {
        "main": {"label": "Główne", "color": "#F43E25"},
        "live": {"label": "Live", "color": "#cae153"},
        "po_godzinach": {"label": "Po Godzinach", "color": "#47CFEB"},
        "sp": {"label": "SP (specjalne)", "color": "#632F53"},
        "half": {"label": "Półodcinki", "color": "#999"},
        "special": {"label": "Specjalne", "color": "#9b51e0"},
        "afterparty": {"label": "Afterparty", "color": "#ff6900"},
        "prawie": {"label": "Prawie", "color": "#ff6900"},
        "na_placu_budowy": {"label": "Na Placu Budowy", "color": "#0693e3"},
        "na_spacerze": {"label": "Na Spacerze", "color": "#00d084"},
        "video": {"label": "Video", "color": "#8ed1fc"},
        "w_biegu": {"label": "W Biegu", "color": "#7bdcb5"},
    }

    data = {
        "episodes": episodes,
        "stats": {
            "total_episodes": total_episodes,
            "total_listening_hours": total_hours,
            "average_duration": avg_duration,
            "min_duration": min_duration,
            "max_duration": max_duration,
        },
        "categories": categories,
        "average_duration": avg_duration,
    }
    return data


_GENERATED_TOP_LEVEL_FIELDS = {"episodes", "stats", "categories", "average_duration"}
_GENERATED_STATS_FIELDS = {
    "total_episodes",
    "total_listening_hours",
    "average_duration",
    "min_duration",
    "max_duration",
}
_GENERATED_EPISODE_FIELDS = {
    "record_key",
    "episode",
    "title",
    "date",
    "duration",
    "minutes",
    "category",
    "year",
    "url",
    "hosts",
    "hosts_status",
    "hosts_source",
    "hosts_source_url",
    "hosts_provenance",
}
_GENERATED_URL_FIELDS = {"url", "hosts_source_url", "source_url"}


def _normalize_generated_payload(value, field_name: str = ""):
    if isinstance(value, dict):
        return {
            key: _normalize_generated_payload(item, key)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_normalize_generated_payload(item, field_name) for item in value]
    if field_name in _GENERATED_URL_FIELDS and isinstance(value, str) and value:
        return _canonical_public_url(value)
    return value


def validate_generated_data(data: dict, rows: list[dict]) -> None:
    """Reject incomplete or inconsistent generated output before publishing."""
    if not isinstance(data, dict):
        raise ValueError("Generated data must be an object")
    unknown_top_level = set(data) - _GENERATED_TOP_LEVEL_FIELDS
    if unknown_top_level:
        raise ValueError(f"Generated data has unknown fields: {sorted(unknown_top_level)}")
    episodes = data.get("episodes")
    if not isinstance(episodes, list) or len(episodes) != len(rows):
        raise ValueError("Generated episode count does not match the archive")
    for episode in episodes:
        if not isinstance(episode, dict):
            raise ValueError("Generated episode must be an object")
        unknown_episode_fields = set(episode) - _GENERATED_EPISODE_FIELDS
        if unknown_episode_fields:
            raise ValueError(f"Generated episode has unknown fields: {sorted(unknown_episode_fields)}")
    stats = data.get("stats")
    if not isinstance(stats, dict):
        raise ValueError("Generated statistics must be an object")
    unknown_stats_fields = set(stats) - _GENERATED_STATS_FIELDS
    if unknown_stats_fields:
        raise ValueError(f"Generated statistics has unknown fields: {sorted(unknown_stats_fields)}")
    categories = data.get("categories")
    if not isinstance(categories, dict):
        raise ValueError("Generated categories must be an object")
    for category, metadata in categories.items():
        if not isinstance(metadata, dict) or set(metadata) != {"label", "color"}:
            raise ValueError(f"Generated category {category!r} has an invalid schema")

    expected = generate_data_json(rows)
    if _normalize_generated_payload(data) != _normalize_generated_payload(expected):
        raise ValueError("Generated data fields do not match archive-derived expectations")

    identifiers = [str(episode.get("episode", "")) for episode in episodes]
    if any(not identifier for identifier in identifiers):
        raise ValueError("Generated data contains an empty episode identifier")

    expected_ids = [str(row.get("episode", "")) for row in rows]
    if identifiers != expected_ids:
        raise ValueError("Generated data order or identifiers do not match the archive")

    record_keys = [str(episode.get("record_key", "")) for episode in episodes]
    if any(not record_key for record_key in record_keys) or len(set(record_keys)) != len(record_keys):
        raise ValueError("Generated data contains missing or duplicate record keys")

    for episode, row in zip(episodes, rows):
        url = episode.get("url", "")
        try:
            episode_source_url = _canonical_public_url(str(url))
            row_source_url = _canonical_public_url(row.get("url") or "")
        except ValueError as exc:
            raise ValueError(f"Episode {episode.get('episode')} has no canonical source URL: {exc}") from exc
        if episode_source_url != row_source_url:
            raise ValueError(f"Episode {episode.get('episode')} source URL does not match the archive row")
        if not episode.get("title") or not episode.get("date"):
            raise ValueError(f"Episode {episode.get('episode')} is missing title or date")
        if episode.get("record_key") != build_record_key(row):
            raise ValueError(f"Episode {episode.get('episode')} has a record-key mismatch")
        hosts = episode.get("hosts")
        if not isinstance(hosts, list):
            raise ValueError(f"Episode {episode.get('episode')} has no hosts list")
        try:
            expected_host_source_url = _canonical_public_url(row.get("hosts_source_url") or row.get("url") or "")
            serialized_host_source_url = _canonical_public_url(str(episode.get("hosts_source_url") or ""))
        except ValueError as exc:
            raise ValueError(f"Episode {episode.get('episode')} has an invalid host source URL: {exc}") from exc
        if serialized_host_source_url != expected_host_source_url:
            raise ValueError(f"Episode {episode.get('episode')} host source URL does not match the archive row")
        public_provenance = _public_provenance(row.get("hosts_provenance"), expected_host_source_url)
        if episode.get("hosts_provenance") != public_provenance:
            raise ValueError(f"Episode {episode.get('episode')} provenance is not sanitized")
        validate_host_entry({
            "hosts": hosts,
            "hosts_status": episode.get("hosts_status"),
            "hosts_source": episode.get("hosts_source"),
            "hosts_source_url": episode.get("hosts_source_url"),
            "provenance": public_provenance,
        }, episode.get("record_key", ""), expected_source_url="" if row.get("hosts_source") == "paired_rrn" else expected_host_source_url)

        if episode.get("hosts_status") in HOST_UNRESOLVED_STATUSES:
            raise ValueError(f"Episode {episode.get('episode')} has unresolved host metadata")

    if rows and all(row.get("hosts_status") for row in rows):
        manifest_from_rows(rows, strict=True)

    stats = data.get("stats", {})
    if stats.get("total_episodes") != len(episodes):
        raise ValueError("Generated statistics exclude one or more episodes")
    category_count = sum(1 for episode in episodes if episode.get("category") == "afterparty")
    expected_afterparty = sum(1 for row in rows if detect_category(row["title"], row["episode"]) == "afterparty")
    if category_count != expected_afterparty:
        raise ValueError("Afterparty category count does not match the archive")


def write_data_json(data: dict, dry: bool = False, target_path: Path | None = None) -> None:
    target = target_path or (DATA_JSON_PATH if not dry else DATA_JSON_PATH.with_name(
        DATA_JSON_PATH.name.replace(".json", ".dry.json")
    ))
    _atomic_write_text(
        target,
        json.dumps(data, ensure_ascii=False, indent=2),
        root=target.parent.parent,
        mode=0o644,
    )
    log.info(f"data.json written: {target} ({len(data['episodes'])} episodes)")


# ── README Update ──────────────────────────────────────────────────────────────

def update_readme(data: dict, dry: bool = False, target_path: Path | None = None) -> None:
    """Update the README.md stats table with current numbers."""
    readme_content = _read_bytes_secure(README_PATH, REPO_DIR.parent)
    if readme_content is None:
        log.warning(f"README.md not found at {README_PATH}")
        return

    content = readme_content.decode("utf-8")
    stats = data["stats"]
    total = stats["total_episodes"]
    total_hours = stats["total_listening_hours"]
    avg_duration = stats["average_duration"]
    max_duration = stats["max_duration"]
    afterparty_count = sum(1 for episode in data.get("episodes", []) if episode.get("category") == "afterparty")

    # Update the stats table in README.md
    # Pattern: | Liczba odcinków | <number> |
    content = re.sub(
        r"\| Liczba odcinków \| \d+ \|",
        f"| Liczba odcinków | {total} |",
        content,
    )
    content = re.sub(
        r"\| Godziny odsłuchu \| [\d.]+ \|",
        f"| Godziny odsłuchu | {total_hours} |",
        content,
    )
    content = re.sub(
        r"\| Średnia długość \| [\d.]+ min \|",
        f"| Średnia długość | {avg_duration} min |",
        content,
    )
    content = re.sub(
        r"\| Maksymalna długość \| [\d.]+ min \|",
        f"| Maksymalna długość | {max_duration} min |",
        content,
    )
    content = re.sub(
        r"\| Afterparty \| \d+ \|",
        f"| Afterparty | {afterparty_count} |",
        content,
    )

    target = target_path or (README_PATH if not dry else README_PATH.with_name(
        README_PATH.name.replace(".md", ".dry.md")
    ))
    _atomic_write_text(target, content, root=target.parent.parent, mode=0o644)
    log.info(f"README.md updated: {target} ({total} episodes)")


# ── Obsidian Vault Sync ────────────────────────────────────────────────────────

def sync_to_obsidian(dry: bool = False) -> None:
    """Sync archive and statistics files to the Obsidian vault directory."""
    if not VAULT_DIR.exists():
        if dry:
            log.info(f"[DRY RUN] Vault directory not found: {VAULT_DIR}")
            return
        raise RuntimeError(f"Vault directory not found: {VAULT_DIR}")
    if VAULT_DIR.is_symlink() or not VAULT_DIR.is_dir():
        raise RuntimeError(f"Vault directory is not a safe directory: {VAULT_DIR}")

    def sync_file(source: Path, destination: Path, label: str) -> None:
        content = _read_bytes_secure(source, source.parent.parent)
        if content is None:
            raise RuntimeError(f"Obsidian {label} source is missing")
        _reject_symlink_components(destination, VAULT_DIR)
        _atomic_write_bytes(destination, content, root=VAULT_DIR, mode=0o644)
        verified = _read_bytes_secure(destination, VAULT_DIR)
        if verified != content:
            raise RuntimeError(f"Obsidian {label} verification failed")

    synced = 0

    # Sync archive
    src_archive = ARCHIVE_PATH
    dst_archive = VAULT_DIR / "Nadgryzieni Episode Archive.md"
    if not src_archive.exists():
        raise RuntimeError(f"Archive source not found: {src_archive}")
    if not dry:
        sync_file(src_archive, dst_archive, "archive")
    log.info(f"{'[DRY RUN] Would sync' if dry else 'Synced'} archive to vault: {dst_archive}")
    synced += 1

    # Sync statistics
    src_stats = STATS_PATH
    dst_stats = VAULT_DIR / "Nadgryzieni Statistics.md"
    if not src_stats.exists():
        raise RuntimeError(f"Statistics source not found: {src_stats}")
    if not dry:
        sync_file(src_stats, dst_stats, "statistics")
    log.info(f"{'[DRY RUN] Would sync' if dry else 'Synced'} statistics to vault: {dst_stats}")
    synced += 1

    log.info(f"Obsidian vault sync complete: {synced} file(s) synced")


# ── Statistics Generation ────────────────────────────────────────────────────

def generate_statistics(rows: list[dict]) -> str:
    """Generate comprehensive statistics markdown from archive rows."""
    total = len(rows)
    durations = []
    for r in rows:
        m = duration_to_minutes(r["duration"])
        if m is not None:
            durations.append(m)

    with_duration = len(durations)
    unknown_count = total - with_duration

    if durations:
        avg_min = sum(durations) / len(durations)
        sorted_d = sorted(durations)
        n = len(sorted_d)
        median_min = (sorted_d[n // 2 - 1] + sorted_d[n // 2]) / 2 if n % 2 == 0 else sorted_d[n // 2]
        total_secs = sum(m * 60 for m in durations)
        total_h = int(total_secs // 3600)
        total_m = int((total_secs % 3600) // 60)
        max_min = max(durations)
        min_min = min(durations)
    else:
        avg_min = 0
        median_min = 0
        total_h = 0
        total_m = 0
        max_min = 0
        min_min = 0

    # Episodes per year
    year_counts = Counter()
    year_durations = defaultdict(list)
    for r in rows:
        if r["date"] and len(r["date"]) >= 4:
            year = r["date"][:4]
            try:
                year_int = int(year)
                year_counts[year_int] += 1
                m = duration_to_minutes(r["duration"])
                if m is not None:
                    year_durations[year_int].append(m)
            except ValueError:
                pass

    # Date range
    dates = []
    for r in rows:
        if r["date"] and len(r["date"]) >= 10:
            try:
                dt = datetime.strptime(r["date"][:10], "%Y-%m-%d")
                dates.append(dt)
            except ValueError:
                pass

    today = date.today().isoformat()
    date_range = f"{min(dates).strftime('%Y-%m-%d')} — {max(dates).strftime('%Y-%m-%d')}" if dates else "unknown"

    # Month distribution
    month_counts = Counter()
    for r in rows:
        if r["date"] and len(r["date"]) >= 7:
            try:
                dt = datetime.strptime(r["date"][:7], "%Y-%m")
                month_counts[dt.month] += 1
            except ValueError:
                pass

    month_names = {
        1: "January", 2: "February", 3: "March", 4: "April",
        5: "May", 6: "June", 7: "July", 8: "August",
        9: "September", 10: "October", 11: "November", 12: "December",
    }

    # Day of week distribution
    dow_counts = Counter()
    for r in rows:
        if r["date"] and len(r["date"]) >= 10:
            try:
                dt = datetime.strptime(r["date"][:10], "%Y-%m-%d")
                dow_counts[dt.strftime("%A")] += 1
            except ValueError:
                pass

    # Duration distribution brackets
    brackets = [
        ("Under 30 min", sum(1 for m in durations if m < 30)),
        ("30 min – 1 hour", sum(1 for m in durations if 30 <= m < 60)),
        ("1 hour – 2 hours", sum(1 for m in durations if 60 <= m < 120)),
        ("2 hours – 3 hours", sum(1 for m in durations if 120 <= m < 180)),
        ("3 hours – 4 hours", sum(1 for m in durations if 180 <= m < 240)),
        ("Over 4 hours", sum(1 for m in durations if m >= 240)),
    ]

    # Category counts
    cat_counts = Counter()
    for r in rows:
        cat_counts[detect_category(r["title"], r["episode"])] += 1

    # Most common durations
    dur_counter = Counter()
    for r in rows:
        if r["duration"] and r["duration"] != "?":
            dur_counter[r["duration"]] += 1

    # Title word frequency
    word_counts = Counter()
    for r in rows:
        words = re.findall(r'\b[a-zA-ZążźćńółęśćĄŻŹĆŃÓŁĘŚĆ]+\b', r["title"].lower())
        for w in words:
            if len(w) > 2:
                word_counts[w] += 1

    # Build markdown
    md = f"""# Nadgryzieni — Statistics

> Generated from the episode archive: `Nadgryzieni Episode Archive.md`
> Last updated: {today}

---

## Overview

| Metric | Value |
|---|---|
| **Total episodes** | {total} |
| **Date range** | {date_range} |
| **Episodes with duration** | {with_duration} |
| **Episodes without duration** | {unknown_count} |

---

## Duration Statistics

| Metric | Value |
|---|---|
| **Total listening time** | {total_h}h {total_m}m ({total_h + total_m/60:.1f} hours) |
| **Average duration** | {avg_min:.1f} min ({avg_min/60:.1f}h) |
| **Median duration** | {median_min:.1f} min ({median_min/60:.1f}h) |
| **Longest episode** | {max_min:.1f} min |
| **Shortest episode** | {min_min:.1f} min |
| **Episodes over 2 hours** | {sum(1 for m in durations if m > 120)} |
| **Episodes under 30 minutes** | {sum(1 for m in durations if m < 30)} |

---

## Duration by Year (Average)

| Year | Episodes | Avg Duration |
|---|---|---|
"""
    for year in sorted(year_counts.keys()):
        year_durs = year_durations.get(year, [])
        avg = sum(year_durs) / len(year_durs) if year_durs else 0
        md += f"| {year} | {year_counts[year]} | {avg:.1f} min |\n"

    md += """
---

## Duration Distribution

| Bracket | Count | % |
|---|---|---|
"""
    for bracket, count in brackets:
        pct = count / total * 100 if total else 0
        md += f"| {bracket} | {count} | {pct:.1f}% |\n"

    md += """
---

## Episodes per Year

| Year | Count |
|---|---|
"""
    for year in sorted(year_counts.keys(), reverse=True):
        md += f"| {year} | {year_counts[year]} |\n"

    md += """
---

## Monthly Distribution

| Month | Count | % |
|---|---|---|
"""
    for month in range(1, 13):
        count = month_counts.get(month, 0)
        pct = count / total * 100 if total else 0
        md += f"| {month_names[month]} | {count} | {pct:.1f}% |\n"

    md += """
---

## Publish Day Distribution

| Day | Count | % |
|---|---|---|
"""
    day_order = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
    for day in day_order:
        count = dow_counts.get(day, 0)
        pct = count / total * 100 if total else 0
        md += f"| {day} | {count} | {pct:.1f}% |\n"

    md += """
---

## Sub-Series & Categories

| Category | Count |
|---|---|
"""
    cat_labels = {
        "main": "Main episodes",
        "live": "(Live)",
        "po_godzinach": "(Po Godzinach)",
        "sp": "SP (special)",
        "half": "Half/decimal episodes",
        "special": "(Special)",
        "afterparty": "(Afterparty)",
        "prawie": "(Prawie)",
        "na_placu_budowy": "(Na Placu Budowy)",
        "na_spacerze": "(Na Spacerze)",
        "video": "(Video)",
        "w_biegu": "(W Biegu)",
    }
    for cat in sorted(cat_counts.keys(), key=lambda c: cat_counts[c], reverse=True):
        label = cat_labels.get(cat, cat)
        md += f"| {label} | {cat_counts[cat]} |\n"

    md += f"""
---

## Top 10 Most Common Durations

| Duration | Episodes |
|---|---|
"""
    for dur, count in dur_counter.most_common(10):
        md += f"| {dur} | {count} |\n"

    md += f"""
---

## Top 20 Most Frequent Words in Titles

| Word | Count |
|---|---|
"""
    for word, count in word_counts.most_common(20):
        md += f"| {word} | {count} |\n"

    md += f"""
---

## Summary

- **Total episodes:** {total}
- **Total listening time:** {total_h}h {total_m}m
- **Average duration:** {avg_min:.1f} min
- **Median duration:** {median_min:.1f} min
- **Date range:** {date_range}

---

*Last updated: {today}*
"""
    return md


def write_stats(content: str, dry: bool = False, target_path: Path | None = None) -> None:
    target = target_path or (STATS_PATH if not dry else STATS_PATH.with_name(
        STATS_PATH.name.replace(".md", ".dry.md")
    ))
    _atomic_write_text(target, content, root=target.parent.parent, mode=0o644)
    log.info(f"Statistics written: {target}")


# ── Conditional Retry State ──────────────────────────────────────────────────

def write_retry_state(
    path: Path,
    primary_date: date,
    pending: bool,
    pending_commit: str | None = None,
) -> None:
    """Atomically persist whether the next Sunday or Tuesday retry should run."""
    if pending:
        if not isinstance(pending_commit, str) or not re.fullmatch(r"[0-9a-f]{40}", pending_commit):
            raise ValueError("A pending retry requires the exact local commit SHA")
    elif pending_commit is not None:
        raise ValueError("A non-pending retry cannot carry a commit SHA")
    _ensure_secure_directory(path.parent, path.parent.parent)
    payload = {
        "primary_date": primary_date.isoformat(),
        "pending": bool(pending),
        "pending_commit": pending_commit,
        "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
    }
    _atomic_write_text(
        path,
        json.dumps(payload, indent=2) + "\n",
        root=path.parent.parent,
    )


def _parse_strict_utc_timestamp(value: object) -> datetime:
    if not isinstance(value, str) or not value:
        raise ValueError("UTC timestamp is missing")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
        raise ValueError("UTC timestamp must have a zero UTC offset")
    return parsed.astimezone(timezone.utc)


def _read_retry_state(path: Path) -> dict | None:
    try:
        content = _read_bytes_secure(path, path.parent.parent)
        if content is None:
            return None
        payload = json.loads(content.decode("utf-8"))
        if not isinstance(payload, dict):
            return None
        if set(payload) != {"primary_date", "pending", "pending_commit", "updated_at"}:
            return None
        if not isinstance(payload["pending"], bool):
            return None
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", payload["primary_date"]):
            return None
        date.fromisoformat(payload["primary_date"])
        _parse_strict_utc_timestamp(payload["updated_at"])
        pending_commit = payload["pending_commit"]
        if payload["pending"]:
            if not isinstance(pending_commit, str) or not re.fullmatch(r"[0-9a-f]{40}", pending_commit):
                return None
        elif pending_commit is not None:
            return None
        return payload
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError):
        return None


def retry_is_due(path: Path, today: date | None = None) -> bool:
    """Return true only for Sunday or Tuesday after a pending Friday run."""
    payload = _read_retry_state(path)
    if payload is None:
        return False
    primary_date = date.fromisoformat(payload["primary_date"])
    today = today or datetime.now(timezone.utc).date()
    days_after_primary = (today - primary_date).days
    return (
        bool(payload.get("pending"))
        and primary_date.weekday() == 4
        and today.weekday() in {1, 6}
        and days_after_primary in {2, 4}
    )


def _pipeline_lock_owned_by_current_thread() -> bool:
    return _LOCK_HANDLE is not None and _LOCK_OWNER == threading.get_ident()


def acquire_pipeline_lock() -> bool:
    """Acquire a reentrant process lock owned by the calling thread."""
    global _LOCK_HANDLE, _LOCK_OWNER, _LOCK_DEPTH
    current_owner = threading.get_ident()
    with _LOCK_STATE_GUARD:
        if _LOCK_HANDLE is not None:
            if _LOCK_OWNER == current_owner:
                _LOCK_DEPTH += 1
                return True
            return False
        import fcntl

        _ensure_secure_directory(STATE_DIR, STATE_DIR.parent)
        state_fd = _open_secure_directory_fd(STATE_DIR, STATE_DIR.parent)
        try:
            lock_name = Path(LOCK_PATH).name
            if lock_name in {"", ".", ".."} or Path(lock_name).name != lock_name:
                raise RuntimeError("Pipeline lock name is unsafe")
            descriptor = None
            try:
                descriptor = os.open(
                    lock_name,
                    os.O_RDWR
                    | os.O_CREAT
                    | getattr(os, "O_NOFOLLOW", 0)
                    | getattr(os, "O_NONBLOCK", 0),
                    0o600,
                    dir_fd=state_fd,
                )
                if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                    raise RuntimeError("Pipeline lock is not a regular file")
                handle = os.fdopen(descriptor, "a+", encoding="utf-8")
                descriptor = None
            finally:
                if descriptor is not None:
                    os.close(descriptor)
        finally:
            os.close(state_fd)
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            handle.close()
            log.info("Another Nadgryzieni pipeline run is active; skipping this invocation.")
            return False
        except Exception:
            handle.close()
            raise
        _LOCK_HANDLE = handle
        _LOCK_OWNER = current_owner
        _LOCK_DEPTH = 1
        return True


def release_pipeline_lock() -> None:
    """Release one lock level; only the owning thread may release it."""
    global _LOCK_HANDLE, _LOCK_OWNER, _LOCK_DEPTH
    current_owner = threading.get_ident()
    with _LOCK_STATE_GUARD:
        if _LOCK_HANDLE is None:
            return
        if _LOCK_OWNER != current_owner:
            raise RuntimeError("Pipeline lock can only be released by its owner thread")
        _LOCK_DEPTH -= 1
        if _LOCK_DEPTH > 0:
            return
        handle = _LOCK_HANDLE
        _LOCK_HANDLE = None
        _LOCK_OWNER = None
        _LOCK_DEPTH = 0
        handle.close()


# ── Cache-Busting ────────────────────────────────────────────────────────────

def get_cache_version() -> int:
    """Get or initialize the cache-busting version number."""
    try:
        content = _read_bytes_secure(CACHE_VERSION_FILE, CACHE_VERSION_FILE.parent.parent)
        if content is not None:
            return int(content.decode("utf-8").strip())
    except (OSError, UnicodeDecodeError, ValueError, RuntimeError):
        pass
    versions = []
    for path, pattern in (
        (INDEX_HTML_PATH, r"\?v=(\d+)"),
        (SCRIPT_JS_PATH, r"DATA_VERSION\s*=\s*(\d+)")
    ):
        try:
            content = _read_bytes_secure(path, path.parent.parent)
            if content is not None:
                versions.extend(int(value) for value in re.findall(pattern, content.decode("utf-8")))
        except (OSError, UnicodeDecodeError, ValueError, RuntimeError):
            continue
    return max(versions, default=100)


def bump_cache_version(dry: bool = False) -> int:
    """Increment and save the cache-busting version number."""
    v = get_cache_version() + 1
    if not dry:
        _atomic_write_text(
            CACHE_VERSION_FILE,
            str(v) + "\n",
            root=CACHE_VERSION_FILE.parent.parent,
            mode=0o644,
        )
    return v


def update_cache_busting(
    version: int,
    dry: bool = False,
    index_path: Path | None = None,
    script_path: Path | None = None,
) -> None:
    """Update ?v=N references in index.html and script.js."""
    if dry:
        log.info(f"[DRY RUN] Would bump cache-busting to v={version}")
        return

    index_path = index_path or INDEX_HTML_PATH
    script_path = script_path or SCRIPT_JS_PATH
    index_content = _read_bytes_secure(index_path, index_path.parent.parent)
    if index_content is not None:
        html = index_content.decode("utf-8")
        html = re.sub(r'style\.css\?v=\d+', f'style.css?v={version}', html)
        html = re.sub(r'script\.js\?v=\d+', f'script.js?v={version}', html)
        _atomic_write_text(index_path, html, root=index_path.parent.parent, mode=0o644)
        log.info(f"index.html cache-busting updated to v={version}")

    script_content = _read_bytes_secure(script_path, script_path.parent.parent)
    if script_content is not None:
        js = script_content.decode("utf-8")
        js = re.sub(r"const DATA_VERSION = \d+;", f"const DATA_VERSION = {version};", js)
        _atomic_write_text(script_path, js, root=script_path.parent.parent, mode=0o644)
        log.info(f"script.js DATA_VERSION cache-busting updated to v={version}")


# ── Git Operations ───────────────────────────────────────────────────────────

def _redact_git_output(value: str) -> str:
    """Remove URLs and credential-like values before Git output is logged."""
    return _sanitize_public_text(value)


def _normalise_git_paths(paths: list[str]) -> list[str]:
    normalized = []
    for raw_path in paths:
        path = Path(str(raw_path))
        path_text = path.as_posix()
        if (
            path.is_absolute()
            or ".." in path.parts
            or not path.parts
            or path_text in {"", "."}
            or any(marker in path_text for marker in "*?[]")
            or ":" in path_text
            or "\\" in path_text
        ):
            raise ValueError(f"Unsafe Git path: {raw_path!r}")
        normalized.append(path_text)
    if len(set(normalized)) != len(normalized):
        raise ValueError("Git paths must be unique")
    return normalized


def _git_staged_paths(repo: str) -> set[str]:
    result = subprocess.run(
        ["git", "diff", "--cached", "--name-only", "-z", "--"],
        cwd=repo,
        capture_output=True,
    )
    if result.returncode != 0:
        stderr = result.stderr.decode("utf-8", "replace").strip()
        raise RuntimeError(f"Could not inspect staged Git paths: {_redact_git_output(stderr)}")
    return {
        path for path in result.stdout.decode("utf-8", "replace").split("\0") if path
    }


def _reject_unrelated_staged_paths(repo: str, allowed: set[str]) -> None:
    unrelated = sorted(_git_staged_paths(repo) - allowed)
    if unrelated:
        raise RuntimeError(
            "Refusing automated commit with unrelated pre-staged paths "
            f"(count={len(unrelated)})"
        )


def _local_commits_ahead_of_origin(repo: str) -> bool | None:
    try:
        result = subprocess.run(
            ["git", "rev-list", "--left-right", "--count", "origin/main...HEAD"],
            cwd=repo,
            capture_output=True,
            text=True,
        )
    except OSError:
        return None
    if result.returncode != 0:
        return None
    fields = result.stdout.strip().split()
    if len(fields) != 2:
        return None
    try:
        return int(fields[1]) > 0
    except ValueError:
        return None


def _ensure_ahead_commits_are_allowed(repo: str, allowed: set[str]) -> None:
    result = subprocess.run(
        ["git", "diff", "--name-only", "-z", "origin/main...HEAD", "--"],
        cwd=repo,
        capture_output=True,
    )
    if result.returncode != 0:
        stderr = result.stderr.decode("utf-8", "replace").strip()
        raise RuntimeError(f"Could not inspect local commits ahead of origin: {_redact_git_output(stderr)}")
    changed_paths = {
        path for path in result.stdout.decode("utf-8", "replace").split("\0") if path
    }
    unrelated = sorted(changed_paths - allowed)
    if unrelated:
        raise RuntimeError(
            "Refusing to push local commits with unrelated paths "
            f"(count={len(unrelated)})"
        )


def _git_head_sha(repo: str) -> str | None:
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return None
    value = result.stdout.strip()
    return value if re.fullmatch(r"[0-9a-f]{40}", value) else None


def _ensure_main_branch(repo: str) -> None:
    result = subprocess.run(
        ["git", "branch", "--show-current"],
        cwd=repo,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0 or result.stdout.strip() != "main":
        raise RuntimeError("Automated publication requires the local main branch")


def _push_with_retries(repo: str) -> bool:
    _ensure_main_branch(repo)
    push_cmd = ["git", "push", "origin", "HEAD:main"]
    for attempt in range(1, 4):
        pushed = subprocess.run(push_cmd, cwd=repo, capture_output=True, text=True)
        if pushed.returncode == 0:
            log.info("Git push complete")
            return True
        log.warning("Git push attempt %d/3 failed: %s", attempt, _redact_git_output(pushed.stderr.strip()))
        if attempt < 3:
            time.sleep(2 ** attempt)
    raise GitPushPendingError("Git push failed after 3 attempts; local commit remains pending")


def _git_commit_and_push_paths_locked(message: str, paths: list[str], dry: bool = False) -> bool:
    """Stage only the supplied generated files, commit, and push safely."""
    if dry:
        log.info(f"[DRY RUN] Would commit: {message}")
        return False
    if not paths:
        raise ValueError("At least one Git path is required")

    normalized_paths = _normalise_git_paths(paths)
    allowed_paths = set(normalized_paths)
    repo = str(REPO_DIR)
    _reject_unrelated_staged_paths(repo, allowed_paths)
    pushed_existing_commit = False
    ahead_of_origin = _local_commits_ahead_of_origin(repo)
    if ahead_of_origin is None:
        raise RuntimeError("Could not determine whether local commits are ahead of origin")
    if ahead_of_origin:
        retry_state = _read_retry_state(RETRY_STATE_PATH)
        current_commit = _git_head_sha(repo)
        if (
            retry_state is None
            or retry_state.get("pending") is not True
            or retry_state.get("pending_commit") != current_commit
        ):
            raise RuntimeError("Refusing to push an unbound local commit")
        _ensure_main_branch(repo)
        _ensure_ahead_commits_are_allowed(repo, allowed_paths)
        _push_with_retries(repo)
        pushed_existing_commit = True
    status_cmd = ["git", "status", "--porcelain", "--untracked-files=all", "--", *normalized_paths]
    status = subprocess.run(status_cmd, cwd=repo, capture_output=True, text=True)
    if status.returncode != 0:
        raise RuntimeError(f"Git status failed: {_redact_git_output(status.stderr.strip())}")
    if not status.stdout.strip():
        log.info("No generated file changes to commit.")
        return pushed_existing_commit

    add_cmd = ["git", "add", "--", *normalized_paths]
    added = subprocess.run(add_cmd, cwd=repo, capture_output=True, text=True)
    if added.returncode != 0:
        raise RuntimeError(f"Git staging failed: {_redact_git_output(added.stderr.strip())}")
    _reject_unrelated_staged_paths(repo, allowed_paths)

    staged = subprocess.run(["git", "diff", "--cached", "--quiet"], cwd=repo)
    if staged.returncode == 0:
        log.info("Generated files produced no staged diff.")
        return False
    if staged.returncode != 1:
        raise RuntimeError("Could not inspect the staged Git diff")

    commit = subprocess.run(
        ["git", "commit", "-m", message, "--", *normalized_paths],
        cwd=repo,
        capture_output=True,
        text=True,
    )
    if commit.returncode != 0:
        raise RuntimeError(f"Git commit failed: {_redact_git_output(commit.stderr.strip())}")

    _push_with_retries(repo)
    log.info("Git commit and push complete")
    return True


def git_commit_and_push_paths(message: str, paths: list[str], dry: bool = False) -> bool:
    """Recover safely, then stage only supplied generated files and publish them."""
    if dry:
        return _git_commit_and_push_paths_locked(message, paths, dry=True)
    lock_owned_here = not _pipeline_lock_owned_by_current_thread()
    if lock_owned_here and not acquire_pipeline_lock():
        raise RuntimeError("Could not acquire publication lock")
    try:
        _recover_publication_journal()
        return _git_commit_and_push_paths_locked(message, paths, dry=False)
    finally:
        if lock_owned_here:
            release_pipeline_lock()


def git_commit_and_push(message: str, dry: bool = False) -> bool:
    """Stage the normal generated-file set, commit if needed, and push."""
    return git_commit_and_push_paths(message, PUBLISH_PATHS, dry=dry)


# ── Main Pipeline ────────────────────────────────────────────────────────────

def _with_publication_lock(function):
    @functools.wraps(function)
    def locked(*args, **kwargs):
        lock_owned_here = not _pipeline_lock_owned_by_current_thread()
        if lock_owned_here and not acquire_pipeline_lock():
            log.info("Publication lock is busy; skipping this pipeline run.")
            return 0
        try:
            return function(*args, **kwargs)
        finally:
            if lock_owned_here:
                release_pipeline_lock()
    return locked


@_with_publication_lock
def run_pipeline(dry: bool = False, force: bool = False, refresh_hosts: bool = False) -> int:
    log.info("=" * 60)
    log.info("Nadgryzieni Pipeline — starting")
    mode_label = 'DRY RUN' if dry else 'LIVE'
    if refresh_hosts:
        mode_label += ' + REFRESH HOSTS'
    log.info(f"Mode: {mode_label}")
    log.info("=" * 60)

    _recover_publication_journal()

    # Step 1: Fetch RSS
    log.info("Step 1: Fetching RSS feed...")
    xml_bytes = fetch_rss()
    items = parse_rss_items(xml_bytes)
    log.info(f"  Parsed {len(items)} episodes from RSS")

    # Preserve known URLs and refresh them from the current RSS feed.
    url_by_title: dict[str, list[str]] = defaultdict(list)
    current_data = {}
    current_data_bytes = _read_bytes_secure(DATA_JSON_PATH, REPO_DIR.parent)
    if current_data_bytes is not None:
        try:
            current_data = json.loads(current_data_bytes.decode("utf-8"))
            for episode in current_data.get("episodes", []):
                title_key = normalize_lookup_title(episode.get("title", ""))
                if title_key and episode.get("url") and episode["url"] not in url_by_title[title_key]:
                    url_by_title[title_key].append(episode["url"])
        except (OSError, json.JSONDecodeError):
            log.warning("Could not read existing episode URLs; rebuilding from RSS")
    for item in items:
        title_key = normalize_lookup_title(item.get("title", ""))
        if title_key and item.get("url") and item["url"] not in url_by_title[title_key]:
            url_by_title[title_key].append(item["url"])

    # Step 2: Load existing archive
    log.info("Step 2: Loading existing archive...")
    existing_rows, _ = parse_archive(ARCHIVE_PATH)
    attach_existing_data(existing_rows, current_data)
    log.info(f"  Archive has {len(existing_rows)} rows")

    # Build both stable-record and episode-number lookups. Episode numbers are not unique.
    existing_record_keys = {build_record_key(row) for row in existing_rows}
    existing_ep_numbers = set()
    for r in existing_rows:
        # Extract just the numeric part for comparison
        ep_str = r["episode"].strip()
        existing_ep_numbers.add(ep_str)

    # Step 3: Find new episodes
    log.info("Step 3: Checking for new episodes...")
    new_episodes = []
    for item in items:
        ep_num = item["episode_number"]
        candidate = {
            "counter": str(len(existing_rows) + len(new_episodes) + 1),
            "episode": ep_num,
            "title": item["title"],
            "date": item["date"],
            "duration": item["duration"] if item["duration"] else "?",
            "url": resolve_existing_source_url(item, existing_rows),
        }
        candidate["record_key"] = build_record_key(candidate)
        if candidate["record_key"] in existing_record_keys:
            continue
        new_episodes.append(candidate)
        existing_record_keys.add(candidate["record_key"])
        existing_ep_numbers.add(ep_num)

    # Step 3b: Check Patreon for afterparty episodes
    log.info("Step 3b: Checking Patreon for afterparty episodes...")
    patreon_new = merge_patreon_episodes(existing_ep_numbers, new_episodes)
    if patreon_new:
        log.info(f"  Found {len(patreon_new)} Patreon afterparty episode(s):")
        for ep in patreon_new:
            log.info("    #%s: %s...", _safe_log_text(ep.get("episode")), _safe_log_text(ep.get("title")))
        new_episodes.extend(patreon_new)
        existing_ep_numbers.update(ep["episode"] for ep in patreon_new)
        for ep in patreon_new:
            title_key = normalize_lookup_title(ep.get("title", ""))
            if title_key and ep.get("url") and ep["url"] not in url_by_title[title_key]:
                url_by_title[title_key].append(ep["url"])

    if not new_episodes and not force and not refresh_hosts:
        if not current_data:
            raise RuntimeError("Existing generated data is missing; rerun with --force")
        validate_generated_data(current_data, existing_rows)
        if not _entry_exists_verified(HOST_METADATA_PATH, root=REPO_DIR):
            raise RuntimeError("host_metadata.json is missing; rerun with --force")
        load_host_metadata(record_rows=existing_rows)
        log.info("No new episodes found. Existing generated artifacts validated.")
        return 0

    if new_episodes:
        log.info(f"  Found {len(new_episodes)} new episode(s):")
        for ep in new_episodes:
            log.info("    #%s: %s...", _safe_log_text(ep.get("episode")), _safe_log_text(ep.get("title")))

    # Step 4: Merge and write archive
    log.info("Step 4: Updating archive...")
    all_rows = existing_rows + new_episodes

    # Sort by publish date (oldest first)
    # SP episodes (non-numeric) are interleaved by date
    # For episodes with same date, sort by episode number
    def sort_key(r):
        date_str = r["date"] if r["date"] else "9999-12-31"
        ep = r["episode"]
        # Numeric episodes: use episode number as secondary sort
        # SP episodes: use 0 as secondary sort (sorts first among same date)
        try:
            ep_num = float(ep)
        except ValueError:
            ep_num = 0
        return (date_str, ep_num, r["title"])
    all_rows.sort(key=sort_key)

    # Reassign counters after sorting
    for i, r in enumerate(all_rows, start=1):
        r["counter"] = str(i)

    # Host enrichment is mandatory before generated output is published.
    log.info("Step 4b: Enriching host metadata...")
    if not _entry_exists_verified(HOST_METADATA_PATH, root=REPO_DIR):
        raise RuntimeError(
            "host_metadata.json is missing; run `python3 nadgryzieni_hosts.py audit ...` "
            "and apply the verified audit before the weekly pipeline"
        )
    host_manifest = load_host_metadata(record_rows=existing_rows)
    refresh_keys = {row["record_key"] for row in all_rows} if refresh_hosts else {
        build_record_key(row) for row in new_episodes
    }
    host_manifest = enrich_host_rows(all_rows, host_manifest, refresh_keys=refresh_keys)
    apply_host_metadata(all_rows, host_manifest, strict=True)

    publish_outputs = bool(new_episodes or force or refresh_hosts)
    stage_dir = None
    try:
        if dry:
            write_archive(all_rows, dry=True)

            # Step 5: Generate data.json
            log.info("Step 5: Generating data.json...")
            data = generate_data_json(all_rows, url_by_title)
            validate_generated_data(data, all_rows)
            write_data_json(data, dry=True)
            write_host_metadata(host_manifest, path=HOST_METADATA_PATH, dry=True, record_rows=all_rows)

            # Step 5b: Update README.md stats table
            log.info("Step 5b: Updating README.md...")
            update_readme(data, dry=True)

            if publish_outputs:
                log.info("Step 6: Generating statistics...")
                write_stats(generate_statistics(all_rows), dry=True)
                log.info("Step 7: Bumping cache-busting version...")
                new_version = bump_cache_version(dry=True)
                update_cache_busting(new_version, dry=True)
        else:
            stage_dir = Path(tempfile.mkdtemp(prefix=".nadgryzieni-pipeline-stage-", dir=str(REPO_DIR.parent)))
            staged_archive = stage_dir / ARCHIVE_PATH.name
            staged_data = stage_dir / DATA_JSON_PATH.name
            staged_manifest = stage_dir / HOST_METADATA_PATH.name
            staged_readme = stage_dir / README_PATH.name
            staged_stats = stage_dir / STATS_PATH.name
            staged_cache = stage_dir / CACHE_VERSION_FILE.name
            staged_index = stage_dir / INDEX_HTML_PATH.name
            staged_script = stage_dir / SCRIPT_JS_PATH.name

            write_archive(all_rows, target_path=staged_archive)
            log.info("Step 5: Generating data.json...")
            data = generate_data_json(all_rows, url_by_title)
            validate_generated_data(data, all_rows)
            write_data_json(data, target_path=staged_data)
            write_host_metadata(host_manifest, path=staged_manifest, record_rows=all_rows)

            log.info("Step 5b: Updating README.md...")
            update_readme(data, target_path=staged_readme)
            log.info("Step 6: Generating statistics...")
            write_stats(generate_statistics(all_rows), target_path=staged_stats)
            log.info("Step 7: Bumping cache-busting version...")
            new_version = bump_cache_version(dry=True)
            index_content = _read_bytes_secure(INDEX_HTML_PATH, REPO_DIR)
            script_content = _read_bytes_secure(SCRIPT_JS_PATH, REPO_DIR)
            if index_content is None or script_content is None:
                raise RuntimeError("Cache-bust source files are missing")
            _atomic_write_bytes(staged_index, index_content, root=REPO_DIR.parent, mode=0o644)
            _atomic_write_bytes(staged_script, script_content, root=REPO_DIR.parent, mode=0o644)
            update_cache_busting(new_version, index_path=staged_index, script_path=staged_script)
            _atomic_write_text(staged_cache, str(new_version) + "\n", root=REPO_DIR.parent, mode=0o644)

            # Re-validate the exact files that are about to be published.
            staged_data_content = _read_bytes_secure(staged_data, REPO_DIR.parent)
            if staged_data_content is None:
                raise FileNotFoundError(staged_data)
            staged_data_payload = json.loads(staged_data_content.decode("utf-8"))
            validate_generated_data(staged_data_payload, all_rows)
            load_host_metadata(staged_manifest, record_rows=all_rows)
            staged_rows, _ = parse_archive(staged_archive)
            if len(staged_rows) != len(all_rows):
                raise ValueError("Staged archive row count does not match the generated dataset")
            atomic_replace_group([
                (ARCHIVE_PATH, staged_archive),
                (DATA_JSON_PATH, staged_data),
                (HOST_METADATA_PATH, staged_manifest),
                (README_PATH, staged_readme),
                (STATS_PATH, staged_stats),
                (CACHE_VERSION_FILE, staged_cache),
                (INDEX_HTML_PATH, staged_index),
                (SCRIPT_JS_PATH, staged_script),
            ])
    finally:
        if stage_dir is not None:
            _remove_directory_verified(stage_dir, root=REPO_DIR.parent)

    # Step 8: Sync to Obsidian before publishing, then verify Git changes.
    if publish_outputs:
        log.info("Step 8: Syncing to Obsidian vault...")
        sync_to_obsidian(dry=dry)
        log.info("Step 9: Committing and pushing to Git...")
        today = datetime.now(timezone.utc).date().isoformat()
        action = "host refresh" if refresh_hosts and not new_episodes else "regeneration" if force and not new_episodes else "archive update"
        commit_msg = f"Nadgryzieni {action} – {today} ({len(new_episodes)} new episodes)"
        git_commit_and_push(commit_msg, dry=dry)

    log.info("=" * 60)
    log.info(f"Pipeline complete! {len(new_episodes)} new episode(s) added.")
    if not new_episodes:
        log.info("No new episodes — archive and data.json regenerated (force mode).")
    log.info("=" * 60)
    return len(new_episodes)


def _run_main_locked(
    *,
    dry: bool,
    force: bool,
    refresh_hosts: bool,
    run_kind: str,
    today: date,
) -> int:
    if run_kind == "retry" and not dry:
        retry_state = _read_retry_state(RETRY_STATE_PATH)
        if retry_state is None or retry_state.get("pending") is not True:
            raise RuntimeError("Retry state is missing or not pending")
        repo = str(REPO_DIR)
        ahead_of_origin = _local_commits_ahead_of_origin(repo)
        if ahead_of_origin is not True:
            raise RuntimeError("Retry commit is not verifiably ahead of origin")
        _ensure_main_branch(repo)
        allowed_paths = set(_normalise_git_paths(PUBLISH_PATHS))
        _reject_unrelated_staged_paths(repo, allowed_paths)
        expected_commit = retry_state["pending_commit"]
        current_commit = _git_head_sha(repo)
        if current_commit is None or current_commit != expected_commit:
            raise RuntimeError("Retry HEAD does not match the recorded pending commit")
        _ensure_ahead_commits_are_allowed(repo, allowed_paths)
        try:
            _push_with_retries(repo)
        except GitPushPendingError:
            write_retry_state(
                RETRY_STATE_PATH,
                date.fromisoformat(retry_state["primary_date"]),
                pending=True,
                pending_commit=current_commit,
            )
            raise
        write_retry_state(RETRY_STATE_PATH, today, pending=False)
        return 0

    if run_kind == "primary" and not dry:
        write_retry_state(RETRY_STATE_PATH, today, pending=False)

    try:
        new_count = run_pipeline(dry=dry, force=force, refresh_hosts=refresh_hosts)
    except GitPushPendingError:
        if run_kind in {"primary", "retry"} and not dry:
            pending_commit = _git_head_sha(str(REPO_DIR))
            if pending_commit is None:
                raise RuntimeError("Git push failed but the pending local commit is unavailable")
            write_retry_state(RETRY_STATE_PATH, today, pending=True, pending_commit=pending_commit)
        raise
    except Exception:
        if run_kind in {"primary", "retry"} and not dry:
            write_retry_state(RETRY_STATE_PATH, today, pending=False)
        raise

    if run_kind in {"primary", "retry"} and not dry:
        write_retry_state(RETRY_STATE_PATH, today, pending=False)
    return new_count


def main() -> int:
    dry = "--dry" in sys.argv
    force = "--force" in sys.argv
    refresh_hosts = "--refresh-hosts" in sys.argv
    run_kind = os.environ.get("NADGRYZIENI_RUN_KIND", "manual").lower()
    today = datetime.now(timezone.utc).date()

    if not acquire_pipeline_lock():
        return 0

    try:
        _recover_publication_journal()
        if run_kind == "retry" and not dry and not retry_is_due(RETRY_STATE_PATH, today):
            log.info("No pending Friday run; skipping conditional retry.")
            return 0
        return _run_main_locked(
            dry=dry,
            force=force,
            refresh_hosts=refresh_hosts,
            run_kind=run_kind,
            today=today,
        )
    finally:
        release_pipeline_lock()


if __name__ == "__main__":
    main()