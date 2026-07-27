#!/usr/bin/env python3
"""scrape_nadgryzieni.py

Scrapes the Nadgryzieni episode list from:
    https://retrorocketnetwork.pl/category/nadgryzen i-rss/
(there is a space in the URL text above only for readability – the real
URL is https://retrorocketnetwork.pl/category/nadgryzieni-rss/)

It builds a markdown table with the columns:
    Episode counter | Episode number | Episode title | Publish date | Duration

The script:
1. Crawls pagination until no "next" link is present.
2. Extracts for each episode:
   * title (raw, as shown on the listing page)
   * URL to the episode detail page
3. Visits each episode page and extracts:
   * publish date → ISO ``YYYY‑MM‑DD``
   * duration – the line that starts with "Duration:" (case‑insensitive)
   * if no duration, extracts the audio file size (e.g. "12 MB") and
     estimates duration using a bitrate that is auto‑detected from the first
     few episodes that *do* have both size and duration.
4. Parses an episode number from the title using two regexes:
   * ``^(\d+):`` (new format, e.g. "598: …")
   * ``Nadgryzieni\s*(?:&#8211;|[-–])\s*(\d+)`` (old format)
   * If none matches, the placeholder ``SP`` is used.
5. Sorts rows by numeric episode number (ascending). Rows with ``SP`` are
   placed after all numbered episodes, preserving their original order.
6. Assigns a monotonically increasing ``Episode counter`` starting at 1.
7. Writes the table to the Obsidian vault file:
   ``/Users/tarkin/Library/Mobile Documents/com~apple~CloudDocs/! Hermes !/Scarif Vault/20-Podcast/Nadgryzieni — Episode Archive.md``
   A timestamped backup ``…Episode Archive.backup.<YYYYMMDD_HHMMSS>.md`` is
   created first.
8. Commits the change to the Git repository that lives in the same folder
   (the repo was cloned earlier). The commit message is:
   ``Update episode archive – <YYYY‑MM‑DD>``

Running the script without arguments performs a full update. Use ``--dry`` to
write to ``Episode Archive.dry.md`` instead of the real file (no commit).
"""

import sys
import re
import os
import shutil
import datetime
import statistics
from pathlib import Path

import requests
from bs4 import BeautifulSoup
from dateutil import parser as dtparser

BASE_URL = "https://retrorocketnetwork.pl/category/nadgryzieni-rss/"
ARCHIVE_PATH = Path(
    "/Users/tarkin/Library/Mobile Documents/com~apple~CloudDocs/! Hermes !/Scarif Vault/20-Podcast/Nadgryzieni — Episode Archive.md"
)
BACKUP_TEMPLATE = (
    "/Users/tarkin/Library/Mobile Documents/com~apple~CloudDocs/! Hermes !/Scarif Vault/20-Podcast/Nadgryzieni — Episode Archive.backup.{timestamp}.md"
)

# ---------------------------------------------------------------------------
# Helper utilities
# ---------------------------------------------------------------------------
def fetch(url: str) -> str:
    resp = requests.get(url, timeout=30)
    resp.raise_for_status()
    return resp.text

def get_next_page(soup: BeautifulSoup) -> str | None:
    # WordPress theme usually provides <link rel="next" href="…"> in the head
    link = soup.find('link', {'rel': 'next'})
    if link and link.get('href'):
        return link['href']
    # Fallback: look for a pagination anchor with text "Next" or similar
    nav = soup.find('a', string=re.compile(r'next', re.I))
    if nav and nav.get('href'):
        return nav['href']
    return None

def parse_episode_number(title: str) -> str:
    # Try new format "598: …"
    m = re.match(r"^(\d+):", title.strip())
    if m:
        return m.group(1)
    # Try old format "Nadgryzieni – 05 – …"
    m = re.search(r"Nadgryzieni\s*(?:&#8211;|[-–])\s*(\d+)", title, re.I)
    if m:
        return m.group(1)
    # No number → special placeholder
    return "SP"

def extract_duration(text: str) -> str | None:
    # Look for a line starting with "Duration:" (case‑insensitive)
    m = re.search(r"Duration:\s*([\d:]+)", text, re.I)
    if m:
        return m.group(1).strip()
    return None

def extract_filesize(text: str) -> float | None:
    # Find patterns like "12 MB" or "12 MB"
    m = re.search(r"([\d.]+)\s*MB", text, re.I)
    if m:
        return float(m.group(1))  # megabytes
    return None

def size_to_seconds(size_mb: float, bitrate_kbps: int) -> int:
    # size (MB) → bytes → bits / bitrate
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

# ---------------------------------------------------------------------------
# Main scraping logic
# ---------------------------------------------------------------------------
def crawl_episode_links() -> list[tuple[str, str]]:
    """Return a list of (title, episode_url) tuples by walking pagination."""
    results = []
    next_page = BASE_URL
    while next_page:
        html = fetch(next_page)
        soup = BeautifulSoup(html, "lxml")
        # The theme lists episodes as <article> with an <h2><a href=...>
        for article in soup.find_all('article'):
            a = article.find('a')
            if not a or not a.get('href'):
                continue
            title = a.get_text(strip=True)
            url = a['href']
            results.append((title, url))
        next_page = get_next_page(soup)
    return results

def scrape_episode_detail(title: str, url: str) -> dict:
    html = fetch(url)
    soup = BeautifulSoup(html, "lxml")
    # Date – try <time datetime="..."> first
    date_str = None
    time_tag = soup.find('time')
    if time_tag and time_tag.get('datetime'):
        date_str = time_tag['datetime']
    elif time_tag and time_tag.text:
        date_str = time_tag.text
    if not date_str:
        # fallback: look for a meta tag or a span with class "date"
        meta = soup.find('meta', {'property': 'article:published_time'})
        if meta and meta.get('content'):
            date_str = meta['content']
    # Parse to ISO
    publish_date = None
    if date_str:
        try:
            dt = dtparser.parse(date_str)
            publish_date = dt.strftime('%Y-%m-%d')
        except Exception:
            pass
    # Duration & file size (the whole page as text is enough for regexes)
    page_text = soup.get_text(separator='\n')
    duration = extract_duration(page_text)
    filesize_mb = None
    if not duration:
        filesize_mb = extract_filesize(page_text)
    return {
        'title': title,
        'url': url,
        'date': publish_date or "",
        'duration': duration,  # may be None
        'filesize_mb': filesize_mb,
    }

def detect_bitrate(sample_rows: list[dict]) -> int:
    """Given rows that have both duration (H:MM:SS) and filesize, compute median bitrate.
    Returns an integer kbps (default 64 if not enough data)."""
    bitrates = []
    for row in sample_rows:
        dur = row['duration']
        size = row.get('filesize_mb')
        if not dur or not size:
            continue
        # convert duration to seconds
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
    # First pass – gather raw data
    raw = []
    for title, url in episodes:
        raw.append(scrape_episode_detail(title, url))
    # Determine bitrate from first 5 rows that have both fields
    sample = [r for r in raw if r['duration'] and r.get('filesize_mb')][:5]
    bitrate = detect_bitrate(sample)
    # Second pass – fill missing durations
    for r in raw:
        if not r['duration'] and r.get('filesize_mb'):
            secs = size_to_seconds(r['filesize_mb'], bitrate)
            r['duration'] = sec_to_hms(secs)
        elif not r['duration']:
            r['duration'] = "?"
    # Add episode number and placeholder handling
    for r in raw:
        r['episode_number'] = parse_episode_number(r['title'])
    # Sort: numeric first, then SP placeholder
    def sort_key(item):
        num = item['episode_number']
        if num == 'SP':
            return (float('inf'), item['title'])
        try:
            return (int(num), '')
        except ValueError:
            return (float('inf'), item['title'])
    raw.sort(key=sort_key)
    # Assign counter
    for idx, r in enumerate(raw, start=1):
        r['counter'] = idx
    return raw

def markdown_table(rows: list[dict]) -> str:
    header = "| Episode counter | Episode number | Episode title | Publish date | Duration |"
    separator = "| - | ------------- | ------------ | ------------ | -------- |"
    lines = [header, separator]
    for r in rows:
        # Escape any pipe characters in the title so the markdown table stays well‑formed
        sanitized_title = r['title'].replace('|', '\\|')
        line = f"| {r['counter']} | {r['episode_number']} | {sanitized_title} | {r['date']} | {r['duration']} |"
        lines.append(line)
    return "\n".join(lines) + "\n"

def backup_file(path: Path):
    if not path.is_file():
        return
    timestamp = datetime.datetime.now().strftime('%Y%m%d_%H%M%S')
    backup_path = Path(BACKUP_TEMPLATE.format(timestamp=timestamp))
    shutil.copy2(path, backup_path)

def git_commit(file_path: Path):
    repo_dir = file_path.parent
    cmds = [
        f"cd '{repo_dir}' && git add '{file_path.name}'",
        f"cd '{repo_dir}' && git commit -m 'Update episode archive – {datetime.date.today()}'",
        f"cd '{repo_dir}' && git push",
    ]
    for cmd in cmds:
        os.system(cmd)

def main():
    dry = '--dry' in sys.argv
    episodes = crawl_episode_links()
    rows = build_rows(episodes)
    md_content = markdown_table(rows)
    target_path = ARCHIVE_PATH if not dry else ARCHIVE_PATH.with_name('Nadgryzieni — Episode Archive.dry.md')
    # Backup only for the real file
    if not dry:
        backup_file(ARCHIVE_PATH)
    # Write the new markdown
    target_path.write_text(md_content, encoding='utf-8')
    print(f"{'Dry run' if dry else 'Updated'} archive written to {target_path}")
    # Git commit (skip for dry run)
    if not dry:
        git_commit(target_path)

if __name__ == '__main__':
    main()
