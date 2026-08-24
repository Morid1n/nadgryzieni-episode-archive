import json
import unittest
from pathlib import Path

from nadgryzieni_hosts import (
    HostNameError,
    _cached_direct_entry,
    host_dedupe_key,
    normalize_host_name,
    parse_patreon_post_payload,
    parse_rrn_hosts,
)


ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures" / "hosts"


def fixture(name):
    return (FIXTURES / name).read_text(encoding="utf-8")


def patreon_payload(document):
    return {
        "data": {
            "attributes": {
                "content_json_string": json.dumps(document, ensure_ascii=False),
            }
        }
    }


class HostNameNormalizationTests(unittest.TestCase):
    def test_normalize_host_name_nfkc_collapses_unicode_whitespace(self):
        self.assertEqual(
            normalize_host_name("  Wojtek\u00a0Pietrusiewicz\u2003  "),
            "Wojtek Pietrusiewicz",
        )

    def test_host_dedupe_key_is_case_insensitive_after_normalization(self):
        self.assertEqual(
            host_dedupe_key("  WOJTEK\u00a0Pietrusiewicz "),
            host_dedupe_key("Wojtek Pietrusiewicz"),
        )

    def test_norbert_cala_is_always_published_as_npc(self):
        self.assertEqual(normalize_host_name("Norbert Cała"), "NPC")
        self.assertEqual(normalize_host_name("  NORBERT\u00a0CAŁA  "), "NPC")
        self.assertEqual(host_dedupe_key("Norbert Cała"), host_dedupe_key("NPC"))
        self.assertEqual(normalize_host_name("Norbi"), "NPC")
        self.assertEqual(normalize_host_name("Miłoszu"), "Miłosz")
        self.assertEqual(normalize_host_name("Steve’a Ballmera"), "Steve Ballmer")
        self.assertEqual(
            normalize_host_name("Michała „Nozbe” Śliwińskiego"),
            "Michał Śliwiński",
        )

    def test_host_name_separators_are_rejected(self):
        with self.assertRaises(HostNameError):
            normalize_host_name("Name; Other")
        with self.assertRaises(HostNameError):
            host_dedupe_key("Name | Other")

    def test_cached_host_alias_is_normalized_before_future_update(self):
        cached = _cached_direct_entry(
            {"record_key": "rk_cache", "episode": "1", "title": "1: Test"},
            {"hosts": ["Norbert Cała"], "hosts_status": "verified", "hosts_source": "rrn"},
        )
        self.assertEqual(cached["hosts"], ["NPC"])


class RrnParserTests(unittest.TestCase):
    def test_old_h2_host_block_is_scoped_to_article_content(self):
        result = parse_rrn_hosts(
            fixture("rrn-old-h2.html"),
            expected_title="Nadgryzieni 300: Stary format",
            expected_episode="300",
        )
        self.assertEqual(result.status, "verified")
        self.assertEqual(
            result.hosts,
            ["Wojtek Pietrusiewicz", "Thomas Voland"],
        )
        self.assertEqual(result.excluded_hosts, [])

    def test_new_p_host_block_strips_social_suffixes_dedupes_and_excludes_struck_entries(self):
        result = parse_rrn_hosts(
            fixture("rrn-new-p-social.html"),
            expected_title="Nadgryzieni 301: Nowy format",
            expected_episode="301",
        )
        self.assertEqual(result.status, "verified")
        self.assertEqual(
            result.hosts,
            ["Anna Kowalska", "Łukasz Żółć", "Thomas Voland"],
        )
        self.assertEqual(result.excluded_hosts, ["Stary Gospodarz", "Drugi Usunięty"])
        self.assertFalse(any("@" in host for host in result.hosts))

    def test_missing_host_block_is_not_listed(self):
        result = parse_rrn_hosts(fixture("rrn-no-host-block.html"))
        self.assertEqual(result.status, "not_listed")
        self.assertEqual(result.hosts, [])

    def test_prose_mention_of_prowadzacy_is_not_a_host_block(self):
        result = parse_rrn_hosts(fixture("rrn-prose-only.html"))
        self.assertEqual(result.status, "not_listed")
        self.assertEqual(result.hosts, [])

    def test_guest_is_a_host_even_when_no_prowadzacy_block_exists(self):
        html = """
        <article>
          <p>Gościem tego odcinka jest Jakub Tepper, z którym rozmawiamy.</p>
        </article>
        """
        result = parse_rrn_hosts(html)
        self.assertEqual(result.status, "verified")
        self.assertEqual(result.hosts, ["Jakub Tepper"])

    def test_guest_marker_without_name_does_not_infer_a_host(self):
        html = """
        <article>
          <p>Dzisiaj nagraliśmy odcinek z nowym gościem.</p>
        </article>
        """
        result = parse_rrn_hosts(html)
        self.assertEqual(result.status, "not_listed")
        self.assertEqual(result.hosts, [])
        self.assertIn("no unambiguous name", " ".join(result.diagnostics))

    def test_description_extraction_keeps_only_explicit_name_tokens(self):
        result = parse_rrn_hosts(
            """<article><p>Gościem specjalnym jest redaktor serwisu Aperture.pl — Wojtek „Alchemic” Równanek.</p></article>""",
            expected_url="https://example.test/guest-name-boundary",
        )
        self.assertEqual(result.hosts, ['Wojtek „Alchemic” Równanek'])

    def test_description_extraction_does_not_publish_common_noun_as_person(self):
        result = parse_rrn_hosts(
            """<article><p>W tym odcinku gościem jest kobieta, o której jeszcze opowiemy.</p></article>""",
            expected_url="https://example.test/guest-common-noun",
        )
        self.assertEqual(result.hosts, [])
        self.assertEqual(result.status, "not_listed")

    def test_description_extraction_accepts_name_before_guest_marker(self):
        result = parse_rrn_hosts(
            """<article><p>W tym odcinku Kamil jest gościem i opowiada o zdarzeniu.</p></article>""",
            expected_url="https://example.test/guest-name-before-marker",
        )
        self.assertEqual(result.hosts, ['Kamil Szmit'])

    def test_description_extraction_accepts_name_after_dash_marker(self):
        result = parse_rrn_hosts(
            """<article><p>Dołącza do nas gość specjalny – Zdzisiek z TP-Linka – który opowiada o nowościach.</p></article>""",
            expected_url="https://example.test/guest-name-after-dash",
        )
        self.assertEqual(result.hosts, ['Zdzisław Kaczyk'])

    def test_description_extraction_normalizes_zbyszek_identity(self):
        result = parse_rrn_hosts(
            """<article><p>Gościem tego odcinka jest Zbyszek z Macoscope.</p></article>""",
            expected_url="https://example.test/guest-zbyszek",
        )
        self.assertEqual(result.hosts, ['Zbigniew Sobiecki'])

    def test_description_extraction_accepts_appeared_guest_role(self):
        result = parse_rrn_hosts(
            """<article><p>Dzisiaj w roli gościa specjalnego pojawił się Błażej Faliszek, który pracuje nad filtrem.</p></article>""",
            expected_url="https://example.test/guest-role-appeared",
        )
        self.assertEqual(result.hosts, ['Błażej Faliszek'])

    def test_description_extraction_accepts_guest_marker_with_czyli_name(self):
        result = parse_rrn_hosts(
            """<article><p>Gości specjalista od bezpieczeństwa, czyli nasz własny Krzysztof Młynarski.</p></article>""",
            expected_url="https://example.test/guest-marker-czyli",
        )
        self.assertEqual(result.hosts, ['Krzysztof Młynarski'])

    def test_description_extraction_accepts_guest_as_role_predicate(self):
        result = parse_rrn_hosts(
            """<article><p>Dzisiaj za gościa robi Norbert. Po trzecie omawiamy nowy temat.</p></article>""",
            expected_url="https://example.test/guest-role-predicate",
        )
        self.assertEqual(result.hosts, ['NPC'])

    def test_description_extraction_accepts_name_described_as_guest(self):
        result = parse_rrn_hosts(
            """<article><p>Michał Śliwiński z Nozbe, jako specjalny gość, opowiada o Apple.</p></article>""",
            expected_url="https://example.test/guest-name-described",
        )
        self.assertEqual(result.hosts, ['Michał Śliwiński'])

    def test_description_extraction_ignores_guest_of_previous_episode(self):
        result = parse_rrn_hosts(
            """<article><p>Nie zapomnieliśmy o gościu ostatniego odcinka — Miłoszu.</p></article>""",
            expected_url="https://example.test/guest-previous-episode",
        )
        self.assertEqual(result.hosts, [])

    def test_structural_empty_host_list_is_not_listed(self):
        result = parse_rrn_hosts(fixture("rrn-empty-list.html"))
        self.assertEqual(result.status, "not_listed")
        self.assertEqual(result.hosts, [])

    def test_multiple_host_blocks_fail_closed(self):
        result = parse_rrn_hosts(fixture("rrn-ambiguous.html"))
        self.assertEqual(result.status, "parse_error")
        self.assertIn("multiple", " ".join(result.diagnostics).lower())

    def test_heading_without_immediately_following_list_fails_closed(self):
        result = parse_rrn_hosts(fixture("rrn-invalid-following.html"))
        self.assertEqual(result.status, "parse_error")
        self.assertEqual(result.hosts, [])

    def test_list_with_non_direct_entries_fails_closed(self):
        result = parse_rrn_hosts(fixture("rrn-malformed-list.html"))
        self.assertEqual(result.status, "parse_error")
        self.assertIn("direct list", " ".join(result.diagnostics).lower())

    def test_forbidden_separator_in_source_entry_is_diagnosed(self):
        result = parse_rrn_hosts(fixture("rrn-separators.html"))
        self.assertEqual(result.status, "parse_error")
        self.assertTrue(
            any("semicolon" in diagnostic.lower() or "pipe" in diagnostic.lower()
                for diagnostic in result.diagnostics)
        )

    def test_presentational_wrapper_list_is_unwrapped(self):
        html = """
        <article><h2>Prowadzący:</h2>
        <ul><li style="list-style-type:none"><ul>
          <li>Thomas Voland (<a href="https://twitter.com/thomas">@thomas</a>)</li>
          <li>Wojtek Pietrusiewicz (<a href="https://twitter.com/morid1n">@morid1n</a>)</li>
        </ul></li></ul></article>
        """
        result = parse_rrn_hosts(html)
        self.assertEqual(result.status, "verified")
        self.assertEqual(result.hosts, ["Thomas Voland", "Wojtek Pietrusiewicz"])

    def test_fractional_archive_marker_can_match_legacy_title_body(self):
        html = """
        <article><h1>WWDC 2012</h1><p>Brak sekcji prowadzących.</p></article>
        """
        result = parse_rrn_hosts(
            html,
            expected_title="86½: (Live) WWDC 2012",
            expected_episode="86.5",
        )
        self.assertEqual(result.status, "not_listed")

    def test_explicit_guest_from_description_is_merged_into_hosts(self):
        html = """
        <article><h2>Prowadzący:</h2>
        <ul><li>Thomas Voland</li></ul>
        <p>Gościem tego odcinka jest Miłosz Bolechowski, z którym rozmawiamy.</p>
        </article>
        """
        result = parse_rrn_hosts(html)
        self.assertEqual(result.status, "verified")
        self.assertEqual(result.hosts, ["Thomas Voland", "Miłosz Bolechowski"])
        self.assertNotIn("guests", result.to_dict())


class PatreonParserTests(unittest.TestCase):
    def test_patreon_prosemirror_content_extracts_ordered_hosts(self):
        document = {
            "type": "doc",
            "content": [
                {"type": "paragraph", "content": [{"type": "text", "text": "Prowadzący:"}]},
                {
                    "type": "bulletList",
                    "content": [
                        {
                            "type": "listItem",
                            "content": [
                                {
                                    "type": "paragraph",
                                    "content": [
                                        {"type": "text", "text": "Michał Żółć "},
                                        {
                                            "type": "text",
                                            "text": "@michal",
                                            "marks": [
                                                {
                                                    "type": "link",
                                                    "attrs": {"href": "https://twitter.example/michal"},
                                                }
                                            ],
                                        },
                                    ],
                                }
                            ],
                        },
                        {
                            "type": "listItem",
                            "content": [
                                {
                                    "type": "paragraph",
                                    "content": [{"type": "text", "text": "michał\u00a0żółć"}],
                                }
                            ],
                        },
                        {
                            "type": "listItem",
                            "content": [
                                {
                                    "type": "paragraph",
                                    "content": [{"type": "text", "text": "Élodie Nowak"}],
                                }
                            ],
                        },
                    ],
                },
            ],
        }
        result = parse_patreon_post_payload(
            patreon_payload(document),
            source_url="https://www.patreon.com/iMagazinePL/posts/301-test#comments",
        )
        self.assertEqual(result.status, "verified")
        self.assertEqual(result.hosts, ["Michał Żółć", "Élodie Nowak"])
        self.assertEqual(
            result.source_url,
            "https://www.patreon.com/iMagazinePL/posts/301-test",
        )

    def test_patreon_unavailable_content_is_an_explicit_parse_error(self):
        payload = {"data": {"attributes": {"content_json_string": None}}}
        result = parse_patreon_post_payload(payload)
        self.assertEqual(result.status, "parse_error")
        self.assertIn("unavailable", " ".join(result.diagnostics).lower())

    def test_patreon_description_person_is_merged_into_hosts(self):
        document = {
            "type": "doc",
            "content": [
                {"type": "paragraph", "content": [{"type": "text", "text": "Prowadzący:"}]},
                {
                    "type": "bulletList",
                    "content": [{
                        "type": "listItem",
                        "content": [{"type": "paragraph", "content": [{"type": "text", "text": "Thomas Voland"}]}],
                    }],
                },
                {"type": "paragraph", "content": [{"type": "text", "text": "Goście:"}]},
                {
                    "type": "bulletList",
                    "content": [{
                        "type": "listItem",
                        "content": [{"type": "paragraph", "content": [{"type": "text", "text": "Miłosz Bolechowski"}]}],
                    }],
                },
            ],
        }
        result = parse_patreon_post_payload(patreon_payload(document))
        self.assertEqual(result.status, "verified")
        self.assertEqual(result.hosts, ["Thomas Voland", "Miłosz Bolechowski"])


if __name__ == "__main__":
    unittest.main()
