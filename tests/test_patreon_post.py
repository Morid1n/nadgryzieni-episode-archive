import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace


REPO_DIR = Path(__file__).resolve().parents[1]


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


register = load_module(
    "register_patreon_post_for_tests",
    REPO_DIR / "cron" / "register-patreon-post.py",
)
pipeline = load_module(
    "nadgryzieni_pipeline_for_patreon_tests",
    REPO_DIR / "nadgryzieni_pipeline.py",
)


class PatreonRenderedUrlTests(unittest.TestCase):
    def test_register_accepts_rendered_afterparty_url_with_non_afterparty_slug(self):
        args = SimpleNamespace(
            episode=611,
            slug="611-mx-keypad-vs-171882117",
            url="https://www.patreon.com/iMagazinePL/posts/611-mx-keypad-vs-171882117",
            title="611: (Afterparty) MX Keypad vs Stream Deck – po co nam przyciski w dobie agentów AI?",
            date="2026-10-09",
            duration="1:31:19",
        )

        entry = register.build_entry(args)

        self.assertEqual(entry["episode"], 611)
        self.assertEqual(entry["slug"], args.slug)
        self.assertEqual(entry["duration"], "1:31:19")

    def test_manifest_accepts_generic_slug_only_with_verified_afterparty_title(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "patreon_posts.json"
            path.write_text(json.dumps({
                "version": 2,
                "posts": [{
                    "episode": 611,
                    "slug": "611-mx-keypad-vs-171882117",
                    "url": "https://www.patreon.com/iMagazinePL/posts/611-mx-keypad-vs-171882117",
                    "title": "611: (Afterparty) MX Keypad vs Stream Deck – po co nam przyciski w dobie agentów AI?",
                    "date": "2026-10-09",
                    "duration": "1:31:19",
                }],
            }), encoding="utf-8")

            manifest = pipeline.load_patreon_manifest(path)

        self.assertEqual(len(manifest), 1)
        self.assertEqual(manifest[0]["episode"], 611)
        self.assertEqual(manifest[0]["url"], "https://www.patreon.com/iMagazinePL/posts/611-mx-keypad-vs-171882117")

    def test_manifest_rejects_generic_slug_without_afterparty_metadata(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "patreon_posts.json"
            path.write_text(json.dumps({
                "version": 2,
                "posts": [{
                    "episode": 611,
                    "slug": "611-mx-keypad-vs-171882117",
                    "url": "https://www.patreon.com/iMagazinePL/posts/611-mx-keypad-vs-171882117",
                }],
            }), encoding="utf-8")

            self.assertEqual(pipeline.load_patreon_manifest(path), [])


if __name__ == "__main__":
    unittest.main()
