"""Contracts for the paired regular-episode/Afterparty hero panel."""

import json
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class PairedReleaseHeroContractTests(unittest.TestCase):
    def setUp(self):
        self.html = (ROOT / "index.html").read_text(encoding="utf-8")
        self.script = (ROOT / "script.js").read_text(encoding="utf-8")
        self.data = json.loads((ROOT / "data.json").read_text(encoding="utf-8"))

    def test_hero_has_a_semantic_release_panel(self):
        self.assertIn('class="latest-release"', self.html)
        self.assertIn('id="latest-release-title"', self.html)
        self.assertIn('id="latest-release-list"', self.html)
        self.assertIn('id="latest-release-date"', self.html)

    def test_renderer_has_pair_aware_release_selection(self):
        self.assertIn("function getLatestRelease", self.script)
        self.assertIn("function renderLatestRelease", self.script)
        self.assertIn("isAfterparty", self.script)
        self.assertIn("latest-release-paired", self.script)

    def test_current_data_contains_the_regular_and_afterparty_pair(self):
        latest_two = self.data["episodes"][-2:]
        self.assertEqual(latest_two[1]["episode"], f"{latest_two[0]['episode']}.5")
        self.assertEqual({episode["date"] for episode in latest_two}, {latest_two[0]["date"]})
        self.assertEqual({episode["category"] for episode in latest_two}, {"main", "afterparty"})
        self.assertTrue(all(episode["url"] for episode in latest_two))


if __name__ == "__main__":
    unittest.main()
