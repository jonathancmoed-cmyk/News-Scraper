# Roadmap — News Scraper

This doc tracks planned updates, tweaks, and shipped work. Use checkboxes for status and link issues/PRs (e.g., #12).

## Status Key
- [ ] Planned
- [~] In progress
- [x] Done
- [!] Blocked / Needs decision

---

## 🔜 Next Up (1–2 weeks)
- [ ] Feed diagnostics panel (to check whether there are bad feeds in the .yaml)
- [ ] Sidebar: slider/env var for HTTP cache retention (`NEWS_HTTP_CACHE_MAX_AGE`)
- [ ] Archive: create a single Excel file containing all the articles/URLs (without duplicates) that the scraper has ever pulled
      Continuously update as the scraper runs
      [ask burgess what information is needed in that excel] 

## 🛠 In Progress
- [~] HTTP cache pruning (probabilistic) — tune probability/retention after observing file growth

## ✅ Shipped
- [x] Replace SQLite cache with JSON + atomic writes
- [x] Sidebar maintenance: clear HTTP/pubtime caches
- [x] Archive shards + periodic compaction scaffolding

## 🧪 Ideas / Icebox
- [ ]

## 🧹 Tech Debt
- [ ] Tests for `fetch_cached` (429/backoff, cooldown, cache hit/miss)
- [ ] Tests for pubtime extraction (meta / JSON-LD)
- [ ] CI lint + type check (ruff + mypy) on PRs
- [ ] Type hints across `fetch.py` / `archive.py`

## 📦 Release Targets
- **v0.3**: Diagnostics + cache controls  
- **v0.4**: Archive compaction UI + feed validation

## 📓 Decisions Log (ADR-lite)
- 2025-08-18: Dropped SQLite for JSON cache to avoid Streamlit thread issues.
- 2025-08-18: Added probabilistic pruning to control cache bloat with lower I/O.

## 🔗 Links
- `feeds.yaml`: `config/feeds.yaml`


