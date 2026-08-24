import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("nadgryzieni_pipeline_host_schema", ROOT / "nadgryzieni_pipeline.py")
pipeline = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = pipeline
spec.loader.exec_module(pipeline)


class HostSchemaTests(unittest.TestCase):
    def test_record_key_distinguishes_duplicate_episode_number_records(self):
        first = {
            "episode": "135",
            "title": "135: Test",
            "date": "2015-01-01",
            "duration": "1:00:00",
            "url": "https://retrorocketnetwork.pl/135-test/",
        }
        second = dict(first, date="2015-01-02", duration="1:01:00")
        self.assertNotEqual(pipeline.build_record_key(first), pipeline.build_record_key(second))

    def test_record_key_survives_duration_correction_or_missing_rss_duration(self):
        first = {
            "episode": "602",
            "title": "602: Test",
            "date": "2026-08-21",
            "duration": "2:45:40",
            "url": "https://retrorocketnetwork.pl/602-test/",
        }
        corrected = dict(first, duration="2:51:07")
        missing = dict(first, duration="?")
        self.assertEqual(pipeline.build_record_key(first), pipeline.build_record_key(corrected))
        self.assertEqual(pipeline.build_record_key(first), pipeline.build_record_key(missing))

    def test_rss_url_is_reconciled_for_duplicate_episode_metadata(self):
        row = {
            "episode": "135",
            "title": "135: Test",
            "date": "2013-08-30",
            "duration": "58:30",
            "url": "https://retrorocketnetwork.pl/135-canonical/",
        }
        item = dict(row, episode_number="135", url="https://retrorocketnetwork.pl/136-wrong-link/")
        self.assertEqual(pipeline.resolve_existing_source_url(item, [row]), row["url"])

    def test_parser_preserves_empty_hosts_column(self):
        content = "\n".join([
            "| # | Ep. | Episode title | Hosts | Publish date | Duration |",
            "| - | --- | --- | --- | --- | --- |",
            "| 1 | 135 | 135: Test |  | 2015-01-01 | 1:00:00 |",
            "",
        ])
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "archive.md"
            path.write_text(content, encoding="utf-8")
            rows, _ = pipeline.parse_archive(path)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["title"], "135: Test")
        self.assertEqual(rows[0]["date"], "2015-01-01")
        self.assertEqual(rows[0]["duration"], "1:00:00")
        self.assertEqual(rows[0]["hosts"], [])

    def test_not_listed_sentinel_is_explicit(self):
        row = {
            "counter": "1",
            "episode": "1",
            "title": "1: Test",
            "date": "2010-01-01",
            "duration": "1:00:00",
            "url": "https://retrorocketnetwork.pl/one/",
            "hosts": [],
            "hosts_status": "not_listed",
            "hosts_source": "rrn",
            "hosts_source_url": "https://retrorocketnetwork.pl/one/",
        }
        self.assertEqual(pipeline.hosts_cell(row), "Brak danych")
        data = pipeline.generate_data_json([row])
        pipeline.validate_generated_data(data, [row])
        self.assertEqual(data["episodes"][0]["hosts_status"], "not_listed")
        self.assertEqual(data["episodes"][0]["hosts"], [])

    def test_manifest_join_is_by_record_key(self):
        row = {
            "counter": "1",
            "episode": "135",
            "title": "135: Test",
            "date": "2015-01-01",
            "duration": "1:00:00",
            "url": "https://retrorocketnetwork.pl/135-test/",
        }
        key = pipeline.build_record_key(row)
        manifest = {"schema_version": 1, "records": {key: {
            "hosts": ["Wojtek Pietrusiewicz"],
            "hosts_status": "verified",
            "hosts_source": "rrn",
            "hosts_source_url": row["url"],
        }}}
        pipeline.apply_host_metadata([row], manifest, strict=True)
        self.assertEqual(row["hosts"], ["Wojtek Pietrusiewicz"])
        self.assertEqual(row["hosts_source"], "rrn")


if __name__ == "__main__":
    unittest.main()
