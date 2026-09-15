#!/usr/bin/env python3
"""Trigger idempotent Buffer reconciliation after each completed YouTube probe."""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
from pathlib import Path
from typing import Callable

import nadgryzieni_upcoming as upcoming


STATE_DIR = upcoming.STATE_DIR
DISCOVERY_STATE_PATH = upcoming.STATE_PATH
TRIGGER_STATE_PATH = STATE_DIR / "nadgryzieni-buffer-trigger-state.json"
BUFFER_RECONCILE_WRAPPER = Path(__file__).resolve().parent.parent / "nadgryzieni_buffer_reconcile.sh"
TRIGGER_STATE_SCHEMA_VERSION = 1
BUFFER_TIMEOUT_SECONDS = 150


def _validate_trigger_state(state: dict) -> None:
    if not isinstance(state, dict) or set(state) - {
        "schema_version",
        "last_reconciled_probe_slot_utc",
    }:
        raise RuntimeError("Buffer trigger state contains unknown fields")
    if state.get("schema_version") != TRIGGER_STATE_SCHEMA_VERSION:
        raise RuntimeError("Buffer trigger state has an unsupported schema")
    slot = state.get("last_reconciled_probe_slot_utc")
    if slot is not None:
        _validate_probe_slot(slot)


def _validate_probe_slot(value: object) -> str:
    if not isinstance(value, str):
        raise RuntimeError("Buffer trigger received an invalid probe slot")
    try:
        parsed = upcoming.parse_iso_utc(value)
    except (TypeError, ValueError) as exc:
        raise RuntimeError("Buffer trigger received an invalid probe slot") from exc
    if (
        parsed.hour not in upcoming.PROBE_HOURS_UTC
        or parsed.minute != upcoming.PROBE_MINUTE_UTC
        or parsed.second != 0
        or parsed.microsecond != 0
    ):
        raise RuntimeError("Buffer trigger received an invalid probe slot")
    return upcoming.iso_utc(parsed)


def reconcile_buffer() -> None:
    """Run the fixed local reconciler without a shell or secret-bearing argv."""
    wrapper = BUFFER_RECONCILE_WRAPPER
    try:
        metadata = wrapper.lstat()
    except OSError as exc:
        raise RuntimeError("Buffer reconciliation wrapper is unavailable") from exc
    if not stat.S_ISREG(metadata.st_mode) or wrapper.is_symlink():
        raise RuntimeError("Buffer reconciliation wrapper is not a regular file")
    if metadata.st_uid != os.getuid() or metadata.st_mode & 0o022:
        raise RuntimeError("Buffer reconciliation wrapper permissions are unsafe")
    try:
        completed = subprocess.run(
            ["/bin/bash", str(wrapper)],
            check=False,
            timeout=BUFFER_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("Buffer reconciliation timed out") from exc
    if completed.returncode != 0:
        raise RuntimeError(f"Buffer reconciliation exited with status {completed.returncode}")


def run_after_probe(
    *,
    discovery_state_path: Path = DISCOVERY_STATE_PATH,
    trigger_state_path: Path = TRIGGER_STATE_PATH,
    reconcile: Callable[[], None] = reconcile_buffer,
) -> dict[str, object]:
    """Reconcile once per published probe slot, marking success only afterward."""
    with upcoming._exclusive_cycle_lock(trigger_state_path) as acquired:
        if not acquired:
            return {"status": "already_running"}
        discovery_state = upcoming._migrate_state(
            upcoming._read_json(
                discovery_state_path,
                {"schema_version": upcoming.STATE_SCHEMA_VERSION},
                root=discovery_state_path.parent.parent,
            )
        )
        upcoming._validate_state(discovery_state)
        raw_slot = discovery_state.get("last_probe_slot_utc")
        if raw_slot is None:
            return {"status": "no_completed_probe"}
        probe_slot = _validate_probe_slot(raw_slot)
        if discovery_state.get("publish_pending") is True:
            return {"status": "publication_pending", "probe_slot_utc": probe_slot}

        trigger_state = upcoming._read_json(
            trigger_state_path,
            {"schema_version": TRIGGER_STATE_SCHEMA_VERSION},
            root=trigger_state_path.parent.parent,
        )
        _validate_trigger_state(trigger_state)
        if trigger_state.get("last_reconciled_probe_slot_utc") == probe_slot:
            return {"status": "already_reconciled", "probe_slot_utc": probe_slot}

        has_upcoming_event = "video_id" in discovery_state
        if has_upcoming_event:
            reconcile()
        new_state = {
            "schema_version": TRIGGER_STATE_SCHEMA_VERSION,
            "last_reconciled_probe_slot_utc": probe_slot,
        }
        upcoming._write_json_atomic(
            trigger_state_path,
            new_state,
            root=trigger_state_path.parent.parent,
        )
        return {
            "status": "reconciled" if has_upcoming_event else "no_upcoming_event",
            "probe_slot_utc": probe_slot,
        }


def main() -> int:
    try:
        result = run_after_probe()
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
