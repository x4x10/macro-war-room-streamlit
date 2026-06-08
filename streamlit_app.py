from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any

import pandas as pd
import requests
import streamlit as st
import streamlit.components.v1 as components

DEFAULT_PUBLIC_URL = "https://joe-pipes-any-zope.trycloudflare.com"

st.set_page_config(
    page_title="Macro War Room",
    page_icon="MW",
    layout="wide",
    initial_sidebar_state="collapsed",
)


def config_value(key: str, default: str = "") -> str:
    """Read Streamlit secrets first, then environment variables."""
    try:
        value = st.secrets.get(key, None)
    except Exception:
        value = None
    return str(value or os.getenv(key, default)).rstrip("/")


API_BASE_URL = config_value("API_BASE_URL", DEFAULT_PUBLIC_URL)
APP_EMBED_URL = config_value("APP_EMBED_URL", DEFAULT_PUBLIC_URL)
REQUEST_TIMEOUT = float(config_value("REQUEST_TIMEOUT", "12") or "12")


@st.cache_data(ttl=30, show_spinner=False)
def fetch_json(path: str) -> dict[str, Any]:
    url = f"{API_BASE_URL}{path}"
    response = requests.get(url, timeout=REQUEST_TIMEOUT)
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict):
        raise ValueError(f"Unexpected response from {url}")
    return payload


def safe_number(value: Any, digits: int = 0) -> str:
    if isinstance(value, (int, float)):
        return f"{value:.{digits}f}"
    return "unavailable"


def instrument_rows(summary: dict[str, Any]) -> list[dict[str, Any]]:
    instruments = summary.get("instruments") or {}
    rows: list[dict[str, Any]] = []
    if not isinstance(instruments, dict):
        return rows
    for instrument_id, row in sorted(instruments.items()):
        if not isinstance(row, dict):
            continue
        rows.append(
            {
                "instrument": instrument_id,
                "value": row.get("value"),
                "condition": row.get("conditionState", "unavailable"),
                "freshness": row.get("freshnessState", "unavailable"),
                "validity": row.get("validityState", "unavailable"),
                "role": row.get("role", "context"),
                "scoring_role": row.get("scoringRole", "primary"),
                "nearest_level": (row.get("nearestRule") or {}).get("label", ""),
            }
        )
    return rows


def health_rows(health_data: dict[str, Any]) -> list[dict[str, Any]]:
    audits = health_data.get("instrumentAudits") or []
    rows: list[dict[str, Any]] = []
    if not isinstance(audits, list):
        return rows
    for audit in audits:
        if not isinstance(audit, dict):
            continue
        rows.append(
            {
                "instrument": audit.get("instrumentId"),
                "quote": "present" if audit.get("quotePresent") else "missing",
                "freshness": audit.get("freshnessState"),
                "validity": audit.get("validityState"),
                "source": audit.get("sourceMode"),
                "history": "ok" if audit.get("historyAvailable") else "missing",
                "role": audit.get("role"),
                "scoring_role": audit.get("scoringRole"),
            }
        )
    return rows


st.markdown(
    """
    <style>
      .stApp { background: #0b0a09; color: #f6ecdd; }
      section[data-testid="stSidebar"] { background: #11100e; }
      div[data-testid="stMetric"] { background: #11100e; border: 1px solid rgba(243,162,64,.16); padding: 14px; }
      .mw-kicker { color: #f3a240; letter-spacing: .18em; font-size: 12px; text-transform: uppercase; }
      .mw-note { color: #b9a68d; font-size: 14px; }
      .mw-frame { border: 1px solid rgba(243,162,64,.14); background: #11100e; padding: 16px; }
      a { color: #ffc46e !important; }
    </style>
    """,
    unsafe_allow_html=True,
)

st.markdown('<div class="mw-kicker">Macro War Room</div>', unsafe_allow_html=True)
st.title("Live risk dashboard")
st.caption("Streamlit wrapper for the existing Macro War Room API. The full production UI remains the Next.js app.")

with st.sidebar:
    st.header("Runtime")
    st.write("API base URL")
    st.code(API_BASE_URL)
    st.write("Full dashboard URL")
    st.code(APP_EMBED_URL)
    if st.button("Refresh now"):
        st.cache_data.clear()
        st.rerun()

try:
    health = fetch_json("/api/health")
    summary = fetch_json("/api/dashboard/summary/v2")
    health_data = fetch_json("/api/health/data")
except Exception as exc:
    st.error("The Streamlit app is running, but it cannot reach the Macro War Room API.")
    st.write(str(exc))
    st.markdown(
        "Set `API_BASE_URL` in Streamlit secrets to a public gateway URL, for example the Cloudflare tunnel or Railway gateway."
    )
    st.stop()

bootstrap = health.get("bootstrap") or {}
counters = summary.get("counters") or {}
source_mode = summary.get("sourceMode") or health.get("sourceMode") or "unknown"
market_state = summary.get("marketState", "unknown")
as_of = summary.get("asOf") or bootstrap.get("latestSummaryAt")

col1, col2, col3, col4 = st.columns(4)
col1.metric("Crash risk", safe_number(summary.get("crashRisk"), 0))
col2.metric("Confidence", f"{safe_number(summary.get('confidence'), 0)}%")
col3.metric("Data readiness", f"{safe_number(summary.get('dataReadiness'), 0)}%")
col4.metric("Source", f"{source_mode} / {market_state}")

st.markdown(
    f"""
    <div class="mw-frame">
      <div class="mw-kicker">Current state</div>
      <p><b>{summary.get('dominantRegime', 'unknown')}</b> / {summary.get('condition', 'unknown')}</p>
      <p>{summary.get('recommendationState', 'unknown')} - {summary.get('recommendationReason', 'No reason reported.')}</p>
      <p class="mw-note">As of: {as_of or 'unavailable'} | Bootstrap: {bootstrap.get('loaded', '?')}/{bootstrap.get('total', '?')} ({bootstrap.get('status', 'unknown')})</p>
    </div>
    """,
    unsafe_allow_html=True,
)

st.subheader("Data quality")
q1, q2, q3, q4 = st.columns(4)
q1.metric("Invalid", counters.get("invalidSignals", 0))
q2.metric("Stale", counters.get("staleSignals", 0))
q3.metric("New alerts", counters.get("newAlerts", 0))
q4.metric("Data alerts", counters.get("dataAlerts", 0))

rows = instrument_rows(summary)
if rows:
    df = pd.DataFrame(rows)
    st.subheader("Instruments")
    st.dataframe(df, use_container_width=True, hide_index=True)

hrows = health_rows(health_data)
if hrows:
    hdf = pd.DataFrame(hrows)
    st.subheader("Health audit")
    st.dataframe(hdf, use_container_width=True, hide_index=True)

st.subheader("Full dashboard")
st.markdown(f"Open full dashboard: [{APP_EMBED_URL}]({APP_EMBED_URL})")
embed_height = 900
components.html(
    f"""
    <iframe
      src="{APP_EMBED_URL}"
      width="100%"
      height="{embed_height}"
      style="border: 1px solid rgba(243,162,64,.16); background: #0b0a09;"
      loading="lazy"
      referrerpolicy="no-referrer"
    ></iframe>
    """,
    height=embed_height + 24,
)

st.caption(f"Rendered by Streamlit at {datetime.now(timezone.utc).isoformat(timespec='seconds')}")
