#!/usr/bin/env python3
"""
nadgryzieni_pipeline.py — Weekly automation pipeline for the Nadgryzieni episode archive.

Runs every Saturday (scheduled via Hermes cronjob). Does the following:

1. Fetches the RSS feed and parses episode items.
2. Compares against the existing repo archive to find new episodes.
3. Appends new episodes to `Nadgryzieni Episode Archive.md` (column-padded markdown table).
4. Regenerates `data.json` (Chart.js scatter plot data) from the archive.
5. Regenerates `Nadgryzieni Statistics.md` with comprehensive stats.
6. Bumps cache-busting version (?v=N) in index.html, script.js, data.json references.
7. Commits and pushes to Git (triggers GitHub Pages rebuild).
8. Syncs archive and statistics to Obsidian vault directory.

If no new episodes are found, the script exits silently (no output, no commit).

Usage:
    python3 nadgryzieni_pipeline.py           # full run
    python3 nadgryzieni_pipeline.py --dry     # dry run (no git commit/push)
    python3 nadgryzieni_pipeline.py --force   # force regeneration even if no new episodes
"""

from __future__ import annotations

import json
import hashlib
import os
import re
import shutil
import ssl
import subprocess
import sys
import tempfile
import time
import urllib.request
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict
from datetime import datetime, date, timedelta
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import urlparse
import unicodedata

# ── Configuration ────────────────────────────────────────────────────────────
# Resolve from the script location so manual and cron invocations use the same checkout.
REPO_DIR = Path(os.environ.get("NADGRYZIENI_REPO_DIR", Path(__file__).resolve().parent))
ARCHIVE_PATH = REPO_DIR / "Nadgryzieni Episode Archive.md"
STATS_PATH = REPO_DIR / "Nadgryzieni Statistics.md"
DATA_JSON_PATH = REPO_DIR / "data.json"
HOST_METADATA_PATH = REPO_DIR / "host_metadata.json"
INDEX_HTML_PATH = REPO_DIR / "index.html"
SCRIPT_JS_PATH = REPO_DIR / "script.js"
README_PATH = REPO_DIR / "README.md"

# Obsidian vault directory for syncing archive and statistics
VAULT_DIR = Path("/Users/tarkin/Library/Mobile Documents/com~apple~CloudDocs/! Hermes !/Scarif Vault/20-Podcast")

RSS_URL = "https://retrorocketnetwork.pl/category/nadgryzieni-rss/feed/"

# Patreon creator page (public posts are scraped; private RSS requires auth)
PATREON_URL = "https://www.patreon.com/iMagazinePL/posts"
PATREON_MANIFEST_PATH = REPO_DIR / "patreon_posts.json"

# Content widths for the padded markdown table (content is left-justified,
# with 1 space padding on each side between | characters)
# Derived from the existing archive; Hosts is appended so the legacy column
# order remains stable: counter, episode, title, date, duration, hosts.
COL_CONTENT_WIDTHS = [3, 5, 108, 12, 8, 34]

# Durable operational state. These files are deliberately outside the Git checkout.
STATE_DIR = Path(os.environ.get(
    "NADGRYZIENI_STATE_DIR",
    str(Path.home() / ".hermes" / "profiles" / "r2-d2" / "state"),
))
RETRY_STATE_PATH = STATE_DIR / "nadgryzieni-retry-state.json"
LOCK_PATH = STATE_DIR / "nadgryzieni-pipeline.lock"
_LOCK_HANDLE = None

# Cache-busting version is committed with the generated site, not kept in /tmp.
CACHE_VERSION_FILE = REPO_DIR / ".cache-version"

# Only generated/published files may be staged by an automated run.
PUBLISH_PATHS = [
    "Nadgryzieni Episode Archive.md",
    "Nadgryzieni Statistics.md",
    "README.md",
    "data.json",
    "host_metadata.json",
    "patreon_posts.json",
    "index.html",
    "script.js",
    ".cache-version",
]

# Logging
import logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stderr)],
)
log = logging.getLogger("nadgryzieni-pipeline")

# RSS namespace
RSS_NS = {"itunes": "http://www.itunes.com/dtds/podcast-1.0.dtd"}


def create_ssl_context() -> ssl.SSLContext:
    """Return a certificate-validating HTTPS context."""
    return ssl.create_default_context()


def parse_publish_date(value: str) -> str:
    """Normalize an RSS/Patreon date to ISO YYYY-MM-DD where possible."""
    if not value:
        return ""
    try:
        return parsedate_to_datetime(value).date().isoformat()
    except (TypeError, ValueError, IndexError, OverflowError):
        return value[:10] if len(value) >= 10 else ""


# ── RSS Fetching ─────────────────────────────────────────────────────────────

def fetch_rss(max_retries: int = 3) -> bytes:
    """Fetch the RSS feed with retries. Returns raw XML bytes."""
    ctx = create_ssl_context()
    for attempt in range(1, max_retries + 1):
        try:
            req = urllib.request.Request(RSS_URL, headers={
                "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
            })
            with urllib.request.urlopen(req, timeout=30, context=ctx) as resp:
                return resp.read()
        except Exception as exc:
            log.warning(f"RSS fetch attempt {attempt}/{max_retries} failed: {exc}")
            if attempt < max_retries:
                time.sleep(2 ** attempt)
    raise RuntimeError(f"RSS fetch failed after {max_retries} attempts")


# ── RSS Parsing ──────────────────────────────────────────────────────────────

def parse_rss_items(xml_bytes: bytes) -> list[dict]:
    """Parse RSS XML and return list of episode dicts with title, date, duration, url."""
    root = ET.fromstring(xml_bytes)
    items = []
    for entry in root.findall(".//item"):
        title_el = entry.find("title")
        pubdate_el = entry.find("pubDate")
        duration_el = entry.find(".//itunes:duration", RSS_NS)
        guid_el = entry.find("guid")
        link_el = entry.find("link")

        if title_el is None or pubdate_el is None:
            continue

        title = title_el.text.strip() if title_el.text else ""
        pubdate = pubdate_el.text.strip() if pubdate_el.text else ""
        duration = duration_el.text.strip() if duration_el is not None and duration_el.text else ""
        guid = guid_el.text.strip() if guid_el is not None and guid_el.text else ""
        link = link_el.text.strip() if link_el is not None and link_el.text else ""
        if not link and guid.startswith("http"):
            link = guid

        # Parse pubDate (RFC 822)
        iso_date = parse_publish_date(pubdate)

        # Extract episode number from title
        ep_num = extract_episode_number(title)

        items.append({
            "title": title,
            "date": iso_date,
            "duration": duration,
            "guid": guid,
            "url": link,
            "episode_number": ep_num,
        })
    return items


# ── Patreon Scraping ──────────────────────────────────────────────────────────

def load_patreon_manifest(path: Path = PATREON_MANIFEST_PATH) -> list[dict]:
    """Load the tracked Patreon fallback manifest without exposing credentials.

    Browser-assisted cron runs may include verified title/date/duration metadata
    because Patreon can block the pipeline's non-browser post-page fetch. Those
    fields are optional for backwards compatibility with older manifest entries.
    """
    if not path.exists():
        return []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        log.warning("Could not read Patreon manifest: %s", type(exc).__name__)
        return []

    posts = []
    for post in payload.get("posts", []):
        try:
            episode = int(post["episode"])
            slug = str(post["slug"]).strip()
            url = str(post["url"]).strip()
        except (KeyError, TypeError, ValueError):
            continue
        parsed_url = urlparse(url)
        if (
            parsed_url.scheme != "https"
            or parsed_url.netloc != "www.patreon.com"
            or not re.fullmatch(rf"{episode}-afterparty(?:-[a-z0-9]+)*-\d{{6,12}}", slug)
        ):
            continue

        entry = {"episode": episode, "slug": slug, "url": url}
        title = post.get("title")
        if title is not None:
            title = str(title).strip()
            if not title or "(Afterparty)" not in title:
                continue
            entry["title"] = title

        pub_date = post.get("date")
        if pub_date is not None:
            pub_date = str(pub_date).strip()
            if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", pub_date):
                continue
            entry["date"] = pub_date

        duration = post.get("duration")
        if duration is not None:
            duration = str(duration).strip()
            duration_parts = duration.split(":")
            if len(duration_parts) not in (2, 3) or any(
                not part.isdigit() for part in duration_parts
            ):
                continue
            entry["duration"] = duration

        posts.append(entry)
    return posts


PATREON_MANIFEST = load_patreon_manifest()
PATREON_KNOWN_POSTS = [(post["episode"], post["slug"]) for post in PATREON_MANIFEST]
PATREON_POST_URLS = {f"{post['episode']}.5": post["url"] for post in PATREON_MANIFEST}


def _fetch_patreon_post_page(slug: str) -> str | None:
    """Fetch a single Patreon post page and return its HTML."""
    ctx = create_ssl_context()
    url = f"https://www.patreon.com/iMagazinePL/posts/{slug}"
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
        "Accept": "text/html",
    })
    try:
        with urllib.request.urlopen(req, timeout=15, context=ctx) as resp:
            return resp.read().decode("utf-8", errors="replace")
    except Exception:
        return None


def _parse_patreon_post(html: str, source_url: str = "") -> dict | None:
    """Extract title, duration, and date from a Patreon post page HTML."""
    # Extract title from og:title meta tag
    title_match = re.search(r'og:title" content="([^"]+)"', html)
    if not title_match:
        return None
    title = title_match.group(1)
    # Strip the " | iMagazinePL x Nadgryzieni" suffix (and any other " | ..." suffixes)
    if " | " in title:
        title = title.split(" | ")[0].strip()

    # Extract duration
    duration = None
    # Try: "duration":"HH:MM:SS" format
    dur_match = re.search(r'"duration":"(\d{1,2}:\d{2}:\d{2})"', html)
    if dur_match:
        duration = dur_match.group(1)
    else:
        # Try: duration in seconds (integer)
        dur_sec = re.search(r'"duration":(\d+)', html)
        if dur_sec:
            secs = int(dur_sec.group(1))
            h = secs // 3600
            m = (secs % 3600) // 60
            s = secs % 60
            duration = f"{h}:{m:02d}:{s:02d}"

    # Extract published date
    date_match = re.search(r'"published_at":"([^"]+)"', html)
    pub_date = parse_publish_date(date_match.group(1)) if date_match else ""

    return {
        "title": title,
        "duration": duration,
        "date": pub_date,
        "url": source_url,
    }


def _fetch_patreon_posts_from_list() -> list[dict]:
    """Fetch known Patreon posts, using browser-verified manifest metadata when present."""
    posts = []
    for post in PATREON_MANIFEST:
        ep_num = post["episode"]
        slug = post["slug"]
        if post.get("title"):
            parsed = {
                "title": post["title"],
                "duration": post.get("duration"),
                "date": post.get("date", ""),
                "url": post["url"],
            }
        else:
            html = _fetch_patreon_post_page(slug)
            if not html:
                continue
            parsed = _parse_patreon_post(html, post["url"])
            if not parsed:
                continue
        parsed["episode_number"] = ep_num
        posts.append(parsed)
    return posts


def _merge_patreon_posts(*sources: list[dict]) -> list[dict]:
    """Merge Patreon source results by episode, preferring verified metadata."""
    merged: dict[str, dict] = {}
    for source in sources:
        for post in source:
            key = str(post.get("episode_number") or post.get("episode") or post.get("url") or "").strip()
            if not key:
                continue
            current = merged.setdefault(key, {})
            for field, value in post.items():
                if value not in (None, ""):
                    current[field] = value
    return list(merged.values())


def fetch_patreon_posts() -> list[dict]:
    """Fetch and merge Patreon Afterparty episodes from all available sources.

    1. If PATREON_RSS_URL env var is set, fetch authenticated RSS XML.
    2. Try scraping the public posts page for post slugs (JS-rendered, may fail).
    3. Merge the tracked verified post manifest, including browser-verified metadata.

    Returns a deduplicated list of dicts with keys: title, episode_number,
    duration, date, and url where available.
    """
    source_results: list[list[dict]] = []

    # 1. Try authenticated RSS
    rss_url = os.environ.get("PATREON_RSS_URL")
    if rss_url:
        try:
            parsed = urlparse(rss_url)
            if parsed.scheme != "https" or not parsed.netloc:
                raise ValueError("PATREON_RSS_URL must use HTTPS")
            ctx = create_ssl_context()
            req = urllib.request.Request(rss_url, headers={
                "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
                "Accept": "application/xml",
            })
            with urllib.request.urlopen(req, timeout=30, context=ctx) as resp:
                xml_bytes = resp.read()
            rss_posts = _parse_patreon_rss(xml_bytes)
            if rss_posts:
                source_results.append(rss_posts)
            else:
                log.warning("Patreon RSS returned no Afterparty records")
        except Exception as exc:
            log.warning("Patreon RSS fetch failed: %s", type(exc).__name__)

    # 2. Try scraping the public posts page
    public_posts = _scrape_patreon_posts_page()
    if public_posts:
        source_results.append(public_posts)

    # 3. Merge the verified post manifest. This is intentionally consulted even
    # when another source returned records so browser-discovered posts cannot be
    # lost when authenticated RSS is incomplete.
    log.info("Loading the verified Patreon post manifest.")
    manifest_posts = _fetch_patreon_posts_from_list()
    if manifest_posts:
        source_results.append(manifest_posts)

    posts = _merge_patreon_posts(*source_results)
    if posts:
        log.info("Found %d Patreon Afterparty record(s) across configured sources.", len(posts))
    else:
        log.info("No Patreon Afterparty posts found.")
    return posts


def _scrape_patreon_posts_page() -> list[dict]:
    """Scrape the Patreon posts page to find Afterparty episode links.

    The page is JS-rendered, so this may not find any posts.
    Returns empty list if posts are not in the HTML.
    """
    try:
        ctx = create_ssl_context()
        patreon_url = PATREON_URL
        req = urllib.request.Request(patreon_url, headers={
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
            "Accept": "text/html",
        })
        with urllib.request.urlopen(req, timeout=30, context=ctx) as resp:
            html = resp.read().decode("utf-8", errors="replace")
    except Exception as exc:
        log.warning("Patreon page fetch failed: %s", type(exc).__name__)
        return []

    # Extract Next.js data chunks and concatenate
    all_data = ""
    for push in re.finditer(r'self\.__next_f\.push\(\[\d+,\s*"((?:[^"\\]|\\.)*)"\]\)', html):
        try:
            chunk = json.loads('"' + push.group(1) + '"')
            all_data += chunk
        except json.JSONDecodeError:
            continue

    # Look for post URL slugs in the data. Both `afterparty-ID` and
    # `afterparty-z-ID` forms occur in Patreon URLs.
    slug_pattern = re.compile(r'/iMagazinePL/posts/(\d+-afterparty(?:-[a-z]+)?-\d{6,12})')
    all_slugs = slug_pattern.findall(all_data) + slug_pattern.findall(html)

    # Deduplicate and convert to int
    seen = set()
    posts = []
    for slug in all_slugs:
        ep_num = int(slug.split("-", 1)[0])
        if ep_num in seen:
            continue
        seen.add(ep_num)
        page_html = _fetch_patreon_post_page(slug)
        if page_html:
            source_url = f"https://www.patreon.com/iMagazinePL/posts/{slug}"
            parsed = _parse_patreon_post(page_html, source_url)
            if parsed:
                parsed["episode_number"] = str(ep_num)
                posts.append(parsed)

    return posts


def _parse_patreon_rss(xml_bytes: bytes) -> list[dict]:
    """Parse Patreon RSS XML to extract afterparty episodes."""
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError:
        return []
    posts = []
    for item in root.findall(".//item"):
        title_el = item.find("title")
        if title_el is None or title_el.text is None:
            continue
        title = title_el.text.strip()
        if "(Afterparty)" not in title:
            continue
        ep_match = re.match(r"^(\d+):\s*\(Afterparty\)", title)
        if ep_match:
            # Try to get duration from either common iTunes namespace.
            duration = None
            itunes_dur = item.find(".//{http://www.itunes.com/dtds/rss-2.0.dtd}duration")
            if itunes_dur is None:
                itunes_dur = item.find(".//{http://www.itunes.com/dtds/podcast-1.0.dtd}duration")
            if itunes_dur is not None and itunes_dur.text:
                try:
                    secs = int(itunes_dur.text)
                    h = secs // 3600
                    m = (secs % 3600) // 60
                    s = secs % 60
                    duration = f"{h}:{m:02d}:{s:02d}"
                except (ValueError, TypeError):
                    duration = itunes_dur.text
            # Try to get pubDate and canonical source URL.
            date_str = ""
            pub_date = item.find("pubDate")
            if pub_date is not None and pub_date.text:
                date_str = parse_publish_date(pub_date.text.strip())
            link_el = item.find("link")
            guid_el = item.find("guid")
            source_url = link_el.text.strip() if link_el is not None and link_el.text else ""
            if not source_url and guid_el is not None and guid_el.text:
                source_url = guid_el.text.strip()
            posts.append({
                "title": title,
                "episode_number": ep_match.group(1),
                "duration": duration,
                "date": date_str,
                "url": source_url,
            })
    return posts


def merge_patreon_episodes(existing_ep_numbers: set, new_episodes: list) -> list:
    """Check Patreon for afterparty episodes not in the regular feed.

    Patreon Afterparty episodes share episode numbers with main episodes
    (e.g., Afterparty 595 accompanies main episode 595). To avoid collisions,
    we use fractional numbering: Afterparty 595 → 595.5, 596 → 596.5, etc.

    Only adds episodes that are not already in the archive or new_episodes list.
    Returns a list of new Patreon episode dicts.
    """
    patreon_posts = fetch_patreon_posts()
    if not patreon_posts:
        return []

    all_known_numbers = existing_ep_numbers | {e["episode"] for e in new_episodes}
    patreon_new = []

    for post in patreon_posts:
        ep_num = post["episode_number"]
        patreon_ep_id = f"{ep_num}.5"  # Fractional to avoid collision with main episode
        if patreon_ep_id in all_known_numbers:
            continue
        patreon_new.append({
            "counter": "",  # Will be assigned after sorting
            "episode": patreon_ep_id,
            "title": post["title"],
            "date": post.get("date", ""),
            "duration": post.get("duration") or "?",
            "url": post.get("url") or PATREON_POST_URLS.get(patreon_ep_id, ""),
        })
        all_known_numbers.add(patreon_ep_id)

    return patreon_new


def extract_episode_number(title: str) -> str:
    """Extract episode number from title. Returns 'SP' for specials, or a number string."""
    # Try "598: Title" format
    m = re.match(r"^(\d+)([½⅓⅔]?)[\s:]", title.strip())
    if m:
        base = int(m.group(1))
        frac = m.group(2)
        if frac == '½':
            return f"{base}.5"
        elif frac == '⅓':
            return f"{base}.3"
        elif frac == '⅔':
            return f"{base}.6"
        else:
            return str(base)
    # Try old format "Nadgryzieni – 05 – Title"
    m = re.search(r"Nadgryzieni\s*(?:&#8211;|[-–])\s*(\d+)", title, re.I)
    if m:
        return m.group(1)
    # Try "Nadgryzieni – 05 – Title" with different dash
    m = re.search(r"Nadgryzieni\s*[-–]\s*(\d+)\s*[-–]", title, re.I)
    if m:
        return m.group(1)
    return "SP"


# ── Host metadata and record identity ────────────────────────────────────────

HOST_SCHEMA_VERSION = 1
HOST_STATUSES = {"verified", "not_listed", "unavailable", "ambiguous", "manual_review"}
HOST_UNRESOLVED_STATUSES = {"unavailable", "ambiguous", "manual_review"}
HOST_SOURCES = {"rrn", "patreon", "paired_rrn", "manual"}
HOST_ALIAS_POLICY = {
    "mode": "conservative",
    "description": "Only NFKC, whitespace and case-insensitive deduplication are automatic; semantic aliases and name changes are not merged without an explicit reviewed mapping.",
    "aliases": {},
}
HOST_SENTINEL = "Brak danych"


def normalize_identity_text(value: str) -> str:
    """Normalize text used only for deterministic identity/fingerprint keys."""
    return " ".join(unicodedata.normalize("NFKC", str(value or "")).replace("\u00a0", " ").split()).strip()


def canonical_source_url(value: str) -> str:
    """Canonicalize a public source URL for identity comparisons."""
    value = str(value or "").strip()
    if not value:
        return ""
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return value
    path = parsed.path or "/"
    if path != "/":
        path = path.rstrip("/") + "/"
    return parsed._replace(
        scheme=parsed.scheme.lower(),
        netloc=parsed.netloc.lower(),
        path=path,
        fragment="",
    ).geturl()


def build_record_key(row: dict) -> str:
    """Return a stable record key that does not rely on episode number alone or duration."""
    identity = {
        "source_url": canonical_source_url(row.get("url") or row.get("source_url", "")),
        "date": normalize_identity_text(row.get("date", "")),
        "episode": normalize_identity_text(row.get("episode", "")),
        "title": normalize_identity_text(row.get("title", "")),
    }
    digest = hashlib.sha256(
        json.dumps(identity, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()[:24]
    return f"rk_{digest}"


def dataset_fingerprint(rows: list[dict]) -> str:
    """Fingerprint non-host record identity fields for audit/apply reconciliation."""
    canonical_rows = []
    for row in rows:
        canonical_rows.append({
            "record_key": build_record_key(row),
            "source_url": canonical_source_url(row.get("url") or row.get("source_url", "")),
            "episode": normalize_identity_text(row.get("episode", "")),
            "title": normalize_identity_text(row.get("title", "")),
            "date": normalize_identity_text(row.get("date", "")),
            "duration": normalize_identity_text(row.get("duration", "")),
        })
    canonical_rows.sort(key=lambda value: value["record_key"])
    payload = json.dumps(canonical_rows, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _host_dedupe_key(value: str) -> str:
    return normalize_identity_text(value).casefold()


def validate_host_entry(entry: dict, record_key: str = "") -> dict:
    """Validate and normalize one manifest entry without changing host spelling."""
    if not isinstance(entry, dict):
        raise ValueError(f"Host metadata for {record_key or 'record'} is not an object")
    if record_key and entry.get("record_key") not in (None, "", record_key):
        raise ValueError(f"Host metadata record-key mismatch for {record_key}")
    hosts = entry.get("hosts")
    if not isinstance(hosts, list):
        raise ValueError(f"Host metadata for {record_key or 'record'} has no hosts list")
    normalized_hosts = []
    seen = set()
    for host in hosts:
        if not isinstance(host, str) or not host.strip():
            raise ValueError(f"Host metadata for {record_key or 'record'} has an invalid host name")
        display = normalize_identity_text(host)
        if ";" in display or "|" in display:
            raise ValueError(f"Host metadata for {record_key or 'record'} contains a table delimiter")
        key = _host_dedupe_key(display)
        if key in seen:
            raise ValueError(f"Host metadata for {record_key or 'record'} contains duplicate host names")
        seen.add(key)
        normalized_hosts.append(display)
    normalized_hosts.sort(key=_host_dedupe_key)

    status = str(entry.get("hosts_status") or "")
    source = str(entry.get("hosts_source") or "")
    source_url = str(entry.get("hosts_source_url") or "").strip()
    if status not in HOST_STATUSES:
        raise ValueError(f"Host metadata for {record_key or 'record'} has invalid status: {status!r}")
    if source not in HOST_SOURCES:
        raise ValueError(f"Host metadata for {record_key or 'record'} has invalid source: {source!r}")
    if status == "verified" and not normalized_hosts:
        raise ValueError(f"Verified host metadata for {record_key or 'record'} must contain a host")
    if status == "not_listed" and normalized_hosts:
        raise ValueError(f"Not-listed host metadata for {record_key or 'record'} cannot contain hosts")
    if not source_url or urlparse(source_url).scheme not in {"http", "https"} or not urlparse(source_url).netloc:
        raise ValueError(f"Host metadata for {record_key or 'record'} has no valid source URL")
    provenance = entry.get("provenance")
    if provenance is None:
        provenance = {"kind": "direct_source", "source_url": canonical_source_url(source_url)}
    if not isinstance(provenance, dict) or not provenance.get("kind"):
        raise ValueError(f"Host metadata for {record_key or 'record'} has invalid provenance")
    if source == "paired_rrn":
        if provenance.get("kind") != "paired_rrn" or not provenance.get("paired_record_key"):
            raise ValueError(f"Paired host metadata for {record_key or 'record'} lacks its paired record")
        if provenance.get("rule") != "afterparty_same_hosts_from_main":
            raise ValueError(f"Paired host metadata for {record_key or 'record'} lacks the approved rule")
    result = {
        "record_key": record_key,
        "hosts": normalized_hosts,
        "hosts_status": status,
        "hosts_source": source,
        "hosts_source_url": canonical_source_url(source_url),
        "provenance": provenance,
    }
    if isinstance(entry.get("diagnostics"), list):
        result["diagnostics"] = [str(item) for item in entry["diagnostics"]]
    if isinstance(entry.get("audit"), dict):
        result["audit"] = dict(entry["audit"])
    return result


def load_host_metadata(path: Path = HOST_METADATA_PATH) -> dict:
    """Load and validate the tracked host manifest."""
    if not path.exists():
        return {"schema_version": HOST_SCHEMA_VERSION, "alias_policy": HOST_ALIAS_POLICY, "records": {}}
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("schema_version") != HOST_SCHEMA_VERSION or not isinstance(payload.get("records"), dict):
        raise ValueError("host_metadata.json has an unsupported schema")
    alias_policy = payload.get("alias_policy", HOST_ALIAS_POLICY)
    if not isinstance(alias_policy, dict) or alias_policy.get("mode") != "conservative" or not isinstance(alias_policy.get("aliases", {}), dict):
        raise ValueError("host_metadata.json has an invalid alias policy")
    records = {}
    for record_key, entry in payload["records"].items():
        records[str(record_key)] = validate_host_entry(entry, str(record_key))
    return {"schema_version": HOST_SCHEMA_VERSION, "alias_policy": alias_policy, "records": records}


def write_host_metadata(manifest: dict, path: Path = HOST_METADATA_PATH, dry: bool = False) -> None:
    """Write a deterministic manifest, or a .dry artifact when requested."""
    records = manifest.get("records", {})
    normalized = {
        "schema_version": HOST_SCHEMA_VERSION,
        "alias_policy": manifest.get("alias_policy", HOST_ALIAS_POLICY),
        "records": {
            key: validate_host_entry(records[key], key)
            for key in sorted(records)
        },
    }
    target = path if not dry else path.with_name(path.name.replace(".json", ".dry.json"))
    target.write_text(json.dumps(normalized, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    log.info("Host metadata written: %s (%d records)", target, len(normalized["records"]))


def parse_hosts_cell(value: str) -> list[str]:
    """Parse a six-column Hosts cell while accepting the legacy blank value."""
    value = normalize_identity_text(value)
    if not value or value == HOST_SENTINEL:
        return []
    return [normalize_identity_text(part) for part in value.split(";") if normalize_identity_text(part)]


def hosts_cell(row: dict) -> str:
    """Serialize host metadata into the human-readable Markdown cell."""
    hosts = row.get("hosts") or []
    if row.get("hosts_status") in HOST_UNRESOLVED_STATUSES:
        return "Do weryfikacji"
    if row.get("hosts_status") == "not_listed" or not hosts:
        return HOST_SENTINEL
    validated = validate_host_entry({
        "hosts": hosts,
        "hosts_status": row.get("hosts_status", "verified"),
        "hosts_source": row.get("hosts_source", "rrn"),
        "hosts_source_url": row.get("hosts_source_url") or row.get("url", "https://example.invalid/"),
        "provenance": row.get("hosts_provenance") or row.get("provenance"),
    })
    return "; ".join(validated["hosts"])


def apply_host_metadata(rows: list[dict], manifest: dict, strict: bool = False) -> list[dict]:
    """Join manifest entries onto rows by record_key."""
    records = manifest.get("records", {})
    for row in rows:
        row["record_key"] = build_record_key(row)
        entry = records.get(row["record_key"])
        if entry:
            normalized = validate_host_entry(entry, row["record_key"])
            row.update({key: value for key, value in normalized.items() if key != "provenance"})
            row["hosts_provenance"] = normalized["provenance"]
        elif strict:
            raise ValueError(f"No host metadata for record {row['record_key']}")
    return rows


def manifest_from_rows(rows: list[dict], base: dict | None = None, strict: bool = True) -> dict:
    """Build the tracked manifest from enriched rows while preserving old entries."""
    records = dict((base or {}).get("records", {}))
    for row in rows:
        row["record_key"] = build_record_key(row)
        if not row.get("hosts_status"):
            if strict:
                raise ValueError(f"Record {row['record_key']} has no host status")
            continue
        entry_input = {
            "hosts": row.get("hosts", []),
            "hosts_status": row.get("hosts_status"),
            "hosts_source": row.get("hosts_source"),
            "hosts_source_url": row.get("hosts_source_url") or row.get("url"),
            "provenance": row.get("hosts_provenance"),
        }
        previous = records.get(row["record_key"], {})
        for optional_field in ("diagnostics", "audit"):
            if row.get(optional_field) is not None:
                entry_input[optional_field] = row[optional_field]
            elif isinstance(previous, dict) and previous.get(optional_field) is not None:
                entry_input[optional_field] = previous[optional_field]
        entry = validate_host_entry(entry_input, row["record_key"])
        records[row["record_key"]] = entry
    return {
        "schema_version": HOST_SCHEMA_VERSION,
        "alias_policy": (base or {}).get("alias_policy", HOST_ALIAS_POLICY),
        "records": records,
    }


def enrich_host_rows(rows: list[dict], manifest: dict, refresh_keys: set[str] | None = None) -> dict:
    """Fetch only missing/refreshed rows and explicitly pair approved Afterparty rows."""
    import nadgryzieni_hosts as host_tools

    refresh_keys = refresh_keys or set()
    apply_host_metadata(rows, manifest, strict=False)
    rows_by_episode = {
        str(row.get("episode", "")): row
        for row in rows
        if "(afterparty)" not in normalize_identity_text(row.get("title", "")).casefold()
    }
    fetch_cache = {}
    robots_cache = {}
    last_fetch = [0.0]
    pending_pairs = []
    for row in rows:
        row["record_key"] = build_record_key(row)
        needs_refresh = row["record_key"] in refresh_keys or not row.get("hosts_status")
        if not needs_refresh:
            continue
        episode = str(row.get("episode", ""))
        try:
            numeric_episode = float(episode)
        except ValueError:
            numeric_episode = None
        is_afterparty = "(afterparty)" in normalize_identity_text(row.get("title", "")).casefold()
        base_episode = str(int(numeric_episode)) if numeric_episode is not None else ""
        main = rows_by_episode.get(base_episode)
        if is_afterparty and numeric_episode is not None and numeric_episode >= 550 and main is not None:
            pending_pairs.append((row, main))
            continue
        result = host_tools._direct_audit_entry(row, fetch_cache, robots_cache, last_fetch, 0.25)
        row.update({
            "hosts": list(result.get("hosts", [])),
            "hosts_status": result.get("hosts_status", "manual_review"),
            "hosts_source": result.get("hosts_source", "manual"),
            "hosts_source_url": result.get("hosts_source_url", ""),
            "hosts_provenance": result.get("provenance"),
        })
        if result.get("diagnostics"):
            row["diagnostics"] = list(result["diagnostics"])
    for row, main in pending_pairs:
        if main.get("hosts_status") not in {"verified", "not_listed"}:
            raise ValueError(
                f"Cannot pair host metadata for {row.get('episode')}: main record is unresolved"
            )
        row.update({
            "hosts": list(main.get("hosts", [])),
            "hosts_status": main["hosts_status"],
            "hosts_source": "paired_rrn",
            "hosts_source_url": main.get("hosts_source_url") or main.get("url", ""),
            "hosts_provenance": {
                "kind": "paired_rrn",
                "rule": "afterparty_same_hosts_from_main",
                "paired_record_key": main["record_key"],
                "paired_episode": str(main.get("episode", "")),
            },
        })
    return manifest_from_rows(rows, base=manifest, strict=True)


# ── Archive Reading ──────────────────────────────────────────────────────────

def parse_archive(path: Path) -> tuple[list[dict], str]:
    """Parse either the legacy five-column or enriched six-column archive table."""
    content = path.read_text(encoding="utf-8")
    lines = content.splitlines()

    table_start = None
    for i, line in enumerate(lines):
        if line.strip().startswith("|") and "Episode title" in line and "Publish date" in line:
            table_start = i
            break

    if table_start is None:
        raise ValueError("Could not find table in archive")

    header_lines = lines[table_start:table_start + 2]
    header_parts = [part.strip() for part in header_lines[0].strip().split("|")[1:-1]]
    try:
        counter_index = header_parts.index("#")
        episode_index = header_parts.index("Ep.")
        title_index = header_parts.index("Episode title")
        date_index = header_parts.index("Publish date")
        duration_index = header_parts.index("Duration")
    except ValueError as exc:
        raise ValueError("Archive header is missing a required column") from exc
    hosts_index = header_parts.index("Hosts") if "Hosts" in header_parts else None

    # Find table end (blank line after table)
    table_end = table_start + 2
    for i in range(table_start + 2, len(lines)):
        if lines[i].strip() == "":
            table_end = i
            break
    else:
        table_end = len(lines)

    rows = []
    for line in lines[table_start + 2:table_end]:
        stripped = line.strip()
        if not stripped.startswith("|"):
            continue
        parts = [part.strip() for part in stripped.split("|")[1:-1]]
        if len(parts) < len(header_parts) or not parts[counter_index].isdigit():
            continue
        row = {
            "counter": parts[counter_index],
            "episode": parts[episode_index],
            "title": parts[title_index],
            "date": parts[date_index],
            "duration": parts[duration_index],
            "hosts": parse_hosts_cell(parts[hosts_index]) if hosts_index is not None else [],
        }
        if hosts_index is not None and parts[hosts_index] == HOST_SENTINEL:
            row["hosts_status"] = "not_listed"
        elif hosts_index is not None and parts[hosts_index] == "Do weryfikacji":
            row["hosts"] = []
            row["hosts_status"] = "manual_review"
        rows.append(row)

    return rows, "\n".join(header_lines)


def _row_match_key(row: dict) -> tuple[str, str, str, str, str]:
    return (
        normalize_identity_text(row.get("episode", "")),
        normalize_identity_text(row.get("title", "")),
        normalize_identity_text(row.get("date", "")),
        normalize_identity_text(row.get("duration", "")),
        normalize_identity_text(row.get("counter", "")),
    )


def attach_existing_data(rows: list[dict], data: dict) -> list[dict]:
    """Restore source URLs and durable fields when reading a legacy archive table."""
    episodes = data.get("episodes", []) if isinstance(data, dict) else []
    by_exact: dict[tuple[str, str, str, str], list[dict]] = defaultdict(list)
    by_title: dict[str, list[dict]] = defaultdict(list)
    for episode in episodes:
        key = (
            normalize_identity_text(episode.get("episode", "")),
            normalize_identity_text(episode.get("title", "")),
            normalize_identity_text(episode.get("date", "")),
            normalize_identity_text(episode.get("duration", "")),
        )
        by_exact[key].append(episode)
        by_title[normalize_lookup_title(episode.get("title", ""))].append(episode)

    for row in rows:
        exact_key = _row_match_key(row)[:4]
        matches = by_exact.get(exact_key, [])
        if len(matches) == 1:
            source = matches[0]
        else:
            title_matches = by_title.get(normalize_lookup_title(row.get("title", "")), [])
            source = title_matches[0] if len(title_matches) == 1 else None
        if source:
            for field in ("url", "record_key", "hosts", "hosts_status", "hosts_source", "hosts_source_url", "hosts_provenance"):
                if source.get(field) not in (None, ""):
                    row[field] = source[field]
        row["record_key"] = build_record_key(row)
    return rows


# ── Archive Writing ──────────────────────────────────────────────────────────

def pad_field(text: str, width: int) -> str:
    """Left-pad text to width with spaces (markdown table padding)."""
    return text.ljust(width)


def write_archive(rows: list[dict], dry: bool = False, target_path: Path | None = None) -> None:
    """Write the archive markdown table with column padding."""
    cw = COL_CONTENT_WIDTHS

    header_values = ["#", "Ep.", "Episode title", "Publish date", "Duration", "Hosts"]
    header = "| " + " | ".join(pad_field(value, width) for value, width in zip(header_values, cw)) + " |"
    separator = "| " + " | ".join("-" * width for width in cw) + " |"

    lines = [header, separator]
    for r in rows:
        host_value = hosts_cell(r)
        line = "| " + " | ".join(pad_field(value, width) for value, width in zip(
            [r["counter"], r["episode"], r["title"], r["date"], r["duration"], host_value],
            cw,
        )) + " |"
        lines.append(line)

    content = "\n".join(lines) + "\n"

    target = target_path or (ARCHIVE_PATH if not dry else ARCHIVE_PATH.with_name(
        ARCHIVE_PATH.name.replace(".md", ".dry.md")
    ))
    target.write_text(content, encoding="utf-8")
    log.info(f"Archive written: {target} ({len(rows)} rows)")


# ── data.json Generation ─────────────────────────────────────────────────────

def duration_to_minutes(duration: str) -> float | None:
    """Convert HH:MM:SS or MM:SS to minutes (float)."""
    if not duration or duration == "?":
        return None
    parts = duration.split(":")
    try:
        if len(parts) == 3:
            h, m, s = int(parts[0]), int(parts[1]), int(parts[2])
            return h * 60 + m + s / 60
        elif len(parts) == 2:
            m, s = int(parts[0]), int(parts[1])
            return m + s / 60
        else:
            return int(parts[0])
    except (ValueError, IndexError):
        return None


def detect_category(title: str, episode: str) -> str:
    """Detect episode sub-series category from title and episode number.
    
    Priority order:
    1. SP episode number → sp
    2. (Live) → live
    3. (Po Godzinach) → po_godzinach
    4. ½ fraction → half (only ½, not ⅓ or ⅔)
    5. (Special) → special
    6. (Afterparty) → afterparty
    7. (Prawie) → prawie
    8. (Na Placu Budowy) → na_placu_budowy
    9. (Na Spacerze) → na_spacerze
    10. (Video) → video
    11. (W Biegu) → w_biegu
    12. default → main
    """
    t = title.lower()

    # Check for SP episode numbers
    if episode == "SP" or title.startswith("SP") or title.startswith("#SP"):
        return "sp"

    # Check for Live sub-series
    if "(live)" in t or "(live" in t:
        return "live"

    # Check for Po Godzinach sub-series
    if "(po godzinach)" in t or "po godzinach" in t:
        return "po_godzinach"

    # Check for ½ fraction (only ½, not ⅓ or ⅔)
    if "½" in title:
        return "half"

    # Check remaining sub-series tags
    if "(special)" in t:
        return "special"
    elif "(afterparty)" in t:
        return "afterparty"
    elif "(prawie)" in t:
        return "prawie"
    elif "(na placu budowy)" in t:
        return "na_placu_budowy"
    elif "(na spacerze)" in t:
        return "na_spacerze"
    elif "(video)" in t or "video" in t:
        return "video"
    elif "(w biegu)" in t:
        return "w_biegu"
    return "main"


def minutes_to_duration_str(minutes: float) -> str:
    """Convert minutes float to H:MM:SS or MM:SS string."""
    total_seconds = int(round(minutes * 60))
    h = total_seconds // 3600
    m = (total_seconds % 3600) // 60
    s = total_seconds % 60
    if h:
        return f"{h}:{m:02d}:{s:02d}"
    else:
        return f"{m}:{s:02d}"


def normalize_lookup_title(title: str) -> str:
    """Normalize title whitespace for matching RSS and archive records."""
    import unicodedata
    return " ".join(unicodedata.normalize("NFKC", title).replace("\u00a0", " ").split()).strip()


def resolve_existing_source_url(item: dict, rows: list[dict]) -> str:
    """Prefer an existing canonical URL when RSS misassigns a historical link."""
    title = normalize_lookup_title(item.get("title", ""))
    episode = normalize_identity_text(item.get("episode_number", ""))
    date_value = normalize_identity_text(item.get("date", ""))
    duration = normalize_identity_text(item.get("duration", ""))
    matches = [
        row for row in rows
        if normalize_identity_text(row.get("episode", "")) == episode
        and normalize_lookup_title(row.get("title", "")) == title
        and normalize_identity_text(row.get("date", "")) == date_value
        and (not duration or normalize_identity_text(row.get("duration", "")) == duration)
        and row.get("url")
    ]
    if len(matches) == 1:
        return str(matches[0]["url"])
    return str(item.get("url") or "")


def generate_data_json(rows: list[dict], url_by_title: dict[str, str | list[str]] | None = None) -> dict:
    """Generate the data.json structure for Chart.js."""
    episodes = []
    durations_min = []
    year_counts = Counter()
    url_by_title = url_by_title or {}

    for r in rows:
        minutes = duration_to_minutes(r["duration"])
        year = ""
        if r["date"] and len(r["date"]) >= 4:
            year = r["date"][:4]
            try:
                year_counts[int(year)] += 1
            except ValueError:
                pass

        if minutes is not None:
            durations_min.append(minutes)

        category = detect_category(r["title"], r["episode"])
        dur_str = r["duration"] if r["duration"] else "?"
        source_url = r.get("url", "")
        if not source_url:
            fallback = url_by_title.get(normalize_lookup_title(r["title"]))
            if isinstance(fallback, list):
                source_url = fallback[0] if len(set(fallback)) == 1 else ""
            elif isinstance(fallback, str):
                source_url = fallback

        hosts = sorted(
            [normalize_identity_text(host) for host in (r.get("hosts") or [])],
            key=_host_dedupe_key,
        )
        hosts_status = r.get("hosts_status") or "not_listed"
        hosts_source = r.get("hosts_source") or ("rrn" if "retrorocketnetwork.pl" in source_url else "manual")
        hosts_source_url = canonical_source_url(r.get("hosts_source_url") or source_url)
        record_key = build_record_key(r)

        episodes.append({
            "record_key": record_key,
            "episode": r["episode"],
            "title": r["title"],
            "date": r["date"],
            "duration": dur_str,
            "minutes": round(minutes, 2) if minutes is not None else None,
            "category": category,
            "year": int(year) if year else None,
            "url": source_url,
            "hosts": hosts,
            "hosts_status": hosts_status,
            "hosts_source": hosts_source,
            "hosts_source_url": hosts_source_url,
            "hosts_provenance": r.get("hosts_provenance"),
        })

    # Stats
    total_episodes = len(episodes)
    total_seconds = sum(m * 60 for m in durations_min) if durations_min else 0
    total_hours = round(total_seconds / 3600, 1) if total_seconds else 0
    avg_duration = round(sum(durations_min) / len(durations_min), 1) if durations_min else 0
    max_duration = round(max(durations_min), 2) if durations_min else 0
    min_duration = round(min(durations_min), 2) if durations_min else 0

    categories = {
        "main": {"label": "Główne", "color": "#F43E25"},
        "live": {"label": "Live", "color": "#cae153"},
        "po_godzinach": {"label": "Po Godzinach", "color": "#47CFEB"},
        "sp": {"label": "SP (specjalne)", "color": "#632F53"},
        "half": {"label": "Półodcinki", "color": "#999"},
        "special": {"label": "Specjalne", "color": "#9b51e0"},
        "afterparty": {"label": "Afterparty", "color": "#ff6900"},
        "prawie": {"label": "Prawie", "color": "#ff6900"},
        "na_placu_budowy": {"label": "Na Placu Budowy", "color": "#0693e3"},
        "na_spacerze": {"label": "Na Spacerze", "color": "#00d084"},
        "video": {"label": "Video", "color": "#8ed1fc"},
        "w_biegu": {"label": "W Biegu", "color": "#7bdcb5"},
    }

    data = {
        "episodes": episodes,
        "stats": {
            "total_episodes": total_episodes,
            "total_listening_hours": total_hours,
            "average_duration": avg_duration,
            "min_duration": min_duration,
            "max_duration": max_duration,
        },
        "categories": categories,
        "average_duration": avg_duration,
    }
    return data


def validate_generated_data(data: dict, rows: list[dict]) -> None:
    """Reject incomplete or inconsistent generated output before publishing."""
    episodes = data.get("episodes")
    if not isinstance(episodes, list) or len(episodes) != len(rows):
        raise ValueError("Generated episode count does not match the archive")

    identifiers = [str(episode.get("episode", "")) for episode in episodes]
    if any(not identifier for identifier in identifiers):
        raise ValueError("Generated data contains an empty episode identifier")

    expected_ids = [str(row.get("episode", "")) for row in rows]
    if identifiers != expected_ids:
        raise ValueError("Generated data order or identifiers do not match the archive")

    record_keys = [str(episode.get("record_key", "")) for episode in episodes]
    if any(not record_key for record_key in record_keys) or len(set(record_keys)) != len(record_keys):
        raise ValueError("Generated data contains missing or duplicate record keys")

    for episode, row in zip(episodes, rows):
        url = episode.get("url", "")
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError(f"Episode {episode.get('episode')} has no canonical source URL")
        if not episode.get("title") or not episode.get("date"):
            raise ValueError(f"Episode {episode.get('episode')} is missing title or date")
        if episode.get("record_key") != build_record_key(row):
            raise ValueError(f"Episode {episode.get('episode')} has a record-key mismatch")
        hosts = episode.get("hosts")
        if not isinstance(hosts, list):
            raise ValueError(f"Episode {episode.get('episode')} has no hosts list")
        validate_host_entry({
            "hosts": hosts,
            "hosts_status": episode.get("hosts_status"),
            "hosts_source": episode.get("hosts_source"),
            "hosts_source_url": episode.get("hosts_source_url"),
            "provenance": row.get("hosts_provenance") or episode.get("hosts_provenance"),
        }, episode.get("record_key", ""))
        if episode.get("hosts_status") in HOST_UNRESOLVED_STATUSES:
            raise ValueError(f"Episode {episode.get('episode')} has unresolved host metadata")

    stats = data.get("stats", {})
    if stats.get("total_episodes") != len(episodes):
        raise ValueError("Generated statistics exclude one or more episodes")
    category_count = sum(1 for episode in episodes if episode.get("category") == "afterparty")
    expected_afterparty = sum(1 for row in rows if detect_category(row["title"], row["episode"]) == "afterparty")
    if category_count != expected_afterparty:
        raise ValueError("Afterparty category count does not match the archive")


def write_data_json(data: dict, dry: bool = False, target_path: Path | None = None) -> None:
    target = target_path or (DATA_JSON_PATH if not dry else DATA_JSON_PATH.with_name(
        DATA_JSON_PATH.name.replace(".json", ".dry.json")
    ))
    target.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    log.info(f"data.json written: {target} ({len(data['episodes'])} episodes)")


# ── README Update ──────────────────────────────────────────────────────────────

def update_readme(data: dict, dry: bool = False, target_path: Path | None = None) -> None:
    """Update the README.md stats table with current numbers."""
    if not README_PATH.exists():
        log.warning(f"README.md not found at {README_PATH}")
        return

    content = README_PATH.read_text(encoding="utf-8")
    stats = data["stats"]
    total = stats["total_episodes"]
    total_hours = stats["total_listening_hours"]
    avg_duration = stats["average_duration"]
    max_duration = stats["max_duration"]
    afterparty_count = sum(1 for episode in data.get("episodes", []) if episode.get("category") == "afterparty")

    # Update the stats table in README.md
    # Pattern: | Liczba odcinków | <number> |
    content = re.sub(
        r"\| Liczba odcinków \| \d+ \|",
        f"| Liczba odcinków | {total} |",
        content,
    )
    content = re.sub(
        r"\| Godziny odsłuchu \| [\d.]+ \|",
        f"| Godziny odsłuchu | {total_hours} |",
        content,
    )
    content = re.sub(
        r"\| Średnia długość \| [\d.]+ min \|",
        f"| Średnia długość | {avg_duration} min |",
        content,
    )
    content = re.sub(
        r"\| Maksymalna długość \| [\d.]+ min \|",
        f"| Maksymalna długość | {max_duration} min |",
        content,
    )
    content = re.sub(
        r"\| Afterparty \| \d+ \|",
        f"| Afterparty | {afterparty_count} |",
        content,
    )

    target = target_path or (README_PATH if not dry else README_PATH.with_name(
        README_PATH.name.replace(".md", ".dry.md")
    ))
    target.write_text(content, encoding="utf-8")
    log.info(f"README.md updated: {target} ({total} episodes)")


# ── Obsidian Vault Sync ────────────────────────────────────────────────────────

def sync_to_obsidian(dry: bool = False) -> None:
    """Sync archive and statistics files to the Obsidian vault directory."""
    if not VAULT_DIR.exists():
        if dry:
            log.info(f"[DRY RUN] Vault directory not found: {VAULT_DIR}")
            return
        raise RuntimeError(f"Vault directory not found: {VAULT_DIR}")

    synced = 0

    # Sync archive
    src_archive = ARCHIVE_PATH
    dst_archive = VAULT_DIR / "Nadgryzieni Episode Archive.md"
    if not src_archive.exists():
        raise RuntimeError(f"Archive source not found: {src_archive}")
    if not dry:
        dst_archive.write_bytes(src_archive.read_bytes())
        if dst_archive.read_bytes() != src_archive.read_bytes():
            raise RuntimeError("Obsidian archive verification failed")
    log.info(f"{'[DRY RUN] Would sync' if dry else 'Synced'} archive to vault: {dst_archive}")
    synced += 1

    # Sync statistics
    src_stats = STATS_PATH
    dst_stats = VAULT_DIR / "Nadgryzieni Statistics.md"
    if not src_stats.exists():
        raise RuntimeError(f"Statistics source not found: {src_stats}")
    if not dry:
        dst_stats.write_bytes(src_stats.read_bytes())
        if dst_stats.read_bytes() != src_stats.read_bytes():
            raise RuntimeError("Obsidian statistics verification failed")
    log.info(f"{'[DRY RUN] Would sync' if dry else 'Synced'} statistics to vault: {dst_stats}")
    synced += 1

    log.info(f"Obsidian vault sync complete: {synced} file(s) synced")


# ── Statistics Generation ────────────────────────────────────────────────────

def generate_statistics(rows: list[dict]) -> str:
    """Generate comprehensive statistics markdown from archive rows."""
    total = len(rows)
    durations = []
    for r in rows:
        m = duration_to_minutes(r["duration"])
        if m is not None:
            durations.append(m)

    with_duration = len(durations)
    unknown_count = total - with_duration

    if durations:
        avg_min = sum(durations) / len(durations)
        sorted_d = sorted(durations)
        n = len(sorted_d)
        median_min = (sorted_d[n // 2 - 1] + sorted_d[n // 2]) / 2 if n % 2 == 0 else sorted_d[n // 2]
        total_secs = sum(m * 60 for m in durations)
        total_h = int(total_secs // 3600)
        total_m = int((total_secs % 3600) // 60)
        max_min = max(durations)
        min_min = min(durations)
    else:
        avg_min = 0
        median_min = 0
        total_h = 0
        total_m = 0
        max_min = 0
        min_min = 0

    # Episodes per year
    year_counts = Counter()
    year_durations = defaultdict(list)
    for r in rows:
        if r["date"] and len(r["date"]) >= 4:
            year = r["date"][:4]
            try:
                year_int = int(year)
                year_counts[year_int] += 1
                m = duration_to_minutes(r["duration"])
                if m is not None:
                    year_durations[year_int].append(m)
            except ValueError:
                pass

    # Date range
    dates = []
    for r in rows:
        if r["date"] and len(r["date"]) >= 10:
            try:
                dt = datetime.strptime(r["date"][:10], "%Y-%m-%d")
                dates.append(dt)
            except ValueError:
                pass

    today = date.today().isoformat()
    date_range = f"{min(dates).strftime('%Y-%m-%d')} — {max(dates).strftime('%Y-%m-%d')}" if dates else "unknown"

    # Month distribution
    month_counts = Counter()
    for r in rows:
        if r["date"] and len(r["date"]) >= 7:
            try:
                dt = datetime.strptime(r["date"][:7], "%Y-%m")
                month_counts[dt.month] += 1
            except ValueError:
                pass

    month_names = {
        1: "January", 2: "February", 3: "March", 4: "April",
        5: "May", 6: "June", 7: "July", 8: "August",
        9: "September", 10: "October", 11: "November", 12: "December",
    }

    # Day of week distribution
    dow_counts = Counter()
    for r in rows:
        if r["date"] and len(r["date"]) >= 10:
            try:
                dt = datetime.strptime(r["date"][:10], "%Y-%m-%d")
                dow_counts[dt.strftime("%A")] += 1
            except ValueError:
                pass

    # Duration distribution brackets
    brackets = [
        ("Under 30 min", sum(1 for m in durations if m < 30)),
        ("30 min – 1 hour", sum(1 for m in durations if 30 <= m < 60)),
        ("1 hour – 2 hours", sum(1 for m in durations if 60 <= m < 120)),
        ("2 hours – 3 hours", sum(1 for m in durations if 120 <= m < 180)),
        ("3 hours – 4 hours", sum(1 for m in durations if 180 <= m < 240)),
        ("Over 4 hours", sum(1 for m in durations if m >= 240)),
    ]

    # Category counts
    cat_counts = Counter()
    for r in rows:
        cat_counts[detect_category(r["title"], r["episode"])] += 1

    # Most common durations
    dur_counter = Counter()
    for r in rows:
        if r["duration"] and r["duration"] != "?":
            dur_counter[r["duration"]] += 1

    # Title word frequency
    word_counts = Counter()
    for r in rows:
        words = re.findall(r'\b[a-zA-ZążźćńółęśćĄŻŹĆŃÓŁĘŚĆ]+\b', r["title"].lower())
        for w in words:
            if len(w) > 2:
                word_counts[w] += 1

    # Build markdown
    md = f"""# Nadgryzieni — Statistics

> Generated from the episode archive: `Nadgryzieni Episode Archive.md`
> Last updated: {today}

---

## Overview

| Metric | Value |
|---|---|
| **Total episodes** | {total} |
| **Date range** | {date_range} |
| **Episodes with duration** | {with_duration} |
| **Episodes without duration** | {unknown_count} |

---

## Duration Statistics

| Metric | Value |
|---|---|
| **Total listening time** | {total_h}h {total_m}m ({total_h + total_m/60:.1f} hours) |
| **Average duration** | {avg_min:.1f} min ({avg_min/60:.1f}h) |
| **Median duration** | {median_min:.1f} min ({median_min/60:.1f}h) |
| **Longest episode** | {max_min:.1f} min |
| **Shortest episode** | {min_min:.1f} min |
| **Episodes over 2 hours** | {sum(1 for m in durations if m > 120)} |
| **Episodes under 30 minutes** | {sum(1 for m in durations if m < 30)} |

---

## Duration by Year (Average)

| Year | Episodes | Avg Duration |
|---|---|---|
"""
    for year in sorted(year_counts.keys()):
        year_durs = year_durations.get(year, [])
        avg = sum(year_durs) / len(year_durs) if year_durs else 0
        md += f"| {year} | {year_counts[year]} | {avg:.1f} min |\n"

    md += """
---

## Duration Distribution

| Bracket | Count | % |
|---|---|---|
"""
    for bracket, count in brackets:
        pct = count / total * 100 if total else 0
        md += f"| {bracket} | {count} | {pct:.1f}% |\n"

    md += """
---

## Episodes per Year

| Year | Count |
|---|---|
"""
    for year in sorted(year_counts.keys(), reverse=True):
        md += f"| {year} | {year_counts[year]} |\n"

    md += """
---

## Monthly Distribution

| Month | Count | % |
|---|---|---|
"""
    for month in range(1, 13):
        count = month_counts.get(month, 0)
        pct = count / total * 100 if total else 0
        md += f"| {month_names[month]} | {count} | {pct:.1f}% |\n"

    md += """
---

## Publish Day Distribution

| Day | Count | % |
|---|---|---|
"""
    day_order = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
    for day in day_order:
        count = dow_counts.get(day, 0)
        pct = count / total * 100 if total else 0
        md += f"| {day} | {count} | {pct:.1f}% |\n"

    md += """
---

## Sub-Series & Categories

| Category | Count |
|---|---|
"""
    cat_labels = {
        "main": "Main episodes",
        "live": "(Live)",
        "po_godzinach": "(Po Godzinach)",
        "sp": "SP (special)",
        "half": "Half/decimal episodes",
        "special": "(Special)",
        "afterparty": "(Afterparty)",
        "prawie": "(Prawie)",
        "na_placu_budowy": "(Na Placu Budowy)",
        "na_spacerze": "(Na Spacerze)",
        "video": "(Video)",
        "w_biegu": "(W Biegu)",
    }
    for cat in sorted(cat_counts.keys(), key=lambda c: cat_counts[c], reverse=True):
        label = cat_labels.get(cat, cat)
        md += f"| {label} | {cat_counts[cat]} |\n"

    md += f"""
---

## Top 10 Most Common Durations

| Duration | Episodes |
|---|---|
"""
    for dur, count in dur_counter.most_common(10):
        md += f"| {dur} | {count} |\n"

    md += f"""
---

## Top 20 Most Frequent Words in Titles

| Word | Count |
|---|---|
"""
    for word, count in word_counts.most_common(20):
        md += f"| {word} | {count} |\n"

    md += f"""
---

## Summary

- **Total episodes:** {total}
- **Total listening time:** {total_h}h {total_m}m
- **Average duration:** {avg_min:.1f} min
- **Median duration:** {median_min:.1f} min
- **Date range:** {date_range}

---

*Last updated: {today}*
"""
    return md


def write_stats(content: str, dry: bool = False) -> None:
    target = STATS_PATH if not dry else STATS_PATH.with_name(
        STATS_PATH.name.replace(".md", ".dry.md")
    )
    target.write_text(content, encoding="utf-8")
    log.info(f"Statistics written: {target}")


# ── Conditional Retry State ──────────────────────────────────────────────────

def write_retry_state(path: Path, primary_date: date, pending: bool) -> None:
    """Atomically persist whether the next Tuesday retry should run."""
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        path.parent.chmod(0o700)
    except OSError:
        pass
    payload = {
        "primary_date": primary_date.isoformat(),
        "pending": bool(pending),
        "updated_at": datetime.now().isoformat(timespec="seconds"),
    }
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    try:
        temporary.chmod(0o600)
    except OSError:
        pass
    temporary.replace(path)


def retry_is_due(path: Path, today: date | None = None) -> bool:
    """Return true only for the Tuesday window immediately after a no-new Saturday run."""
    if not path.exists():
        return False
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        primary_date = date.fromisoformat(payload["primary_date"])
    except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError):
        return False
    today = today or date.today()
    return bool(payload.get("pending")) and today - primary_date == timedelta(days=3)


def acquire_pipeline_lock() -> bool:
    """Acquire a process lock that is automatically released if the process exits."""
    global _LOCK_HANDLE
    import fcntl

    STATE_DIR.mkdir(parents=True, exist_ok=True)
    try:
        STATE_DIR.chmod(0o700)
    except OSError:
        pass
    _LOCK_HANDLE = LOCK_PATH.open("a+", encoding="utf-8")
    try:
        fcntl.flock(_LOCK_HANDLE.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        _LOCK_HANDLE.close()
        _LOCK_HANDLE = None
        log.info("Another Nadgryzieni pipeline run is active; skipping this invocation.")
        return False
    return True


# ── Cache-Busting ────────────────────────────────────────────────────────────

def get_cache_version() -> int:
    """Get or initialize the cache-busting version number."""
    if CACHE_VERSION_FILE.exists():
        try:
            return int(CACHE_VERSION_FILE.read_text().strip())
        except (OSError, ValueError):
            pass
    versions = []
    for path, pattern in (
        (INDEX_HTML_PATH, r"\?v=(\d+)"),
        (SCRIPT_JS_PATH, r"DATA_VERSION\s*=\s*(\d+)")
    ):
        try:
            versions.extend(int(value) for value in re.findall(pattern, path.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            continue
    return max(versions, default=100)


def bump_cache_version(dry: bool = False) -> int:
    """Increment and save the cache-busting version number."""
    v = get_cache_version() + 1
    if not dry:
        CACHE_VERSION_FILE.write_text(str(v) + "\n", encoding="utf-8")
    return v


def update_cache_busting(version: int, dry: bool = False) -> None:
    """Update ?v=N references in index.html and script.js."""
    if dry:
        log.info(f"[DRY RUN] Would bump cache-busting to v={version}")
        return

    # Update index.html
    if INDEX_HTML_PATH.exists():
        html = INDEX_HTML_PATH.read_text(encoding="utf-8")
        html = re.sub(r'style\.css\?v=\d+', f'style.css?v={version}', html)
        html = re.sub(r'script\.js\?v=\d+', f'script.js?v={version}', html)
        INDEX_HTML_PATH.write_text(html, encoding="utf-8")
        log.info(f"index.html cache-busting updated to v={version}")

    # Update script.js data version constant
    if SCRIPT_JS_PATH.exists():
        js = SCRIPT_JS_PATH.read_text(encoding="utf-8")
        js = re.sub(r"const DATA_VERSION = \d+;", f"const DATA_VERSION = {version};", js)
        SCRIPT_JS_PATH.write_text(js, encoding="utf-8")
        log.info(f"script.js DATA_VERSION cache-busting updated to v={version}")


# ── Git Operations ───────────────────────────────────────────────────────────

def git_commit_and_push(message: str, dry: bool = False) -> bool:
    """Stage only generated files, commit if needed, and retry the push safely."""
    if dry:
        log.info(f"[DRY RUN] Would commit: {message}")
        return False

    repo = str(REPO_DIR)
    status_cmd = ["git", "status", "--porcelain", "--untracked-files=all", "--", *PUBLISH_PATHS]
    status = subprocess.run(status_cmd, cwd=repo, capture_output=True, text=True)
    if status.returncode != 0:
        raise RuntimeError(f"Git status failed: {status.stderr.strip()}")
    if not status.stdout.strip():
        log.info("No generated file changes to commit.")
        return False

    add_cmd = ["git", "add", "--", *PUBLISH_PATHS]
    added = subprocess.run(add_cmd, cwd=repo, capture_output=True, text=True)
    if added.returncode != 0:
        raise RuntimeError(f"Git staging failed: {added.stderr.strip()}")

    staged = subprocess.run(["git", "diff", "--cached", "--quiet"], cwd=repo)
    if staged.returncode == 0:
        log.info("Generated files produced no staged diff.")
        return False
    if staged.returncode != 1:
        raise RuntimeError("Could not inspect the staged Git diff")

    commit = subprocess.run(
        ["git", "commit", "-m", message],
        cwd=repo,
        capture_output=True,
        text=True,
    )
    if commit.returncode != 0:
        raise RuntimeError(f"Git commit failed: {commit.stderr.strip()}")

    push_cmd = ["git", "push", "origin", "HEAD:main"]
    for attempt in range(1, 4):
        pushed = subprocess.run(push_cmd, cwd=repo, capture_output=True, text=True)
        if pushed.returncode == 0:
            log.info("Git commit and push complete")
            return True
        log.warning("Git push attempt %d/3 failed: %s", attempt, pushed.stderr.strip())
        if attempt < 3:
            time.sleep(2 ** attempt)
    raise RuntimeError("Git push failed after 3 attempts")


# ── Main Pipeline ────────────────────────────────────────────────────────────

def run_pipeline(dry: bool = False, force: bool = False, refresh_hosts: bool = False) -> int:
    log.info("=" * 60)
    log.info("Nadgryzieni Pipeline — starting")
    mode_label = 'DRY RUN' if dry else 'LIVE'
    if refresh_hosts:
        mode_label += ' + REFRESH HOSTS'
    log.info(f"Mode: {mode_label}")
    log.info("=" * 60)

    # Step 1: Fetch RSS
    log.info("Step 1: Fetching RSS feed...")
    xml_bytes = fetch_rss()
    items = parse_rss_items(xml_bytes)
    log.info(f"  Parsed {len(items)} episodes from RSS")

    # Preserve known URLs and refresh them from the current RSS feed.
    url_by_title: dict[str, list[str]] = defaultdict(list)
    current_data = {}
    if DATA_JSON_PATH.exists():
        try:
            current_data = json.loads(DATA_JSON_PATH.read_text(encoding="utf-8"))
            for episode in current_data.get("episodes", []):
                title_key = normalize_lookup_title(episode.get("title", ""))
                if title_key and episode.get("url") and episode["url"] not in url_by_title[title_key]:
                    url_by_title[title_key].append(episode["url"])
        except (OSError, json.JSONDecodeError):
            log.warning("Could not read existing episode URLs; rebuilding from RSS")
    for item in items:
        title_key = normalize_lookup_title(item.get("title", ""))
        if title_key and item.get("url") and item["url"] not in url_by_title[title_key]:
            url_by_title[title_key].append(item["url"])

    # Step 2: Load existing archive
    log.info("Step 2: Loading existing archive...")
    existing_rows, _ = parse_archive(ARCHIVE_PATH)
    attach_existing_data(existing_rows, current_data)
    log.info(f"  Archive has {len(existing_rows)} rows")

    # Build both stable-record and episode-number lookups. Episode numbers are not unique.
    existing_record_keys = {build_record_key(row) for row in existing_rows}
    existing_ep_numbers = set()
    for r in existing_rows:
        # Extract just the numeric part for comparison
        ep_str = r["episode"].strip()
        existing_ep_numbers.add(ep_str)

    # Step 3: Find new episodes
    log.info("Step 3: Checking for new episodes...")
    new_episodes = []
    for item in items:
        ep_num = item["episode_number"]
        candidate = {
            "counter": str(len(existing_rows) + len(new_episodes) + 1),
            "episode": ep_num,
            "title": item["title"],
            "date": item["date"],
            "duration": item["duration"] if item["duration"] else "?",
            "url": resolve_existing_source_url(item, existing_rows),
        }
        candidate["record_key"] = build_record_key(candidate)
        if candidate["record_key"] in existing_record_keys:
            continue
        new_episodes.append(candidate)
        existing_record_keys.add(candidate["record_key"])
        existing_ep_numbers.add(ep_num)

    # Step 3b: Check Patreon for afterparty episodes
    log.info("Step 3b: Checking Patreon for afterparty episodes...")
    patreon_new = merge_patreon_episodes(existing_ep_numbers, new_episodes)
    if patreon_new:
        log.info(f"  Found {len(patreon_new)} Patreon afterparty episode(s):")
        for ep in patreon_new:
            log.info(f"    #{ep['episode']}: {ep['title'][:60]}...")
        new_episodes.extend(patreon_new)
        existing_ep_numbers.update(ep["episode"] for ep in patreon_new)
        for ep in patreon_new:
            title_key = normalize_lookup_title(ep.get("title", ""))
            if title_key and ep.get("url") and ep["url"] not in url_by_title[title_key]:
                url_by_title[title_key].append(ep["url"])

    if not new_episodes and not force and not refresh_hosts:
        log.info("No new episodes found. Exiting silently.")
        return 0

    if new_episodes:
        log.info(f"  Found {len(new_episodes)} new episode(s):")
        for ep in new_episodes:
            log.info(f"    #{ep['episode']}: {ep['title'][:60]}...")

    # Step 4: Merge and write archive
    log.info("Step 4: Updating archive...")
    all_rows = existing_rows + new_episodes

    # Sort by publish date (oldest first)
    # SP episodes (non-numeric) are interleaved by date
    # For episodes with same date, sort by episode number
    def sort_key(r):
        date_str = r["date"] if r["date"] else "9999-12-31"
        ep = r["episode"]
        # Numeric episodes: use episode number as secondary sort
        # SP episodes: use 0 as secondary sort (sorts first among same date)
        try:
            ep_num = float(ep)
        except ValueError:
            ep_num = 0
        return (date_str, ep_num, r["title"])
    all_rows.sort(key=sort_key)

    # Reassign counters after sorting
    for i, r in enumerate(all_rows, start=1):
        r["counter"] = str(i)

    # Host enrichment is mandatory before generated output is published.
    log.info("Step 4b: Enriching host metadata...")
    if not HOST_METADATA_PATH.exists():
        raise RuntimeError(
            "host_metadata.json is missing; run `python3 nadgryzieni_hosts.py audit ...` "
            "and apply the verified audit before the weekly pipeline"
        )
    host_manifest = load_host_metadata()
    refresh_keys = {row["record_key"] for row in all_rows} if refresh_hosts else {
        build_record_key(row) for row in new_episodes
    }
    host_manifest = enrich_host_rows(all_rows, host_manifest, refresh_keys=refresh_keys)
    apply_host_metadata(all_rows, host_manifest, strict=True)

    write_archive(all_rows, dry=dry)

    # Step 5: Generate data.json
    log.info("Step 5: Generating data.json...")
    data = generate_data_json(all_rows, url_by_title)
    validate_generated_data(data, all_rows)
    write_data_json(data, dry=dry)
    write_host_metadata(host_manifest, path=HOST_METADATA_PATH, dry=dry)

    # Step 5b: Update README.md stats table
    log.info("Step 5b: Updating README.md...")
    update_readme(data, dry=dry)

    # Step 6: Generate statistics when publishing or explicitly forced.
    if new_episodes or force or refresh_hosts:
        log.info("Step 6: Generating statistics...")
        stats_md = generate_statistics(all_rows)
        write_stats(stats_md, dry=dry)
    else:
        log.info("Step 6: Skipping statistics (no new episodes).")

    # Step 7: Bump cache-busting when publishing or explicitly forced.
    if new_episodes or force or refresh_hosts:
        log.info("Step 7: Bumping cache-busting version...")
        new_version = bump_cache_version(dry=dry)
        update_cache_busting(new_version, dry=dry)
    else:
        log.info("Step 7: Skipping cache-busting bump (no new episodes).")

    # Step 8: Sync to Obsidian before publishing, then verify Git changes.
    if new_episodes or force or refresh_hosts:
        log.info("Step 8: Syncing to Obsidian vault...")
        sync_to_obsidian(dry=dry)
        log.info("Step 9: Committing and pushing to Git...")
        today = date.today().isoformat()
        action = "host refresh" if refresh_hosts and not new_episodes else "regeneration" if force and not new_episodes else "archive update"
        commit_msg = f"Nadgryzieni {action} – {today} ({len(new_episodes)} new episodes)"
        git_commit_and_push(commit_msg, dry=dry)

    log.info("=" * 60)
    log.info(f"Pipeline complete! {len(new_episodes)} new episode(s) added.")
    if not new_episodes:
        log.info("No new episodes — archive and data.json regenerated (force mode).")
    log.info("=" * 60)
    return len(new_episodes)


def main() -> int:
    dry = "--dry" in sys.argv
    force = "--force" in sys.argv
    refresh_hosts = "--refresh-hosts" in sys.argv
    run_kind = os.environ.get("NADGRYZIENI_RUN_KIND", "manual").lower()
    today = date.today()

    if run_kind == "retry" and not dry and not retry_is_due(RETRY_STATE_PATH, today):
        log.info("No pending Saturday run; skipping conditional retry.")
        return 0

    if not acquire_pipeline_lock():
        return 0

    if run_kind == "primary" and not dry:
        write_retry_state(RETRY_STATE_PATH, today, pending=False)

    try:
        new_count = run_pipeline(dry=dry, force=force, refresh_hosts=refresh_hosts)
    except Exception:
        if run_kind in {"primary", "retry"} and not dry:
            write_retry_state(RETRY_STATE_PATH, today, pending=False)
        raise

    if run_kind == "primary" and not dry:
        write_retry_state(RETRY_STATE_PATH, today, pending=new_count == 0)
    elif run_kind == "retry" and not dry:
        write_retry_state(RETRY_STATE_PATH, today, pending=False)
    return new_count


if __name__ == "__main__":
    main()