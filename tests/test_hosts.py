import json
import tempfile
import unittest
from email.message import Message
from pathlib import Path
from unittest.mock import Mock, patch

import nadgryzieni_hosts as hosts
from nadgryzieni_hosts import (
    HostNameError,
    _cached_direct_entry,
    _load_host_cache,
    _save_host_cache,
    _description_sentence_names,
    canonical_url,
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
        self.assertEqual(normalize_host_name('Steve \'Woz\' Wozniak'), 'Steve "Woz" Wozniak')
        self.assertEqual(normalize_host_name('Steve “Woz” Wozniak'), 'Steve "Woz" Wozniak')
        self.assertEqual(normalize_host_name('Steve Wozniak'), 'Steve "Woz" Wozniak')
        self.assertEqual(
            normalize_host_name("Michała „Nozbe” Śliwińskiego"),
            "Michał Śliwiński",
        )

    def test_host_cache_rejects_symlink_destination(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            outside = root / "outside.json"
            outside.write_text("untouched", encoding="utf-8")
            destination = root / "cache.json"
            destination.symlink_to(outside)
            with self.assertRaisesRegex(RuntimeError, "symlink"):
                _save_host_cache(destination, {})
            self.assertEqual(outside.read_text(encoding="utf-8"), "untouched")

    def test_host_cache_reader_rejects_symlink_and_scalar(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            outside = root / "outside.json"
            outside.write_text(json.dumps({
                "schema_version": 1,
                "parser_version": "nadgryzieni-hosts/1.9",
                "records": {"x": {"hosts": ["External"]}},
            }), encoding="utf-8")
            cache = root / "cache.json"
            cache.symlink_to(outside)
            with self.assertRaisesRegex(RuntimeError, "symlink"):
                _load_host_cache(cache)
            cache.unlink()
            cache.write_text("[]", encoding="utf-8")
            self.assertEqual(_load_host_cache(cache), {})

    def test_public_sanitizer_redacts_unstructured_credential_values(self):
        text = hosts._sanitize_public_text(
            "credential value CREDENTIAL_SENTINEL token value TOKEN_SENTINEL "
            "password PASSWORD_SENTINEL privateKeyMaterial MATERIAL_SENTINEL"
        )
        for sentinel in ("CREDENTIAL_SENTINEL", "TOKEN_SENTINEL", "PASSWORD_SENTINEL", "MATERIAL_SENTINEL"):
            self.assertNotIn(sentinel, text)

    def test_public_sanitizer_redacts_standalone_bearer_and_basic_values(self):
        text = hosts._sanitize_public_text(
            "Bearer BEARER_SENTINEL Basic BASIC_SENTINEL"
        )
        self.assertNotIn("BEARER_SENTINEL", text)
        self.assertNotIn("BASIC_SENTINEL", text)
        self.assertEqual(text.count("[REDACTED]"), 2)

    def test_canonical_url_rejects_empty_query_and_fragment_delimiters(self):
        with self.assertRaisesRegex(ValueError, "query"):
            canonical_url("https://example.com/path?")
        with self.assertRaisesRegex(ValueError, "fragment"):
            canonical_url("https://example.com/path#")

    def test_description_parser_rejects_organization_phrases(self):
        self.assertEqual(
            _description_sentence_names("Gościem jest Open Source i Retro Rocket Network"),
            [],
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
    def test_description_people_ignores_blockquotes_asides_and_nested_lists(self):
        html = """
        <article>
          <blockquote><p>Gościem jest Jan Kowalski.</p></blockquote>
          <aside><p>Gościem jest Piotr Nowak.</p></aside>
          <p>Gościem jest <ul><li>Jan Kowalski</li></ul></p>
        </article>
        """
        result = parse_rrn_hosts(html)
        self.assertEqual(result.status, "not_listed")
        self.assertEqual(result.hosts, [])

    def test_description_people_ignores_sidebar_class_and_complementary_role(self):
        html = """
        <article>
          <div class="sidebar"><p>Gościem jest Jan Kowalski.</p></div>
          <div role="complementary"><p>Gościem jest Piotr Nowak.</p></div>
          <div class="content"><p>Gościem jest Anna Nowak.</p></div>
        </article>
        """
        result = parse_rrn_hosts(html)
        self.assertEqual(result.status, "verified")
        self.assertEqual(result.hosts, ["Anna Nowak"])

    def test_description_list_itself_ignores_semantic_non_content_markers(self):
        for marker in ('class="sidebar"', 'class="right-sidebar"', 'role="navigation"'):
            with self.subTest(marker=marker):
                result = parse_rrn_hosts(
                    f"<article><h2>Goście:</h2><ul {marker}><li>Jan Kowalski</li></ul></article>"
                )
                self.assertEqual(result.status, "not_listed")
                self.assertEqual(result.hosts, [])

    def test_description_people_ignores_previous_episode_form_with_im_odcinku(self):
        result = parse_rrn_hosts(
            """<article><p>W poprzednim odcinku gościem — Jan Kowalski — był.</p></article>""",
            expected_url="https://example.test/guest-previous-episode-im",
        )
        self.assertEqual(result.status, "not_listed")
        self.assertEqual(result.hosts, [])

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

    def test_description_extraction_does_not_publish_product_name_as_person(self):
        result = parse_rrn_hosts(
            """<article><p>Gość specjalny – Apple Silicon – opowiada o nowych komputerach.</p></article>""",
            expected_url="https://example.test/guest-product-name",
        )
        self.assertEqual(result.hosts, [])
        self.assertEqual(result.status, "not_listed")

    def test_description_extraction_accepts_name_before_guest_marker(self):
        result = parse_rrn_hosts(
            """<article><p>W tym odcinku Kamil jest gościem i opowiada o zdarzeniu.</p></article>""",
            expected_url="https://example.test/guest-name-before-marker",
        )
        self.assertEqual(result.hosts, ['Kamil Szmit'])

    def test_description_extraction_drops_sentence_initial_modifier_from_name(self):
        result = parse_rrn_hosts(
            """<article><p>Dzisiaj Kamil jest gościem i opowiada o zdarzeniu.</p></article>""",
            expected_url="https://example.test/guest-leading-modifier",
        )
        self.assertEqual(result.status, "verified")
        self.assertEqual(result.hosts, ["Kamil Szmit"])

    def test_description_extraction_rejects_previous_episode_mentions_in_every_pattern(self):
        result = parse_rrn_hosts(
            """<article><p>Gościem poprzedniego odcinka był Jan Kowalski.</p></article>""",
            expected_url="https://example.test/guest-previous-episode-without-dash",
        )
        self.assertEqual(result.status, "not_listed")
        self.assertEqual(result.hosts, [])

    def test_description_extraction_accepts_plural_guest_sentence(self):
        result = parse_rrn_hosts(
            """<article><p>Gośćmi są Jan Kowalski i Anna Nowak.</p></article>""",
            expected_url="https://example.test/guest-plural",
        )
        self.assertEqual(result.status, "verified")
        self.assertEqual(result.hosts, ["Jan Kowalski", "Anna Nowak"])

    def test_description_extraction_accepts_conjunction_after_singular_guest_marker(self):
        result = parse_rrn_hosts(
            """<article><p>Gościem jest Jan Kowalski i Anna Nowak.</p></article>""",
            expected_url="https://example.test/guest-singular-conjunction",
        )
        self.assertEqual(result.status, "verified")
        self.assertEqual(result.hosts, ["Jan Kowalski", "Anna Nowak"])

    def test_description_extraction_drops_honorific_before_guest_name(self):
        result = parse_rrn_hosts(
            """<article><p>Gościem jest Dr. Jan Kowalski.</p></article>""",
            expected_url="https://example.test/guest-honorific",
        )
        self.assertEqual(result.status, "verified")
        self.assertEqual(result.hosts, ["Jan Kowalski"])

    def test_description_extraction_accepts_special_guest_colon_form(self):
        result = parse_rrn_hosts(
            """<article><p>Gość specjalny: Jan Kowalski.</p></article>""",
            expected_url="https://example.test/guest-special-colon",
        )
        self.assertEqual(result.status, "verified")
        self.assertEqual(result.hosts, ["Jan Kowalski"])

    def test_description_extraction_never_publishes_guest_role_word(self):
        result = parse_rrn_hosts(
            """<article><p>Gościem jest Jan Kowalski, a gościem jest Anna Nowak.</p></article>""",
            expected_url="https://example.test/guest-role-word",
        )
        self.assertEqual(result.status, "verified")
        self.assertEqual(result.hosts, ["Jan Kowalski", "Anna Nowak"])

    def test_description_extraction_does_not_discard_explicit_name_before_ordinary_semicolon(self):
        result = parse_rrn_hosts(
            """<article><p>Gościem jest Jan Kowalski; rozmawiamy o technologii.</p></article>""",
            expected_url="https://example.test/guest-semicolon",
        )
        self.assertEqual(result.status, "verified")
        self.assertEqual(result.hosts, ["Jan Kowalski"])

    def test_malformed_html_fails_closed_even_when_a_host_list_is_recoverable(self):
        result = parse_rrn_hosts(
            """<article><h2>Prowadzący:</h2><ul><li>Jan Kowalski</article>""",
            expected_url="https://example.test/malformed-html",
        )
        self.assertEqual(result.status, "parse_error")
        self.assertEqual(result.hosts, [])

    def test_recoverable_unexpected_closing_tag_does_not_block_real_host_detection(self):
        result = parse_rrn_hosts(
            """</span><article><p>Gościem jest Jan Kowalski.</p></article>""",
            expected_url="https://example.test/recoverable-closing-tag",
        )
        self.assertEqual(result.status, "verified")
        self.assertEqual(result.hosts, ["Jan Kowalski"])

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

    def test_structural_host_list_rejects_non_content_wrapper_and_list_markers(self):
        cases = (
            '<nav><ul><li>Jan Kowalski</li></ul></nav>',
            '<div class="sidebar"><ul><li>Jan Kowalski</li></ul></div>',
            '<ul class="sidebar"><li>Jan Kowalski</li></ul>',
        )
        for list_markup in cases:
            with self.subTest(list_markup=list_markup):
                result = parse_rrn_hosts(
                    f"<article><h2>Prowadzący:</h2>{list_markup}</article>"
                )
                self.assertNotEqual(result.status, "verified")
                self.assertEqual(result.hosts, [])

    def test_structural_host_list_rejects_text_plus_nested_list_item(self):
        result = parse_rrn_hosts(
            "<article><h2>Prowadzący:</h2><ul>"
            "<li>Jan Kowalski<ul><li>Sidebar Name</li></ul></li>"
            "</ul></article>"
        )
        self.assertEqual(result.status, "parse_error")
        self.assertEqual(result.hosts, [])

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

    def test_patreon_structural_host_list_rejects_nested_lists(self):
        document = {
            "type": "doc",
            "content": [
                {"type": "paragraph", "content": [{"type": "text", "text": "Prowadzący:"}]},
                {
                    "type": "bulletList",
                    "content": [{
                        "type": "listItem",
                        "content": [
                            {"type": "paragraph", "content": [{"type": "text", "text": "Jan Kowalski"}]},
                            {"type": "bulletList", "content": [{
                                "type": "listItem",
                                "content": [{"type": "paragraph", "content": [{"type": "text", "text": "Sidebar Name"}]}],
                            }]},
                        ],
                    }],
                },
            ],
        }
        result = parse_patreon_post_payload(patreon_payload(document))
        self.assertEqual(result.status, "parse_error")
        self.assertEqual(result.hosts, [])

    def test_patreon_description_list_rejects_nested_lists(self):
        document = {
            "type": "doc",
            "content": [
                {"type": "paragraph", "content": [{"type": "text", "text": "Goście:"}]},
                {"type": "bulletList", "content": [{
                    "type": "listItem",
                    "content": [
                        {"type": "paragraph", "content": [{"type": "text", "text": "Jan Kowalski"}]},
                        {"type": "bulletList", "content": []},
                    ],
                }]},
            ],
        }
        result = parse_patreon_post_payload(patreon_payload(document))
        self.assertNotEqual(result.status, "verified")
        self.assertEqual(result.hosts, [])

    def test_patreon_multiple_description_people_blocks_are_ambiguous(self):
        document = {
            "type": "doc",
            "content": [
                {"type": "paragraph", "content": [{"type": "text", "text": "Goście:"}]},
                {"type": "bulletList", "content": [{
                    "type": "listItem",
                    "content": [{"type": "paragraph", "content": [{"type": "text", "text": "First Guest"}]}],
                }]},
                {"type": "paragraph", "content": [{"type": "text", "text": "Goście:"}]},
                {"type": "bulletList", "content": [{
                    "type": "listItem",
                    "content": [{"type": "paragraph", "content": [{"type": "text", "text": "Second Guest"}]}],
                }]},
            ],
        }
        result = parse_patreon_post_payload(patreon_payload(document))
        self.assertEqual(result.status, "ambiguous")
        self.assertEqual(result.hosts, [])
        self.assertIn("multiple description", " ".join(result.diagnostics))

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
    def test_patreon_structural_hosts_are_cleared_when_description_blocks_are_ambiguous(self):
        document = {
            "type": "doc",
            "content": [
                {"type": "paragraph", "content": [{"type": "text", "text": "Prowadzący:"}]},
                {"type": "bulletList", "content": [{
                    "type": "listItem",
                    "content": [{"type": "paragraph", "content": [{"type": "text", "text": "Structural Host"}]}],
                }]},
                {"type": "paragraph", "content": [{"type": "text", "text": "Goście:"}]},
                {"type": "bulletList", "content": [{
                    "type": "listItem",
                    "content": [{"type": "paragraph", "content": [{"type": "text", "text": "First Guest"}]}],
                }]},
                {"type": "paragraph", "content": [{"type": "text", "text": "Goście:"}]},
                {"type": "bulletList", "content": [{
                    "type": "listItem",
                    "content": [{"type": "paragraph", "content": [{"type": "text", "text": "Second Guest"}]}],
                }]},
            ],
        }
        result = parse_patreon_post_payload(patreon_payload(document))
        self.assertEqual(result.status, "ambiguous")
        self.assertEqual(result.hosts, [])
        self.assertEqual(result.provenance, {})


class AuditSafetyTests(unittest.TestCase):
    def test_public_sanitizer_redacts_secret_like_keys_and_values(self):
        sanitized = hosts._sanitize_public_value({
            "password": "PUBLIC_KEY_SECRET",
            "api_key": "PUBLIC_API_SECRET",
            "normal": "safe",
        })
        encoded = json.dumps(sanitized, ensure_ascii=False)
        self.assertNotIn("PUBLIC_KEY_SECRET", encoded)
        self.assertNotIn("PUBLIC_API_SECRET", encoded)
        self.assertIn("[REDACTED_KEY]", sanitized)

    def test_public_sanitizer_redacts_camelcase_and_suffix_secret_keys(self):
        sanitized = hosts._sanitize_public_value({
            "privateKeyMaterial": "CAMEL_PRIVATE_SENTINEL",
            "credentialData": "CAMEL_CREDENTIAL_SENTINEL",
            "authorizationHeader": "CAMEL_AUTH_SENTINEL",
            "refreshTokenValue": "CAMEL_REFRESH_SENTINEL",
        })
        encoded = json.dumps(sanitized, ensure_ascii=False)
        for sentinel in (
            "CAMEL_PRIVATE_SENTINEL",
            "CAMEL_CREDENTIAL_SENTINEL",
            "CAMEL_AUTH_SENTINEL",
            "CAMEL_REFRESH_SENTINEL",
        ):
            self.assertNotIn(sentinel, encoded)
        for key in (
            "privateKeyMaterial",
            "credentialData",
            "authorizationHeader",
            "refreshTokenValue",
        ):
            self.assertNotIn(key, encoded)

    def test_non_rrn_credential_url_is_not_published_in_audit_result(self):
        result = hosts._direct_audit_entry(
            {
                "record_key": "manual",
                "episode": "1",
                "title": "Manual",
                "date": "2026-01-01",
                "duration": "?",
                "url": "https://user:pass@example.invalid/post?token=SECRET_SENTINEL",
            },
            {},
            {},
            [0.0],
            0.0,
        )
        self.assertEqual(result["hosts_source_url"], "")
        self.assertEqual(result["provenance"]["source_url"], "")
        self.assertNotIn("SECRET_SENTINEL", json.dumps(result))

    def test_audit_report_preserves_safe_canonical_source_urls(self):
        report = {
            "records": {
                "rk": {
                    "hosts_source_url": "https://WWW.Example.invalid/post/",
                    "provenance": {"source_url": "https://WWW.Example.invalid/post/"},
                }
            }
        }
        sanitized = hosts._sanitize_audit_report(report)
        self.assertEqual(
            sanitized["records"]["rk"]["hosts_source_url"],
            "https://www.example.invalid/post",
        )
        self.assertEqual(
            sanitized["records"]["rk"]["provenance"]["source_url"],
            "https://www.example.invalid/post",
        )

    def test_robots_redirect_is_denied_without_following_target(self):
        from urllib.error import HTTPError

        class NoRedirectOpener:
            def open(self, request, timeout):
                raise HTTPError(request.full_url, 302, "redirect", Message(), None)

        with patch.object(hosts, "build_opener", return_value=NoRedirectOpener()):
            cache = {}
            self.assertFalse(hosts._robots_allowed("https://example.invalid/post", cache))
            self.assertFalse(cache["https://example.invalid"])

    def test_apply_audit_write_rebuilds_snapshot_after_lock(self):
        pipeline = Mock()
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            repo = root / "repo"
            repo.mkdir()
            pipeline.REPO_DIR = repo
            pipeline.ARCHIVE_PATH = repo / "archive.md"
            pipeline.DATA_JSON_PATH = repo / "data.json"
            pipeline.HOST_METADATA_PATH = repo / "host_metadata.json"
            pipeline.README_PATH = repo / "README.md"
            rows_locked = [{"record_key": "rk", "episode": "1", "title": "1: Test", "host_marker": "locked"}]
            report = {
                "records": {"rk": {"hosts": [], "hosts_status": "not_listed", "hosts_source": "rrn", "hosts_source_url": "https://example.invalid/episode-1", "provenance": {}}},
                "parser_version": "test",
                "dataset_fingerprint": "fingerprint",
            }
            manifest = {"records": {"rk": {}}}
            pipeline._read_bytes_secure.return_value = json.dumps(report).encode("utf-8")
            events = []
            pipeline.acquire_pipeline_lock.side_effect = lambda: events.append("acquire") or True
            pipeline.manifest_from_rows.return_value = manifest
            pipeline.generate_data_json.return_value = {}
            pipeline.parse_archive.return_value = (rows_locked, "")
            audit_path = root / "audit.json"
            audit_path.write_text(json.dumps(report), encoding="utf-8")
            def load_rows():
                events.append("load")
                return pipeline, rows_locked, {}
            with patch.object(hosts, "_pipeline_module", return_value=pipeline), patch.object(
                hosts, "_load_current_rows", side_effect=load_rows
            ), patch.object(hosts, "_validate_audit_against_current"), patch.object(
                pipeline, "_remove_directory_verified"
            ):
                hosts.apply_audit(audit_path, dry_run=False, write=True)
        self.assertEqual(events[:2], ["acquire", "load"])
        self.assertIs(pipeline.generate_data_json.call_args.args[0], rows_locked)

    def test_apply_audit_dry_run_validates_manifest_with_record_rows(self):
        pipeline = Mock()
        rows = [{
            "record_key": "rk",
            "episode": "1",
            "title": "1: Test",
            "date": "2026-01-01",
            "duration": "01:00",
            "url": "https://example.invalid/episode-1",
            "hosts": [],
            "hosts_status": "verified",
            "hosts_source": "rrn",
            "hosts_source_url": "https://example.invalid/episode-1",
            "hosts_provenance": {"kind": "direct_source", "source_url": "https://example.invalid/episode-1"},
        }]
        report = {
            "records": {
                "rk": {
                    "hosts": ["Jan Kowalski"],
                    "hosts_status": "verified",
                    "hosts_source": "rrn",
                    "hosts_source_url": "https://example.invalid/episode-1",
                    "provenance": {"kind": "direct_source", "source_url": "https://example.invalid/episode-1"},
                },
            },
            "parser_version": "test",
            "dataset_fingerprint": "fingerprint",
        }
        manifest = {"records": {"rk": {}}}
        pipeline._read_bytes_secure.return_value = json.dumps(report).encode("utf-8")
        pipeline.manifest_from_rows.return_value = manifest
        pipeline.generate_data_json.return_value = {}
        with tempfile.TemporaryDirectory() as temp_dir:
            audit_path = Path(temp_dir) / "audit.json"
            audit_path.write_text(json.dumps(report), encoding="utf-8")
            with patch.object(hosts, "_load_current_rows", return_value=(pipeline, rows, {})), patch.object(
                hosts, "_validate_audit_against_current"
            ), patch.object(hosts, "_publishable_manifest"):
                hosts.apply_audit(audit_path, dry_run=True, write=False)
        self.assertIs(pipeline.write_host_metadata.call_args.kwargs["record_rows"], rows)

    def test_audit_repository_acquires_shared_lock(self):
        calls = []

        class PipelineStub:
            def _pipeline_lock_owned_by_current_thread(self):
                return False

            def acquire_pipeline_lock(self):
                calls.append("acquire")
                return True

            def release_pipeline_lock(self):
                calls.append("release")

        with patch.object(hosts, "_pipeline_module", return_value=PipelineStub()), patch.object(
            hosts, "_audit_repository_locked", return_value={"ok": True}
        ):
            self.assertEqual(hosts.audit_repository(Path("/tmp/report.json")), {"ok": True})
        self.assertEqual(calls, ["acquire", "release"])


if __name__ == "__main__":
    unittest.main()
