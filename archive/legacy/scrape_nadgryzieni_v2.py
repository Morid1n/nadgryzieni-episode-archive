#!/usr/bin/env python3
"""scrape_nadgryzieni_v2.py

Full rewrite of the Nadgryzieni episode scraper.
Crawls all pagination pages of https://retrorocketnetwork.pl/category/nadgryzieni-rss/
Visits each episode page for duration (or filesize), estimates duration if needed.
Produces a CommonMark markdown table sorted by episode number.
"""

from __future__ import annotations

import sys
import re
import os
import shutil
import datetime
import statistics
import time
from pathlib import Path
from urllib.error import HTTPError

from bs4 import BeautifulSoup

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
BASE_URL = "https://retrorocketnetwork.pl/category/nadgryzieni-rss/"
ARCHIVE_PATH = Path(
    "/Users/tarkin/Library/Mobile Documents/com~apple~CloudDocs/! Hermes !/Scarif Vault/20-Podcast/Nadgryzieni Episode Archive.md"
)
TOTAL_PAGES = 38  # Known from prior discovery
RATE_LIMIT_SECONDS = 0.3  # Respectful delay between requests

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def fetch(url: str) -> str:
    """Fetch URL with basic error handling."""
    try:
        import urllib.request as urllib_req
        with urllib_req.urlopen(url, timeout=30) as resp:
            return resp.read().decode('utf-8')
    except HTTPError as e:
        print(f"  HTTP {e.code} for {url}")
        return ""

def crawl_episode_links() -> list[tuple[str, str]]:
    """Return a list of (title, episode_url) tuples from all 38 pages."""
    results = []
    for page in range(1, TOTAL_PAGES + 1):
        url = f"{BASE_URL}page/{page}/" if page > 1 else BASE_URL
        html = fetch(url)
        if not html:
            print(f"  Failed to fetch page {page}")
            continue
        soup = BeautifulSoup(html, "html.parser")
        for article in soup.find_all('article'):
            a = article.find('a')
            if not a or not a.get('href'):
                continue
            title = a.get_text(strip=True)
            url_episode = a['href']
            results.append((title, url_episode))
        if page % 5 == 0:
            print(f"  Crawled {page}/{TOTAL_PAGES} pages, {len(results)} episodes so far...")
        time.sleep(RATE_LIMIT_SECONDS)
    return results

def parse_episode_number(title: str) -> str:
    """Extract episode number from title. Returns 'SP' for specials."""
    # Try new format "598: Title"
    m = re.match(r"^(\d+)([½⅓⅔]?)[:\s-]", title.strip())
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
    return "SP"

def extract_duration(text: str) -> str | None:
    """Look for a line with 'Duration: H:MM:SS' or 'Duration: MM:SS'."""
    m = re.search(r"Duration:\s*([\d:]+)", text, re.I)
    if m:
        return m.group(1).strip()
    return None

def extract_filesize(text: str) -> float | None:
    """Find pattern like '12 MB' or '12 MB' (with thin space)."""
    m = re.search(r"([\d.]+)\s*MB", text, re.I)
    if m:
        return float(m.group(1))
    return None

def size_to_seconds(size_mb: float, bitrate_kbps: int) -> int:
    bits = size_mb * 1024 * 1024 * 8
    return int(bits / (bitrate_kbps * 1000))

def sec_to_hms(seconds: int) -> str:
    h = seconds // 3600
    m = (seconds % 3600) // 60
    s = seconds % 60
    if h:
        return f"{h}:{m:02d}:{s:02d}"
    else:
        return f"{m}:{s:02d}"

def scrape_episode_detail(title: str, url: str) -> dict:
    """Fetch an individual episode page and extract date, duration, filesize."""
    html = fetch(url)
    if not html:
        return {
            'title': title,
            'url': url,
            'date': '',
            'duration': None,
            'filesize_mb': None,
        }
    
    soup = BeautifulSoup(html, "html.parser")
    
    # Date – try <time datetime="..."> first
    date_str = None
    time_tag = soup.find('time')
    if time_tag and time_tag.get('datetime'):
        date_str = time_tag['datetime']
    elif time_tag and time_tag.text:
        date_str = time_tag.text
    if not date_str:
        # fallback: meta tag
        meta = soup.find('meta', {'property': 'article:published_time'})
        if meta and meta.get('content'):
            date_str = meta['content']
    
    publish_date = None
    if date_str:
        try:
            from dateutil import parser as dtparser
            dt = dtparser.parse(date_str)
            publish_date = dt.strftime('%Y-%m-%d')
        except Exception:
            pass
    
    # Duration & file size
    page_text = soup.get_text(separator='\n')
    duration = extract_duration(page_text)
    filesize_mb = extract_filesize(page_text)
    
    time.sleep(RATE_LIMIT_SECONDS)
    return {
        'title': title,
        'url': url,
        'date': publish_date or "",
        'duration': duration,
        'filesize_mb': filesize_mb,
    }

def detect_bitrate(sample_rows: list[dict]) -> int:
    """Given rows with both duration and filesize, compute median bitrate in kbps."""
    bitrates = []
    for row in sample_rows:
        dur = row['duration']
        size = row.get('filesize_mb')
        if not dur or not size:
            continue
        parts = list(map(int, dur.split(':')))
        if len(parts) == 3:
            secs = parts[0] * 3600 + parts[1] * 60 + parts[2]
        elif len(parts) == 2:
            secs = parts[0] * 60 + parts[1]
        else:
            continue
        kbps = int((size * 1024 * 1024 * 8) / (secs * 1000))
        bitrates.append(kbps)
    if not bitrates:
        return 64
    return int(statistics.median(bitrates))

def build_rows(episodes: list[tuple[str, str]]) -> list[dict]:
    """Scrape all episodes and build sorted rows."""
    print("Phase 1: Scraping individual episode pages...")
    raw = []
    total = len(episodes)
    for i, (title, url) in enumerate(episodes):
        row = scrape_episode_detail(title, url)
        raw.append(row)
        if (i + 1) % 50 == 0:
            with_dur = sum(1 for r in raw if r['duration'])
            print(f"  {i+1}/{total} episodes scraped, {with_dur} with duration...")
    
    # Detect bitrate from episodes that have both
    sample = [r for r in raw if r['duration'] and r.get('filesize_mb')][:10]
    bitrate = detect_bitrate(sample)
    print(f"Detected bitrate: {bitrate} kbps from {len(sample)} samples")
    
    # Fill missing durations
    for r in raw:
        if not r['duration'] and r.get('filesize_mb'):
            secs = size_to_seconds(r['filesize_mb'], bitrate)
            r['duration'] = sec_to_hms(secs)
        elif not r['duration']:
            r['duration'] = "?"
    
    # Add episode number
    for r in raw:
        r['episode_number'] = parse_episode_number(r['title'])
    
    # Sort by publish date (earliest first), episodes without date sort first
    def sort_key(item):
        date_str = item['date']
        # Use '0001-01-01' for episodes without a date so they sort first
        date_key = date_str if date_str else '0001-01-01'
        return (date_key, item['title'])
    
    raw.sort(key=sort_key)
    
    # Assign counter
    for idx, r in enumerate(raw, start=1):
        r['counter'] = idx
    
    return raw

def markdown_table(rows: list[dict]) -> str:
    header = "| # | Ep. | Episode title | Publish date | Duration |"
    separator = "| --- | --- | ------------- | ------------ | -------- |"
    lines = [header, separator]
    for r in rows:
        sanitized_title = r['title'].replace('|', '\\|')
        line = f"| {r['counter']} | {r['episode_number']} | {sanitized_title} | {r['date']} | {r['duration']} |"
        lines.append(line)
    return "\n".join(lines) + "\n"

def backup_file(path: Path):
    if not path.is_file():
        return
    timestamp = datetime.datetime.now().strftime('%Y%m%d_%H%M%S')
    backup_path = Path(str(path).replace('Archive.md', f'Archive.backup.{timestamp}.md'))
    shutil.copy2(path, backup_path)
    print(f"  Backup created: {backup_path.name}")

def git_commit(file_path: Path):
    repo_dir = file_path.parent
    today = datetime.date.today().isoformat()
    cmds = [
        f"cd '{repo_dir}' && git add '{file_path.name}'",
        f"cd '{repo_dir}' && git commit -m 'Update episode archive – {today}'",
        f"cd '{repo_dir}' && git push",
    ]
    for cmd in cmds:
        print(f"  Running: {cmd}")
        os.system(cmd)

def main():
    dry = '--dry' in sys.argv
    
    # Step 1: Crawl all listing pages
    print(f"Crawling listing pages (up to page {TOTAL_PAGES})...")
    episodes = crawl_episode_links()
    print(f"Found {len(episodes)} episode links across all pages")
    
    if not episodes:
        print("ERROR: No episodes found!")
        sys.exit(1)
    
    # Step 2: Scrape details and build rows
    rows = build_rows(episodes)
    
    # Print summary
    with_dur = sum(1 for r in rows if r['duration'] and r['duration'] != '?')
    without_dur = sum(1 for r in rows if r['duration'] == '?')
    print(f"\nSummary:")
    print(f"  Total episodes: {len(rows)}")
    print(f"  With duration: {with_dur}")
    print(f"  Without duration (estimated): {without_dur}")
    print(f"  Episode range: {rows[0]['episode_number']} to {rows[-1]['episode_number']}")
    
    # Step 3: Write markdown table
    md_content = markdown_table(rows)
    
    target_path = ARCHIVE_PATH if not dry else ARCHIVE_PATH.with_name('Nadgryzieni Episode Archive.dry.md')
    
    if not dry:
        backup_file(ARCHIVE_PATH)
    
    target_path.write_text(md_content, encoding='utf-8')
    print(f"\n{'Dry run' if dry else 'Updated'} archive written to {target_path}")
    
    # Step 4: Git commit (non-dry only)
    if not dry:
        print("\nPhase 2: Git commit and push...")
        git_commit(target_path)
        print("Done!")
    else:
        print("\nDry run complete. Use without --dry to commit.")

if __name__ == '__main__':
    main()
