import json
import subprocess
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


class FrontendSignalsBehaviorTests(unittest.TestCase):
    def test_longest_and_shortest_cards_render_titles_without_a_second_number_prefix(self):
        script = (REPO_DIR / "script.js").read_text(encoding="utf-8")
        episodes = [
            {
                "episode": "421",
                "title": "421: Dużo nowego info o Apple Vision Pro i recenzja Diablo 4",
                "date": "2023-06-10",
                "duration": "3:00:00",
                "minutes": 180,
                "category": "main",
            },
            {
                "episode": "12",
                "title": "Nadgryzieni – 12 – Krótki odcinek",
                "date": "2010-01-10",
                "duration": "0:10:00",
                "minutes": 10,
                "category": "main",
            },
        ]
        harness = f"""
const vm = require('vm');
const source = {json.dumps(script)};
const elements = Object.fromEntries([
    '#longest-duration', '#longest-title', '#longest-meta',
    '#shortest-duration', '#shortest-title', '#shortest-meta',
    '#busiest-year', '#busiest-year-count', '#chart-range',
].map((selector) => [selector, {{ textContent: '' }}]));
const context = {{
    console, URL, Intl, Date, setTimeout, clearTimeout,
    fetch: async () => ({{ ok: true, json: async () => ({{ episodes: [] }}) }}),
    document: {{
        documentElement: {{ dataset: {{}} }},
        querySelector: (selector) => elements[selector] || null,
        querySelectorAll: () => [],
    }},
    window: {{
        matchMedia: () => ({{ matches: false, addEventListener() {{}}, addListener() {{}} }}),
        addEventListener() {{}},
    }},
}};
context.globalThis = context;
context.__episodes = {json.dumps(episodes)};
vm.runInNewContext(
    source + '\\nnormalizedEpisodes = normalizeEpisodes(globalThis.__episodes); renderSignals();',
    context,
    {{ timeout: 5000 }},
);
console.log(JSON.stringify({{
    longest: elements['#longest-title'].textContent,
    shortest: elements['#shortest-title'].textContent,
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
        self.assertEqual(
            json.loads(completed.stdout),
            {
                "longest": "421: Dużo nowego info o Apple Vision Pro i recenzja Diablo 4",
                "shortest": "Nadgryzieni – 12 – Krótki odcinek",
            },
        )


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


class FrontendLineOnlyChartContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.html = (REPO_DIR / "index.html").read_text(encoding="utf-8")
        cls.script = (REPO_DIR / "script.js").read_text(encoding="utf-8")
        cls.style = (REPO_DIR / "style.css").read_text(encoding="utf-8")

    def test_timeline_exposes_only_the_line_chart_without_mode_switching(self):
        self.assertNotIn('data-chart-mode=', self.html)
        self.assertNotIn('Punkty', self.html)
        self.assertNotIn('chartMode', self.script)
        self.assertNotIn('updateModeControls(', self.script)
        self.assertNotIn("type: 'scatter'", self.script)
        self.assertNotIn('.chart-controls', self.style)
        self.assertNotIn('.chart-mode-buttons', self.style)
        self.assertNotIn('.mode-button', self.style)
        self.assertIn("type: 'line'", self.script)

    def test_line_chart_uses_pre_expansion_fifteen_pixel_chronological_spacing(self):
        self.assertIn("const CHART_EPISODE_SPACING = 15;", self.script)
        self.assertNotIn("const CHART_EPISODE_SPACING = 22.5;", self.script)
        self.assertIn("normalizedEpisodes.length * CHART_EPISODE_SPACING", self.script)
        self.assertEqual(578 * 15, 8670)


class FrontendTooltipPointerCoordinateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.script = (REPO_DIR / "script.js").read_text(encoding="utf-8")

    def test_pointer_plugin_scales_chart_coordinates_to_canvas_css_pixels(self):
        plugin_start = self.script.index("const chartTooltipPointerPlugin = {")
        plugin_end = self.script.index("\n};", plugin_start) + len("\n};")
        plugin = self.script[plugin_start:plugin_end]
        self.assertIn("canvasRect.width / chart.width", plugin)
        self.assertIn("canvasRect.height / chart.height", plugin)

    def test_pointer_plugin_prefers_current_native_client_coordinates(self):
        plugin_start = self.script.index("const chartTooltipPointerPlugin = {")
        plugin_end = self.script.index("\n};", plugin_start) + len("\n};")
        plugin_source = self.script[plugin_start:plugin_end]
        harness = f"""
const vm = require('vm');
const source = {json.dumps(plugin_source)} + '\\nglobalThis.plugin = chartTooltipPointerPlugin;';
const context = {{ globalThis: {{}} }};
context.globalThis = context;
vm.runInNewContext(source, context, {{ timeout: 5000 }});
const chart = {{
    canvas: {{ getBoundingClientRect: () => ({{ left: 100, top: 200, width: 13005, height: 540 }}) }},
    width: 8670,
    height: 540,
}};
context.plugin.beforeEvent(chart, {{
    event: {{
        type: 'mousemove',
        x: 100,
        y: 80,
        native: {{ clientX: 450, clientY: 280 }},
    }},
}});
console.log(JSON.stringify(chart.$tooltipPointer));
"""
        completed = subprocess.run(
            ["node"],
            input=harness,
            capture_output=True,
            text=True,
            timeout=10,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(json.loads(completed.stdout), {"clientX": 450, "clientY": 280})


if __name__ == "__main__":
    unittest.main()
