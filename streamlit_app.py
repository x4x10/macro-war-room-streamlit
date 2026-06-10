from __future__ import annotations

import math
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from io import StringIO
from typing import Any, Callable
from urllib.parse import quote

import pandas as pd
import requests
import streamlit as st

REQUEST_TIMEOUT = 12
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
HEADERS = {"User-Agent": USER_AGENT, "Accept": "application/json,text/csv,text/plain;q=0.9,*/*;q=0.8"}

st.set_page_config(
    page_title="Macro War Room",
    page_icon="MW",
    layout="wide",
    initial_sidebar_state="collapsed",
)


@dataclass
class Quote:
    instrument: str
    label: str
    value: float | None
    previous: float | None
    timestamp: datetime | None
    source: str
    role: str
    scoring_role: str = "primary"
    validity: str = "valid"
    error: str = ""

    @property
    def change(self) -> float | None:
        if self.value is None or self.previous is None:
            return None
        return self.value - self.previous

    @property
    def change_pct(self) -> float | None:
        if self.value is None or self.previous in (None, 0):
            return None
        return ((self.value - self.previous) / abs(self.previous)) * 100

    @property
    def freshness(self) -> str:
        if self.value is None:
            return "unavailable"
        if self.timestamp is None:
            return "unknown"
        age_hours = (datetime.now(timezone.utc) - self.timestamp).total_seconds() / 3600
        if age_hours <= 24:
            return "live"
        if age_hours <= 96:
            return "stale"
        return "degraded"


INSTRUMENTS: dict[str, dict[str, str]] = {
    "CL1!": {"label": "WTI crude", "role": "oil / primary"},
    "USOIL": {"label": "WTI spot proxy", "role": "oil / verifier", "scoring_role": "verifier"},
    "UKOIL": {"label": "Brent crude", "role": "oil / primary"},
    "US10Y": {"label": "US 10Y yield", "role": "rates / primary"},
    "TNX": {"label": "10Y index verifier", "role": "rates / verifier", "scoring_role": "verifier"},
    "MOVE": {"label": "MOVE bond vol", "role": "rates vol / confirmer"},
    "VIX": {"label": "VIX", "role": "fear / confirmation"},
    "VX1!": {"label": "VIX front future", "role": "vol / context"},
    "VX2!": {"label": "VIX second future", "role": "vol / context"},
    "HYG": {"label": "High-yield ETF", "role": "credit / primary"},
    "BAMLH0A0HYM2": {"label": "HY OAS spread", "role": "credit / most important"},
    "DXY": {"label": "US dollar", "role": "liquidity / primary"},
    "T10Y2Y": {"label": "10Y-2Y curve", "role": "curve / confirmer"},
    "SPX": {"label": "S&P 500", "role": "risk assets / context"},
    "NDX": {"label": "Nasdaq 100", "role": "risk assets / context"},
    "GC1!": {"label": "Gold futures", "role": "gold / primary"},
    "GOLD": {"label": "Gold spot proxy", "role": "gold / verifier", "scoring_role": "verifier"},
    "BTCUSDT": {"label": "Bitcoin", "role": "liquidity / secondary"},
}


def utc_from_seconds(value: int | float | None) -> datetime | None:
    if value is None or not math.isfinite(float(value)):
        return None
    return datetime.fromtimestamp(float(value), tz=timezone.utc)


def clean_number(value: Any) -> float | None:
    if value is None:
        return None
    try:
        result = float(str(value).replace("$", "").replace(",", "").replace("%", "").strip())
        return result if math.isfinite(result) else None
    except Exception:
        return None


def request_get(url: str, *, accept: str | None = None) -> requests.Response:
    headers = dict(HEADERS)
    if accept:
        headers["Accept"] = accept
    response = requests.get(url, headers=headers, timeout=REQUEST_TIMEOUT)
    response.raise_for_status()
    return response


@st.cache_data(ttl=120, show_spinner=False)
def fred_points(series_id: str, days: int = 21) -> list[tuple[datetime, float]]:
    url = f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={quote(series_id)}"
    rows = pd.read_csv(url)
    points: list[tuple[datetime, float]] = []
    for _, row in rows.tail(days * 2).iterrows():
        value = clean_number(row.get(series_id))
        if value is None:
            continue
        ts = pd.to_datetime(row.get("observation_date"), utc=True, errors="coerce")
        if pd.isna(ts):
            continue
        points.append((ts.to_pydatetime(), value))
    return points


@st.cache_data(ttl=90, show_spinner=False)
def yahoo_quote(symbol: str) -> tuple[float, float, datetime | None, str]:
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{quote(symbol, safe='')}?interval=1m&range=1d&includePrePost=true"
    payload = request_get(url).json()
    result = (payload.get("chart") or {}).get("result") or []
    if not result:
        raise RuntimeError(f"Yahoo missing {symbol}")
    row = result[0]
    meta = row.get("meta") or {}
    timestamps = row.get("timestamp") or []
    quote_row = ((row.get("indicators") or {}).get("quote") or [{}])[0]
    closes = [x for x in quote_row.get("close", []) if isinstance(x, (int, float))]
    value = clean_number(meta.get("regularMarketPrice")) or (closes[-1] if closes else None)
    previous = clean_number(meta.get("previousClose")) or clean_number(meta.get("chartPreviousClose")) or (closes[-2] if len(closes) > 1 else value)
    if value is None or previous is None:
        raise RuntimeError(f"Yahoo price missing {symbol}")
    ts = utc_from_seconds(meta.get("regularMarketTime") or (timestamps[-1] if timestamps else None))
    return value, previous, ts, "Yahoo public"


@st.cache_data(ttl=600, show_spinner=False)
def stooq_quote(symbol: str) -> tuple[float, float, datetime | None, str]:
    url = f"https://stooq.com/q/d/l/?s={quote(symbol)}&i=d"
    text = request_get(url, accept="text/csv").text
    rows = pd.read_csv(StringIO(text)).dropna(subset=["Close"])
    if rows.empty:
        raise RuntimeError(f"Stooq missing {symbol}")
    latest = rows.iloc[-1]
    previous = rows.iloc[-2] if len(rows) > 1 else latest
    value = clean_number(latest.get("Close"))
    prev = clean_number(previous.get("Close"))
    if value is None or prev is None:
        raise RuntimeError(f"Stooq price missing {symbol}")
    ts = pd.to_datetime(latest.get("Date"), utc=True, errors="coerce")
    return value, prev, None if pd.isna(ts) else ts.to_pydatetime(), "Stooq public"


@st.cache_data(ttl=60, show_spinner=False)
def coinbase_quote(product: str = "BTC-USD") -> tuple[float, float, datetime | None, str]:
    payload = request_get(f"https://api.exchange.coinbase.com/products/{product}/ticker").json()
    value = clean_number(payload.get("price") or payload.get("ask") or payload.get("bid"))
    if value is None:
        raise RuntimeError("Coinbase BTC price missing")
    ts = pd.to_datetime(payload.get("time"), utc=True, errors="coerce")
    return value, value, None if pd.isna(ts) else ts.to_pydatetime(), "Coinbase public"


@st.cache_data(ttl=900, show_spinner=False)
def cboe_vx(position: int) -> tuple[float, float, datetime | None, str]:
    for day_offset in range(0, 8):
        day = (datetime.now(timezone.utc) - pd.Timedelta(days=day_offset)).strftime("%Y-%m-%d")
        url = f"https://ww2.cboe.com/us/futures/market_statistics/settlement/csv?dt={day}"
        try:
            text = request_get(url, accept="text/csv").text
            rows = pd.read_csv(StringIO(text))
            values = rows.loc[rows.get("Product") == "VX", "Price"].map(clean_number).dropna().tolist()
            if len(values) >= position:
                value = float(values[position - 1])
                ts = datetime.fromisoformat(f"{day}T21:00:00+00:00")
                return value, value, ts, "Cboe settlement"
        except Exception:
            continue
    raise RuntimeError(f"Cboe VX{position} missing")


def fred_quote(instrument: str, series_id: str, label: str, transform: Callable[[float], float] | None = None) -> Quote:
    meta = INSTRUMENTS[instrument]
    try:
        points = fred_points(series_id)
        if not points:
            raise RuntimeError(f"FRED {series_id} missing")
        ts, value = points[-1]
        prev = points[-2][1] if len(points) > 1 else value
        if transform:
            value = transform(value)
            prev = transform(prev)
        return Quote(instrument, meta["label"], value, prev, ts, label, meta["role"], meta.get("scoring_role", "primary"))
    except Exception as exc:
        return unavailable(instrument, label, exc)


def unavailable(instrument: str, source: str, exc: Exception | str) -> Quote:
    meta = INSTRUMENTS[instrument]
    return Quote(
        instrument=instrument,
        label=meta["label"],
        value=None,
        previous=None,
        timestamp=None,
        source=source,
        role=meta["role"],
        scoring_role=meta.get("scoring_role", "primary"),
        validity="invalid",
        error=str(exc),
    )


def quote_with_fallback(
    instrument: str,
    loaders: list[tuple[str, Callable[[], tuple[float, float, datetime | None, str]]]],
    transform: Callable[[float], float] | None = None,
) -> Quote:
    meta = INSTRUMENTS[instrument]
    errors: list[str] = []
    for source_name, loader in loaders:
        try:
            value, previous, ts, source = loader()
            if transform:
                value = transform(value)
                previous = transform(previous)
            return Quote(instrument, meta["label"], value, previous, ts, source, meta["role"], meta.get("scoring_role", "primary"))
        except Exception as exc:
            errors.append(f"{source_name}: {exc}")
    return unavailable(instrument, " / ".join(source for source, _ in loaders), "; ".join(errors))


@st.cache_data(ttl=75, show_spinner="Loading direct live public data...")
def load_quotes() -> dict[str, Quote]:
    quotes: dict[str, Quote] = {}
    quotes["CL1!"] = quote_with_fallback("CL1!", [("Yahoo CL", lambda: yahoo_quote("CL=F")), ("FRED WTI", lambda: fred_latest("DCOILWTICO", "FRED WTI spot"))])
    quotes["USOIL"] = fred_quote("USOIL", "DCOILWTICO", "FRED WTI spot")
    quotes["UKOIL"] = quote_with_fallback("UKOIL", [("Yahoo Brent", lambda: yahoo_quote("BZ=F")), ("FRED Brent", lambda: fred_latest("DCOILBRENTEU", "FRED Brent spot"))])
    quotes["US10Y"] = fred_quote("US10Y", "DGS10", "FRED DGS10")
    us10y = quotes["US10Y"]
    quotes["TNX"] = Quote("TNX", INSTRUMENTS["TNX"]["label"], None if us10y.value is None else us10y.value * 10, None if us10y.previous is None else us10y.previous * 10, us10y.timestamp, "FRED DGS10 x10", INSTRUMENTS["TNX"]["role"], "verifier", us10y.validity, us10y.error)
    quotes["MOVE"] = quote_with_fallback("MOVE", [("Yahoo MOVE", lambda: yahoo_quote("^MOVE"))])
    quotes["VIX"] = quote_with_fallback("VIX", [("Yahoo VIX", lambda: yahoo_quote("^VIX")), ("FRED VIX", lambda: fred_latest("VIXCLS", "FRED VIX close"))])
    quotes["VX1!"] = quote_with_fallback("VX1!", [("Cboe VX1", lambda: cboe_vx(1))])
    quotes["VX2!"] = quote_with_fallback("VX2!", [("Cboe VX2", lambda: cboe_vx(2))])
    quotes["HYG"] = quote_with_fallback("HYG", [("Yahoo HYG", lambda: yahoo_quote("HYG")), ("Stooq HYG", lambda: stooq_quote("hyg.us"))])
    quotes["BAMLH0A0HYM2"] = fred_quote("BAMLH0A0HYM2", "BAMLH0A0HYM2", "FRED HY OAS")
    quotes["DXY"] = quote_with_fallback("DXY", [("Yahoo DXY", lambda: yahoo_quote("DX-Y.NYB")), ("FRED dollar proxy", lambda: fred_latest("DTWEXBGS", "FRED broad dollar proxy"))])
    quotes["T10Y2Y"] = fred_quote("T10Y2Y", "T10Y2Y", "FRED 10Y2Y")
    quotes["SPX"] = quote_with_fallback("SPX", [("Yahoo SPX", lambda: yahoo_quote("^GSPC")), ("FRED SP500", lambda: fred_latest("SP500", "FRED SP500"))])
    quotes["NDX"] = quote_with_fallback("NDX", [("Yahoo NDX", lambda: yahoo_quote("^NDX")), ("Stooq NASDAQ100", lambda: stooq_quote("ndx"))])
    quotes["GC1!"] = quote_with_fallback("GC1!", [("Yahoo Gold", lambda: yahoo_quote("GC=F")), ("FRED Gold", lambda: fred_latest("GOLDAMGBD228NLBM", "FRED gold spot"))])
    quotes["GOLD"] = fred_quote("GOLD", "GOLDAMGBD228NLBM", "FRED gold spot")
    quotes["BTCUSDT"] = quote_with_fallback("BTCUSDT", [("Coinbase BTC", lambda: coinbase_quote("BTC-USD")), ("Yahoo BTC", lambda: yahoo_quote("BTC-USD"))])
    return quotes


def fred_latest(series_id: str, source: str) -> tuple[float, float, datetime | None, str]:
    points = fred_points(series_id)
    if not points:
        raise RuntimeError(f"FRED {series_id} missing")
    ts, value = points[-1]
    previous = points[-2][1] if len(points) > 1 else value
    return value, previous, ts, source


def clamp(value: float, low: float = 0, high: float = 100) -> float:
    return max(low, min(high, value))


def value(q: Quote | None) -> float | None:
    return q.value if q and q.value is not None and math.isfinite(q.value) else None


def high_score(raw: float | None, start: float, danger: float) -> float | None:
    if raw is None:
        return None
    return clamp((raw - start) / (danger - start) * 100)


def low_score(raw: float | None, start: float, danger: float) -> float | None:
    if raw is None:
        return None
    return clamp((start - raw) / (start - danger) * 100)


def curve_score(raw: float | None) -> float | None:
    if raw is None:
        return None
    return clamp((-raw + 0.15) / 1.15 * 100)


def weighted(parts: list[tuple[str, float, float | None]]) -> tuple[float, dict[str, float]]:
    available = [(name, weight, score) for name, weight, score in parts if score is not None]
    if not available:
        return 0.0, {}
    total_weight = sum(weight for _, weight, _ in available)
    contributions = {name: (weight / total_weight) * score for name, weight, score in available}
    return sum(contributions.values()), contributions


def compute_risk(quotes: dict[str, Quote]) -> dict[str, Any]:
    scores = {
        "BAMLH0A0HYM2": high_score(value(quotes.get("BAMLH0A0HYM2")), 3.2, 7.0),
        "HYG": low_score(value(quotes.get("HYG")), 84.0, 78.0),
        "MOVE": high_score(value(quotes.get("MOVE")), 85.0, 155.0),
        "VIX": high_score(value(quotes.get("VIX")), 16.0, 40.0),
        "US10Y": high_score(value(quotes.get("US10Y")), 3.8, 4.8),
        "DXY": high_score(value(quotes.get("DXY")), 100.0, 112.0),
        "T10Y2Y": curve_score(value(quotes.get("T10Y2Y"))),
        "BTCUSDT": low_score(value(quotes.get("BTCUSDT")), 110000.0, 75000.0),
        "CL1!": high_score(value(quotes.get("CL1!")), 75.0, 105.0),
        "UKOIL": high_score(value(quotes.get("UKOIL")), 78.0, 110.0),
        "GC1!": high_score(value(quotes.get("GC1!")), 2700.0, 4200.0),
    }

    credit, credit_contrib = weighted([
        ("BAMLH0A0HYM2", 0.45, scores["BAMLH0A0HYM2"]),
        ("HYG", 0.35, scores["HYG"]),
        ("MOVE", 0.15, scores["MOVE"]),
        ("VIX", 0.05, scores["VIX"]),
    ])
    liquidity, liquidity_contrib = weighted([
        ("US10Y", 0.40, scores["US10Y"]),
        ("DXY", 0.30, scores["DXY"]),
        ("T10Y2Y", 0.20, scores["T10Y2Y"]),
        ("BTCUSDT", 0.10, scores["BTCUSDT"]),
    ])
    geopolitics, geopolitical_contrib = weighted([
        ("CL1!", 0.40, scores["CL1!"]),
        ("UKOIL", 0.30, scores["UKOIL"]),
        ("GC1!", 0.20, scores["GC1!"]),
        ("VIX", 0.10, scores["VIX"]),
    ])

    systemic = 0.20 * geopolitics + 0.50 * credit + 0.30 * liquidity
    crash_risk = systemic
    overrides: list[str] = []

    spread = value(quotes.get("BAMLH0A0HYM2"))
    hyg = value(quotes.get("HYG"))
    us10y = value(quotes.get("US10Y"))
    vix = value(quotes.get("VIX"))
    cl = value(quotes.get("CL1!"))
    brent = value(quotes.get("UKOIL"))

    def floor_at(level: float, label: str) -> None:
        nonlocal crash_risk
        if crash_risk < level:
            crash_risk = level
            overrides.append(label)

    if spread is not None:
        if spread >= 8.0:
            floor_at(92, "HY OAS >= 8.0")
        elif spread >= 6.0:
            floor_at(80, "HY OAS >= 6.0")
        elif spread >= 5.0:
            floor_at(68, "HY OAS >= 5.0")
        elif spread >= 4.25:
            floor_at(55, "HY OAS >= 4.25")
    if hyg is not None:
        if hyg < 78:
            floor_at(82, "HYG < 78")
        elif hyg < 79:
            floor_at(70, "HYG < 79")
        elif hyg < 80:
            floor_at(58, "HYG < 80")
    if hyg is not None and spread is not None:
        if hyg < 79 and spread >= 5.0:
            floor_at(85, "Confirmed credit break")
        elif hyg < 80 and spread >= 4.25:
            floor_at(72, "Early confirmed credit break")
    if us10y is not None:
        if us10y >= 4.5:
            crash_risk += 10
            overrides.append("US10Y >= 4.50")
        elif us10y >= 4.3:
            crash_risk += 6
            overrides.append("US10Y >= 4.30")
    if vix is not None:
        if vix >= 40:
            crash_risk += 12
            overrides.append("VIX >= 40")
        elif vix >= 30:
            crash_risk += 6
            overrides.append("VIX >= 30")
    oil_peak = max([x for x in [cl, brent] if x is not None], default=None)
    if oil_peak is not None:
        if oil_peak >= 100:
            crash_risk += 8
            overrides.append("Oil >= 100")
        elif oil_peak >= 90:
            crash_risk += 4
            overrides.append("Oil >= 90")

    signal_contrib = {**credit_contrib, **liquidity_contrib, **geopolitical_contrib}
    top_drivers = sorted(signal_contrib.items(), key=lambda item: item[1], reverse=True)[:5]

    crash_risk = clamp(crash_risk)
    if crash_risk >= 80:
        condition = "red"
        posture = "De-risk / defensive"
    elif crash_risk >= 60:
        condition = "orange"
        posture = "Monitor closely / reduce risk"
    elif crash_risk >= 35:
        condition = "yellow"
        posture = "Cautious neutral"
    else:
        condition = "green"
        posture = "Normal monitoring"

    valid = sum(1 for quote in quotes.values() if quote.value is not None)
    readiness = round((valid / len(INSTRUMENTS)) * 100)
    confidence = max(30, min(95, readiness - (0 if spread is not None and hyg is not None else 10)))

    return {
        "crash_risk": crash_risk,
        "condition": condition,
        "posture": posture,
        "confidence": confidence,
        "readiness": readiness,
        "pillars": {"Credit stress": credit, "Liquidity pressure": liquidity, "Geopolitical heat": geopolitics},
        "signal_contrib": signal_contrib,
        "top_drivers": top_drivers,
        "overrides": overrides,
    }


def fmt(value: float | None, digits: int = 2) -> str:
    return "unavailable" if value is None else f"{value:,.{digits}f}"


def pill(text: str, tone: str = "neutral") -> str:
    return f'<span class="mw-pill mw-{tone}">{text}</span>'


st.markdown(
    """
    <style>
      .stApp { background: #070706; color: #f6ecdd; }
      section[data-testid="stSidebar"] { background: #0d0c0a; }
      div[data-testid="stMetric"] { background: #0d0c0a; border: 1px solid rgba(243,162,64,.14); padding: 14px; }
      .mw-kicker { color: #f3a240; letter-spacing: .22em; font-size: 12px; text-transform: uppercase; }
      .mw-note { color: #b9a68d; font-size: 14px; }
      .mw-frame { border: 1px solid rgba(243,162,64,.14); background: #0d0c0a; padding: 16px; margin: 12px 0; }
      .mw-grid { display:grid; grid-template-columns: repeat(auto-fit,minmax(220px,1fr)); gap:12px; }
      .mw-card { border: 1px solid rgba(243,162,64,.12); background:#100f0d; padding:14px; min-height:120px; }
      .mw-value { font-size: 28px; line-height: 1.1; font-weight: 700; color:#f6ecdd; }
      .mw-pill { display:inline-block; padding:4px 8px; margin:2px 4px 2px 0; border:1px solid rgba(243,162,64,.18); font-size:12px; text-transform:uppercase; letter-spacing:.12em; }
      .mw-green { color:#82c99a; border-color:rgba(130,201,154,.28); }
      .mw-yellow { color:#e8c66c; border-color:rgba(232,198,108,.28); }
      .mw-orange { color:#f3a240; border-color:rgba(243,162,64,.28); }
      .mw-red { color:#ef7770; border-color:rgba(239,119,112,.28); }
      .mw-neutral { color:#b9a68d; }
      .mw-live-dot { width:8px; height:8px; border-radius:50%; background:#82c99a; display:inline-block; margin-right:8px; animation: mw-live 1.8s ease-in-out infinite; }
      @keyframes mw-live { 0%,100%{opacity:.35} 50%{opacity:1} }
      @media (prefers-reduced-motion: reduce) { .mw-live-dot { animation:none; } }
      a { color: #ffc46e !important; }
    </style>
    """,
    unsafe_allow_html=True,
)

with st.sidebar:
    st.header("Runtime")
    st.write("Data path")
    st.code("Streamlit direct live public sources")
    st.write("No Cloudflare / no backend API")
    if st.button("Refresh now"):
        st.cache_data.clear()
        st.rerun()

st.markdown('<div class="mw-kicker">Macro War Room</div>', unsafe_allow_html=True)
st.title("Live risk dashboard")
st.caption("Self-contained Streamlit version. It pulls public live market data directly; no Cloudflare tunnel or backend API is required.")

quotes = load_quotes()
risk = compute_risk(quotes)
last_ts = max((q.timestamp for q in quotes.values() if q.timestamp), default=None)
valid_count = sum(1 for q in quotes.values() if q.value is not None)

st.markdown(
    f"""
    <div class="mw-frame">
      <div class="mw-kicker">Now</div>
      <p><span class="mw-live-dot"></span><b>{valid_count}/18 instruments loaded from direct public live sources</b></p>
      <p>{pill(risk['condition'], risk['condition'])} {pill('no mock', 'green')} {pill('no cloudflare', 'neutral')}</p>
      <p class="mw-note">Latest source timestamp: {last_ts.isoformat(timespec='seconds') if last_ts else 'unavailable'} · Rendered: {datetime.now(timezone.utc).isoformat(timespec='seconds')}</p>
    </div>
    """,
    unsafe_allow_html=True,
)

col1, col2, col3, col4 = st.columns(4)
col1.metric("Crash risk", f"{risk['crash_risk']:.0f} / 100")
col2.metric("Condition", str(risk["condition"]).upper())
col3.metric("Data readiness", f"{risk['readiness']}%")
col4.metric("Confidence", f"{risk['confidence']}%")

st.markdown(
    f"""
    <div class="mw-frame">
      <div class="mw-kicker">Primary crash drivers</div>
      <p><b>{risk['posture']}</b></p>
      <p>Crash risk is currently driven mostly by: {', '.join(name for name, _ in risk['top_drivers'][:3]) or 'unavailable'}.</p>
      <p class="mw-note">Priority model: HY OAS first, HYG second, then US10Y, VIX, and oil. Duplicate verifier symbols do not receive full independent weight.</p>
    </div>
    """,
    unsafe_allow_html=True,
)

st.subheader("Risk pillars")
pillar_cols = st.columns(3)
for col, (name, score) in zip(pillar_cols, risk["pillars"].items()):
    col.metric(name, f"{score:.0f}")

if risk["overrides"]:
    st.warning("Overrides active: " + ", ".join(risk["overrides"]))
else:
    st.info("No hard crash override is currently active.")

rows = []
for instrument_id in INSTRUMENTS:
    q = quotes[instrument_id]
    rows.append(
        {
            "instrument": instrument_id,
            "label": q.label,
            "value": fmt(q.value),
            "change %": "" if q.change_pct is None else f"{q.change_pct:.2f}%",
            "freshness": q.freshness,
            "validity": q.validity,
            "role": q.role,
            "scoring": q.scoring_role,
            "source": q.source,
        }
    )

st.subheader("Instrument audit")
st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)

contrib_rows = [
    {"driver": name, "risk contribution": round(score, 2)}
    for name, score in sorted(risk["signal_contrib"].items(), key=lambda item: item[1], reverse=True)
]
st.subheader("Contribution analysis")
st.bar_chart(pd.DataFrame(contrib_rows).set_index("driver"))
st.dataframe(pd.DataFrame(contrib_rows), use_container_width=True, hide_index=True)

unavailable_rows = [
    {"instrument": q.instrument, "source attempted": q.source, "error": q.error}
    for q in quotes.values()
    if q.value is None
]
if unavailable_rows:
    st.subheader("Unavailable live sources")
    st.dataframe(pd.DataFrame(unavailable_rows), use_container_width=True, hide_index=True)

st.caption("This Streamlit deployment intentionally uses no mock data. If a public provider blocks or delays a symbol, that symbol is marked unavailable instead of fabricated.")
