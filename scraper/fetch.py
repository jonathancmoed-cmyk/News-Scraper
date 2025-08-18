import os, json, time, re, random, base64, tempfile, shutil
from pathlib import Path
from urllib.parse import urlparse, parse_qsl
from datetime import datetime, timedelta, timezone
import threading

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
import feedparser
import pandas as pd
from dateutil import parser as dtparser, tz as dttz

from scraper.config import CACHE_DIR, OUTPUT_DIR

# ========== NETWORK LAYER ==========
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/139.0.0.0 Safari/537.36 NewsScraper/1.0"
)
DEFAULT_HEADERS = {
    "User-Agent": UA,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Connection": "keep-alive",
}

def google_news_when(minutes_back: int) -> str:
    if minutes_back <= 60:      return "1h"
    if minutes_back <= 180:     return "3h"
    if minutes_back <= 360:     return "6h"
    if minutes_back <= 720:     return "12h"
    if minutes_back <= 1440:    return "1d"
    if minutes_back <= 2880:    return "2d"
    if minutes_back <= 10080:   return "1w"
    return "1m"

def make_session() -> requests.Session:
    s = requests.Session()
    s.headers.update(DEFAULT_HEADERS)
    retry = Retry(
        total=6, connect=3, read=3,
        backoff_factor=1.2,
        status_forcelist=(429,500,502,503,504),
        allowed_methods=("GET","HEAD","OPTIONS"),
        raise_on_status=False,
    )
    s.mount("https://", HTTPAdapter(max_retries=retry))
    s.mount("http://",  HTTPAdapter(max_retries=retry))
    return s

def make_light_session() -> requests.Session:
    s = requests.Session()
    s.headers.update(DEFAULT_HEADERS)
    retry = Retry(
        total=1, connect=1, read=1,
        backoff_factor=0.3,
        status_forcelist=(429,500,502,503,504),
        allowed_methods=("GET","HEAD","OPTIONS"),
        raise_on_status=False,
    )
    s.mount("https://", HTTPAdapter(max_retries=retry))
    s.mount("http://",  HTTPAdapter(max_retries=retry))
    return s

SESSION = make_session()
SESSION_RSSHUB = make_light_session()

# polite wait + cooldown (guard with locks)
_last_hit = {}
MIN_GAP = {"rsshub.app":5.0,"apnews.com":2.0,"www.theguardian.com":0.6}
DEFAULT_GAP = 0.8
_HOST_COOLDOWN_UNTIL = {}
_NET_LOCK = threading.RLock()

def _polite_wait(host: str):
    with _NET_LOCK:
        gap = MIN_GAP.get(host, DEFAULT_GAP)
        last = _last_hit.get(host, 0.0)
    wait = (last + gap) - time.time()
    if wait > 0:
        time.sleep(wait)

def _update_last_hit(host: str):
    with _NET_LOCK:
        _last_hit[host] = time.time()

def _is_in_cooldown(host: str) -> bool:
    with _NET_LOCK:
        return time.time() < _HOST_COOLDOWN_UNTIL.get(host, 0)

def _set_cooldown(host: str, seconds: float):
    with _NET_LOCK:
        _HOST_COOLDOWN_UNTIL[host] = time.time() + seconds

# ========== FILE-BASED HTTP CACHE (JSON) ==========
HTTP_CACHE_FILE = CACHE_DIR / "url_cache.json"
_HTTP_CACHE_LOCK = threading.RLock()
_HTTP_CACHE: dict[str, dict] = {}  # in-memory map: url -> record

def _atomic_save_json(path: Path, data: dict):
    tmp_fd, tmp_path = tempfile.mkstemp(dir=str(path.parent), prefix=path.name, suffix=".tmp")
    os.close(tmp_fd)
    try:
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        shutil.move(tmp_path, path)
    finally:
        try:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
        except Exception:
            pass

def _load_http_cache(path: Path) -> dict:
    if path.exists():
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}

def _save_http_cache():
    with _HTTP_CACHE_LOCK:
        _atomic_save_json(HTTP_CACHE_FILE, _HTTP_CACHE)

def _get_cached_record(url: str):
    with _HTTP_CACHE_LOCK:
        return _HTTP_CACHE.get(url)

def _set_cached_record(url: str, status: int, content_bytes: bytes, headers: dict):
    rec = {
        "fetched_at": time.time(),
        "status": int(status or 0),
        "content_b64": base64.b64encode(content_bytes or b"").decode("ascii"),
        "headers": dict(headers or {}),
    }
    with _HTTP_CACHE_LOCK:
        _HTTP_CACHE[url] = rec
        _atomic_save_json(HTTP_CACHE_FILE, _HTTP_CACHE)

# load once at import
_HTTP_CACHE = _load_http_cache(HTTP_CACHE_FILE)

# --- cache pruning settings + helper ---
# Keep HTTP cache entries for this many seconds (default 7 days).
# You can override via environment variable: NEWS_HTTP_CACHE_MAX_AGE=259200  (3 days)
HTTP_CACHE_MAX_AGE = int(os.getenv("NEWS_HTTP_CACHE_MAX_AGE", str(7 * 86400)))

def _prune_http_cache(now: float | None = None):
    """Remove HTTP cache entries older than HTTP_CACHE_MAX_AGE, then save atomically."""
    now = now or time.time()
    with _HTTP_CACHE_LOCK:
        stale_urls = [u for u, rec in _HTTP_CACHE.items()
                      if now - rec.get("fetched_at", 0) > HTTP_CACHE_MAX_AGE]
        if not stale_urls:
            return
        for u in stale_urls:
            _HTTP_CACHE.pop(u, None)
        _atomic_save_json(HTTP_CACHE_FILE, _HTTP_CACHE)
# --- end pruning helper ---


def fetch_cached(url: str, max_age_seconds: int = 180, headers: dict | None = None, timeout_override: int | None = None):
    """
    Cache layer backed by a JSON file. Thread-safe via locks + atomic writes.
    """
    host = urlparse(url).netloc
    now = time.time()

    # check cache
    rec = _get_cached_record(url)
    if rec and (now - rec.get("fetched_at", 0)) < max_age_seconds:
        class Resp: pass
        resp = Resp()
        resp.status_code = rec.get("status", 0)
        try:
            resp.content = base64.b64decode(rec.get("content_b64", "") or "")
        except Exception:
            resp.content = b""
        resp.headers = rec.get("headers", {}) or {}
        return resp, True

    # cooldown
    if _is_in_cooldown(host):
        class Resp: pass
        r = Resp()
        r.status_code = 503
        # if stale cache exists, return that content; else empty
        if rec:
            try:
                r.content = base64.b64decode(rec.get("content_b64", "") or "")
            except Exception:
                r.content = b""
            r.headers = rec.get("headers", {}) or {}
        else:
            r.content = b""
            r.headers = {}
        return r, bool(rec)

    # polite wait
    _polite_wait(host)

    # session
    sess = SESSION_RSSHUB if host == "rsshub.app" else SESSION
    timeout = timeout_override if timeout_override is not None else (8 if host == "rsshub.app" else 15)
    merged_headers = {**DEFAULT_HEADERS, **(headers or {})}

    # perform request with retry and capture response
    try:
        resp = sess.get(url, timeout=timeout, headers=merged_headers)
    except requests.RequestException:
        if host == "rsshub.app":
            _set_cooldown(host, 180)
        # synthesize a response from stale cache if any
        class Resp: pass
        r = Resp()
        r.status_code = 503
        if rec:
            try:
                r.content = base64.b64decode(rec.get("content_b64", "") or "")
            except Exception:
                r.content = b""
            r.headers = rec.get("headers", {}) or {}
        else:
            r.content = b""
            r.headers = {}
        return r, bool(rec)

    _update_last_hit(host)

    # 429 one-shot backoff
    if getattr(resp, "status_code", 0) == 429:
        ra = resp.headers.get("Retry-After")
        try:
            sleep_s = float(ra)
        except Exception:
            sleep_s = max(MIN_GAP.get(host, DEFAULT_GAP), 3.0)
        time.sleep(sleep_s)
        _polite_wait(host)
        try:
            resp = sess.get(url, timeout=timeout, headers=merged_headers)
        except requests.RequestException:
            if host == "rsshub.app":
                _set_cooldown(host, 180)
            return None, False
        _update_last_hit(host)

    # cooldown for flaky hosts
    if resp.status_code in (500, 502, 503, 504) and host == "rsshub.app":
        _set_cooldown(host, 180)

    # persist cache
    _set_cached_record(
        url,
        getattr(resp, "status_code", 0),
        getattr(resp, "content", b""),
        dict(getattr(resp, "headers", {}))
    )

    # probabilistic prune (~10%) to limit disk writes while preventing bloat
    if random.random() < 0.10:
        _prune_http_cache(now)

    return resp, False

# ========== FEED + PAGE HELPERS ==========
def unwrap_google_news(url: str) -> str:
    try:
        p = urlparse(url or "")
        if p.netloc in ("news.google.com","news.googleusercontent.com"):
            qs = dict(parse_qsl(p.query))
            if "url" in qs: return qs["url"]
    except: pass
    return url or ""

def _is_yahoo_finance(url: str) -> bool:
    try:
        host = urlparse(url).netloc.lower()
        return host.endswith("finance.yahoo.com") or host.endswith("yahoo.com")
    except Exception:
        return False

def parse_feed_with_cache(feed_url: str, ttl_seconds: int = 300):
    resp,_ = fetch_cached(feed_url, max_age_seconds=ttl_seconds, headers=DEFAULT_HEADERS)
    if not resp or getattr(resp,"status_code",0)!=200:
        return feedparser.parse(b"")
    return feedparser.parse(resp.content)

def fetch_article_with_cache(url: str, ttl_seconds: int = 6*3600):
    resp,_ = fetch_cached(url, max_age_seconds=ttl_seconds, headers=DEFAULT_HEADERS, timeout_override=5)
    return resp

def _http_get(url: str):
    resp = fetch_article_with_cache(url, ttl_seconds=6*3600)
    code = getattr(resp,"status_code",0) or 0
    if code==200:
        try: return resp.content.decode("utf-8","ignore"), "ok"
        except: return None,"error"
    if code==403: return None,"http403"
    if code==429: return None,"http429"
    if 400<=code<500: return None,"http4xx"
    if 500<=code<600: return None,"http5xx"
    return None,"error"

# JSON-LD regex
_JSONLD_RE = re.compile(r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>', re.I|re.DOTALL)

def _jsonld_date_published(html: str):
    try:
        for m in _JSONLD_RE.finditer(html):
            data = json.loads(m.group(1))
            items = data if isinstance(data,list) else [data]
            for obj in items:
                if isinstance(obj,dict):
                    if isinstance(obj.get("datePublished"),str):
                        return obj["datePublished"].strip()
    except: pass
    return None

# Meta <meta property="article:published_time" content="..."> extraction
_META_TAG_RE = re.compile(
    r'<meta[^>]+(?:property|name)=["\'](?:article:published_time|og:published_time)["\'][^>]*>',
    re.I
)
_CONTENT_ATTR_RE = re.compile(r'\bcontent=["\']([^"\']+)["\']', re.I)

def _meta_published_from_html(html: str):
    """
    Return the value of content=... from an appropriate meta tag, if present.
    """
    for tag in _META_TAG_RE.findall(html):
        m = _CONTENT_ATTR_RE.search(tag)
        if m:
            return m.group(1).strip()
    return None

# pubtime JSON cache (guarded)
CACHE_FILE = CACHE_DIR / "url_pubtime_cache.json"
UTC = dttz.UTC
_CACHE_LOCK = threading.RLock()
_PAGE_FETCHES = 0
_PAGE_FETCHES_LOCK = threading.RLock()
CACHE_TTL_HOURS = 24

def _load_cache(path: Path) -> dict:
    with _CACHE_LOCK:
        if path.exists():
            try:
                return json.load(open(path,"r",encoding="utf-8"))
            except Exception:
                return {}
        return {}

def _atomic_save_json_pub(path: Path, data: dict):
    tmp_fd, tmp_path = tempfile.mkstemp(dir=str(path.parent), prefix=path.name, suffix=".tmp")
    os.close(tmp_fd)
    try:
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        shutil.move(tmp_path, path)
    finally:
        try:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
        except Exception:
            pass

def _save_cache(path: Path, data: dict):
    with _CACHE_LOCK:
        try:
            _atomic_save_json_pub(path, data)
        except Exception:
            pass

_PUBTIME_CACHE = _load_cache(CACHE_FILE)

def fetch_page_published_time(url: str,fallback_tz):
    global _PAGE_FETCHES, _PUBTIME_CACHE
    if not url:
        return None, "fetch_failed", "error"

    # JSON cache hit
    with _CACHE_LOCK:
        rec = _PUBTIME_CACHE.get(url)
    if rec:
        try:
            return dtparser.parse(rec["published_utc"]), rec.get("source"), "cached"
        except Exception:
            pass

    # throttle hard cap
    with _PAGE_FETCHES_LOCK:
        if _PAGE_FETCHES >= 300:
            return None, "fetch_failed", "no_meta"

    html, status = _http_get(url)
    with _PAGE_FETCHES_LOCK:
        _PAGE_FETCHES += 1

    if status != "ok" or not html:
        return None, "fetch_failed", status

    # 1) meta tags
    raw = _meta_published_from_html(html)
    if raw:
        try:
            dt = dtparser.parse(raw)
            dt_utc = dt.astimezone(UTC)
            with _CACHE_LOCK:
                _PUBTIME_CACHE[url] = {"published_utc": dt_utc.isoformat(), "source": "page_meta"}
                _save_cache(CACHE_FILE, _PUBTIME_CACHE)
            return dt_utc, "page_meta", "ok"
        except Exception:
            pass

    # 2) JSON-LD
    raw = _jsonld_date_published(html)
    if raw:
        try:
            dt = dtparser.parse(raw)
            dt_utc = dt.astimezone(UTC)
            with _CACHE_LOCK:
                _PUBTIME_CACHE[url] = {"published_utc": dt_utc.isoformat(), "source": "json_ld"}
                _save_cache(CACHE_FILE, _PUBTIME_CACHE)
            return dt_utc, "json_ld", "ok"
        except Exception:
            pass

    return None, "fetch_failed", "no_meta"

def _feed_fallback_tz(feed_cfg: dict):
    tz_name=feed_cfg.get("tz")
    return dttz.gettz(tz_name) or UTC

def parse_from_feed_fields(entry: dict,fallback_tz):
    for key in ("published","pubDate","issued"):
        raw=entry.get(key)
        if raw:
            dt=dtparser.parse(raw)
            if dt.tzinfo is None: dt=dt.replace(tzinfo=fallback_tz)
            return dt.astimezone(UTC),key
    return None,None

def pick_summary(entry: dict) -> str:
    return entry.get("summary") or entry.get("description") or ""

# A mutex to ensure only one fetch_headlines run at a time in this process
_RUN_LOCK = threading.RLock()

# ========== FETCH HEADLINES ==========
def fetch_headlines(
    feeds,
    include_kw=None,
    exclude_kw=None,
    minutes_back=1440,
    debug_article=False,
    debug_date_sources=True,
):
    with _RUN_LOCK:
        # soft-reset the per-process fetch counter for each user-triggered run
        with _PAGE_FETCHES_LOCK:
            _PAGE_FETCHES = 0  # reset count for this run

        # normalize filters once
        include_kw = [k.strip().lower() for k in (include_kw or []) if isinstance(k, str) and k.strip()]
        exclude_kw = [k.strip().lower() for k in (exclude_kw or []) if isinstance(k, str) and k.strip()]

        now_utc = datetime.now(timezone.utc)
        cutoff = now_utc - timedelta(minutes=minutes_back)
        rows = []

        for feed in feeds:
            url = feed.get("url")
            if not url:
                continue

            # Expand {when} for Google News feeds
            if "{when}" in url:
                url = url.replace("{when}", google_news_when(minutes_back))

            parsed = parse_feed_with_cache(url)
            entries = getattr(parsed, "entries", []) or []
            fallback_tz = _feed_fallback_tz(feed)
            source_name = feed.get("name") or "unknown"
            source_type = feed.get("source_type") or "rss"

            for e in entries:
                title = (e.get("title") or "").strip()
                if not title:
                    continue
                summary = pick_summary(e)
                link = unwrap_google_news(e.get("link") or "")

                # ---------- APPLY KEYWORD FILTERS ----------
                blob = f"{title} {summary}".lower()
                if include_kw and not any(k in blob for k in include_kw):
                    continue  # must match at least one include term
                if exclude_kw and any(k in blob for k in exclude_kw):
                    continue  # drop if any exclude term matches
                # ------------------------------------------

                # ---------- YAHOO-ONLY FILTER ----------
                if feed.get("only_yahoo") and link:
                    if not _is_yahoo_finance(link):
                        continue  # skip this item
                # ---------------------------------------


                # --- TIMESTAMP PICKING (per-feed control) ---
                
                # 1) feed-provided publish time
                published_dt, ts_source = parse_from_feed_fields(e, fallback_tz)
                fetch_status = "ok"
                
                # EARLY CUTOFF: if feed gave a time and it's older than the window, skip now (no page fetch)
                if published_dt is not None and published_dt < cutoff:
                    continue
                
                # Read per-feed policy from feeds.yaml (valid: "off", "missing_only", "prefer")
                page_time_mode = (feed.get("page_time_mode") or "off").lower()
                
                # 2) Page "published" time according to mode
                if link:
                    if page_time_mode == "prefer":
                        # Fetch page time and prefer it if it's earlier (e.g., CNBC/BBC)
                        page_pub_dt, page_src, page_status = fetch_page_published_time(link, fallback_tz)
                        if page_pub_dt and ((published_dt is None) or (page_pub_dt < published_dt - timedelta(seconds=30))):
                            published_dt = page_pub_dt
                            ts_source = page_src or "page_meta"
                            fetch_status = page_status or "ok"
                
                    elif page_time_mode == "missing_only":
                        # Only fetch page time if the feed had no time
                        if published_dt is None:
                            page_pub_dt, page_src, page_status = fetch_page_published_time(link, fallback_tz)
                            if page_pub_dt:
                                published_dt = page_pub_dt
                                ts_source = page_src or "page_meta"
                                fetch_status = page_status or "ok"
                
                    else:
                        # "off" -> trust the feed time; do nothing
                        pass
                
                # 3) Final fallback so it still appears
                if published_dt is None:
                    published_dt = now_utc
                    ts_source = "fallback_now"
                    if fetch_status is None:
                        fetch_status = "no_date_anywhere"
                
                # Re-check cutoff after any fallback/page fetch
                if published_dt < cutoff:
                    continue

                # --- END TIMESTAMP PICKING ---


                rows.append({
                    "headline": title,
                    "summary": summary,
                    "source": source_name,
                    "link": link,
                    "source_type": source_type,
                    "published": published_dt.isoformat(),
                    "timestamp": now_utc.isoformat(),
                    "event_type": "",
                    "timestamp_source": ts_source,
                    "page_fetch_status": fetch_status or "ok",
                })

        df = pd.DataFrame(rows, columns=[
            "headline","summary","source","link","source_type",
            "published","timestamp","event_type","timestamp_source","page_fetch_status"
        ])

        if df.empty:
            return df

        # De-dup + newest-first
        def _to_dt_or_min(x):
            try:
                return dtparser.parse(x) if x else datetime(1970,1,1, tzinfo=timezone.utc)
            except Exception:
                return datetime(1970,1,1, tzinfo=timezone.utc)

        df = df.drop_duplicates(subset=["headline","link"]).reset_index(drop=True)
        df["_sort_pub"] = df["published"].apply(_to_dt_or_min)
        df["_sort_ts"] = df["timestamp"].apply(_to_dt_or_min)
        df = df.sort_values(by=["_sort_pub","_sort_ts"], ascending=False)\
               .drop(columns=["_sort_pub","_sort_ts"])\
               .reset_index(drop=True)

        return df

# ========== LOCALIZE + EXCEL ==========
def _iso_to_local_str(iso_utc: str,display_tz_name: str,style: str="human") -> str:
    try:
        display_tz=dttz.gettz(display_tz_name) or dttz.UTC
        dt_utc=dtparser.parse(iso_utc)
        dt_loc=dt_utc.astimezone(display_tz)
        return dt_loc.strftime("%Y-%m-%d %H:%M:%S %Z")
    except: return iso_utc

def localize_df_for_display(df_utc: pd.DataFrame,display_tz_name: str,style="human"):
    xdf=df_utc.copy()
    for col in ("published","timestamp"):
        if col in xdf.columns:
            xdf[col]=xdf[col].apply(lambda s:_iso_to_local_str(s,display_tz_name,style))
    return xdf

def save_excel(df_utc: pd.DataFrame,out_dir: Path,display_tz_name: str) -> Path:
    from openpyxl import Workbook
    xdf=localize_df_for_display(df_utc,display_tz_name)
    display_tz=dttz.gettz(display_tz_name) or dttz.UTC
    local_now=datetime.now(timezone.utc).astimezone(display_tz)
    out_path=out_dir/f"headlines_{local_now.strftime('%Y%m%d_%H%M')}.xlsx"
    with pd.ExcelWriter(out_path,engine="openpyxl") as w:
        xdf.to_excel(w,index=False,sheet_name="Headlines")
    return out_path
