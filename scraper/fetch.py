import os, json, sqlite3, time, re, random
from pathlib import Path
from urllib.parse import urlparse, parse_qsl
from datetime import datetime, timedelta, timezone

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

# polite wait + cooldown
_last_hit = {}
MIN_GAP = {"rsshub.app":5.0,"apnews.com":2.0,"www.theguardian.com":0.6}
DEFAULT_GAP = 0.8
_HOST_COOLDOWN_UNTIL = {}

def _polite_wait(host: str):
    gap = MIN_GAP.get(host, DEFAULT_GAP)
    last = _last_hit.get(host, 0.0)
    wait = (last + gap) - time.time()
    if wait > 0:
        time.sleep(wait)

def _update_last_hit(host: str):
    _last_hit[host] = time.time()

def _is_in_cooldown(host: str) -> bool:
    return time.time() < _HOST_COOLDOWN_UNTIL.get(host, 0)

def _set_cooldown(host: str, seconds: float):
    _HOST_COOLDOWN_UNTIL[host] = time.time() + seconds

# sqlite cache
CACHE_DB = CACHE_DIR / "url_cache.sqlite"

# hold a global connection (can be re-created if needed)
_conn = None

def _get_conn():
    """Get a valid SQLite connection, reopen if broken."""
    global _conn
    import sqlite3

    if _conn is None:
        _conn = sqlite3.connect(
            CACHE_DB,
            check_same_thread=False,   # allow Streamlit reruns
            timeout=30                 # wait if locked
        )
        _conn.execute("PRAGMA journal_mode=WAL;")    # better concurrency
        _conn.execute("PRAGMA synchronous=NORMAL;")
        _conn.execute("""CREATE TABLE IF NOT EXISTS cache (
          url TEXT PRIMARY KEY,
          fetched_at REAL,
          status INTEGER,
          content BLOB,
          headers TEXT
        )""")
        _conn.commit()
        return _conn

    try:
        _conn.execute("SELECT 1")  # test if still alive
    except Exception:
        _conn = None
        return _get_conn()
    return _conn


def fetch_cached(url: str, max_age_seconds: int = 180, headers: dict | None = None, timeout_override: int | None = None):
    host = urlparse(url).netloc
    # check cache
    cur = _get_conn().execute("SELECT fetched_at, status, content, headers FROM cache WHERE url=?", (url,))
    row = cur.fetchone()
    now = time.time()
    if row and (now - row[0] < max_age_seconds):
        class Resp: pass
        resp = Resp()
        resp.status_code = row[1]
        resp.content = row[2]
        resp.headers = json.loads(row[3])
        return resp, True
    # cooldown
    if _is_in_cooldown(host):
        class Resp: pass
        r = Resp()
        r.status_code = 503
        r.content = row[2] if row else b""
        r.headers = json.loads(row[3]) if row else {}
        return r, bool(row)
    # polite wait
    _polite_wait(host)
    # session
    sess = SESSION_RSSHUB if host == "rsshub.app" else SESSION
    timeout = timeout_override if timeout_override is not None else (8 if host=="rsshub.app" else 15)
    merged_headers = {**DEFAULT_HEADERS, **(headers or {})}
    try:
        resp = sess.get(url, timeout=timeout, headers=merged_headers)
    except requests.RequestException:
        if host=="rsshub.app": _set_cooldown(host,180)
        return None, False
    _update_last_hit(host)
    # 429 handling
    if getattr(resp,"status_code",0)==429:
        ra = resp.headers.get("Retry-After")
        try: sleep_s = float(ra)
        except: sleep_s = max(MIN_GAP.get(host,DEFAULT_GAP),3.0)
        time.sleep(sleep_s)
        _polite_wait(host)
        try:
            resp = sess.get(url, timeout=timeout, headers=merged_headers)
        except requests.RequestException:
            if host=="rsshub.app": _set_cooldown(host,180)
            return None, False
        _update_last_hit(host)
    if resp.status_code in (500,502,503,504) and host=="rsshub.app":
        _set_cooldown(host,180)
    _get_conn().execute(
        "REPLACE INTO cache(url,fetched_at,status,content,headers) VALUES (?,?,?,?,?)",
        (url, now, resp.status_code, resp.content, json.dumps(dict(resp.headers)))
    )
    _get_conn().commit()
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

# pubtime JSON cache
CACHE_FILE = CACHE_DIR / "url_pubtime_cache.json"
UTC = dttz.UTC
_PAGE_FETCHES = 0
CACHE_TTL_HOURS = 24

def _load_cache(path: Path) -> dict:
    if path.exists():
        try: return json.load(open(path,"r",encoding="utf-8"))
        except: return {}
    return {}

def _save_cache(path: Path,data: dict):
    try: json.dump(data, open(path,"w",encoding="utf-8"), ensure_ascii=False, indent=2)
    except: pass

_PUBTIME_CACHE = _load_cache(CACHE_FILE)

def fetch_page_published_time(url: str,fallback_tz):
    global _PAGE_FETCHES, _PUBTIME_CACHE
    if not url: return None,"fetch_failed","error"
    rec = _PUBTIME_CACHE.get(url)
    if rec: return dtparser.parse(rec["published_utc"]),rec.get("source"),"cached"
    if _PAGE_FETCHES>=300: return None,"fetch_failed","no_meta"
    html,status=_http_get(url)
    _PAGE_FETCHES+=1
    if status!="ok" or not html: return None,"fetch_failed",status
    # check meta tags
    for pattern,label in [
        (r'article:published_time','article:published_time'),
        (r'og:published_time','og:published_time')
    ]:
        m=re.search(pattern,html,re.I)
        if m:
            dt=dtparser.parse(m.group(0))
            return dt.astimezone(UTC),f"page_meta_{label}","ok"
    # JSON-LD
    raw=_jsonld_date_published(html)
    if raw:
        dt=dtparser.parse(raw)
        return dt.astimezone(UTC),"json_ld","ok"
    return None,"fetch_failed","no_meta"

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

# ========== FETCH HEADLINES ==========
def fetch_headlines(
    feeds,
    include_kw=None,
    exclude_kw=None,
    minutes_back=1440,
    debug_article=False,
    debug_date_sources=True,
):
    now_utc=datetime.now(timezone.utc)
    cutoff=now_utc-timedelta(minutes=minutes_back)
    rows=[]
    for feed in feeds:
        url=feed.get("url")
        if not url: continue
        if "{when}" in url:
            url=url.replace("{when}",google_news_when(minutes_back))
        parsed=parse_feed_with_cache(url)
        entries=getattr(parsed,"entries",[]) or []
        fallback_tz=_feed_fallback_tz(feed)
        for e in entries:
            title=(e.get("title") or "").strip()
            if not title: continue
            summary=pick_summary(e)
            link=unwrap_google_news(e.get("link") or "")
            published_dt,ts_source=parse_from_feed_fields(e,fallback_tz)
            if published_dt is None and link:
                published_dt,ts_source,_=fetch_page_published_time(link,fallback_tz)
            if published_dt is None: 
                published_dt=now_utc; ts_source="fallback_now"
            if published_dt<cutoff: continue
            rows.append({
                "headline":title,"summary":summary,"source":feed.get("name"),
                "link":link,"source_type":"rss","published":published_dt.isoformat(),
                "timestamp":now_utc.isoformat(),"event_type":"",
                "timestamp_source":ts_source,"page_fetch_status":"ok",
            })
    df=pd.DataFrame(rows)
    if df.empty: return df
    df=df.drop_duplicates(subset=["headline","link"]).reset_index(drop=True)
    df=df.sort_values(by="published",ascending=False).reset_index(drop=True)
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
