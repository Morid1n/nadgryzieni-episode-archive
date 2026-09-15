import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path


REPO_DIR = Path(__file__).resolve().parents[1]
MODULE_PATH = REPO_DIR / "nadgryzieni_buffer_trigger.py"
spec = importlib.util.spec_from_file_location("nadgryzieni_buffer_trigger_for_tests", MODULE_PATH)
assert spec and spec.loader
trigger = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = trigger
spec.loader.exec_module(trigger)


class BufferAfterUpcomingTriggerTests(unittest.TestCase):
    def _write_discovery_state(self, path: Path, *, slot: str, publish_pending: bool = False) -> None:
        path.write_text(
            json.dumps(
                {
                    "schema_version": 2,
                    "last_probe_slot_utc": slot,
                    "hold_until_utc": None,
                    "publish_pending": publish_pending,
                    "video_id": "TitleChg608",
                    "scheduled_start_utc": "2026-09-19T07:00:00Z",
                    **(
                        {"pending_artifact_sha256": "a" * 64}
                        if publish_pending
                        else {}
                    ),
                }
            ),
            encoding="utf-8",
        )

    def test_new_completed_probe_slot_runs_reconcile_once(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            discovery_state = root / "nadgryzieni-upcoming-state.json"
            trigger_state = root / "nadgryzieni-buffer-trigger-state.json"
            self._write_discovery_state(discovery_state, slot="2026-09-15T10:30:00Z")
            calls = []

            first = trigger.run_after_probe(
                discovery_state_path=discovery_state,
                trigger_state_path=trigger_state,
                reconcile=lambda: calls.append("reconcile"),
            )
            second = trigger.run_after_probe(
                discovery_state_path=discovery_state,
                trigger_state_path=trigger_state,
                reconcile=lambda: self.fail("same probe slot must not reconcile twice"),
            )
            state = json.loads(trigger_state.read_text(encoding="utf-8"))

        self.assertEqual(first["status"], "reconciled")
        self.assertEqual(second["status"], "already_reconciled")
        self.assertEqual(calls, ["reconcile"])
        self.assertEqual(state["last_reconciled_probe_slot_utc"], "2026-09-15T10:30:00Z")

    def test_failed_reconcile_is_not_marked_and_retries(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            discovery_state = root / "nadgryzieni-upcoming-state.json"
            trigger_state = root / "nadgryzieni-buffer-trigger-state.json"
            self._write_discovery_state(discovery_state, slot="2026-09-15T16:30:00Z")
            calls = []

            def fail():
                calls.append("failed")
                raise RuntimeError("temporary Buffer failure")

            with self.assertRaisesRegex(RuntimeError, "temporary Buffer failure"):
                trigger.run_after_probe(
                    discovery_state_path=discovery_state,
                    trigger_state_path=trigger_state,
                    reconcile=fail,
                )
            result = trigger.run_after_probe(
                discovery_state_path=discovery_state,
                trigger_state_path=trigger_state,
                reconcile=lambda: calls.append("success"),
            )

        self.assertEqual(result["status"], "reconciled")
        self.assertEqual(calls, ["failed", "success"])

    def test_pending_upcoming_publication_defers_buffer(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            discovery_state = root / "nadgryzieni-upcoming-state.json"
            trigger_state = root / "nadgryzieni-buffer-trigger-state.json"
            self._write_discovery_state(
                discovery_state,
                slot="2026-09-15T22:30:00Z",
                publish_pending=True,
            )

            result = trigger.run_after_probe(
                discovery_state_path=discovery_state,
                trigger_state_path=trigger_state,
                reconcile=lambda: self.fail("unpublished upcoming data must not reach Buffer"),
            )

        self.assertEqual(result["status"], "publication_pending")

    def test_invalid_probe_slot_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            discovery_state = root / "nadgryzieni-upcoming-state.json"
            trigger_state = root / "nadgryzieni-buffer-trigger-state.json"
            self._write_discovery_state(discovery_state, slot="2026-09-15T11:30:00Z")

            with self.assertRaisesRegex(RuntimeError, "probe slot"):
                trigger.run_after_probe(
                    discovery_state_path=discovery_state,
                    trigger_state_path=trigger_state,
                    reconcile=lambda: self.fail("invalid slot must not reconcile"),
                )


if __name__ == "__main__":
    unittest.main()
