# Archived legacy rebuild tools

These files are retained for manual recovery only and are not used by the weekly automated workflow.

- `scrape_nadgryzieni_v2.py` is the older full-site rebuild scraper.
- `run_nadgryzieni.sh` is its manual launcher.

The active workflow is `../../nadgryzieni_pipeline.py`, which consumes the RSS feed, authenticated/public/manifest Patreon sources, validates generated output, syncs Obsidian, and publishes only intended generated files.

To use the legacy rebuild, run the wrapper from this directory and review its output before replacing any canonical archive data.
