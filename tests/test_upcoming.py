import importlib.util
import json
import os
import sys
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

REPO_DIR = Path(__file__).resolve().parents[1]
MODULE_PATH = REPO_DIR / "nadgryzieni_upcoming.py"
spec = importlib.util.spec_from_file_location("nadgryzieni_upcoming_for_tests", MODULE_PATH)
upcoming = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = upcoming
spec.loader.exec_module(upcoming)


class UpcomingDiscoveryTests(unittest.TestCase):
    def test_cycle_lock_rejects_fifo_without_blocking(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            state_dir = root / "state"
            state_dir.mkdir()
            state_path = state_dir / "upcoming.json"
            lock_path = state_dir / "upcoming.json.lock"
            os.mkfifo(lock_path)
            with self.assertRaisesRegex(RuntimeError, "regular"):
                with upcoming._exclusive_cycle_lock(state_path):
                    pass

    def test_parse_iso_utc_rejects_nonzero_offsets(self):
        with self.assertRaisesRegex(ValueError, "zero UTC offset"):
            upcoming.parse_iso_utc("2026-08-28T09:00:00+02:00")

    def test_state_schema_rejects_unknown_fields_and_invalid_types(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            state_path = root / "state.json"
            artifact_path = root / "upcoming.json"
            state_path.write_text(json.dumps({
                "schema_version": 1,
                "publish_pending": "true",
            }), encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "boolean"):
                upcoming.run_cycle(
                    datetime(2026, 8, 24, 4, 30, tzinfo=timezone.utc),
                    state_path=state_path,
                    artifact_path=artifact_path,
                    discover=lambda _now: None,
                )
            state_path.write_text(json.dumps({"schema_version": 1, "unexpected": True}), encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "unknown"):
                upcoming.run_cycle(
                    datetime(2026, 8, 24, 4, 30, tzinfo=timezone.utc),
                    state_path=state_path,
                    artifact_path=artifact_path,
                    discover=lambda _now: None,
                )
            state_path.write_text(json.dumps({"publish_pending": False}), encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "schema_version"):
                upcoming.run_cycle(
                    datetime(2026, 8, 24, 4, 30, tzinfo=timezone.utc),
                    state_path=state_path,
                    artifact_path=artifact_path,
                    discover=lambda _now: None,
                )

    def test_pending_publication_requires_digest_and_rejects_mismatch(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            state_path = root / "state.json"
            artifact_path = root / "upcoming.json"
            state_path.write_text(json.dumps({
                "schema_version": 1,
                "publish_pending": True,
            }), encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "digest"):
                upcoming.run_cycle(
                    datetime(2026, 8, 24, 4, 30, tzinfo=timezone.utc),
                    state_path=state_path,
                    artifact_path=artifact_path,
                    discover=lambda _now: None,
                    publish=lambda: True,
                )
            artifact_path.write_text('{"actual": true}\n', encoding="utf-8")
            state_path.write_text(json.dumps({
                "schema_version": 1,
                "publish_pending": True,
                "pending_artifact_sha256": "0" * 64,
            }), encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "does not match"):
                upcoming.run_cycle(
                    datetime(2026, 8, 24, 4, 30, tzinfo=timezone.utc),
                    state_path=state_path,
                    artifact_path=artifact_path,
                    discover=lambda _now: self.fail("discovery must not run for a mismatched pending artifact"),
                    publish=lambda: self.fail("publication must not run for a mismatched pending artifact"),
                )

    def test_artifact_writer_rejects_symlink_destination(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            state_path = root / "state.json"
            outside = root / "outside.json"
            outside.write_text("untouched", encoding="utf-8")
            artifact_path = root / "upcoming.json"
            artifact_path.symlink_to(outside)
            with self.assertRaisesRegex(RuntimeError, "symlink"):
                upcoming.run_cycle(
                    datetime(2026, 8, 24, 4, 30, tzinfo=timezone.utc),
                    state_path=state_path,
                    artifact_path=artifact_path,
                    discover=lambda _now: None,
                )
            self.assertEqual(outside.read_text(encoding="utf-8"), "untouched")

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

    def test_malformed_or_out_of_range_timestamps_fail_closed(self):
        now = datetime(2026, 8, 24, 4, 30, tzinfo=timezone.utc)
        page = '<title>Malformed - YouTube</title>' + (
            'eStreamabilityRenderer":{\"videoId\":\"abc123456\",'
            '"scheduledStartTime":"999999999999999999999999"}}'
        )
        self.assertEqual(upcoming.parse_scheduled_streams(page, now=now), [])

    def test_malformed_duplicate_does_not_hide_valid_same_video_id(self):
        now = datetime(2026, 8, 24, 4, 30, tzinfo=timezone.utc)
        start = int((now + timedelta(days=1)).timestamp())
        page = (
            '<title>Upcoming - YouTube</title>'
            'eStreamabilityRenderer":{\"videoId\":\"abc123456\",\"scheduledStartTime\":\"999999999999999999999\"}'
            f'eStreamabilityRenderer":{{"videoId":"abc123456","scheduledStartTime":"{start}"}}'
        )
        streams = upcoming.parse_scheduled_streams(page, now=now)
        self.assertEqual(len(streams), 1)
        self.assertEqual(streams[0].video_id, "abc123456")

    def test_malformed_renderer_does_not_cross_boundary_into_later_renderer(self):
        now = datetime(2026, 8, 24, 4, 30, tzinfo=timezone.utc)
        start = int((now + timedelta(days=1)).timestamp())
        page = (
            '<title>Upcoming - YouTube</title>'
            'eStreamabilityRenderer":{"videoId":"wrongid"'
            f'eStreamabilityRenderer":{{"videoId":"goodid","scheduledStartTime":"{start}"}}'
        )
        streams = upcoming.parse_scheduled_streams(page, now=now)
        self.assertEqual([stream.video_id for stream in streams], ["goodid"])

    def test_renderer_field_ignores_nested_object_fields(self):
        body = 'nested: {"videoId":"badid"}, videoId: "goodid"'
        self.assertEqual(upcoming._renderer_field(body, "videoId"), "goodid")

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
                publish=lambda: (publish_calls.append(True) or True),
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

    def test_pending_marker_reconciles_artifact_crash_window(self):
        probe = datetime(2026, 8, 24, 4, 30, tzinfo=timezone.utc)
        stream = upcoming.Stream(
            video_id="fRAaGylDNM8",
            title="603: Steve Jobs i NeXT",
            start_utc=datetime(2026, 8, 28, 7, 0, tzinfo=timezone.utc),
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            state_path = root / "state.json"
            artifact_path = root / "upcoming.json"
            real_write = upcoming._write_json_atomic
            calls = []

            def crash_after_artifact(path, payload, **kwargs):
                calls.append(path)
                if len(calls) == 3 and path == state_path:
                    raise RuntimeError("simulated state replacement crash")
                return real_write(path, payload, **kwargs)

            with patch.object(upcoming, "_write_json_atomic", side_effect=crash_after_artifact):
                with self.assertRaisesRegex(RuntimeError, "replacement crash"):
                    upcoming.run_cycle(
                        probe,
                        state_path=state_path,
                        artifact_path=artifact_path,
                        discover=lambda _now: stream,
                        publish=lambda: True,
                    )
            provisional = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertTrue(provisional["publish_pending"])
            self.assertEqual(
                provisional["pending_artifact_sha256"],
                upcoming._artifact_digest(artifact_path),
            )
            publish_calls = []
            self.assertTrue(
                upcoming._retry_pending_publication(
                    provisional,
                    state_path,
                    artifact_path,
                    lambda: (publish_calls.append(True) or True),
                )
            )
            self.assertEqual(publish_calls, [True])
            reconciled = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertFalse(reconciled["publish_pending"])
            self.assertNotIn("pending_artifact_sha256", reconciled)

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
            state = json.loads(state_path.read_text())
            self.assertTrue(state["publish_pending"])

            def successful_publish():
                publish_calls.append("success")
                return True

            paused = upcoming.run_cycle(
                datetime(2026, 8, 25, 4, 30, tzinfo=timezone.utc),
                state_path=state_path,
                artifact_path=artifact_path,
                discover=lambda _now: self.fail("discovery must remain paused"),
                publish=successful_publish,
            )
            self.assertEqual(paused["status"], "paused")
            self.assertEqual(publish_calls, ["failed", "success"])
            self.assertFalse(json.loads(state_path.read_text())["publish_pending"])

    def test_pending_commit_is_retried_without_discovery(self):
        probe = datetime(2026, 8, 24, 4, 30, tzinfo=timezone.utc)
        stream = upcoming.Stream(
            video_id="fRAaGylDNM8",
            title="603: Steve Jobs i NeXT",
            start_utc=datetime(2026, 8, 28, 7, 0, tzinfo=timezone.utc),
        )
        pending_commit = "b" * 40

        def pending_publish():
            raise upcoming.PublishPendingError(pending_commit)

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            state_path = root / "state.json"
            artifact_path = root / "upcoming.json"
            with self.assertRaises(upcoming.PublishPendingError):
                upcoming.run_cycle(
                    probe,
                    state_path=state_path,
                    artifact_path=artifact_path,
                    discover=lambda _now: stream,
                    publish=pending_publish,
                )
            pending_state = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertEqual(pending_state["pending_commit"], pending_commit)
            retry_calls = []
            with patch.object(
                upcoming,
                "publish_git",
                side_effect=lambda commit: retry_calls.append(commit) or True,
            ):
                retried = upcoming.run_cycle(
                    probe + timedelta(days=1),
                    state_path=state_path,
                    artifact_path=artifact_path,
                    discover=lambda _now: self.fail("pending publication must not rediscover"),
                    publish=upcoming.publish_git,
                )
            self.assertEqual(retried["status"], "paused")
            self.assertEqual(retry_calls, [pending_commit])
            final_state = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertFalse(final_state["publish_pending"])
            self.assertNotIn("pending_commit", final_state)

    def test_false_publication_result_keeps_pending_for_retry(self):
        probe = datetime(2026, 8, 24, 4, 30, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            state_path = root / "state.json"
            artifact_path = root / "upcoming.json"
            artifact_path.write_text('{"pending": true}\n', encoding="utf-8")
            state_path.write_text(json.dumps({
                "schema_version": upcoming.STATE_SCHEMA_VERSION,
                "publish_pending": True,
                "hold_until_utc": "2026-08-29T04:30:00Z",
                "pending_artifact_sha256": upcoming._artifact_digest(artifact_path),
            }), encoding="utf-8")
            retried = upcoming.run_cycle(
                probe + timedelta(days=1),
                state_path=state_path,
                artifact_path=artifact_path,
                discover=lambda _now: self.fail("pending publication must retry without discovery"),
                publish=lambda: False,
            )
            self.assertEqual(retried["status"], "paused")
            self.assertTrue(retried["publication_retried"])
            self.assertTrue(json.loads(state_path.read_text(encoding="utf-8"))["publish_pending"])

    def test_not_found_publication_retries_before_same_day_gate(self):
        probe = datetime(2026, 8, 24, 4, 30, tzinfo=timezone.utc)
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
                    discover=lambda _now: None,
                    publish=failing_publish,
                )
            retried = upcoming.run_cycle(
                probe + timedelta(minutes=1),
                state_path=state_path,
                artifact_path=artifact_path,
                discover=lambda _now: self.fail("same-day retry must not rediscover"),
                publish=lambda: (publish_calls.append("success") or True),
            )
            self.assertEqual(retried["status"], "already_probed")
            self.assertTrue(retried["publication_retried"])
            self.assertEqual(publish_calls, ["failed", "success"])

    def test_atomic_writer_rejects_fifo_after_destination_swap(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            destination = root / "state.json"
            destination.write_text("old", encoding="utf-8")
            original_open = upcoming.os.open
            swapped = False

            def swap_destination(path, flags, *args, **kwargs):
                nonlocal swapped
                if str(path) == destination.name and not swapped:
                    destination.unlink()
                    os.mkfifo(destination)
                    swapped = True
                    if not flags & getattr(os, "O_NONBLOCK", 0):
                        raise AssertionError("O_NONBLOCK missing")
                return original_open(path, flags, *args, **kwargs)

            with patch.object(upcoming.os, "open", side_effect=swap_destination):
                with self.assertRaisesRegex(RuntimeError, "atomic-write destination|regular"):
                    upcoming._write_json_atomic(destination, {"value": "new"}, root=root)

    def test_upcoming_fetch_rejects_default_redirect_following_opener(self):
        with patch.object(upcoming, "urlopen", side_effect=AssertionError("default opener used")), patch.object(
            upcoming, "_open_https_no_redirect", side_effect=RuntimeError("redirect rejected")
        ) as opened:
            with self.assertRaises(RuntimeError):
                upcoming.fetch_text(upcoming.YOUTUBE_LIVE_URL)
        opened.assert_called_once()

    def test_upcoming_fetch_rejects_non_https_url(self):
        with self.assertRaisesRegex(ValueError, "HTTPS"):
            upcoming.fetch_text("http://127.0.0.1:9/live")

    def test_artifact_for_rejects_malformed_stream_video_id(self):
        stream = upcoming.Stream(
            video_id="bad?id",
            title="Upcoming",
            start_utc=datetime(2026, 8, 28, 7, 0, tzinfo=timezone.utc),
        )
        with self.assertRaisesRegex(ValueError, "video ID"):
            upcoming.artifact_for(stream, datetime(2026, 8, 24, 4, 30, tzinfo=timezone.utc))

    def test_publish_git_releases_pipeline_lock(self):
        acquired = []
        released = []
        fake_pipeline = SimpleNamespace(
            acquire_pipeline_lock=lambda: acquired.append(True) or True,
            release_pipeline_lock=lambda: released.append(True),
            git_commit_and_push_paths=lambda message, paths: True,
        )
        with patch.dict(sys.modules, {"nadgryzieni_pipeline": fake_pipeline}):
            self.assertTrue(upcoming.publish_git())
        self.assertEqual(acquired, [True])
        self.assertEqual(released, [True])

    def test_publish_git_retries_exact_pending_commit_without_pipeline_retry_marker(self):
        pending_commit = "a" * 40
        calls = []
        fake_pipeline = SimpleNamespace(
            acquire_pipeline_lock=lambda: True,
            release_pipeline_lock=lambda: None,
            _local_commits_ahead_of_origin=lambda _repo: True,
            _git_head_sha=lambda _repo: pending_commit,
            _ensure_main_branch=lambda _repo: calls.append("main"),
            _ensure_ahead_commits_are_allowed=lambda _repo, _allowed: calls.append("paths"),
            _push_with_retries=lambda _repo: calls.append("push") or True,
            git_commit_and_push_paths=lambda *_args, **_kwargs: self.fail("unbound pipeline commit path used"),
        )
        with patch.dict(sys.modules, {"nadgryzieni_pipeline": fake_pipeline}):
            self.assertTrue(upcoming.publish_git(pending_commit))
        self.assertEqual(calls, ["main", "paths", "push"])

    def test_overlapping_cycles_are_serialized(self):
        probe = datetime(2026, 8, 24, 4, 30, tzinfo=timezone.utc)
        stream = upcoming.Stream(
            video_id="fRAaGylDNM8",
            title="603: Steve Jobs i NeXT",
            start_utc=datetime(2026, 8, 28, 7, 0, tzinfo=timezone.utc),
        )
        started = threading.Event()
        release = threading.Event()
        results = []

        def blocking_discovery(_now):
            started.set()
            self.assertTrue(release.wait(timeout=5))
            return stream

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            state_path = root / "state.json"
            artifact_path = root / "upcoming.json"
            first = threading.Thread(
                target=lambda: results.append(upcoming.run_cycle(
                    probe,
                    state_path=state_path,
                    artifact_path=artifact_path,
                    discover=blocking_discovery,
                ))
            )
            second = threading.Thread(
                target=lambda: results.append(upcoming.run_cycle(
                    probe,
                    state_path=state_path,
                    artifact_path=artifact_path,
                    discover=lambda _now: self.fail("second cycle must not discover"),
                ))
            )
            first.start()
            self.assertTrue(started.wait(timeout=5))
            second.start()
            second.join(timeout=5)
            release.set()
            first.join(timeout=5)
            self.assertFalse(first.is_alive() or second.is_alive())
            self.assertEqual(sorted(result["status"] for result in results), ["already_running", "found"])


if __name__ == "__main__":
    unittest.main()
