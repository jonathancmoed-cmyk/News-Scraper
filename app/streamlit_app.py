import os, sys
sys.path.append(os.path.dirname(os.path.dirname(__file__)))

import time
from io import BytesIO

import pandas as pd
import streamlit as st

from scraper.config import load_feeds, CACHE_DIR
from scraper.fetch import fetch_headlines, localize_df_for_display


st.set_page_config(page_title="News Scraper", layout="wide")
st.title("News Scraper")


# ---- Helpers ----
def _split_csv(text: str):
    return [s.strip() for s in text.split(",") if s.strip()] if text else []


def _ensure_columns(df: pd.DataFrame, cols: list[str]) -> pd.DataFrame:
    for c in cols:
        if c not in df.columns:
            df[c] = ""
    return df


def _fmt_bytes(n: int | None) -> str:
    if n is None:
        return "—"
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def _file_size(path) -> int | None:
    try:
        return os.path.getsize(path)
    except Exception:
        return None


# --- Sidebar controls ---
with st.sidebar:
    st.header("Filters")
    include_kw_text = st.text_input("Include keywords (comma-separated)", "")
    exclude_kw_text = st.text_input("Exclude keywords (comma-separated)", "")
    minutes_back = st.number_input(
        "Minutes back (lookback window)",
        min_value=10, max_value=10080, value=1440, step=10
    )
    display_tz = st.text_input("Display timezone", "Europe/Amsterdam")
    run_btn = st.button("Run scraper")

    st.markdown("---")
    st.header("Maintenance")

    http_cache = CACHE_DIR / "url_cache.json"
    pubtime_json = CACHE_DIR / "url_pubtime_cache.json"

    col_a, col_b = st.columns(2)
    with col_a:
        st.caption("HTTP cache (JSON)")
        st.write(f"**File:** `{http_cache.name}`")
        st.write(f"**Exists:** {http_cache.exists()}")
        st.write(f"**Size:** {_fmt_bytes(_file_size(http_cache)) if http_cache.exists() else '—'}")

    with col_b:
        st.caption("Published-time JSON cache")
        st.write(f"**File:** `{pubtime_json.name}`")
        st.write(f"**Exists:** {pubtime_json.exists()}")
        st.write(f"**Size:** {_fmt_bytes(_file_size(pubtime_json)) if pubtime_json.exists() else '—'}")

    clear_http = st.button("Clear HTTP cache")
    clear_pub = st.button("Clear pubtime cache")

    if clear_http:
        try:
            if http_cache.exists():
                os.remove(http_cache)
                st.success(f"Deleted: {http_cache.name}")
            else:
                st.info("No HTTP cache file found.")
        except Exception as e:
            st.error(f"Failed to delete HTTP cache: {e}")

    if clear_pub:
        try:
            if pubtime_json.exists():
                os.remove(pubtime_json)
                st.success(f"Deleted: {pubtime_json.name}")
            else:
                st.info("No pubtime cache file found.")
        except Exception as e:
            st.error(f"Failed to delete pubtime cache: {e}")


# --- Main action ---
if run_btn:
    with st.spinner("Fetching headlines..."):
        feeds = load_feeds()
        include_kw = _split_csv(include_kw_text)
        exclude_kw = _split_csv(exclude_kw_text)

        t0 = time.time()
        df = fetch_headlines(
            feeds,
            include_kw=include_kw,
            exclude_kw=exclude_kw,
            minutes_back=int(minutes_back),
        )
        runtime = time.time() - t0

    st.success(f"Collected **{len(df)}** rows in **{runtime:.2f}s**.")

    if df.empty:
        st.warning("No rows returned. Try widening the lookback window or removing filters.")
    else:
        # Localize for display
        df_local = localize_df_for_display(df, display_tz, style="human")

        # Show key columns
        show_cols = [
            "headline", "summary", "source", "link", "source_type",
            "published", "timestamp", "event_type", "timestamp_source", "page_fetch_status"
        ]
        df_local = _ensure_columns(df_local, show_cols)
        st.dataframe(df_local[show_cols].head(100), use_container_width=True)

        # Diagnostics
        with st.expander("Diagnostics"):
            src_counts = (
                df["timestamp_source"]
                .value_counts(dropna=False)
                .rename_axis("timestamp_source")
                .reset_index(name="count")
            )
            fetch_counts = (
                df["page_fetch_status"]
                .value_counts(dropna=False)
                .rename_axis("page_fetch_status")
                .reset_index(name="count")
            )
            col1, col2 = st.columns(2)
            with col1:
                st.caption("Timestamp sources")
                st.dataframe(src_counts, use_container_width=True, hide_index=True)
            with col2:
                st.caption("Page fetch statuses")
                st.dataframe(fetch_counts, use_container_width=True, hide_index=True)

        # Download Excel (in-memory)
        buf = BytesIO()
        df_local.to_excel(buf, index=False, engine="openpyxl")
        st.download_button(
            "Download Excel",
            data=buf.getvalue(),
            file_name="headlines.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
else:
    st.info("Set filters (optional) and click **Run scraper**.")
