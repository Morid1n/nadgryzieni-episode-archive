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


class FrontendUpcomingContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.html = (REPO_DIR / "index.html").read_text(encoding="utf-8")
        cls.script = (REPO_DIR / "script.js").read_text(encoding="utf-8")
        cls.style = (REPO_DIR / "style.css").read_text(encoding="utf-8")

    def test_upcoming_card_has_separate_semantic_markup(self):
        self.assertIn('id="upcoming-event"', self.html)
        self.assertIn('id="upcoming-event-title"', self.html)
        self.assertIn('id="upcoming-event-time"', self.html)
        self.assertIn('id="upcoming-event-link"', self.html)
        self.assertIn('hidden', self.html)

    def test_runtime_loads_and_validates_upcoming_artifact(self):
        self.assertIn("function renderUpcomingEvent(payload)", self.script)
        self.assertIn("upcoming.json?v=", self.script)
        self.assertIn("Europe/Warsaw", self.script)
        self.assertIn("upcomingEvent.hidden = true", self.script)
        self.assertIn("upcomingEventLink.textContent", self.script)
        self.assertIn("url.origin !== 'https://www.youtube.com'", self.script)
        self.assertIn("typeof event.url !== 'string'", self.script)
        self.assertIn("event.url.trim() !== event.url", self.script)
        self.assertIn("/[?#]$/.test(event.url)", self.script)
        self.assertIn("/^https:\\/\\/www\\.youtube\\.com\\/watch\\?v=([A-Za-z0-9_-]{6,20})$/", self.script)
        self.assertIn("url.username || url.password", self.script)
        self.assertIn("url.port", self.script)
        self.assertIn("queryEntries.length !== 1", self.script)
        self.assertIn("url.hash", self.script)

    def test_upcoming_card_has_responsive_styles(self):
        self.assertIn(".upcoming-event", self.style)
        self.assertIn(".upcoming-event-card", self.style)
        self.assertIn(".upcoming-event-link", self.style)

    def test_upcoming_card_uses_latest_release_surface_background(self):
        latest_start = self.style.index("\n.latest-release {\n    position: relative;\n    min-height: 380px;") + 1
        latest_block = self.style[latest_start:self.style.index("}", latest_start)]
        upcoming_start = self.style.index(".upcoming-event-card {")
        upcoming_block = self.style[upcoming_start:self.style.index("}", upcoming_start)]
        self.assertIn("background: var(--surface);", latest_block)
        self.assertIn("background: var(--surface);", upcoming_block)
        self.assertNotIn("linear-gradient", upcoming_block)

    def test_upcoming_title_matches_recent_episode_typography_without_resizing(self):
        latest_title_start = self.style.index(".latest-release-item-title {")
        latest_title_block = self.style[latest_title_start:self.style.index("}", latest_title_start)]
        upcoming_title_start = self.style.index(".upcoming-event-copy h2 {")
        upcoming_title_block = self.style[upcoming_title_start:self.style.index("}", upcoming_title_start)]
        for declaration in (
            "font-family: var(--font-body);",
            "font-weight: 700;",
            "letter-spacing: -0.025em;",
        ):
            self.assertIn(declaration, latest_title_block)
            self.assertIn(declaration, upcoming_title_block)
        self.assertIn("font-size: clamp(1.5rem, 3vw, 2.45rem);", upcoming_title_block)

    def test_year_bars_render_values_above_bars_and_keep_tooltips(self):
        self.assertIn(".year-bar-count", self.style)
        self.assertIn("bottom: calc(var(--bar-height) + 8px);", self.style)
        self.assertIn("countLabel.className = 'year-bar-count';", self.script)
        self.assertIn("countLabel.textContent = integerFormatter.format(count);", self.script)
        self.assertIn("bar.setAttribute('aria-label', `${year}: ${count} odcinków`);", self.script)
        self.assertIn("bar.title = `${year}: ${count} odcinków`;", self.script)


if __name__ == "__main__":
    unittest.main()
