# News Scraper (Streamlit)

A browser-based news scraper that collects and filters headlines from configured RSS feeds.  
It handles retries, caching, and per-page metadata extraction, and exports results to Excel.  
Deployed on Streamlit Cloud — so you only need a browser, no Python install.

---

## How to use
1. Open the app URL (Streamlit Cloud).
2. (Optional) Enter include/exclude keywords.
3. Set a lookback window in minutes (default: 1440 = 1 day).
4. Press **Run scraper**.
5. Preview results, check diagnostics, and download Excel.

---

## Updating feeds
Feeds are listed in [`config/feeds.yaml`](config/feeds.yaml).  
Edit this file in GitHub to add or remove sources.  
Streamlit Cloud redeploys automatically and changes go live.

---

## Project structure
- `app/streamlit_app.py` → Streamlit UI (user-facing app).
- `scraper/fetch.py` → Scraper engine (network layer, parsing, caching, Excel export).
- `scraper/config.py` → Central paths and YAML loader.
- `config/feeds.yaml` → List of RSS feeds + timezones.
- `output/` → Generated Excel files (not tracked in Git).
- `cache/` → SQLite + JSON caches (not tracked in Git).

---

## Tech
- [Streamlit](https://streamlit.io/) for the UI
- [feedparser](https://pypi.org/project/feedparser/) + [requests](https://pypi.org/project/requests/) for feeds
- [pandas](https://pandas.pydata.org/) for data handling
- [openpyxl](https://openpyxl.readthedocs.io/) for Excel export
- [pyyaml](https://pyyaml.org/) for YAML config
