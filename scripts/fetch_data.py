#!/usr/bin/env python3
"""
Builds data/stocks.json for the momentum screener.

Sources (no login needed):
  - Nifty 500 constituents: NSE / niftyindices public CSV
  - Prices & volume (1 year daily): Yahoo Finance via yfinance
  - Fundamentals (Market cap, P/E, ROCE, ROE, 3Y growth, Debt/Equity):
    Screener.in public company pages

Fundamentals change quarterly, so they are cached in data/fundamentals.json
and only a slice of the oldest entries is refreshed each run. This keeps the
daily job fast and polite to Screener.in. Prices are refreshed every run.
"""
from __future__ import annotations

import io
import json
import math
import random
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

import numpy as np
import pandas as pd
import requests
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
STOCKS_FILE = DATA_DIR / "stocks.json"
FUND_FILE = DATA_DIR / "fundamentals.json"

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)
NIFTY500_URLS = [
    "https://archives.nseindia.com/content/indices/ind_nifty500list.csv",
    "https://www.niftyindices.com/IndexConstituent/ind_nifty500list.csv",
]

FUND_STALE_DAYS = 5          # refresh fundamentals older than this...
FUND_MAX_REFRESH = 120       # ...but at most this many per run (missing ones always fetched)
SCREENER_DELAY = (1.2, 2.2)  # polite random delay between Screener.in requests (seconds)
MIN_PRICE_COVERAGE = 0.6     # abort without overwriting if fewer stocks than this got prices


# --------------------------------------------------------------------------- helpers
def log(msg: str) -> None:
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def num(text: str | None) -> float | None:
    """Parse '1,23,456.7', '18.5 %', '-3%' -> float; blanks -> None."""
    if text is None:
        return None
    t = text.replace(",", "").replace("₹", "").replace("%", "").strip()
    m = re.search(r"-?\d+(?:\.\d+)?", t)
    return float(m.group()) if m else None


def clean(v, nd: int = 2):
    """Round floats and turn NaN/inf into None so the JSON stays valid."""
    if v is None:
        return None
    if isinstance(v, (np.integer,)):
        return int(v)
    if isinstance(v, (float, np.floating)):
        if math.isnan(v) or math.isinf(v):
            return None
        return round(float(v), nd)
    return v


def load_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def session() -> requests.Session:
    s = requests.Session()
    s.headers.update({"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9"})
    return s


# --------------------------------------------------------------------------- universe
def get_universe(sess: requests.Session) -> pd.DataFrame:
    for url in NIFTY500_URLS:
        try:
            r = sess.get(url, timeout=30)
            r.raise_for_status()
            df = pd.read_csv(io.StringIO(r.text))
            df.columns = [c.strip() for c in df.columns]
            if "Symbol" in df.columns and len(df) > 400:
                log(f"Universe: {len(df)} symbols from {url}")
                return df[["Symbol", "Company Name", "Industry"]].rename(
                    columns={"Symbol": "symbol", "Company Name": "name", "Industry": "industry"}
                )
        except Exception as e:
            log(f"Universe source failed ({url}): {e}")

    prev = load_json(STOCKS_FILE, {})
    if prev.get("stocks") and not prev.get("sample"):
        log("Using symbol list from previous stocks.json")
        return pd.DataFrame(
            [{"symbol": s["symbol"], "name": s.get("name"), "industry": s.get("industry")} for s in prev["stocks"]]
        )
    sys.exit("Could not load the Nifty 500 list and no previous data exists.")


# --------------------------------------------------------------------------- prices / technicals
def rsi_wilder(close: pd.Series, period: int = 14) -> float | None:
    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    avg_loss = loss.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    g, l = avg_gain.iloc[-1], avg_loss.iloc[-1]
    if pd.isna(g) or pd.isna(l):
        return None
    if l == 0:
        return 100.0
    return 100 - 100 / (1 + g / l)


def technicals(df: pd.DataFrame) -> dict | None:
    df = df.dropna(subset=["Close"])
    df = df[df["Close"] > 0]
    if len(df) < 130:  # need ~6 months of history
        return None
    close, high, vol = df["Close"], df["High"], df["Volume"].fillna(0)
    last = float(close.iloc[-1])

    def ret(n: int):
        return (last / float(close.iloc[-1 - n]) - 1) * 100 if len(close) > n else None

    high52 = float(high.tail(252).max())
    vol_avg = float(vol.iloc[-21:-1].mean())  # previous 20 sessions, excluding today
    vol_today = float(vol.iloc[-1])
    return {
        "price": last,
        "return3m": ret(63),
        "return6m": ret(126),
        "high52w": high52,
        "downFrom52wHigh": (high52 - last) / high52 * 100 if high52 else None,
        "rsi": rsi_wilder(close),
        "volume": int(vol_today),
        "volumeAvg1m": vol_avg,
        "volumeRatio": vol_today / vol_avg if vol_avg > 0 else None,
        "priceDate": df.index[-1].strftime("%Y-%m-%d"),
    }


def fetch_prices(symbols: list[str]) -> dict[str, dict]:
    import yfinance as yf

    out: dict[str, dict] = {}
    tickers = [f"{s}.NS" for s in symbols]
    for i in range(0, len(tickers), 100):
        chunk = tickers[i : i + 100]
        log(f"Prices: batch {i // 100 + 1} ({len(chunk)} tickers)")
        for attempt in range(3):
            try:
                data = yf.download(
                    chunk, period="1y", interval="1d", group_by="ticker",
                    auto_adjust=True, threads=True, progress=False,
                )
                break
            except Exception as e:
                log(f"  yfinance error: {e}; retrying")
                time.sleep(5 * (attempt + 1))
        else:
            continue
        for t in chunk:
            try:
                df = data[t] if len(chunk) > 1 else data
                tech = technicals(df)
                if tech:
                    out[t[:-3]] = tech
            except Exception:
                pass
        time.sleep(2)
    log(f"Prices: got technicals for {len(out)}/{len(symbols)}")
    return out


# --------------------------------------------------------------------------- fundamentals (Screener.in)
def parse_screener(html: str) -> dict:
    soup = BeautifulSoup(html, "lxml")
    f: dict = {}

    ratio_map = {
        "Market Cap": "marketCap",
        "Current Price": "screenerPrice",
        "Stock P/E": "pe",
        "ROCE": "roce",
        "ROE": "roe",
    }
    for li in soup.select("#top-ratios li"):
        name_el = li.select_one(".name")
        if not name_el:
            continue
        key = ratio_map.get(name_el.get_text(" ", strip=True))
        if key:
            n = li.select_one(".number")
            f[key] = num(n.get_text()) if n else None

    growth_map = {"Compounded Sales Growth": "salesGrowth3y", "Compounded Profit Growth": "profitGrowth3y"}
    for table in soup.select("table.ranges-table"):
        th = table.select_one("th")
        key = growth_map.get(th.get_text(" ", strip=True)) if th else None
        if not key:
            continue
        for tr in table.select("tr"):
            tds = tr.select("td")
            if len(tds) == 2 and tds[0].get_text(strip=True).startswith("3 Years"):
                f[key] = num(tds[1].get_text())

    # Debt / Equity = Borrowings / (Equity Capital + Reserves), latest balance-sheet column
    rows: dict[str, float | None] = {}
    for tr in soup.select("#balance-sheet table.data-table tbody tr"):
        cells = tr.select("td")
        if len(cells) < 2:
            continue
        label = re.sub(r"[^A-Za-z ]", "", cells[0].get_text(" ", strip=True)).strip().lower()
        vals = [num(c.get_text()) for c in cells[1:]]
        vals = [v for v in vals if v is not None]
        if vals:
            rows[label] = vals[-1]
    equity = (rows.get("equity capital") or 0) + (rows.get("reserves") or 0)
    borrow = next((v for k, v in rows.items() if k.startswith("borrowing")), None)
    if borrow is not None and equity > 0:
        f["debtToEquity"] = borrow / equity

    return f


def fetch_fundamentals(sess: requests.Session, symbol: str) -> dict | None:
    slug = quote(symbol, safe="-")
    best: dict | None = None
    for url in (f"https://www.screener.in/company/{slug}/consolidated/", f"https://www.screener.in/company/{slug}/"):
        for attempt in range(3):
            try:
                r = sess.get(url, timeout=30)
                if r.status_code == 429:
                    wait = 30 * (attempt + 1)
                    log(f"  429 from Screener.in, sleeping {wait}s")
                    time.sleep(wait)
                    continue
                if r.status_code == 404:
                    break
                r.raise_for_status()
                f = parse_screener(r.text)
                filled = sum(f.get(k) is not None for k in ("roce", "roe", "salesGrowth3y", "profitGrowth3y", "debtToEquity"))
                if best is None or filled > best["_filled"]:
                    best = {**f, "_filled": filled}
                break
            except Exception as e:
                log(f"  {symbol}: {e}")
                time.sleep(3)
        time.sleep(random.uniform(*SCREENER_DELAY))
        if best and best["_filled"] >= 4:  # consolidated page was good enough
            break
    if best:
        best.pop("_filled", None)
        best["fetchedAt"] = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return best


def update_fundamentals(sess: requests.Session, symbols: list[str]) -> dict[str, dict]:
    cache: dict[str, dict] = load_json(FUND_FILE, {})
    today = datetime.now(timezone.utc).date()

    def age(sym):
        d = cache.get(sym, {}).get("fetchedAt")
        return (today - datetime.strptime(d, "%Y-%m-%d").date()).days if d else 10_000

    missing = [s for s in symbols if s not in cache]
    stale = sorted((s for s in symbols if s in cache and age(s) >= FUND_STALE_DAYS), key=age, reverse=True)
    todo = missing + stale[:FUND_MAX_REFRESH]
    log(f"Fundamentals: {len(missing)} missing, {len(stale)} stale, fetching {len(todo)}")

    for i, sym in enumerate(todo, 1):
        f = fetch_fundamentals(sess, sym)
        if f:
            cache[sym] = f
        if i % 25 == 0:
            log(f"  {i}/{len(todo)} done")
            FUND_FILE.write_text(json.dumps(cache, indent=0, sort_keys=True), encoding="utf-8")

    FUND_FILE.write_text(json.dumps(cache, indent=0, sort_keys=True), encoding="utf-8")
    return cache


# --------------------------------------------------------------------------- main
def main() -> None:
    DATA_DIR.mkdir(exist_ok=True)
    sess = session()
    universe = get_universe(sess)
    symbols = universe["symbol"].astype(str).str.strip().tolist()

    prices = fetch_prices(symbols)
    if len(prices) < MIN_PRICE_COVERAGE * len(symbols):
        sys.exit(f"Only {len(prices)} stocks got prices; keeping previous data.")

    fund = update_fundamentals(sess, symbols)

    stocks = []
    for row in universe.itertuples(index=False):
        sym = str(row.symbol).strip()
        t = prices.get(sym)
        if not t:
            continue
        f = fund.get(sym, {})
        # Market cap and P/E move with price; scale cached values to today's close.
        scale = (t["price"] / f["screenerPrice"]) if f.get("screenerPrice") else 1.0
        stocks.append({
            "symbol": sym,
            "name": row.name,
            "industry": row.industry,
            "price": clean(t["price"]),
            "marketCap": clean(f["marketCap"] * scale, 0) if f.get("marketCap") else None,
            "pe": clean(f["pe"] * scale, 1) if f.get("pe") else None,
            "roce": clean(f.get("roce"), 1),
            "roe": clean(f.get("roe"), 1),
            "debtToEquity": clean(f.get("debtToEquity")),
            "profitGrowth3y": clean(f.get("profitGrowth3y"), 1),
            "salesGrowth3y": clean(f.get("salesGrowth3y"), 1),
            "downFrom52wHigh": clean(t["downFrom52wHigh"], 1),
            "return3m": clean(t["return3m"], 1),
            "return6m": clean(t["return6m"], 1),
            "rsi": clean(t["rsi"], 1),
            "volume": t["volume"],
            "volumeAvg1m": clean(t["volumeAvg1m"], 0),
            "volumeRatio": clean(t["volumeRatio"]),
            "high52w": clean(t["high52w"]),
            "priceDate": t["priceDate"],
            "fundamentalsDate": f.get("fetchedAt"),
        })

    as_of = max((s["priceDate"] for s in stocks), default=None)
    payload = {
        "generatedAt": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "asOf": as_of,
        "universe": "Nifty 500",
        "sample": False,
        "count": len(stocks),
        "stocks": stocks,
    }
    STOCKS_FILE.write_text(json.dumps(payload, separators=(",", ":"), allow_nan=False), encoding="utf-8")
    log(f"Wrote {len(stocks)} stocks to {STOCKS_FILE.relative_to(ROOT)} (as of {as_of})")


if __name__ == "__main__":
    main()
