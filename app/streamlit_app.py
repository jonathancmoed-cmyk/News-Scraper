import os, sys
sys.path.append(os.path.dirname(os.path.dirname(__file__)))

import time
from io import BytesIO

import pandas as pd
import streamlit as st

from scraper.config import load_feeds
from scraper.fetch import fetch_headlines, localize_df_for_display

st.set_page_config(page_title="News Scraper", layout="wide")
st.title("News Scraper")

# --- Sidebar controls ---
with st.sidebar:
    st.header("Filters")
    include_kw_text = st.text_input("Include keywords (comma-separated)", "")
    exclude_kw_text = st.text_input("Exclude keywords (comma-separated)", "")
    minutes_back = st.number_input("Minutes back (lookback window)",
                                   min_value=10, max_value=10080, value=1440, step=10)
    display_tz = st.text_input("Display timezone", "Europe/Amsterdam")
    run_btn = st.button("Run scraper")

def _split_csv(text: str):
    return [s.strip() for s in text.split(",") if s.strip()] if text else []

def _ensure_columns(df: pd.DataFrame, cols: list[str]) -> pd.DataFrame:
    for c in cols:
        if c not in df.columns:
            df[c] = ""
    return df

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

        # Diagnostics (optional, helpful for debugging timestamp sources)
        with st.expander("Diagnostics"):
            src_counts = df["timestamp_source"].value_counts(dropna=False).rename_axis("timestamp_source").reset_index(name="count")
            fetch_counts = df["page_fetch_status"].value_counts(dropna=False).rename_axis("page_fetch_status").reset_index(name="count")
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
