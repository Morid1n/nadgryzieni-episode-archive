import importlib.util
import json
import os
import ssl
import stat
import subprocess
import sys
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch


REPO_DIR = Path(__file__).resolve().parents[1]
MODULE_PATH = REPO_DIR / "nadgryzieni_pipeline.py"
spec = importlib.util.spec_from_file_location("nadgryzieni_pipeline", MODULE_PATH)
pipeline = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = pipeline
spec.loader.exec_module(pipeline)
HOSTS_MODULE_PATH = REPO_DIR / "nadgryzieni_hosts.py"
hosts_spec = importlib.util.spec_from_file_location("nadgryzieni_hosts_for_pipeline_tests", HOSTS_MODULE_PATH)
host_tools = importlib.util.module_from_spec(hosts_spec)
sys.modules[hosts_spec.name] = host_tools
hosts_spec.loader.exec_module(host_tools)


class PipelineHardeningTests(unittest.TestCase):
    def test_ssl_context_requires_certificate_verification(self):
        context = pipeline.create_ssl_context()
        self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(context.check_hostname)

    def test_pipeline_https_opener_rejects_non_https_request(self):
        request = pipeline.urllib.request.Request("http://127.0.0.1:9/source")
        with self.assertRaisesRegex(ValueError, "HTTPS"):
            pipeline._open_https_no_redirect(
                request,
                timeout=0.1,
                context=ssl.create_default_context(),
            )

    def test_patreon_rss_preserves_canonical_url_and_iso_date(self):
        xml = b'''<?xml version="1.0"?>
        <rss xmlns:itunes="http://www.itunes.com/dtds/rss-2.0.dtd">
          <channel>
            <item>
              <title>600: (Afterparty) Test</title>
              <pubDate>Mon, 03 Aug 2026 18:30:00 +0200</pubDate>
              <link>https://www.patreon.com/iMagazinePL/posts/600-afterparty-123456</link>
              <guid>https://www.patreon.com/iMagazinePL/posts/600-afterparty-123456</guid>
              <itunes:duration>3661</itunes:duration>
            </item>
          </channel>
        </rss>'''
        posts = pipeline._parse_patreon_rss(xml)
        self.assertEqual(len(posts), 1)
        self.assertEqual(posts[0]["episode_number"], "600")
        self.assertEqual(posts[0]["date"], "2026-08-03")
        self.assertEqual(posts[0]["duration"], "1:01:01")
        self.assertEqual(posts[0]["url"], "https://www.patreon.com/iMagazinePL/posts/600-afterparty-123456")

    def test_patreon_rss_rejects_external_source_url(self):
        xml = b'''<rss><channel><item>
          <title>601: (Afterparty) External</title>
          <pubDate>Mon, 10 Aug 2026 18:30:00 +0200</pubDate>
          <link>https://evil.example.invalid/post/601</link>
          <guid>https://evil.example.invalid/post/601</guid>
        </item></channel></rss>'''
        self.assertEqual(pipeline._parse_patreon_rss(xml), [])

    def test_patreon_rss_rejects_query_on_canonical_source_url(self):
        xml = b'''<rss><channel><item>
          <title>601: (Afterparty) Query</title>
          <pubDate>Mon, 10 Aug 2026 18:30:00 +0200</pubDate>
          <link>https://www.patreon.com/iMagazinePL/posts/601-afterparty-123456?x=1</link>
        </item></channel></rss>'''
        self.assertEqual(pipeline._parse_patreon_rss(xml), [])

    def test_retry_state_reader_rejects_symlink(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            outside = root / "outside.json"
            outside.write_text(json.dumps({
                "primary_date": "2026-08-08",
                "pending": True,
                "updated_at": "2026-08-08T00:00:00Z",
            }), encoding="utf-8")
            state = root / "retry-state.json"
            state.symlink_to(outside)
            with self.assertRaisesRegex(RuntimeError, "symlink"):
                pipeline.retry_is_due(state, date(2026, 8, 11))

    def test_browser_manifest_metadata_bypasses_blocked_patreon_page_fetch(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            manifest_path = Path(temp_dir) / "patreon_posts.json"
            manifest_path.write_text(json.dumps({
                "version": 2,
                "posts": [{
                    "episode": 600,
                    "slug": "600-afterparty-166017605",
                    "url": "https://www.patreon.com/iMagazinePL/posts/600-afterparty-166017605",
                    "title": "600: (Afterparty) Browser verified",
                    "date": "2026-08-07",
                    "duration": "0:53:55",
                }],
            }), encoding="utf-8")
            manifest = pipeline.load_patreon_manifest(manifest_path)
            self.assertEqual(manifest[0]["duration"], "0:53:55")
            with patch.object(pipeline, "PATREON_MANIFEST", manifest):
                with patch.object(
                    pipeline,
                    "_fetch_patreon_post_page",
                    side_effect=AssertionError("browser metadata should avoid HTTP page fetch"),
                ):
                    posts = pipeline._fetch_patreon_posts_from_list()
            self.assertEqual(posts[0]["episode_number"], 600)
            self.assertEqual(posts[0]["date"], "2026-08-07")

    def test_patreon_sources_merge_browser_manifest_with_rss(self):
        merged = pipeline._merge_patreon_posts(
            [{"episode_number": 600, "title": "RSS title", "date": "2026-08-07"}],
            [{"episode_number": 600, "title": "600: (Afterparty) Browser title", "duration": "0:53:55", "url": "https://www.patreon.com/iMagazinePL/posts/600-afterparty-166017605"}],
        )
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0]["title"], "600: (Afterparty) Browser title")
        self.assertEqual(merged[0]["duration"], "0:53:55")

    def test_patreon_merge_retains_source_url(self):
        with patch.object(
            pipeline,
            "fetch_patreon_posts",
            return_value=[{
                "episode_number": "600",
                "title": "600: (Afterparty) Test",
                "date": "2026-08-03",
                "duration": "1:01:01",
                "url": "https://www.patreon.com/iMagazinePL/posts/600-afterparty-123456",
            }],
        ):
            merged = pipeline.merge_patreon_episodes(set(), [])
        self.assertEqual(merged[0]["episode"], "600.5")
        self.assertEqual(merged[0]["url"], "https://www.patreon.com/iMagazinePL/posts/600-afterparty-123456")

    def test_retry_state_is_due_only_on_sunday_and_tuesday_after_friday_primary(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            state_path = Path(temp_dir) / "retry-state.json"
            primary_date = date(2026, 8, 14)  # Friday
            pipeline.write_retry_state(state_path, primary_date, pending=True, pending_commit="a" * 40)
            self.assertTrue(pipeline.retry_is_due(state_path, date(2026, 8, 16)))  # Sunday
            self.assertTrue(pipeline.retry_is_due(state_path, date(2026, 8, 18)))  # Tuesday
            self.assertFalse(pipeline.retry_is_due(state_path, date(2026, 8, 15)))
            self.assertFalse(pipeline.retry_is_due(state_path, date(2026, 8, 17)))
            self.assertFalse(pipeline.retry_is_due(state_path, date(2026, 8, 19)))
            pipeline.write_retry_state(state_path, primary_date, pending=False)
            self.assertFalse(pipeline.retry_is_due(state_path, date(2026, 8, 16)))
            pipeline.write_retry_state(state_path, date(2026, 8, 8), pending=True, pending_commit="b" * 40)
            self.assertFalse(pipeline.retry_is_due(state_path, date(2026, 8, 10)))

    def test_retry_state_writer_rejects_symlink_destination_and_uses_utc(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            outside = root / "outside.json"
            outside.write_text("untouched", encoding="utf-8")
            destination = root / "retry-state.json"
            destination.symlink_to(outside)
            with self.assertRaisesRegex(RuntimeError, "symlink"):
                pipeline.write_retry_state(destination, date(2026, 8, 7), pending=True, pending_commit="a" * 40)
            self.assertEqual(outside.read_text(encoding="utf-8"), "untouched")

            destination.unlink()
            pipeline.write_retry_state(destination, date(2026, 8, 7), pending=True, pending_commit="a" * 40)
            payload = json.loads(destination.read_text(encoding="utf-8"))
            self.assertTrue(payload["updated_at"].endswith("Z"))
            self.assertTrue(pipeline.retry_is_due(destination, date(2026, 8, 11)))

    def test_retry_state_writer_rejects_symlink_parent_before_chmod(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            outside = root / "outside"
            outside.mkdir()
            outside.chmod(0o755)
            linked_parent = root / "state"
            linked_parent.symlink_to(outside, target_is_directory=True)
            with self.assertRaisesRegex(RuntimeError, "symlink"):
                pipeline.write_retry_state(linked_parent / "retry-state.json", date(2026, 8, 8), pending=True, pending_commit="a" * 40)
            self.assertEqual(outside.stat().st_mode & 0o777, 0o755)

    def test_retry_state_rejects_non_utc_timestamp_and_unknown_fields(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "retry-state.json"
            path.write_text(json.dumps({
                "primary_date": "2026-08-08",
                "pending": True,
                "updated_at": "2026-08-08T12:00:00+02:00",
            }), encoding="utf-8")
            self.assertFalse(pipeline.retry_is_due(path, date(2026, 8, 11)))
            payload = json.loads(path.read_text(encoding="utf-8"))
            payload["updated_at"] = "2026-08-08T10:00:00Z"
            payload["unexpected"] = True
            path.write_text(json.dumps(payload), encoding="utf-8")
            self.assertFalse(pipeline.retry_is_due(path, date(2026, 8, 11)))

    def test_sync_to_obsidian_rejects_symlink_destination(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            vault = root / "vault"
            vault.mkdir()
            outside = root / "outside.md"
            outside.write_text("untouched", encoding="utf-8")
            archive = root / "archive.md"
            archive.write_text("archive", encoding="utf-8")
            stats = root / "stats.md"
            stats.write_text("stats", encoding="utf-8")
            (vault / "Nadgryzieni Episode Archive.md").symlink_to(outside)
            with patch.object(pipeline, "VAULT_DIR", vault), patch.object(pipeline, "ARCHIVE_PATH", archive), patch.object(pipeline, "STATS_PATH", stats):
                with self.assertRaisesRegex(RuntimeError, "symlink"):
                    pipeline.sync_to_obsidian()
            self.assertEqual(outside.read_text(encoding="utf-8"), "untouched")

    def test_source_url_fragment_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "fragment"):
            pipeline.canonical_url("https://retrorocketnetwork.pl/source#token=SECRET")

    def test_rss_items_reject_external_source_url(self):
        xml = b'''<rss><channel>
          <item>
            <title>602: External</title>
            <pubDate>Tue, 11 Aug 2026 18:30:00 +0200</pubDate>
            <link>https://evil.example.invalid/phish</link>
          </item>
          <item>
            <title>603: Allowed</title>
            <pubDate>Wed, 12 Aug 2026 18:30:00 +0200</pubDate>
            <link>https://retrorocketnetwork.pl/nadgryzieni-603</link>
          </item>
        </channel></rss>'''
        items = pipeline.parse_rss_items(xml)
        self.assertEqual([item["url"] for item in items], ["https://retrorocketnetwork.pl/nadgryzieni-603"])

    def test_patreon_manifest_rejects_valid_json_with_non_mapping_top_level(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "patreon_posts.json"
            for payload in ([], {"posts": {}}, {"posts": "not-a-list"}):
                path.write_text(json.dumps(payload), encoding="utf-8")
                self.assertEqual(pipeline.load_patreon_manifest(path), [])

    def test_source_url_query_is_rejected_and_patreon_path_is_exact(self):
        with self.assertRaisesRegex(ValueError, "query"):
            pipeline.canonical_url("https://www.example.com/source?token=%5BREDACTED%5D")
        with tempfile.TemporaryDirectory() as temp_dir:
            manifest_path = Path(temp_dir) / "patreon_posts.json"
            manifest_path.write_text(json.dumps({
                "version": 2,
                "posts": [{
                    "episode": 600,
                    "slug": "600-afterparty-166017605",
                    "url": "https://www.patreon.com/iMagazinePL/posts/600-afterparty-166017605?token=%5BREDACTED%5D",
                }],
            }), encoding="utf-8")
            self.assertEqual(pipeline.load_patreon_manifest(manifest_path), [])

    def test_push_failure_is_explicitly_pending(self):
        failed = subprocess.CompletedProcess(["git", "push"], 1, "", "remote token=[REDACTED]")
        with patch.object(pipeline.subprocess, "run", return_value=failed), patch.object(
            pipeline.time, "sleep"
        ), patch.object(pipeline, "_ensure_main_branch"):
            with self.assertRaises(pipeline.GitPushPendingError):
                pipeline._push_with_retries("/tmp/repo")

    def test_local_commits_ahead_reject_unrelated_paths_before_push(self):
        completed = subprocess.CompletedProcess(
            ["git", "diff"],
            0,
            stdout=b"README.md\0private.txt\0",
            stderr=b"",
        )
        with patch.object(pipeline.subprocess, "run", return_value=completed):
            with self.assertRaisesRegex(RuntimeError, "unrelated paths"):
                pipeline._ensure_ahead_commits_are_allowed("/tmp/repo", {"README.md"})

    def test_main_releases_pipeline_lock_after_run(self):
        with patch.object(pipeline, "acquire_pipeline_lock", return_value=True), patch.object(pipeline, "release_pipeline_lock") as release_lock, patch.object(pipeline, "run_pipeline", return_value=0), patch.object(sys, "argv", ["nadgryzieni_pipeline.py"]), patch.dict(os.environ, {"NADGRYZIENI_RUN_KIND": "manual"}):
            self.assertEqual(pipeline.main(), 0)
        release_lock.assert_called_once_with()

    def test_generated_data_validation_requires_unique_ids_and_urls(self):
        rows = [{
            "counter": "1",
            "episode": "601.5",
            "title": "601: (Afterparty) Test",
            "date": "2026-08-03",
            "duration": "1:01:01",
            "url": "https://www.patreon.com/iMagazinePL/posts/601-afterparty-123456",
        }]
        data = pipeline.generate_data_json(rows)
        pipeline.validate_generated_data(data, rows)
        self.assertEqual(data["stats"]["total_episodes"], 1)
        self.assertEqual(data["episodes"][0]["category"], "afterparty")
        self.assertEqual(data["episodes"][0]["url"], rows[0]["url"])

        invalid_identifier = [dict(rows[0])]
        invalid_data = pipeline.generate_data_json(invalid_identifier)
        invalid_data["episodes"][0]["episode"] = ""
        with self.assertRaises(ValueError):
            pipeline.validate_generated_data(invalid_data, invalid_identifier)

        missing_url = [dict(rows[0], url="")]
        with self.assertRaises(ValueError):
            pipeline.validate_generated_data(pipeline.generate_data_json(missing_url), missing_url)

    def test_generate_data_rejects_external_source_origin(self):
        row = {
            "counter": "1",
            "episode": "1",
            "title": "1: External source",
            "date": "2026-01-01",
            "duration": "1:00",
            "url": "https://evil.example.invalid/phish",
        }
        with self.assertRaisesRegex(ValueError, "origin/path"):
            pipeline.generate_data_json([row])

    def test_generated_data_rejects_unknown_schema_fields(self):
        row = {
            "counter": "1",
            "episode": "1",
            "title": "1: Schema",
            "date": "2026-01-01",
            "duration": "1:00",
            "url": "https://retrorocketnetwork.pl/schema",
            "hosts": ["Wojtek Pietrusiewicz"],
            "hosts_status": "verified",
            "hosts_source": "rrn",
            "hosts_source_url": "https://retrorocketnetwork.pl/schema",
        }
        generated = pipeline.generate_data_json([row])
        generated["guests"] = []
        with self.assertRaisesRegex(ValueError, "unknown"):
            pipeline.validate_generated_data(generated, [row])
        generated = pipeline.generate_data_json([row])
        generated["episodes"][0]["guest_hosts"] = []
        with self.assertRaisesRegex(ValueError, "unknown"):
            pipeline.validate_generated_data(generated, [row])

    def test_generated_data_rejects_mutated_fields_and_statistics(self):
        row = {
            "counter": "1",
            "episode": "1",
            "title": "1: Consistency",
            "date": "2026-01-01",
            "duration": "1:00",
            "url": "https://retrorocketnetwork.pl/consistency",
            "hosts": ["Wojtek Pietrusiewicz"],
            "hosts_status": "verified",
            "hosts_source": "rrn",
            "hosts_source_url": "https://retrorocketnetwork.pl/consistency",
        }
        original = pipeline.generate_data_json([row])
        for mutation in (
            lambda data: data["episodes"][0].update({"hosts": ["Other Person"]}),
            lambda data: data["episodes"][0].update({"minutes": 99}),
            lambda data: data["stats"].update({"average_duration": 99}),
        ):
            mutated = json.loads(json.dumps(original))
            mutation(mutated)
            with self.assertRaisesRegex(ValueError, "archive-derived"):
                pipeline.validate_generated_data(mutated, [row])

    def test_manifest_requires_exact_archive_record_keys_and_schema(self):
        row = {
            "counter": "1",
            "episode": "1",
            "title": "1: Manifest schema",
            "date": "2026-01-01",
            "duration": "1:00",
            "url": "https://retrorocketnetwork.pl/manifest-schema",
            "hosts": ["Wojtek Pietrusiewicz"],
            "hosts_status": "verified",
            "hosts_source": "rrn",
            "hosts_source_url": "https://retrorocketnetwork.pl/manifest-schema",
        }
        with self.assertRaisesRegex(ValueError, "key set"):
            pipeline.validate_manifest_integrity({"records": {}}, record_rows=[row])
        manifest = pipeline.manifest_from_rows([row], strict=True)
        manifest["records"][pipeline.build_record_key(row)]["guests"] = []
        with self.assertRaisesRegex(ValueError, "unknown"):
            pipeline.validate_manifest_integrity(manifest, record_rows=[row])

    def test_archive_writer_escapes_markdown_title_delimiters_and_newlines(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "archive.md"
            row = {
                "counter": "1",
                "episode": "1",
                "title": "1: Pipe | and\nnewline",
                "date": "2026-01-01",
                "duration": "1:00",
                "hosts": [],
            }
            pipeline.write_archive([row], target_path=path)
            content = path.read_text(encoding="utf-8")
            self.assertIn(r"Pipe \| and newline", content)
            parsed, _ = pipeline.parse_archive(path)
            self.assertEqual(parsed[0]["title"], "1: Pipe | and newline")

    def test_atomic_replace_group_publishes_all_files(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            stage_dir = Path(tempfile.mkdtemp(prefix=".nadgryzieni-pipeline-stage-", dir=str(root.parent)))
            target_a = root / "a.txt"
            target_b = root / "b.txt"
            staged_a = stage_dir / "a.stage"
            staged_b = stage_dir / "b.stage"
            target_a.write_text("old-a", encoding="utf-8")
            target_b.write_text("old-b", encoding="utf-8")
            staged_a.write_text("new-a", encoding="utf-8")
            staged_b.write_text("new-b", encoding="utf-8")
            with patch.object(pipeline, "REPO_DIR", root), patch.object(
                pipeline, "PUBLISH_PATHS", ["a.txt", "b.txt"]
            ):
                pipeline.atomic_replace_group([(target_a, staged_a), (target_b, staged_b)])
            self.assertEqual(target_a.read_text(encoding="utf-8"), "new-a")
            self.assertEqual(target_b.read_text(encoding="utf-8"), "new-b")
            stage_dir.rmdir()

    def test_atomic_replace_group_rolls_back_after_a_late_failure(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            stage_dir = Path(tempfile.mkdtemp(prefix=".nadgryzieni-pipeline-stage-", dir=str(root.parent)))
            target_a = root / "a.txt"
            target_b = root / "b.txt"
            staged_a = stage_dir / "a.stage"
            staged_b = stage_dir / "b.stage"
            target_a.write_text("old-a", encoding="utf-8")
            target_b.write_text("old-b", encoding="utf-8")
            staged_a.write_text("new-a", encoding="utf-8")
            staged_b.write_text("new-b", encoding="utf-8")
            original_install = pipeline._install_file_no_replace

            def fail_on_second_staged_file(source, destination, **kwargs):
                if Path(source).name == "b.stage":
                    raise OSError("simulated publication failure")
                return original_install(source, destination, **kwargs)

            with patch.object(pipeline, "REPO_DIR", root), patch.object(
                pipeline, "PUBLISH_PATHS", ["a.txt", "b.txt"]
            ), patch.object(
                pipeline, "_install_file_no_replace", side_effect=fail_on_second_staged_file
            ):
                with self.assertRaises(OSError):
                    pipeline.atomic_replace_group([(target_a, staged_a), (target_b, staged_b)])
            self.assertEqual(target_a.read_text(encoding="utf-8"), "old-a")
            self.assertEqual(target_b.read_text(encoding="utf-8"), "old-b")

    def test_atomic_replace_group_recovers_prepared_journal(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            interrupted_stage_dir = Path(tempfile.mkdtemp(prefix=".nadgryzieni-pipeline-stage-", dir=str(root.parent)))
            final_stage_dir = Path(tempfile.mkdtemp(prefix=".nadgryzieni-pipeline-stage-", dir=str(root.parent)))
            target = root / "a.txt"
            interrupted_staged = interrupted_stage_dir / "a.interrupted"
            final_staged = final_stage_dir / "a.stage"
            backup_dir = root / ".nadgryzieni-publication-backup-test"
            backup_dir.mkdir()
            backup = backup_dir / "0.bak"
            target.write_text("old-a", encoding="utf-8")
            target_identity = {"dev": target.stat().st_dev, "ino": target.stat().st_ino}
            interrupted_staged.write_text("interrupted-stage", encoding="utf-8")
            target.unlink()
            os.link(interrupted_staged, target)
            final_staged.write_text("final-new", encoding="utf-8")
            backup.write_text("old-a", encoding="utf-8")
            journal_path = root / "journal.json"
            journal_path.write_text(json.dumps({
                "phase": "prepared",
                "backup_dir": str(backup_dir),
                "entries": [{
                    "target": str(target),
                    "staged": str(interrupted_staged),
                    "backup": str(backup),
                    "target_existed": True,
                    "target_identity": target_identity,
                    "backup_identity": {"dev": backup.stat().st_dev, "ino": backup.stat().st_ino},
                    "staged_identity": {"dev": interrupted_staged.stat().st_dev, "ino": interrupted_staged.stat().st_ino},
                }],
            }), encoding="utf-8")
            with patch.object(pipeline, "STATE_DIR", root), patch.object(
                pipeline, "REPO_DIR", root
            ), patch.object(
                pipeline, "PUBLISH_PATHS", ["a.txt"]
            ), patch.object(pipeline, "PUBLICATION_JOURNAL_PATH", journal_path):
                pipeline.atomic_replace_group([(target, final_staged)])
            self.assertEqual(target.read_text(encoding="utf-8"), "final-new")
            self.assertFalse(journal_path.exists())
            self.assertFalse(backup_dir.exists())
            final_stage_dir.rmdir()

    def test_atomic_replace_group_rejects_journal_path_traversal(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            outside = root.parent / f"nadgryzieni-outside-{root.name}.txt"
            outside.write_text("must-remain", encoding="utf-8")
            journal_path = root / "journal.json"
            backup_dir = root / ".nadgryzieni-publication-backup-unsafe"
            journal_path.write_text(json.dumps({
                "phase": "prepared",
                "backup_dir": str(backup_dir),
                "entries": [{
                    "target": str(outside),
                    "staged": str(root / "stage"),
                    "backup": str(backup_dir / "0.bak"),
                    "target_existed": True,
                    "target_identity": {"dev": 0, "ino": 0},
                    "backup_identity": None,
                    "staged_identity": {"dev": 0, "ino": 0},
                }],
            }), encoding="utf-8")
            target = root / "target.txt"
            staged = root / "target.stage"
            staged.write_text("new", encoding="utf-8")
            with patch.object(pipeline, "STATE_DIR", root), patch.object(
                pipeline, "REPO_DIR", root
            ), patch.object(pipeline, "PUBLICATION_JOURNAL_PATH", journal_path):
                with self.assertRaisesRegex(RuntimeError, "unsafe"):
                    pipeline.atomic_replace_group([(target, staged)])
            self.assertEqual(outside.read_text(encoding="utf-8"), "must-remain")

    def test_atomic_replace_group_rejects_new_outside_target(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            outside = root.parent / f"nadgryzieni-new-outside-{root.name}.txt"
            outside.write_text("must-remain", encoding="utf-8")
            stage_dir = Path(tempfile.mkdtemp(prefix=".nadgryzieni-pipeline-stage-", dir=str(root.parent)))
            staged = stage_dir / "new.txt"
            staged.write_text("new", encoding="utf-8")
            with patch.object(pipeline, "REPO_DIR", root):
                with self.assertRaisesRegex(ValueError, "unsafe"):
                    pipeline.atomic_replace_group([(outside, staged)])
            self.assertEqual(outside.read_text(encoding="utf-8"), "must-remain")
            staged.unlink()
            stage_dir.rmdir()

    def test_recovery_rejects_symlinked_journal(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            state_dir = root / "state"
            state_dir.mkdir()
            outside = root.parent / f"nadgryzieni-journal-file-{root.name}.json"
            outside.write_text("{}", encoding="utf-8")
            journal_path = state_dir / "journal.json"
            journal_path.symlink_to(outside)
            with patch.object(pipeline, "STATE_DIR", state_dir), patch.object(
                pipeline, "PUBLICATION_JOURNAL_PATH", journal_path
            ):
                with self.assertRaisesRegex(RuntimeError, "symlink"):
                    pipeline._recover_publication_journal()
            self.assertTrue(journal_path.is_symlink())
            outside.unlink()

    def test_recovery_rejects_nonregular_journal(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            state_dir = root / "state"
            state_dir.mkdir()
            journal_path = state_dir / "journal.json"
            journal_path.mkdir()
            with patch.object(pipeline, "PUBLICATION_JOURNAL_PATH", journal_path):
                with self.assertRaisesRegex(RuntimeError, "regular file"):
                    pipeline._recover_publication_journal()
            journal_path.rmdir()

    def test_journal_rejects_nested_target_path(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            state_dir = root / "state"
            state_dir.mkdir()
            journal_path = state_dir / "journal.json"
            backup_dir = root / ".nadgryzieni-publication-backup-nested-test"
            backup_dir.mkdir()
            interrupted_stage_dir = Path(tempfile.mkdtemp(
                prefix=".nadgryzieni-pipeline-stage-", dir=str(root.parent)
            ))
            replacement_stage_dir = Path(tempfile.mkdtemp(
                prefix=".nadgryzieni-pipeline-stage-", dir=str(root.parent)
            ))
            nested_target = root / "nested" / "target.txt"
            interrupted_staged = interrupted_stage_dir / "target.txt"
            replacement_target = root / "replacement.txt"
            replacement_staged = replacement_stage_dir / "replacement.txt"
            replacement_staged.write_text("replacement", encoding="utf-8")
            journal_path.write_text(json.dumps({
                "phase": "prepared",
                "backup_dir": str(backup_dir),
                "entries": [{
                    "target": str(nested_target),
                    "staged": str(interrupted_staged),
                    "backup": None,
                    "target_existed": False,
                    "target_identity": None,
                    "backup_identity": None,
                    "staged_identity": {"dev": 0, "ino": 0},
                }],
            }), encoding="utf-8")
            with patch.object(pipeline, "REPO_DIR", root), patch.object(
                pipeline, "STATE_DIR", state_dir
            ), patch.object(pipeline, "PUBLICATION_JOURNAL_PATH", journal_path):
                with self.assertRaisesRegex(RuntimeError, "unsafe"):
                    pipeline.atomic_replace_group([(replacement_target, replacement_staged)])
            self.assertTrue(journal_path.exists())
            replacement_staged.unlink()
            replacement_stage_dir.rmdir()
            interrupted_stage_dir.rmdir()
            backup_dir.rmdir()

    def test_journal_rejects_duplicate_backup_paths(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            backup_dir = root / ".nadgryzieni-publication-backup-duplicate-test"
            backup_dir.mkdir()
            backup = backup_dir / "0.bak"
            backup.write_text("old", encoding="utf-8")
            stage_dir = Path(tempfile.mkdtemp(
                prefix=".nadgryzieni-pipeline-stage-", dir=str(root.parent)
            ))
            targets = [root / "first.txt", root / "second.txt"]
            staged = [stage_dir / "first.txt", stage_dir / "second.txt"]
            for target in targets:
                target.write_text("new", encoding="utf-8")
            for item in staged:
                item.write_text("stale", encoding="utf-8")
            entries = [
                {
                    "target": str(target),
                    "staged": str(item),
                    "backup": str(backup),
                    "target_existed": True,
                    "target_identity": {"dev": 0, "ino": 0},
                    "backup_identity": {"dev": backup.stat().st_dev, "ino": backup.stat().st_ino},
                    "staged_identity": {"dev": 0, "ino": 0},
                }
                for target, item in zip(targets, staged)
            ]
            with patch.object(pipeline, "REPO_DIR", root), patch.object(
                pipeline, "PUBLISH_PATHS", ["first.txt", "second.txt"]
            ):
                with self.assertRaisesRegex(ValueError, "duplicate backup"):
                    pipeline._validate_publication_journal({
                        "phase": "prepared",
                        "backup_dir": str(backup_dir),
                        "entries": entries,
                    })
            for item in staged:
                item.unlink()
            stage_dir.rmdir()
            for target in targets:
                target.unlink()
            backup.unlink()
            backup_dir.rmdir()

    def test_journal_rejects_stage_file_in_target_directory(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            state_dir = root / "state"
            state_dir.mkdir()
            journal_path = state_dir / "journal.json"
            backup_dir = root / ".nadgryzieni-publication-backup-stage-test"
            backup_dir.mkdir()
            target = root / "target.txt"
            staged = root / "target.interrupted"
            backup = backup_dir / "0.bak"
            target.write_text("new", encoding="utf-8")
            staged.write_text("stale", encoding="utf-8")
            backup.write_text("old", encoding="utf-8")
            journal_path.write_text(json.dumps({
                "phase": "prepared",
                "backup_dir": str(backup_dir),
                "entries": [{
                    "target": str(target),
                    "staged": str(staged),
                    "backup": str(backup),
                    "target_existed": True,
                    "target_identity": {"dev": 0, "ino": 0},
                    "backup_identity": None,
                    "staged_identity": {"dev": 0, "ino": 0},
                }],
            }), encoding="utf-8")
            replacement = root / "replacement.stage"
            replacement.write_text("replacement", encoding="utf-8")
            with patch.object(pipeline, "REPO_DIR", root), patch.object(
                pipeline, "STATE_DIR", state_dir
            ), patch.object(pipeline, "PUBLICATION_JOURNAL_PATH", journal_path):
                with self.assertRaisesRegex(RuntimeError, "unsafe"):
                    pipeline.atomic_replace_group([(root / "replacement.txt", replacement)])
            self.assertTrue(journal_path.exists())
            self.assertEqual(target.read_text(encoding="utf-8"), "new")

    def test_journal_rejects_missing_backup_when_target_is_missing(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            state_dir = root / "state"
            state_dir.mkdir()
            journal_path = state_dir / "journal.json"
            backup_dir = root / ".nadgryzieni-publication-backup-missing-test"
            backup_dir.mkdir()
            stage_dir = Path(tempfile.mkdtemp(prefix=".nadgryzieni-pipeline-stage-", dir=str(root.parent)))
            staged = stage_dir / "target.txt"
            staged.write_text("stale", encoding="utf-8")
            target = root / "missing.txt"
            backup = backup_dir / "0.bak"
            journal_path.write_text(json.dumps({
                "phase": "prepared",
                "backup_dir": str(backup_dir),
                "entries": [{
                    "target": str(target),
                    "staged": str(staged),
                    "backup": str(backup),
                    "target_existed": True,
                    "target_identity": {"dev": 0, "ino": 0},
                    "backup_identity": None,
                    "staged_identity": {"dev": 0, "ino": 0},
                }],
            }), encoding="utf-8")
            final_stage_dir = Path(tempfile.mkdtemp(prefix=".nadgryzieni-pipeline-stage-", dir=str(root.parent)))
            replacement = final_stage_dir / "replacement.txt"
            replacement.write_text("replacement", encoding="utf-8")
            with patch.object(pipeline, "REPO_DIR", root), patch.object(
                pipeline, "STATE_DIR", state_dir
            ), patch.object(pipeline, "PUBLICATION_JOURNAL_PATH", journal_path):
                with self.assertRaisesRegex(RuntimeError, "unsafe"):
                    pipeline.atomic_replace_group([(root / "replacement.txt", replacement)])
            self.assertTrue(journal_path.exists())
            self.assertFalse(target.exists())
            staged.unlink()
            stage_dir.rmdir()
            replacement.unlink()
            final_stage_dir.rmdir()

    def test_journal_rejects_missing_backup_when_target_exists(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            state_dir = root / "state"
            state_dir.mkdir()
            journal_path = state_dir / "journal.json"
            backup_dir = root / ".nadgryzieni-publication-backup-existing-missing-test"
            backup_dir.mkdir()
            target = root / "existing.txt"
            target.write_text("current", encoding="utf-8")
            staged = Path(tempfile.mkdtemp(prefix=".nadgryzieni-pipeline-stage-", dir=str(root.parent))) / "target.txt"
            staged.write_text("stale", encoding="utf-8")
            journal_path.write_text(json.dumps({
                "phase": "prepared",
                "backup_dir": str(backup_dir),
                "entries": [{
                    "target": str(target),
                    "staged": str(staged),
                    "backup": str(backup_dir / "0.bak"),
                    "target_existed": True,
                    "target_identity": {"dev": 0, "ino": 0},
                    "backup_identity": None,
                    "staged_identity": {"dev": 0, "ino": 0},
                }],
            }), encoding="utf-8")
            replacement_dir = Path(tempfile.mkdtemp(prefix=".nadgryzieni-pipeline-stage-", dir=str(root.parent)))
            replacement = replacement_dir / "replacement.txt"
            replacement.write_text("replacement", encoding="utf-8")
            with patch.object(pipeline, "REPO_DIR", root), patch.object(
                pipeline, "STATE_DIR", state_dir
            ), patch.object(pipeline, "PUBLICATION_JOURNAL_PATH", journal_path):
                with self.assertRaisesRegex(RuntimeError, "unsafe"):
                    pipeline.atomic_replace_group([(root / "replacement.txt", replacement)])
            self.assertTrue(journal_path.exists())
            staged.unlink()
            staged.parent.rmdir()
            replacement.unlink()
            replacement_dir.rmdir()

    def test_journal_rejects_symlinked_backup_directory(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            state_dir = root / "state"
            state_dir.mkdir()
            journal_path = state_dir / "journal.json"
            outside = root.parent / f"nadgryzieni-journal-outside-{root.name}"
            outside.mkdir()
            (outside / "0.bak").write_text("outside", encoding="utf-8")
            backup_dir = root / ".nadgryzieni-publication-backup-symlink-test"
            backup_dir.symlink_to(outside, target_is_directory=True)
            stage_dir = Path(tempfile.mkdtemp(prefix=".nadgryzieni-pipeline-stage-", dir=str(root.parent)))
            staged = stage_dir / "target.txt"
            staged.write_text("stale", encoding="utf-8")
            target = root / "target.txt"
            target.write_text("current", encoding="utf-8")
            backup = backup_dir / "0.bak"
            journal_path.write_text(json.dumps({
                "phase": "prepared",
                "backup_dir": str(backup_dir),
                "entries": [{
                    "target": str(target),
                    "staged": str(staged),
                    "backup": str(backup),
                    "target_existed": True,
                    "target_identity": {"dev": 0, "ino": 0},
                    "backup_identity": None,
                    "staged_identity": {"dev": 0, "ino": 0},
                }],
            }), encoding="utf-8")
            final_stage_dir = Path(tempfile.mkdtemp(prefix=".nadgryzieni-pipeline-stage-", dir=str(root.parent)))
            replacement = final_stage_dir / "replacement.txt"
            replacement.write_text("replacement", encoding="utf-8")
            with patch.object(pipeline, "REPO_DIR", root), patch.object(
                pipeline, "STATE_DIR", state_dir
            ), patch.object(pipeline, "PUBLICATION_JOURNAL_PATH", journal_path):
                with self.assertRaisesRegex(RuntimeError, "unsafe"):
                    pipeline.atomic_replace_group([(root / "replacement.txt", replacement)])
            self.assertEqual(target.read_text(encoding="utf-8"), "current")
            self.assertTrue((outside / "0.bak").exists())
            staged.unlink()
            stage_dir.rmdir()
            replacement.unlink()
            final_stage_dir.rmdir()

    def test_run_pipeline_recovers_journal_before_noop_exit(self):
        with patch.object(pipeline, "_recover_publication_journal") as recover, patch.object(
            pipeline, "fetch_rss", return_value=b"<rss/>"
        ), patch.object(pipeline, "parse_rss_items", return_value=[]), patch.object(
            pipeline, "merge_patreon_episodes", return_value=[]
        ):
            self.assertEqual(pipeline.run_pipeline(), 0)
        recover.assert_called_once_with()

    def test_noop_pipeline_validates_existing_data_and_host_manifest(self):
        with patch.object(pipeline, "_recover_publication_journal"), patch.object(
            pipeline, "fetch_rss", return_value=b"<rss/>"
        ), patch.object(pipeline, "parse_rss_items", return_value=[]), patch.object(
            pipeline, "merge_patreon_episodes", return_value=[]
        ), patch.object(pipeline, "validate_generated_data") as validate_data, patch.object(
            pipeline, "load_host_metadata"
        ) as load_manifest:
            self.assertEqual(pipeline.run_pipeline(), 0)
        validate_data.assert_called_once()
        load_manifest.assert_called_once()

    def test_new_records_validate_against_existing_host_manifest_before_enrichment(self):
        existing_row = {
            "counter": "1",
            "episode": "602",
            "title": "602: Existing episode",
            "date": "2026-08-21",
            "duration": "1:00:00",
            "url": "https://retrorocketnetwork.pl/602-existing-episode",
        }
        new_item = {
            "episode_number": "603",
            "title": "603: New episode",
            "date": "2026-08-28",
            "duration": "2:44:21",
            "url": "https://retrorocketnetwork.pl/603-new-episode",
        }

        class StopAfterManifestLoad(Exception):
            pass

        def load_existing_manifest(*, record_rows=None, **_kwargs):
            rows = record_rows or []
            self.assertEqual([row["episode"] for row in rows], ["602"])
            raise StopAfterManifestLoad

        with patch.object(pipeline, "acquire_pipeline_lock", return_value=True), patch.object(
            pipeline, "release_pipeline_lock"
        ), patch.object(pipeline, "_recover_publication_journal"), patch.object(
            pipeline, "fetch_rss", return_value=b"<rss/>"
        ), patch.object(pipeline, "parse_rss_items", return_value=[new_item]), patch.object(
            pipeline, "parse_archive", return_value=([existing_row], "")
        ), patch.object(pipeline, "attach_existing_data"), patch.object(
            pipeline, "resolve_existing_source_url", return_value=new_item["url"]
        ), patch.object(pipeline, "merge_patreon_episodes", return_value=[]), patch.object(
            pipeline, "_entry_exists_verified", return_value=True
        ), patch.object(pipeline, "load_host_metadata", side_effect=load_existing_manifest):
            with self.assertRaises(StopAfterManifestLoad):
                pipeline.run_pipeline()

    def test_main_recovers_journal_before_retry_noop_exit(self):
        with patch.object(pipeline, "acquire_pipeline_lock", return_value=True), patch.object(
            pipeline, "release_pipeline_lock"
        ) as release_lock, patch.object(pipeline, "_recover_publication_journal") as recover, patch.object(
            pipeline, "retry_is_due", return_value=False
        ), patch.object(sys, "argv", ["nadgryzieni_pipeline.py"]), patch.dict(
            os.environ, {"NADGRYZIENI_RUN_KIND": "retry"}
        ), patch.object(pipeline, "run_pipeline") as run:
            self.assertEqual(pipeline.main(), 0)
        recover.assert_called_once_with()
        run.assert_not_called()
        release_lock.assert_called_once_with()

    def test_git_path_normalization_rejects_pathspec_magic(self):
        for unsafe in ("*.json", "file?.json", "[ab].json", ":(glob)*"):
            with self.subTest(unsafe=unsafe):
                with self.assertRaises(ValueError):
                    pipeline._normalise_git_paths([unsafe])

    def test_archive_upcoming_and_woz_invariants_are_preserved(self):
        data = json.loads((REPO_DIR / "data.json").read_text(encoding="utf-8"))
        upcoming = json.loads((REPO_DIR / "upcoming.json").read_text(encoding="utf-8"))
        episode_ids = {str(row.get("episode")) for row in data["episodes"]}
        self.assertEqual(len(data["episodes"]), 580)
        self.assertTrue({"604", "604.5"}.issubset(episode_ids))
        self.assertEqual(
            [event["video_id"] for event in upcoming["events"]],
            ["BOjDA-SWz1o", "aMhP8OXc1oU", "TJYoiwAnX6I"],
        )
        self.assertEqual(
            [event["scheduled_start_utc"] for event in upcoming["events"]],
            [
                "2026-09-09T18:30:00Z",
                "2026-09-10T15:00:00Z",
                "2026-09-11T07:00:00Z",
            ],
        )
        episode_72 = next(row for row in data["episodes"] if str(row["episode"]) == "72")
        self.assertIn('Steve "Woz" Wozniak', episode_72["hosts"])

    def test_host_audit_stage_journal_recovers_before_next_publication(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            state_dir = root / "state"
            state_dir.mkdir()
            journal_path = state_dir / "journal.json"
            lock_path = state_dir / "lock"
            target = root / "target.txt"
            target.write_text("old", encoding="utf-8")
            target_identity = {"dev": target.stat().st_dev, "ino": target.stat().st_ino}
            backup_dir = root / ".nadgryzieni-publication-backup-host-test"
            backup_dir.mkdir()
            backup = backup_dir / "0.bak"
            backup.write_text("old", encoding="utf-8")
            host_stage = Path(tempfile.mkdtemp(prefix=".nadgryzieni-hosts-stage-", dir=str(root.parent)))
            staged = host_stage / "target.txt"
            staged.write_text("stale", encoding="utf-8")
            target.unlink()
            os.link(staged, target)
            journal_path.write_text(json.dumps({
                "phase": "prepared",
                "backup_dir": str(backup_dir),
                "entries": [{
                    "target": str(target),
                    "staged": str(staged),
                    "backup": str(backup),
                    "target_existed": True,
                    "target_identity": target_identity,
                    "backup_identity": {"dev": backup.stat().st_dev, "ino": backup.stat().st_ino},
                    "staged_identity": {"dev": staged.stat().st_dev, "ino": staged.stat().st_ino},
                }],
            }), encoding="utf-8")
            with patch.object(pipeline, "REPO_DIR", root), patch.object(
                pipeline, "STATE_DIR", state_dir
            ), patch.object(
                pipeline, "PUBLISH_PATHS", ["target.txt"]
            ), patch.object(pipeline, "PUBLICATION_JOURNAL_PATH", journal_path), patch.object(
                pipeline, "LOCK_PATH", lock_path
            ), patch.object(pipeline, "_LOCK_HANDLE", None):
                pipeline._recover_publication_journal()
            self.assertEqual(target.read_text(encoding="utf-8"), "old")
            self.assertFalse(journal_path.exists())
            self.assertFalse(host_stage.exists())

    def test_atomic_replace_group_requires_shared_publication_lock(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            target = root / "target.txt"
            staged = root / "target.stage"
            staged.write_text("new", encoding="utf-8")
            with patch.object(pipeline, "acquire_pipeline_lock", return_value=False):
                with self.assertRaisesRegex(RuntimeError, "publication lock"):
                    pipeline.atomic_replace_group([(target, staged)])
            self.assertFalse(target.exists())

    def test_git_publication_rejects_unrelated_staged_paths(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            def git(*args):
                return subprocess.run(
                    ["git", *args], cwd=root, check=True, capture_output=True, text=True
                )
            git("init", "-q")
            git("config", "user.email", "test@example.invalid")
            git("config", "user.name", "Test")
            (root / "upcoming.json").write_text("old\n", encoding="utf-8")
            malicious = "password=PATH_SECRET"
            (root / malicious).write_text("old\n", encoding="utf-8")
            git("add", ".")
            git("commit", "-qm", "initial")
            (root / "upcoming.json").write_text("new\n", encoding="utf-8")
            (root / malicious).write_text("new\n", encoding="utf-8")
            git("add", malicious)
            with patch.object(pipeline, "REPO_DIR", root):
                with self.assertRaisesRegex(RuntimeError, "unrelated") as caught:
                    pipeline.git_commit_and_push_paths("unsafe", ["upcoming.json"])
            self.assertNotIn("PATH_SECRET", str(caught.exception))
            self.assertEqual(git("rev-list", "--count", "HEAD").stdout.strip(), "1")

    def test_git_output_redacts_urls_and_credential_like_values(self):
        output = pipeline._redact_git_output(
            "https://token@example.invalid/repo?password=secret ssh://user:pass@example.invalid/x "
            "git+ssh://user:pass@example.invalid/repo git@github.example:org/repo "
                        "Authorization: Bearer *** password=\"two word secret\" "
            "Authorization: Bearer \"two word secret\" token=abc123"
        )
        self.assertNotIn("token@example", output)
        self.assertNotIn("password=secret", output)
        self.assertNotIn("ssh://user:pass", output)
        self.assertNotIn("git+ssh://user:pass", output)
        self.assertNotIn("two word secret", output)
        self.assertNotIn("word secret", output)
        self.assertNotIn("bearer-secret", output)
        self.assertNotIn("git@github.example", output)
        self.assertNotIn("token=abc123", output)
        self.assertGreaterEqual(output.count("[REDACTED"), 2)

    def test_git_output_redacts_quoted_credentials_and_password_prompts(self):
        output = pipeline._redact_git_output(
            '\\"access_token\\": \\"ACCESS_SENTINEL\\", '
            "client_secret='CLIENT_SENTINEL' refresh_token=REFRESH_SENTINEL "
            "Bearer BEARER_SENTINEL Password for https://example.invalid:"
        )
        self.assertNotIn("ACCESS_SENTINEL", output)
        self.assertNotIn("CLIENT_SENTINEL", output)
        self.assertNotIn("REFRESH_SENTINEL", output)
        self.assertIn('access_token', output)
        self.assertIn('Password for [REDACTED]', output)
        self.assertNotIn('BEARER_SENTINEL', output)

    def test_git_output_redacts_non_bearer_authorization_and_deeply_escaped_credentials(self):
        output = pipeline._redact_git_output(
            'Authorization: Digest username="AUTH_USER_SENTINEL", response="AUTH_RESPONSE_SENTINEL"\n'
            r'\\"access_token\\": \\"DEEP_ACCESS_SENTINEL\\"'
        )
        for sentinel in (
            'AUTH_USER_SENTINEL',
            'AUTH_RESPONSE_SENTINEL',
            'DEEP_ACCESS_SENTINEL',
        ):
            self.assertNotIn(sentinel, output)

    def test_git_output_redacts_generic_credential_marker(self):
        output = pipeline._redact_git_output(
            "remote rejected: credential: raw-secret-value; credentials: another-secret"
        )
        self.assertNotIn("raw-secret-value", output)
        self.assertNotIn("another-secret", output)
        self.assertIn("credential: [REDACTED]", output)

    def test_audit_report_sanitizes_urls_and_diagnostics_before_publication(self):
        report = {
            "records": {
                "rk": {
                    "hosts_source_url": "https://user:pass@example.invalid/post?token=SECRET_SENTINEL",
                    "provenance": {"source_url": "git@github.example:org/repo"},
                    "diagnostics": ["password=\"multi word SECRET_SENTINEL\""],
                }
            }
        }
        sanitized = host_tools._sanitize_audit_report(report)
        encoded = json.dumps(sanitized, ensure_ascii=False)
        self.assertNotIn("SECRET_SENTINEL", encoded)
        self.assertNotIn("user:pass@", encoded)
        self.assertEqual(sanitized["records"]["rk"]["hosts_source_url"], "")
        self.assertEqual(sanitized["records"]["rk"]["provenance"]["source_url"], "")

    def test_pipeline_reader_rejects_fifo_without_blocking(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            fifo = root / "fifo"
            os.mkfifo(fifo)
            with self.assertRaisesRegex(RuntimeError, "regular"):
                pipeline._read_bytes_secure(fifo, root)

    def test_host_metadata_redacts_diagnostics_and_audit_before_write(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "host_metadata.json"
            source_url = "https://example.invalid/episode/1"
            manifest = {"records": {
                "rk_host": {
                    "hosts": ["Host"],
                    "hosts_status": "verified",
                    "hosts_source": "rrn",
                    "hosts_source_url": source_url,
                    "provenance": {"kind": "direct_source", "source_url": source_url},
                    "diagnostics": ["credentialData=HOST_DIAGNOSTIC_SECRET"],
                    "audit": {
                        "privateKeyMaterial": "HOST_AUDIT_SECRET",
                        "safe": "retained",
                    },
                },
            }}
            pipeline.write_host_metadata(manifest, path=path)
            encoded = path.read_text(encoding="utf-8")
            self.assertNotIn("HOST_DIAGNOSTIC_SECRET", encoded)
            self.assertNotIn("HOST_AUDIT_SECRET", encoded)
            self.assertIn("[REDACTED]", encoded)
            self.assertIn("retained", encoded)

    def test_parse_archive_rejects_fifo_without_blocking(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            fifo = Path(temp_dir) / "archive.md"
            os.mkfifo(fifo)
            code = (
                "import sys; from pathlib import Path; "
                "import nadgryzieni_pipeline as p; p.parse_archive(Path(sys.argv[1]))"
            )
            process = subprocess.Popen(
                [sys.executable, "-c", code, str(fifo)],
                cwd=str(REPO_DIR),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            try:
                _, stderr = process.communicate(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()
                process.communicate()
                self.fail("parse_archive blocked on a FIFO")
            self.assertNotEqual(process.returncode, 0)
            self.assertIn("regular", stderr.lower())

    def test_load_host_metadata_rejects_fifo_without_blocking(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            fifo = Path(temp_dir) / "host_metadata.json"
            os.mkfifo(fifo)
            code = (
                "import sys; from pathlib import Path; "
                "import nadgryzieni_pipeline as p; p.load_host_metadata(Path(sys.argv[1]))"
            )
            process = subprocess.Popen(
                [sys.executable, "-c", code, str(fifo)],
                cwd=str(REPO_DIR),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            try:
                _, stderr = process.communicate(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()
                process.communicate()
                self.fail("load_host_metadata blocked on a FIFO")
            self.assertNotEqual(process.returncode, 0)
            self.assertIn("regular", stderr.lower())

    def test_noop_pipeline_rejects_fifo_data_without_blocking(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            fifo = root / "data.json"
            os.mkfifo(fifo)
            state_dir = root / "state"
            state_dir.mkdir()
            code = (
                "import sys; from pathlib import Path; "
                "import nadgryzieni_pipeline as p; "
                "p.REPO_DIR=Path(sys.argv[2]); p.DATA_JSON_PATH=Path(sys.argv[1]); "
                "p.STATE_DIR=Path(sys.argv[3]); p.PUBLICATION_JOURNAL_PATH=Path(sys.argv[3])/'journal.json'; "
                "p._recover_publication_journal=lambda: None; p.fetch_rss=lambda: b''; "
                "p.parse_rss_items=lambda _: []; p.run_pipeline()"
            )
            process = subprocess.Popen(
                [sys.executable, "-c", code, str(fifo), str(root), str(state_dir)],
                cwd=str(REPO_DIR),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            try:
                _, stderr = process.communicate(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()
                process.communicate()
                self.fail("no-op pipeline blocked on a FIFO data artifact")
            self.assertNotEqual(process.returncode, 0)
            self.assertIn("regular", stderr.lower())

    def test_pipeline_lock_rejects_fifo_without_blocking(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            state_dir = Path(temp_dir) / "state"
            state_dir.mkdir()
            lock_path = state_dir / "pipeline.lock"
            os.mkfifo(lock_path)
            with patch.object(pipeline, "STATE_DIR", state_dir), patch.object(
                pipeline, "LOCK_PATH", lock_path
            ), patch.object(pipeline, "_LOCK_HANDLE", None), patch.object(
                pipeline, "_LOCK_OWNER", None
            ), patch.object(pipeline, "_LOCK_DEPTH", 0):
                with self.assertRaisesRegex(RuntimeError, "regular"):
                    pipeline.acquire_pipeline_lock()

    def test_patreon_manifest_rejects_fifo_without_blocking(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            fifo = Path(temp_dir) / "patreon_posts.json"
            os.mkfifo(fifo)
            code = (
                "import sys; from pathlib import Path; "
                "import nadgryzieni_pipeline as p; p.load_patreon_manifest(Path(sys.argv[1]))"
            )
            process = subprocess.Popen(
                [sys.executable, "-c", code, str(fifo)],
                cwd=str(REPO_DIR),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            try:
                _, stderr = process.communicate(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()
                process.communicate()
                self.fail("load_patreon_manifest blocked on a FIFO")
            self.assertNotEqual(process.returncode, 0)
            self.assertIn("regular", stderr.lower())

    def test_audit_reader_rejects_fifo_without_blocking(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            fifo = Path(temp_dir) / "audit.json"
            os.mkfifo(fifo)
            code = (
                "import sys; from pathlib import Path; "
                "import nadgryzieni_hosts as h; h.apply_audit(Path(sys.argv[1]), dry_run=True)"
            )
            process = subprocess.Popen(
                [sys.executable, "-c", code, str(fifo)],
                cwd=str(REPO_DIR),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            try:
                _, stderr = process.communicate(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()
                process.communicate()
                self.fail("apply_audit blocked on a FIFO")
            self.assertNotEqual(process.returncode, 0)
            self.assertIn("regular", stderr.lower())

    def test_readme_reader_rejects_fifo_without_blocking(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            fifo = root / "README.md"
            os.mkfifo(fifo)
            code = (
                "import sys; from pathlib import Path; "
                "import nadgryzieni_pipeline as p; p.REPO_DIR=Path(sys.argv[2]); "
                "p.README_PATH=Path(sys.argv[1]); p.update_readme({'stats': {"
                "'total_episodes': 1, 'total_listening_hours': 1, "
                "'average_duration': 1, 'max_duration': 1}})"
            )
            process = subprocess.Popen(
                [sys.executable, "-c", code, str(fifo), str(root)],
                cwd=str(REPO_DIR),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            try:
                _, stderr = process.communicate(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()
                process.communicate()
                self.fail("update_readme blocked on a FIFO")
            self.assertNotEqual(process.returncode, 0)
            self.assertIn("regular", stderr.lower())

    def test_publication_rejects_target_created_after_journal_without_overwrite(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            stage_dir = Path(tempfile.mkdtemp(prefix=".nadgryzieni-pipeline-stage-", dir=str(root.parent)))
            staged = stage_dir / "new.txt"
            staged.write_text("generated", encoding="utf-8")
            target = root / "new.txt"
            state_dir = root / "state"
            state_dir.mkdir()
            journal_path = state_dir / "journal.json"
            created = False

            def write_journal(payload):
                nonlocal created
                if payload.get("phase") == "prepared" and not created:
                    target.write_text("unrelated", encoding="utf-8")
                    created = True

            with patch.object(pipeline, "REPO_DIR", root), patch.object(
                pipeline, "STATE_DIR", state_dir
            ), patch.object(
                pipeline, "PUBLICATION_JOURNAL_PATH", journal_path
            ), patch.object(
                pipeline, "PUBLISH_PATHS", ["new.txt"]
            ), patch.object(pipeline, "_write_publication_journal", side_effect=write_journal):
                with self.assertRaises((FileExistsError, RuntimeError)):
                    pipeline._atomic_replace_group_locked([(target, staged)])
            self.assertEqual(target.read_text(encoding="utf-8"), "unrelated")
            self.assertTrue(staged.exists())

    def test_publication_rolls_back_previously_absent_targets_after_late_failure(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            state_dir = root / "state"
            state_dir.mkdir()
            journal_path = state_dir / "journal.json"
            stage_dir = Path(tempfile.mkdtemp(prefix=".nadgryzieni-pipeline-stage-", dir=str(root.parent)))
            staged_one = stage_dir / "one.txt"
            staged_two = stage_dir / "two.txt"
            staged_one.write_text("one", encoding="utf-8")
            staged_two.write_text("two", encoding="utf-8")
            target_one = root / "one.txt"
            target_two = root / "two.txt"
            original_install = pipeline._install_file_no_replace
            calls = 0

            def fail_on_second_install(source, target, **kwargs):
                nonlocal calls
                calls += 1
                if calls == 2:
                    raise RuntimeError("simulated late publication failure")
                return original_install(source, target, **kwargs)

            with patch.object(pipeline, "REPO_DIR", root), patch.object(
                pipeline, "STATE_DIR", state_dir
            ), patch.object(
                pipeline, "PUBLICATION_JOURNAL_PATH", journal_path
            ), patch.object(
                pipeline, "PUBLISH_PATHS", ["one.txt", "two.txt"]
            ), patch.object(
                pipeline, "_install_file_no_replace", side_effect=fail_on_second_install
            ):
                with self.assertRaisesRegex(RuntimeError, "late publication failure"):
                    pipeline._atomic_replace_group_locked([(target_one, staged_one), (target_two, staged_two)])
            self.assertFalse(target_one.exists())
            self.assertFalse(target_two.exists())
            self.assertFalse(journal_path.exists())
            self.assertFalse(stage_dir.exists())

    def test_replacement_source_rejects_fifo_without_blocking(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "source"
            target = root / "target"
            os.mkfifo(source)
            with self.assertRaisesRegex(RuntimeError, "Unsafe replacement source"):
                pipeline._replace_file_verified(source, target, root=root)

    def test_replacement_source_inode_change_fails_closed(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "source"
            target = root / "target"
            source.write_text("source", encoding="utf-8")
            original_stat = pipeline.os.stat
            swapped = False

            def swap_source(path, **kwargs):
                nonlocal swapped
                if path == source.name and kwargs.get("follow_symlinks") is False and not swapped:
                    source.unlink()
                    source.write_text("attacker", encoding="utf-8")
                    swapped = True
                return original_stat(path, **kwargs)

            with patch.object(pipeline.os, "stat", side_effect=swap_source):
                with self.assertRaisesRegex(RuntimeError, "changed during publication"):
                    pipeline._replace_file_verified(source, target, root=root)
            self.assertEqual(source.read_text(encoding="utf-8"), "attacker")
            self.assertEqual(target.read_text(encoding="utf-8"), "source")

    def test_generated_public_provenance_is_sanitized_and_canonicalized(self):
        row = {
            "record_key": "rk_public",
            "episode": "1",
            "title": "1: Public provenance",
            "date": "2026-01-01",
            "duration": "1:00",
            "url": "https://retrorocketnetwork.pl/public-provenance/",
            "hosts": [],
            "hosts_status": "not_listed",
            "hosts_source": "rrn",
            "hosts_source_url": "https://retrorocketnetwork.pl/public-provenance/",
            "hosts_provenance": {
                "kind": "direct_source",
                "source_url": "https://retrorocketnetwork.pl/public-provenance/",
                "password": "PUBLIC_KEY_SECRET",
                "evidence": "password=PUBLIC_PROVENANCE_SECRET",
                "privateKeyMaterial": "PUBLIC_CAMEL_SECRET",
            },
        }
        generated = pipeline.generate_data_json([row])
        public = generated["episodes"][0]["hosts_provenance"]
        encoded = json.dumps(public, ensure_ascii=False)
        self.assertNotIn("PUBLIC_KEY_SECRET", encoded)
        self.assertNotIn("PUBLIC_PROVENANCE_SECRET", encoded)
        self.assertNotIn("PUBLIC_CAMEL_SECRET", encoded)
        self.assertEqual(public["source_url"], "https://retrorocketnetwork.pl/public-provenance")
        self.assertIn("[REDACTED_KEY]", public)

    def test_fsync_file_verified_uses_nonblocking_regular_fd(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            target = root / "target"
            target.write_text("data", encoding="utf-8")
            original_open = pipeline.os.open
            observed = []

            def recording_open(path, flags, *args, **kwargs):
                observed.append((path, flags))
                return original_open(path, flags, *args, **kwargs)

            with patch.object(pipeline.os, "open", side_effect=recording_open):
                pipeline._fsync_file_verified(target, root=root)
            file_flags = [flags for path, flags in observed if str(path) == target.name]
            self.assertTrue(file_flags)
            self.assertTrue(file_flags[-1] & getattr(os, "O_NONBLOCK", 0))

    def test_fsync_file_verified_rejects_nonregular_descriptor(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            target = root / "target"
            target.write_text("data", encoding="utf-8")
            with patch.object(pipeline.os, "fstat", return_value=type("Stat", (), {"st_mode": stat.S_IFIFO})()):
                with self.assertRaisesRegex(RuntimeError, "regular"):
                    pipeline._fsync_file_verified(target, root=root)

    def test_pipeline_network_fetchers_reject_redirecting_default_opener(self):
        with patch.object(pipeline.urllib.request, "urlopen", side_effect=AssertionError("default opener used")), patch.object(
            pipeline, "_open_https_no_redirect", side_effect=RuntimeError("redirect rejected")
        ) as opened:
            with self.assertRaises(RuntimeError):
                pipeline.fetch_rss(max_retries=1)
            self.assertIsNone(pipeline._fetch_patreon_post_page("600-afterparty-123456"))
            self.assertEqual(pipeline._scrape_patreon_posts_page(), [])
        self.assertGreaterEqual(opened.call_count, 3)

    def test_prepared_journal_rejects_replaced_existing_target_without_overwrite(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            state_dir = root / "state"
            state_dir.mkdir()
            journal_path = state_dir / "journal.json"
            target = root / "target.txt"
            target.write_text("original", encoding="utf-8")
            target_identity = {"dev": target.stat().st_dev, "ino": target.stat().st_ino}
            target.unlink()
            target.write_text("attacker", encoding="utf-8")
            backup_dir = root / ".nadgryzieni-publication-backup-target-race"
            backup_dir.mkdir()
            backup = backup_dir / "0.bak"
            backup.write_text("original", encoding="utf-8")
            stage_dir = Path(tempfile.mkdtemp(prefix=".nadgryzieni-pipeline-stage-", dir=str(root.parent)))
            staged = stage_dir / "target.txt"
            staged.write_text("generated", encoding="utf-8")
            journal_path.write_text(json.dumps({
                "phase": "prepared",
                "backup_dir": str(backup_dir),
                "entries": [{
                    "target": str(target),
                    "staged": str(staged),
                    "backup": str(backup),
                    "target_existed": True,
                    "target_identity": target_identity,
                    "backup_identity": {"dev": backup.stat().st_dev, "ino": backup.stat().st_ino},
                    "staged_identity": {"dev": staged.stat().st_dev, "ino": staged.stat().st_ino},
                }],
            }), encoding="utf-8")
            with patch.object(pipeline, "REPO_DIR", root), patch.object(
                pipeline, "STATE_DIR", state_dir
            ), patch.object(pipeline, "PUBLISH_PATHS", ["target.txt"]), patch.object(
                pipeline, "PUBLICATION_JOURNAL_PATH", journal_path
            ):
                with self.assertRaisesRegex(RuntimeError, "journal"):
                    pipeline._recover_publication_journal()
            self.assertEqual(target.read_text(encoding="utf-8"), "attacker")
            self.assertTrue(journal_path.exists())
            staged.unlink()
            stage_dir.rmdir()
            backup.unlink()
            backup_dir.rmdir()

    def test_committed_journal_rejects_replaced_target_before_cleanup(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            state_dir = root / "state"
            state_dir.mkdir()
            journal_path = state_dir / "journal.json"
            target = root / "target.txt"
            target.write_text("original", encoding="utf-8")
            target_identity = {"dev": target.stat().st_dev, "ino": target.stat().st_ino}
            target.unlink()
            target.write_text("attacker", encoding="utf-8")
            backup_dir = root / ".nadgryzieni-publication-backup-committed-race"
            backup_dir.mkdir()
            backup = backup_dir / "0.bak"
            backup.write_text("original", encoding="utf-8")
            stage_dir = Path(tempfile.mkdtemp(prefix=".nadgryzieni-pipeline-stage-", dir=str(root.parent)))
            staged = stage_dir / "target.txt"
            staged.write_text("generated", encoding="utf-8")
            journal_path.write_text(json.dumps({
                "phase": "committed",
                "backup_dir": str(backup_dir),
                "entries": [{
                    "target": str(target),
                    "staged": str(staged),
                    "backup": str(backup),
                    "target_existed": True,
                    "target_identity": target_identity,
                    "backup_identity": {"dev": backup.stat().st_dev, "ino": backup.stat().st_ino},
                    "staged_identity": {"dev": staged.stat().st_dev, "ino": staged.stat().st_ino},
                }],
            }), encoding="utf-8")
            with patch.object(pipeline, "REPO_DIR", root), patch.object(
                pipeline, "STATE_DIR", state_dir
            ), patch.object(pipeline, "PUBLISH_PATHS", ["target.txt"]), patch.object(
                pipeline, "PUBLICATION_JOURNAL_PATH", journal_path
            ):
                with self.assertRaisesRegex(RuntimeError, "journal"):
                    pipeline._recover_publication_journal()
            self.assertEqual(target.read_text(encoding="utf-8"), "attacker")
            self.assertTrue(journal_path.exists())
            self.assertTrue(backup.exists())
            self.assertTrue(staged.exists())
            staged.unlink()
            stage_dir.rmdir()
            backup.unlink()
            backup_dir.rmdir()

    def test_committed_journal_missing_target_is_retained(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            state_dir = root / "state"
            state_dir.mkdir()
            journal_path = state_dir / "journal.json"
            backup_dir = root / ".nadgryzieni-publication-backup-missing"
            backup_dir.mkdir()
            stage_dir = Path(tempfile.mkdtemp(prefix=".nadgryzieni-pipeline-stage-", dir=str(root.parent)))
            staged = stage_dir / "target.txt"
            staged.write_text("new", encoding="utf-8")
            journal_path.write_text(json.dumps({
                "phase": "committed",
                "backup_dir": str(backup_dir),
                "entries": [{
                    "target": str(root / "target.txt"),
                    "staged": str(staged),
                    "backup": None,
                    "target_existed": False,
                    "target_identity": None,
                    "backup_identity": None,
                    "staged_identity": {"dev": 0, "ino": 0},
                }],
            }), encoding="utf-8")
            with patch.object(pipeline, "REPO_DIR", root), patch.object(pipeline, "STATE_DIR", state_dir), patch.object(
                pipeline, "PUBLISH_PATHS", ["target.txt"]
            ), patch.object(pipeline, "PUBLICATION_JOURNAL_PATH", journal_path):
                with self.assertRaisesRegex(RuntimeError, "journal"):
                    pipeline._recover_publication_journal()
            self.assertTrue(journal_path.exists())

    def test_prepared_journal_rejects_new_target_that_already_exists(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            state_dir = root / "state"
            state_dir.mkdir()
            journal_path = state_dir / "journal.json"
            backup_dir = root / ".nadgryzieni-publication-backup-existing-new"
            backup_dir.mkdir()
            stage_dir = Path(tempfile.mkdtemp(prefix=".nadgryzieni-pipeline-stage-", dir=str(root.parent)))
            staged = stage_dir / "target.txt"
            staged.write_text("new", encoding="utf-8")
            target = root / "target.txt"
            target.write_text("unrelated", encoding="utf-8")
            journal_path.write_text(json.dumps({
                "phase": "prepared",
                "backup_dir": str(backup_dir),
                "entries": [{
                    "target": str(target),
                    "staged": str(staged),
                    "backup": None,
                    "target_existed": False,
                    "target_identity": None,
                    "backup_identity": None,
                    "staged_identity": {"dev": 0, "ino": 0},
                }],
            }), encoding="utf-8")
            replacement_dir = Path(tempfile.mkdtemp(prefix=".nadgryzieni-pipeline-stage-", dir=str(root.parent)))
            replacement = replacement_dir / "replacement.txt"
            replacement.write_text("replacement", encoding="utf-8")
            with patch.object(pipeline, "REPO_DIR", root), patch.object(
                pipeline, "STATE_DIR", state_dir
            ), patch.object(
                pipeline, "PUBLISH_PATHS", ["target.txt"]
            ), patch.object(pipeline, "PUBLICATION_JOURNAL_PATH", journal_path):
                with self.assertRaisesRegex(RuntimeError, "journal"):
                    pipeline._recover_publication_journal()
            self.assertTrue(journal_path.exists())
            self.assertEqual(target.read_text(encoding="utf-8"), "unrelated")
            staged.unlink()
            stage_dir.rmdir()
            replacement.unlink()
            replacement_dir.rmdir()

    def test_publication_journal_rejects_lexical_backup_traversal(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            target = root / "target.txt"
            target.write_text("old", encoding="utf-8")
            backup_dir = root / ".nadgryzieni-publication-backup-traversal"
            backup_dir.mkdir()
            backup = backup_dir / "sub" / ".." / "0.bak"
            stage_dir = Path(tempfile.mkdtemp(prefix=".nadgryzieni-pipeline-stage-", dir=str(root.parent)))
            staged = stage_dir / "target.txt"
            staged.write_text("new", encoding="utf-8")
            journal = {
                "phase": "prepared",
                "backup_dir": str(backup_dir),
                "entries": [{
                    "target": str(target),
                    "staged": str(staged),
                    "backup": str(backup),
                    "target_existed": True,
                    "target_identity": {"dev": 0, "ino": 0},
                    "backup_identity": None,
                    "staged_identity": {"dev": 0, "ino": 0},
                }],
            }
            with patch.object(pipeline, "REPO_DIR", root), patch.object(pipeline, "PUBLISH_PATHS", ["target.txt"]):
                with self.assertRaisesRegex(ValueError, "backup path"):
                    pipeline._validate_publication_journal(journal)

    def test_generate_data_json_serializes_canonical_non_rrn_url(self):
        row = {
            "record_key": "rk_url",
            "episode": "1",
            "title": "1: Patreon",
            "date": "2026-01-01",
            "duration": "1:00",
            "url": "https://WWW.Patreon.com/iMagazinePL/posts/ABC123/",
            "hosts": [],
            "hosts_status": "not_listed",
            "hosts_source": "manual",
            "hosts_source_url": "https://WWW.Patreon.com/iMagazinePL/posts/ABC123/",
            "hosts_provenance": {"kind": "direct_source", "source_url": "https://WWW.Patreon.com/iMagazinePL/posts/ABC123/"},
        }
        generated = pipeline.generate_data_json([row])
        self.assertEqual(generated["episodes"][0]["url"], "https://www.patreon.com/iMagazinePL/posts/ABC123")

    def test_paired_provenance_binds_to_actual_episode_identity(self):
        main = {
            "episode": "600",
            "title": "600: Main",
            "date": "2026-01-01",
            "duration": "1:00",
            "url": "https://retrorocketnetwork.pl/600-main/",
        }
        afterparty = {
            "episode": "600.5",
            "title": "600: (Afterparty) Main",
            "date": "2026-01-02",
            "duration": "0:30",
            "url": "https://www.patreon.com/iMagazinePL/posts/600-afterparty-123456",
        }
        main_key = pipeline.build_record_key(main)
        afterparty_key = pipeline.build_record_key(afterparty)
        manifest = {"records": {
            main_key: {
                "hosts": ["Host"], "hosts_status": "verified", "hosts_source": "rrn",
                "hosts_source_url": main["url"],
                "provenance": {"kind": "direct_source", "source_url": main["url"]},
            },
            afterparty_key: {
                "hosts": ["Host"], "hosts_status": "verified", "hosts_source": "paired_rrn",
                "hosts_source_url": main["url"],
                "provenance": {
                    "kind": "paired_rrn", "rule": "afterparty_same_hosts_from_main",
                    "paired_record_key": main_key, "paired_episode": "599",
                },
            },
        }}
        with self.assertRaisesRegex(ValueError, "paired episode"):
            pipeline.validate_manifest_integrity(manifest, record_rows=[main, afterparty])

    def test_paired_provenance_source_url_is_bound_to_archive_rows(self):
        main = {
            "episode": "600",
            "title": "600: Main",
            "date": "2026-01-01",
            "duration": "1:00",
            "url": "https://retrorocketnetwork.pl/600-main/",
        }
        afterparty = {
            "episode": "600.5",
            "title": "600: (Afterparty) Main",
            "date": "2026-01-02",
            "duration": "0:30",
            "url": "https://www.patreon.com/iMagazinePL/posts/600-afterparty-123456",
        }
        main_key = pipeline.build_record_key(main)
        afterparty_key = pipeline.build_record_key(afterparty)
        unrelated = "https://retrorocketnetwork.pl/601-unrelated"
        manifest = {"records": {
            main_key: {
                "hosts": ["Host"], "hosts_status": "verified", "hosts_source": "rrn",
                "hosts_source_url": unrelated,
                "provenance": {"kind": "direct_source", "source_url": unrelated},
            },
            afterparty_key: {
                "hosts": ["Host"], "hosts_status": "verified", "hosts_source": "paired_rrn",
                "hosts_source_url": unrelated,
                "provenance": {
                    "kind": "paired_rrn", "rule": "afterparty_same_hosts_from_main",
                    "paired_record_key": main_key, "paired_episode": "600",
                },
            },
        }}
        with self.assertRaisesRegex(ValueError, "source URL"):
            pipeline.validate_manifest_integrity(manifest, record_rows=[main, afterparty])

    def test_paired_provenance_requires_non_afterparty_integer_base(self):
        base_afterparty = {
            "episode": "600",
            "title": "600: (Afterparty) Wrong base",
            "date": "2026-01-01",
            "duration": "1:00",
            "url": "https://retrorocketnetwork.pl/600-afterparty/",
        }
        afterparty = {
            "episode": "600.5",
            "title": "600: (Afterparty) Main",
            "date": "2026-01-02",
            "duration": "0:30",
            "url": "https://www.patreon.com/iMagazinePL/posts/600-afterparty-123456",
        }
        base_key = pipeline.build_record_key(base_afterparty)
        afterparty_key = pipeline.build_record_key(afterparty)
        manifest = {"records": {
            base_key: {
                "hosts": ["Host"], "hosts_status": "verified", "hosts_source": "rrn",
                "hosts_source_url": base_afterparty["url"],
                "provenance": {"kind": "direct_source", "source_url": base_afterparty["url"]},
            },
            afterparty_key: {
                "hosts": ["Host"], "hosts_status": "verified", "hosts_source": "paired_rrn",
                "hosts_source_url": base_afterparty["url"],
                "provenance": {
                    "kind": "paired_rrn", "rule": "afterparty_same_hosts_from_main",
                    "paired_record_key": base_key, "paired_episode": "600",
                },
            },
        }}
        with self.assertRaisesRegex(ValueError, "base|Afterparty"):
            pipeline.validate_manifest_integrity(manifest, record_rows=[base_afterparty, afterparty])

    def test_publication_rejects_unapproved_direct_root_target(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            stage_dir = Path(tempfile.mkdtemp(prefix=".nadgryzieni-pipeline-stage-", dir=str(root.parent)))
            target = root / "unrelated.txt"
            staged = stage_dir / "unrelated.stage"
            staged.write_text("new", encoding="utf-8")
            with patch.object(pipeline, "REPO_DIR", root):
                with self.assertRaisesRegex(ValueError, "unsafe"):
                    pipeline.atomic_replace_group([(target, staged)])
            staged.unlink()
            stage_dir.rmdir()

    def test_audit_validation_binds_identity_fields_to_each_row(self):
        row = {
            "record_key": "rk_identity",
            "episode": "602",
            "title": "602: Test",
            "date": "2026-08-21",
            "duration": "?",
            "url": "https://retrorocketnetwork.pl/602-test/",
        }
        report = {
            "schema_version": 1,
            "parser_version": host_tools.PARSER_VERSION,
            "dataset_fingerprint": pipeline.dataset_fingerprint([row]),
            "records": {"rk_identity": {
                "record_key": "rk_identity",
                "episode": "602",
                "title": "602: swapped identity",
                "date": "2026-08-21",
                "duration": "?",
                "hosts": [],
                "hosts_status": "not_listed",
                "hosts_source": "rrn",
                "hosts_source_url": row["url"],
                "provenance": {"kind": "direct_source", "source_url": row["url"]},
            }},
        }
        with self.assertRaises(ValueError):
            host_tools._validate_audit_against_current(report, [row])


if __name__ == "__main__":
    unittest.main()
