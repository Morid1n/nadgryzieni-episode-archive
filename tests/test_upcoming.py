import importlib.util
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO_DIR = Path(__file__).resolve().parents[1]
MODULE_PATH = REPO_DIR / "nadgryzieni_upcoming.py"
spec = importlib.util.spec_from_file_location("nadgryzieni_upcoming_for_tests", MODULE_PATH)
upcoming = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = upcoming
spec.loader.exec_module(upcoming)


class UpcomingDiscoveryTests(unittest.TestCase):
    def test_parse_scheduled_streams_extracts_title_and_future_event(self):
        now = datetime(2026, 8, 24, 4, 30, tzinfo=timezone.utc)
        start = int((now + timedelta(days=4, hours=3)).timestamp())
        page = (
            '<title>603: Steve Jobs i NeXT – zapomniany rozdział jego życia - YouTube</title>'
            f'eStreamabilityRenderer":{{"videoId":"fRAaGylDNM8",'
            f'"scheduledStartTime":"{start}"}}'
        )
        streams = upcoming.parse_scheduled_streams(page, now=now)
        self.assertEqual(len(streams), 1)
        self.assertEqual(streams[0].video_id, "fRAaGylDNM8")
        self.assertEqual(streams[0].title, "603: Steve Jobs i NeXT – zapomniany rozdział jego życia")
        self.assertEqual(streams[0].link, "https://www.youtube.com/watch?v=fRAaGylDNM8")

    def test_expired_events_are_not_candidates(self):
        now = datetime(2026, 8, 24, 4, 30, tzinfo=timezone.utc)
        start = int((now - timedelta(minutes=1)).timestamp())
        page = f'<title>Old - YouTube</title>eStreamabilityRenderer":{{"videoId":"abc123456","scheduledStartTime":"{start}"}}'
        self.assertEqual(upcoming.parse_scheduled_streams(page, now=now), [])

    def test_next_saturday_probe_is_dst_safe_and_skips_current_saturday(self):
        monday = datetime(2026, 8, 24, 16, 30, tzinfo=timezone.utc)
        saturday = datetime(2026, 8, 29, 4, 31, tzinfo=timezone.utc)
        self.assertEqual(
            upcoming.next_saturday_probe(monday),
            datetime(2026, 8, 29, 4, 30, tzinfo=timezone.utc),
        )
        self.assertEqual(
            upcoming.next_saturday_probe(saturday),
            datetime(2026, 9, 5, 4, 30, tzinfo=timezone.utc),
        )

    def test_cycle_holds_after_found_until_next_saturday(self):
        monday_probe = datetime(2026, 8, 24, 4, 30, tzinfo=timezone.utc)
        stream = upcoming.Stream(
            video_id="fRAaGylDNM8",
            title="603: Steve Jobs i NeXT",
            start_utc=datetime(2026, 8, 28, 7, 0, tzinfo=timezone.utc),
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            state_path = root / "state.json"
            artifact_path = root / "upcoming.json"
            publish_calls = []
            result = upcoming.run_cycle(
                monday_probe,
                state_path=state_path,
                artifact_path=artifact_path,
                discover=lambda now: stream,
                publish=lambda: publish_calls.append(True),
            )
            self.assertEqual(result["status"], "found")
            self.assertEqual(result["hold_until_utc"], "2026-08-29T04:30:00Z")
            self.assertEqual(publish_calls, [True])

            def unexpected_discovery(_now):
                raise AssertionError("discovery must be paused before Saturday")

            paused = upcoming.run_cycle(
                datetime(2026, 8, 25, 4, 30, tzinfo=timezone.utc),
                state_path=state_path,
                artifact_path=artifact_path,
                discover=unexpected_discovery,
            )
            self.assertEqual(paused["status"], "paused")

            resumed = upcoming.run_cycle(
                datetime(2026, 8, 29, 4, 30, tzinfo=timezone.utc),
                state_path=state_path,
                artifact_path=artifact_path,
                discover=lambda now: None,
            )
            self.assertEqual(resumed["status"], "not_found")
            self.assertIsNone(__import__("json").loads(artifact_path.read_text())["event"])

    def test_non_probe_tick_does_not_call_discovery(self):
        calls = []
        result = upcoming.run_cycle(
            datetime(2026, 8, 24, 4, 29, tzinfo=timezone.utc),
            state_path=Path(tempfile.mkdtemp()) / "state.json",
            artifact_path=Path(tempfile.mkdtemp()) / "upcoming.json",
            discover=lambda now: calls.append(now),
        )
        self.assertEqual(result["status"], "outside_probe_window")
        self.assertEqual(calls, [])

    def test_pending_publication_retries_without_discovery_during_hold(self):
        probe = datetime(2026, 8, 24, 4, 30, tzinfo=timezone.utc)
        stream = upcoming.Stream(
            video_id="fRAaGylDNM8",
            title="603: Steve Jobs i NeXT",
            start_utc=datetime(2026, 8, 28, 7, 0, tzinfo=timezone.utc),
        )
        publish_calls = []

        def failing_publish():
            publish_calls.append("failed")
            raise RuntimeError("temporary push failure")

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            state_path = root / "state.json"
            artifact_path = root / "upcoming.json"
            with self.assertRaises(RuntimeError):
                upcoming.run_cycle(
                    probe,
                    state_path=state_path,
                    artifact_path=artifact_path,
                    discover=lambda now: stream,
                    publish=failing_publish,
                )
            state = __import__("json").loads(state_path.read_text())
            self.assertTrue(state["publish_pending"])

            def successful_publish():
                publish_calls.append("success")

            paused = upcoming.run_cycle(
                datetime(2026, 8, 25, 4, 30, tzinfo=timezone.utc),
                state_path=state_path,
                artifact_path=artifact_path,
                discover=lambda _now: self.fail("discovery must remain paused"),
                publish=successful_publish,
            )
            self.assertEqual(paused["status"], "paused")
            self.assertEqual(publish_calls, ["failed", "success"])
            self.assertFalse(__import__("json").loads(state_path.read_text())["publish_pending"])


if __name__ == "__main__":
    unittest.main()
