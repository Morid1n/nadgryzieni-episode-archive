"""Contracts for the paired regular-episode/Afterparty hero panel."""

import json
import subprocess
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

    def evaluate_latest_release(self, episodes):
        harness = f"""
const vm = require('vm');
const source = {json.dumps(self.script)};
const episodes = {json.dumps(episodes)};
const context = {{
    console,
    URL,
    Intl,
    Date,
    setTimeout,
    clearTimeout,
    fetch: async () => ({{ ok: true, json: async () => ({{ episodes: [] }}) }}),
    document: {{
        documentElement: {{ dataset: {{}} }},
        querySelector: () => null,
        querySelectorAll: () => [],
    }},
    window: {{
        matchMedia: () => ({{ matches: false, addEventListener() {{}}, addListener() {{}} }}),
        addEventListener() {{}},
    }},
}};
context.globalThis = context;
context.__episodes = episodes;
vm.runInNewContext(
    source + '\\nnormalizedEpisodes = normalizeEpisodes(globalThis.__episodes); globalThis.__latest = getLatestRelease();',
    context,
    {{ timeout: 5000 }},
);
console.log(JSON.stringify({{
    paired: context.__latest.paired,
    episodeIds: context.__latest.episodes.map((episode) => episode.episodeId),
}}));
"""
        completed = subprocess.run(
            ["node"],
            input=harness,
            capture_output=True,
            text=True,
            timeout=10,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        return json.loads(completed.stdout.strip())

    def test_adjacent_day_main_and_afterparty_are_one_release(self):
        result = self.evaluate_latest_release(
            [
                {
                    "episode": "603",
                    "title": "Main",
                    "date": "2026-08-28",
                    "minutes": 164,
                    "category": "main",
                },
                {
                    "episode": "603.5",
                    "title": "Afterparty",
                    "date": "2026-08-29",
                    "minutes": 59,
                    "category": "afterparty",
                },
            ]
        )
        self.assertEqual(result, {"paired": True, "episodeIds": ["603", "603.5"]})

    def test_different_base_ids_are_not_grouped(self):
        result = self.evaluate_latest_release(
            [
                {"episode": "602", "date": "2026-08-28", "minutes": 100, "category": "main"},
                {"episode": "603.5", "date": "2026-08-29", "minutes": 59, "category": "afterparty"},
            ]
        )
        self.assertEqual(result, {"paired": False, "episodeIds": ["603.5"]})

    def test_two_main_episodes_are_not_grouped(self):
        result = self.evaluate_latest_release(
            [
                {"episode": "603", "date": "2026-08-28", "minutes": 100, "category": "main"},
                {"episode": "603.1", "date": "2026-08-29", "minutes": 59, "category": "main"},
            ]
        )
        self.assertEqual(result, {"paired": False, "episodeIds": ["603.1"]})

    def test_non_main_episode_and_afterparty_are_not_grouped(self):
        result = self.evaluate_latest_release(
            [
                {"episode": "603", "date": "2026-08-28", "minutes": 100, "category": "special"},
                {"episode": "603.5", "date": "2026-08-29", "minutes": 59, "category": "afterparty"},
            ]
        )
        self.assertEqual(result, {"paired": False, "episodeIds": ["603.5"]})

    def test_current_data_renders_the_regular_and_afterparty_pair(self):
        result = self.evaluate_latest_release(self.data["episodes"])
        self.assertEqual(result, {"paired": True, "episodeIds": ["603", "603.5"]})

    def test_current_data_contains_the_regular_and_afterparty_pair(self):
        latest_two = self.data["episodes"][-2:]
        self.assertEqual(latest_two[1]["episode"], f"{latest_two[0]['episode']}.5")
        self.assertEqual({episode["category"] for episode in latest_two}, {"main", "afterparty"})
        self.assertTrue(all(episode["url"] for episode in latest_two))


if __name__ == "__main__":
    unittest.main()
