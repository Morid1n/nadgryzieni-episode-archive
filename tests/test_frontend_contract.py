import unittest
from pathlib import Path


REPO_DIR = Path(__file__).resolve().parents[1]


class FrontendHostsContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.html = (REPO_DIR / "index.html").read_text(encoding="utf-8")
        cls.script = (REPO_DIR / "script.js").read_text(encoding="utf-8")
        cls.style = (REPO_DIR / "style.css").read_text(encoding="utf-8")

    def test_hosts_summary_is_first_content_block_in_archive_overview(self):
        section_start = self.html.index('aria-labelledby="signals-title"')
        summary = self.html.index('id="hosts-summary"', section_start)
        signal_grid = self.html.index('class="signal-grid"', section_start)
        self.assertLess(summary, signal_grid)
        self.assertIn('id="host-summary-list"', self.html)
        self.assertIn('id="hosts-summary-empty"', self.html)

    def test_runtime_preserves_raw_records_and_renders_summary(self):
        self.assertIn("let rawEpisodes = [];", self.script)
        self.assertIn("function renderHostsSummary()", self.script)
        self.assertIn("rawEpisodes = Array.isArray(chartData.episodes)", self.script)
        self.assertIn("renderHostsSummary();", self.script)
        self.assertIn("hostsStatus: episode.hosts_status || ''", self.script)

    def test_cards_and_search_include_hosts_without_html_interpolation(self):
        self.assertIn("hosts.textContent = episode.hosts.length", self.script)
        self.assertIn("Prowadzący: Brak danych", self.script)
        self.assertIn("episode.hosts.join(' ')", self.script)
        self.assertIn("episode.hosts.join(', ')", self.script)
        self.assertNotIn("innerHTML = episode.hosts", self.script)

    def test_hosts_summary_sorts_by_count_then_first_name_then_surname(self):
        self.assertIn("function hostNameSortKey(value)", self.script)
        self.assertIn("hostCollator.compare(leftKey.firstName, rightKey.firstName)", self.script)
        self.assertIn("hostCollator.compare(leftKey.surname, rightKey.surname)", self.script)
        self.assertIn(
            "b.count - a.count || compareHostNames(a.name, b.name)",
            self.script,
        )

    def test_hosts_summary_has_responsive_styles(self):
        self.assertIn(".hosts-summary", self.style)
        self.assertIn(".host-summary-list", self.style)
        self.assertIn(".episode-hosts", self.style)
        self.assertIn(".hosts-summary-heading", self.style)


if __name__ == "__main__":
    unittest.main()
