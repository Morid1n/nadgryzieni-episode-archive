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
import os
import re
import shutil
import ssl
import sys
import time
import urllib.request
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict
from datetime import datetime, date
from pathlib import Path

# ── Configuration ────────────────────────────────────────────────────────────
# Use current working directory as repo dir (cron job sets workdir to repo)
REPO_DIR = Path.cwd()
ARCHIVE_PATH = REPO_DIR / "Nadgryzieni Episode Archive.md"
STATS_PATH = REPO_DIR / "Nadgryzieni Statistics.md"
DATA_JSON_PATH = REPO_DIR / "data.json"
INDEX_HTML_PATH = REPO_DIR / "index.html"
SCRIPT_JS_PATH = REPO_DIR / "script.js"
README_PATH = REPO_DIR / "README.md"

# Obsidian vault directory for syncing archive and statistics
VAULT_DIR = Path("/Users/tarkin/Library/Mobile Documents/com~apple~CloudDocs/! Hermes !/Scarif Vault/20-Podcast")

RSS_URL = "https://retrorocketnetwork.pl/category/nadgryzieni-rss/feed/"

# Patreon creator page (public posts are scraped; private RSS requires auth)
PATREON_URL = "https://www.patreon.com/iMagazinePL/posts"

# Content widths for the padded markdown table (content is left-justified,
# with 1 space padding on each side between | characters)
# Derived from the existing archive: field widths between | are 5, 7, 110, 14, 10
# which gives content widths of 3, 5, 108, 12, 8
COL_CONTENT_WIDTHS = [3, 5, 108, 12, 8]  # counter, episode, title, date, duration

# Cache-busting version — incremented each time the pipeline makes changes
CACHE_VERSION_FILE = Path("/tmp/nadgryzieni_cache_version.txt")

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


# ── RSS Fetching ─────────────────────────────────────────────────────────────

def fetch_rss(max_retries: int = 3) -> bytes:
    """Fetch the RSS feed with retries. Returns raw XML bytes."""
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
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

        if title_el is None or pubdate_el is None:
            continue

        title = title_el.text.strip() if title_el.text else ""
        pubdate = pubdate_el.text.strip() if pubdate_el.text else ""
        duration = duration_el.text.strip() if duration_el is not None and duration_el.text else ""
        guid = guid_el.text.strip() if guid_el is not None and guid_el.text else ""

        # Parse pubDate (RFC 822)
        try:
            from email.utils import parsedate_to_datetime
            dt = parsedate_to_datetime(pubdate)
            iso_date = dt.strftime("%Y-%m-%d")
        except Exception:
            iso_date = pubdate[:10] if len(pubdate) >= 10 else ""

        # Extract episode number from title
        ep_num = extract_episode_number(title)

        items.append({
            "title": title,
            "date": iso_date,
            "duration": duration,
            "guid": guid,
            "episode_number": ep_num,
        })
    return items


# ── Patreon Scraping ──────────────────────────────────────────────────────────

# Known Patreon post slugs for Afterparty episodes
# These are discovered by visiting the Patreon posts page in a browser
# (posts page is JS-rendered, so Python can't extract them directly)
# Format: (episode_number, slug) — update this list as new posts are published
PATREON_KNOWN_POSTS = [
    (595, "595-afterparty-z-163700509"),
    (596, "596-afterparty-z-164064600"),
    (597, "597-afterparty-164068251"),
    (598, "598-afterparty-164626802"),
    (599, "599-afterparty-z-165363196"),
]


def _fetch_patreon_post_page(slug: str) -> str | None:
    """Fetch a single Patreon post page and return its HTML."""
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
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


def _parse_patreon_post(html: str) -> dict | None:
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
    pub_date = date_match.group(1)[:10] if date_match else ""

    return {
        "title": title,
        "duration": duration,
        "date": pub_date,
    }


def _fetch_patreon_posts_from_list() -> list[dict]:
    """Fetch known Patreon Afterparty post pages and extract episode data."""
    posts = []
    for ep_num, slug in PATREON_KNOWN_POSTS:
        html = _fetch_patreon_post_page(slug)
        if not html:
            continue
        parsed = _parse_patreon_post(html)
        if not parsed:
            continue
        parsed["episode_number"] = ep_num
        posts.append(parsed)
    return posts


def fetch_patreon_posts() -> list[dict]:
    """Fetch Patreon Afterparty episodes from multiple sources.

    1. If PATREON_RSS_URL env var is set, fetch authenticated RSS XML.
    2. Otherwise, try scraping the public posts page for post slugs (JS-rendered, may fail).
    3. Fall back to fetching known post pages directly (hardcoded list).

    Returns a list of dicts with keys: title, episode_number, duration, date.
    """
    # 1. Try authenticated RSS
    rss_url = os.environ.get("PATREON_RSS_URL")
    if rss_url:
        try:
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            req = urllib.request.Request(rss_url, headers={
                "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
                "Accept": "application/xml",
            })
            with urllib.request.urlopen(req, timeout=30, context=ctx) as resp:
                xml_bytes = resp.read()
            return _parse_patreon_rss(xml_bytes)
        except Exception as exc:
            log.warning(f"Patreon RSS fetch failed: {exc}")

    # 2. Try scraping the public posts page
    posts = _scrape_patreon_posts_page()
    if posts:
        return posts

    # 3. Fall back to known post list
    log.info("Falling back to known Patreon post list.")
    posts = _fetch_patreon_posts_from_list()
    if posts:
        log.info(f"Found {len(posts)} Patreon Afterparty posts from known list.")
    else:
        log.info("No Patreon Afterparty posts found.")

    return posts


def _scrape_patreon_posts_page() -> list[dict]:
    """Scrape the Patreon posts page to find Afterparty episode links.

    The page is JS-rendered, so this may not find any posts.
    Returns empty list if posts are not in the HTML.
    """
    try:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        patreon_url = "https://www.patreon.com/cw/iMagazinePL/posts"
        req = urllib.request.Request(patreon_url, headers={
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
            "Accept": "text/html",
        })
        with urllib.request.urlopen(req, timeout=30, context=ctx) as resp:
            html = resp.read().decode("utf-8", errors="replace")
    except Exception as exc:
        log.warning(f"Patreon page fetch failed: {exc}")
        return []

    # Extract Next.js data chunks and concatenate
    all_data = ""
    for push in re.finditer(r'self\.__next_f\.push\(\[\d+,\s*"((?:[^"\\]|\\.)*)"\]\)', html):
        try:
            chunk = json.loads('"' + push.group(1) + '"')
            all_data += chunk
        except json.JSONDecodeError:
            continue

    # Look for post URL slugs in the data
        # Pattern: /iMagazinePL/posts/NNN-afterparty-SLUG
        slug_pattern = re.compile(r'/iMagazinePL/posts/(\d+)-afterparty-[a-z]+-(\d{6,12})')
        slugs_found = slug_pattern.findall(all_data)

        # Also try the raw HTML
        slugs_html = slug_pattern.findall(html)

    all_slugs = slugs_found + slugs_html

    # Deduplicate and convert to int
    seen = set()
    posts = []
    for ep_num, post_id in all_slugs:
        ep_num = int(ep_num)
        if ep_num in seen:
            continue
        seen.add(ep_num)
        # Construct a slug to fetch
        slug = f"{ep_num}-afterparty-{post_id}"
        page_html = _fetch_patreon_post_page(slug)
        if page_html:
            parsed = _parse_patreon_post(page_html)
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
            # Try to get duration from itunes:duration
            duration = None
            itunes_dur = item.find(".//{http://www.itunes.com/dtds/rss-2.0.dtd}duration")
            if itunes_dur is not None and itunes_dur.text:
                try:
                    secs = int(itunes_dur.text)
                    h = secs // 3600
                    m = (secs % 3600) // 60
                    s = secs % 60
                    duration = f"{h}:{m:02d}:{s:02d}"
                except (ValueError, TypeError):
                    duration = itunes_dur.text
            # Try to get pubDate
            date_str = ""
            pub_date = item.find("pubDate")
            if pub_date is not None and pub_date.text:
                date_str = pub_date.text[:10]
            posts.append({
                "title": title,
                "episode_number": ep_match.group(1),
                "duration": duration,
                "date": date_str,
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


# ── Archive Reading ──────────────────────────────────────────────────────────

def parse_archive(path: Path) -> tuple[list[dict], str]:
    """Parse the padded markdown table. Returns (rows, header_block)."""
    content = path.read_text(encoding="utf-8")
    lines = content.splitlines()

    # Find table start (first line starting with "|")
    table_start = None
    for i, line in enumerate(lines):
        if line.strip().startswith("|") and "---" not in line:
            table_start = i
            break

    if table_start is None:
        raise ValueError("Could not find table in archive")

    # The header is the first two lines: header + separator
    header_lines = lines[table_start:table_start + 2]

    # Find table end (blank line after table)
    table_end = table_start + 2
    for i in range(table_start + 2, len(lines)):
        if lines[i].strip() == "":
            table_end = i
            break
    else:
        table_end = len(lines)

    # Parse data rows
    rows = []
    for line in lines[table_start + 2:table_end]:
        stripped = line.strip()
        if not stripped.startswith("|"):
            continue
        parts = [p.strip() for p in stripped.split("|") if p.strip()]
        if len(parts) >= 5:
            rows.append({
                "counter": parts[0],
                "episode": parts[1],
                "title": parts[2],
                "date": parts[3],
                "duration": parts[4],
            })

    return rows, "\n".join(header_lines)


# ── Archive Writing ──────────────────────────────────────────────────────────

def pad_field(text: str, width: int) -> str:
    """Left-pad text to width with spaces (markdown table padding)."""
    return text.ljust(width)


def write_archive(rows: list[dict], dry: bool = False) -> None:
    """Write the archive markdown table with column padding."""
    cw = COL_CONTENT_WIDTHS  # content widths: 3, 5, 108, 12, 8

    header = f"| {pad_field('#', cw[0])} | {pad_field('Ep.', cw[1])} | {pad_field('Episode title', cw[2])} | {pad_field('Publish date', cw[3])} | {pad_field('Duration', cw[4])} |"
    separator = f"| {pad_field('', cw[0]).replace(' ', '-')} | {pad_field('', cw[1]).replace(' ', '-')} | {pad_field('', cw[2]).replace(' ', '-')} | {pad_field('', cw[3]).replace(' ', '-')} | {pad_field('', cw[4]).replace(' ', '-')} |"

    lines = [header, separator]
    for r in rows:
        line = f"| {pad_field(r['counter'], cw[0])} | {pad_field(r['episode'], cw[1])} | {pad_field(r['title'], cw[2])} | {pad_field(r['date'], cw[3])} | {pad_field(r['duration'], cw[4])} |"
        lines.append(line)

    content = "\n".join(lines) + "\n"

    target = ARCHIVE_PATH if not dry else ARCHIVE_PATH.with_name(
        ARCHIVE_PATH.name.replace(".md", ".dry.md")
    )
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


def generate_data_json(rows: list[dict]) -> dict:
    """Generate the data.json structure for Chart.js."""
    episodes = []
    durations_min = []
    year_counts = Counter()

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

        episodes.append({
            "episode": r["episode"],
            "title": r["title"],
            "date": r["date"],
            "duration": dur_str,
            "minutes": round(minutes, 2) if minutes is not None else None,
            "category": category,
            "year": int(year) if year else None,
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


def write_data_json(data: dict, dry: bool = False) -> None:
    target = DATA_JSON_PATH if not dry else DATA_JSON_PATH.with_name(
        DATA_JSON_PATH.name.replace(".json", ".dry.json")
    )
    target.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    log.info(f"data.json written: {target} ({len(data['episodes'])} episodes)")


# ── README Update ──────────────────────────────────────────────────────────────

def update_readme(data: dict, dry: bool = False) -> None:
    """Update the README.md stats table with current numbers."""
    if not README_PATH.exists():
        log.warning(f"README.md not found at {README_PATH}")
        return

    content = README_PATH.read_text(encoding="utf-8")
    stats = data["stats"]
    total = stats["total_episodes"]
    total_hours = stats["total_listening_hours"]
    avg_duration = stats["average_duration"]
    max_duration = round(stats["max_duration"], 1)

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

    target = README_PATH if not dry else README_PATH.with_name(
        README_PATH.name.replace(".md", ".dry.md")
    )
    target.write_text(content, encoding="utf-8")
    log.info(f"README.md updated: {target} ({total} episodes)")


# ── Obsidian Vault Sync ────────────────────────────────────────────────────────

def sync_to_obsidian(dry: bool = False) -> None:
    """Sync archive and statistics files to the Obsidian vault directory."""
    if not VAULT_DIR.exists():
        log.warning(f"Vault directory not found: {VAULT_DIR}")
        return

    synced = 0

    # Sync archive
    src_archive = ARCHIVE_PATH
    dst_archive = VAULT_DIR / "Nadgryzieni Episode Archive.md"
    if src_archive.exists():
        if not dry:
            dst_archive.write_bytes(src_archive.read_bytes())
        log.info(f"Synced archive to vault: {dst_archive}")
        synced += 1

    # Sync statistics
    src_stats = STATS_PATH
    dst_stats = VAULT_DIR / "Nadgryzieni Statistics.md"
    if src_stats.exists():
        if not dry:
            dst_stats.write_bytes(src_stats.read_bytes())
        log.info(f"Synced statistics to vault: {dst_stats}")
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


# ── Cache-Busting ────────────────────────────────────────────────────────────

def get_cache_version() -> int:
    """Get or initialize the cache-busting version number."""
    if CACHE_VERSION_FILE.exists():
        try:
            return int(CACHE_VERSION_FILE.read_text().strip())
        except ValueError:
            pass
    return 100  # Default starting version


def bump_cache_version() -> int:
    """Increment and save the cache-busting version number."""
    v = get_cache_version() + 1
    CACHE_VERSION_FILE.write_text(str(v), encoding="utf-8")
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

    # Update script.js data.json reference
    if SCRIPT_JS_PATH.exists():
        js = SCRIPT_JS_PATH.read_text(encoding="utf-8")
        js = re.sub(r"data\.json\?v=\d+", f"data.json?v={version}", js)
        SCRIPT_JS_PATH.write_text(js, encoding="utf-8")
        log.info(f"script.js data.json cache-busting updated to v={version}")


# ── Git Operations ───────────────────────────────────────────────────────────

def git_commit_and_push(message: str, dry: bool = False) -> None:
    """Commit and push changes to Git."""
    if dry:
        log.info(f"[DRY RUN] Would commit: {message}")
        return

    import subprocess
    repo = str(REPO_DIR)
    cmds = [
        ["git", "add", "-A"],
        ["git", "commit", "-m", message],
        ["git", "push", "origin", "main"],
    ]
    for cmd in cmds:
        log.info(f"Running: {' '.join(cmd)}")
        result = subprocess.run(cmd, cwd=repo, capture_output=True, text=True)
        if result.returncode != 0:
            log.error(f"Git command failed: {' '.join(cmd)}")
            log.error(f"stderr: {result.stderr}")
            raise RuntimeError(f"Git command failed: {result.stderr}")
    log.info("Git commit and push complete")


# ── Main Pipeline ────────────────────────────────────────────────────────────

def main():
    dry = "--dry" in sys.argv
    force = "--force" in sys.argv

    log.info("=" * 60)
    log.info("Nadgryzieni Pipeline — starting")
    log.info(f"Mode: {'DRY RUN' if dry else 'LIVE'}")
    log.info("=" * 60)

    # Step 1: Fetch RSS
    log.info("Step 1: Fetching RSS feed...")
    xml_bytes = fetch_rss()
    items = parse_rss_items(xml_bytes)
    log.info(f"  Parsed {len(items)} episodes from RSS")

    # Step 2: Load existing archive
    log.info("Step 2: Loading existing archive...")
    existing_rows, _ = parse_archive(ARCHIVE_PATH)
    log.info(f"  Archive has {len(existing_rows)} rows")

    # Build lookup of existing episode numbers
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
        if ep_num in existing_ep_numbers:
            continue
        # Format duration for archive
        duration = item["duration"] if item["duration"] else "?"
        new_episodes.append({
            "counter": str(len(existing_rows) + len(new_episodes) + 1),
            "episode": ep_num,
            "title": item["title"],
            "date": item["date"],
            "duration": duration,
        })
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

    if not new_episodes and not force:
        log.info("No new episodes found. Exiting silently.")
        return

    if new_episodes:
        log.info(f"  Found {len(new_episodes)} new episode(s):")
        for ep in new_episodes:
            log.info(f"    #{ep['episode']}: {ep['title'][:60]}...")

    # Step 4: Merge and write archive
    log.info("Step 4: Updating archive...")
    all_rows = existing_rows + new_episodes

    # Sort by date (oldest first), episodes without date sort last
    def sort_key(r):
        date_str = r["date"] if r["date"] else "9999-12-31"
        return (date_str, r["title"])
    all_rows.sort(key=sort_key)

    # Reassign counters after sorting
    for i, r in enumerate(all_rows, start=1):
        r["counter"] = str(i)

    write_archive(all_rows, dry=dry)

    # Step 5: Generate data.json
    log.info("Step 5: Generating data.json...")
    data = generate_data_json(all_rows)
    write_data_json(data, dry=dry)

    # Step 5b: Update README.md stats table
    log.info("Step 5b: Updating README.md...")
    update_readme(data, dry=dry)

    # Step 6: Generate statistics (only when new episodes are found)
    if new_episodes:
        log.info("Step 6: Generating statistics...")
        stats_md = generate_statistics(all_rows)
        write_stats(stats_md, dry=dry)
    else:
        log.info("Step 6: Skipping statistics (no new episodes).")

    # Step 7: Bump cache-busting (only when new episodes are found)
    if new_episodes:
        log.info("Step 7: Bumping cache-busting version...")
        new_version = bump_cache_version()
        update_cache_busting(new_version, dry=dry)
    else:
        log.info("Step 7: Skipping cache-busting bump (no new episodes).")

    # Step 8: Git commit and push
    if new_episodes:
        log.info("Step 8: Committing and pushing to Git...")
        today = date.today().isoformat()
        commit_msg = f"Nadgryzieni archive update – {today} ({len(new_episodes)} new episodes)"
        git_commit_and_push(commit_msg, dry=dry)

    # Step 9: Sync to Obsidian vault (only when new episodes are found)
    if new_episodes:
        log.info("Step 9: Syncing to Obsidian vault...")
        sync_to_obsidian(dry=dry)
    else:
        log.info("Step 9: Skipping Obsidian sync (no new episodes).")

    log.info("=" * 60)
    log.info(f"Pipeline complete! {len(new_episodes)} new episode(s) added.")
    if not new_episodes:
        log.info("No new episodes — archive and data.json regenerated (force mode).")
    log.info("=" * 60)


if __name__ == "__main__":
    main()