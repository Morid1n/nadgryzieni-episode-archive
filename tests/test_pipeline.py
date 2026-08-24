import importlib.util
import json
import ssl
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

    def test_retry_state_is_only_due_for_the_next_scheduled_window(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            state_path = Path(temp_dir) / "retry-state.json"
            primary_date = date(2026, 8, 8)
            pipeline.write_retry_state(state_path, primary_date, pending=True)
            self.assertTrue(pipeline.retry_is_due(state_path, primary_date + timedelta(days=3)))
            self.assertFalse(pipeline.retry_is_due(state_path, primary_date + timedelta(days=2)))
            self.assertFalse(pipeline.retry_is_due(state_path, primary_date + timedelta(days=4)))
            pipeline.write_retry_state(state_path, primary_date, pending=False)
            self.assertFalse(pipeline.retry_is_due(state_path, primary_date + timedelta(days=3)))

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

    def test_atomic_replace_group_publishes_all_files(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            target_a = root / "a.txt"
            target_b = root / "b.txt"
            staged_a = root / "a.stage"
            staged_b = root / "b.stage"
            target_a.write_text("old-a", encoding="utf-8")
            target_b.write_text("old-b", encoding="utf-8")
            staged_a.write_text("new-a", encoding="utf-8")
            staged_b.write_text("new-b", encoding="utf-8")
            pipeline.atomic_replace_group([(target_a, staged_a), (target_b, staged_b)])
            self.assertEqual(target_a.read_text(encoding="utf-8"), "new-a")
            self.assertEqual(target_b.read_text(encoding="utf-8"), "new-b")

    def test_atomic_replace_group_rolls_back_after_a_late_failure(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            target_a = root / "a.txt"
            target_b = root / "b.txt"
            staged_a = root / "a.stage"
            staged_b = root / "b.stage"
            target_a.write_text("old-a", encoding="utf-8")
            target_b.write_text("old-b", encoding="utf-8")
            staged_a.write_text("new-a", encoding="utf-8")
            staged_b.write_text("new-b", encoding="utf-8")
            original_replace = pipeline.os.replace
            calls = {"count": 0}

            def fail_on_fourth_call(source, destination):
                calls["count"] += 1
                if calls["count"] == 4:
                    raise OSError("simulated publication failure")
                return original_replace(source, destination)

            with patch.object(pipeline.os, "replace", side_effect=fail_on_fourth_call):
                with self.assertRaises(OSError):
                    pipeline.atomic_replace_group([(target_a, staged_a), (target_b, staged_b)])
            self.assertEqual(target_a.read_text(encoding="utf-8"), "old-a")
            self.assertEqual(target_b.read_text(encoding="utf-8"), "old-b")

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
