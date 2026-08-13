#!/usr/bin/env python3
"""
NSE 500 GTF AUTO-FILTER SCANNER  v3.19
======================================
Pehle wala scanner sirf ~48 fixed names pe fake DEMAND zone laga raha tha.
Is version mein:

  1. Wikipedia / local CSV se LIVE Nifty 500 universe (500 symbols)
  2. Yahoo Finance se 3Y daily OHLC (real prices, no mock LTP)
  3. Indicator 3.15 jaisa strict 1-bar Red-Green / Green-Red zone engine
  4. keepInvalid=false — broken zones delete
  5. Sirf woh stocks dashboard pe aate hain jo Aaj 1D ya 1M zone ke ANDAR
     ya proximal ke 2% ke NEAR hain
  6. List har scan pe badalti hai — same 42 names lock nahi rahenge
  7. Top 3 weekly swing picks bhi isi filtered list se nikalte hain

GitHub Actions: Mon-Fri 15:45 IST
Password ref: 7004602
"""

from __future__ import annotations

import csv
import datetime as dt
import html as htmllib
import json
import math
import os
import re
import sys
import time
import urllib.request
from collections import defaultdict

import numpy as np
import pandas as pd
import yfinance as yf

VERSION = "v3.19 Auto-Filter Engine"
PASSWORD_REF = "7004602"
IMPULSE_ATR = 0.35
IMPULSE_BODY_PCT = 75.0
MIN_ZONE_ATR = 0.05
MAX_ZONE_ATR = 1.80
NEAR_PCT = 0.02
ATR_LEN = 14
LOOKBACK_BARS = 220
MAX_ZONES = 6
BATCH_SIZE = 60
OHLC_PERIOD = "3y"

PSU_BANKS = {
    "SBIN", "PNB", "BANKBARODA", "CANBK", "UNIONBANK", "INDIANB",
    "BANKINDIA", "MAHABANK", "CENTRALBK", "IOB", "UCOBANK", "PSB",
    "J&KBANK", "UCOBANK", "MAHABANK",
}
NIFTY_BANK_MAJORS = {
    "HDFCBANK", "ICICIBANK", "KOTAKBANK", "AXISBANK", "INDUSINDBK",
    "IDFCFIRSTB", "FEDERALBNK", "AUBANK", "BANDHANBNK", "YESBANK",
}
CPSE_SYMS = {
    "NTPC", "POWERGRID", "COALINDIA", "ONGC", "BEL", "BHEL", "NHPC",
    "GAIL", "OIL", "CONCOR", "NBCC", "IRCTC", "IRFC", "RVNL", "HAL",
    "BEML", "HUDCO", "NLCINDIA", "SJVN", "NMDC",
}

SECTOR_FROM_INDUSTRY = {
    "financial services": "FINSERVICE",
    "information technology": "IT",
    "automobile and auto components": "AUTO",
    "healthcare": "HEALTHCARE",
    "fast moving consumer goods": "FMCG",
    "metals & mining": "METAL",
    "oil gas & consumable fuels": "OIL",
    "power": "CPSE",
    "realty": "REALTY",
    "construction": "INFRA",
    "construction materials": "INFRA",
    "capital goods": "INFRA",
    "consumer services": "CONSUMPTION",
    "consumer durables": "CONSUMPTION",
    "telecommunication": "CONSUMPTION",
    "services": "CONSUMPTION",
    "chemicals": "FMCG",
    "textiles": "CONSUMPTION",
    "media entertainment & publication": "CONSUMPTION",
    "diversified": "CONSUMPTION",
}

INDEX_TICKERS = {
    "BANK": "^NSEBANK",
    "IT": "^CNXIT",
    "AUTO": "^CNXAUTO",
    "METAL": "^CNXMETAL",
    "PHARMA": "^CNXPHARMA",
    "FMCG": "^CNXFMCG",
    "OIL": "^CNXENERGY",
    "REALTY": "^CNXREALTY",
    "INFRA": "^CNXINFRA",
    "PSUBANK": "^CNXPSUBANK",
    "FINSERVICE": "NIFTY_FIN_SERVICE.NS",
}


def today_ist():
    return dt.datetime.now(dt.timezone(dt.timedelta(hours=5, minutes=30))).replace(tzinfo=None)


def log(msg):
    print(msg, flush=True)


def send_telegram_alert(message):
    bot_token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not (bot_token and chat_id):
        return
    try:
        import urllib.parse
        url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
        data = urllib.parse.urlencode(
            {"chat_id": chat_id, "text": message, "parse_mode": "HTML"}
        ).encode("utf-8")
        req = urllib.request.Request(url, data=data)
        urllib.request.urlopen(req, timeout=8)
    except Exception as exc:
        log(f"  [TELEGRAM ERROR] {exc}")


def _clean_html(text):
    text = re.sub(r"<[^>]+>", "", text or "")
    text = htmllib.unescape(text).replace("\xa0", " ").strip()
    return re.sub(r"\s+", " ", text)


def fetch_nifty500_universe():
    """Live Wikipedia constituents, else local CSV baked into the repo."""
    rows = []
    try:
        req = urllib.request.Request(
            "https://en.wikipedia.org/wiki/NIFTY_500",
            headers={"User-Agent": "Mozilla/5.0 GTFScanner/3.19"},
        )
        html = urllib.request.urlopen(req, timeout=25).read().decode("utf-8", "replace")
        idx = html.lower().find('id="constituents"')
        section = html[idx: idx + 300000] if idx >= 0 else html
        nxt = section.lower().find("<h2", 20)
        if nxt > 0:
            section = section[:nxt]
        found = re.findall(
            r"<tr[^>]*>\s*<td[^>]*>(.*?)</td>\s*<td[^>]*>(.*?)</td>\s*<td[^>]*>(.*?)</td>\s*<td[^>]*>(.*?)</td>\s*<td[^>]*>(.*?)</td>",
            section,
            re.S | re.I,
        )
        for rec in found:
            comp, industry, sym, series, isin = map(_clean_html, rec)
            if sym and sym.upper() != "SYMBOL":
                rows.append(
                    {
                        "symbol": sym.replace(" ", ""),
                        "company": comp,
                        "industry": industry,
                        "series": series,
                        "isin": isin,
                    }
                )
        if len(rows) >= 400:
            log(f"[*] Wikipedia Nifty 500 universe: {len(rows)} symbols")
            return rows
        log(f"[!] Wikipedia parsed only {len(rows)} rows — falling back to local CSV")
    except Exception as exc:
        log(f"[!] Wikipedia fetch failed ({exc}) — using local CSV")

    csv_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "nifty500_universe.csv")
    if os.path.exists(csv_path):
        with open(csv_path, newline="", encoding="utf-8") as fh:
            for rec in csv.DictReader(fh):
                if rec.get("symbol"):
                    rows.append(
                        {
                            "symbol": rec["symbol"].replace(" ", ""),
                            "company": rec.get("company") or rec["symbol"],
                            "industry": rec.get("industry") or "",
                            "series": rec.get("series") or "EQ",
                            "isin": rec.get("isin") or "",
                        }
                    )
        log(f"[*] Local Nifty 500 CSV universe: {len(rows)} symbols")
        return rows
    raise RuntimeError("Nifty 500 universe not available (wiki + local CSV both failed)")


def map_sector(symbol, company, industry):
    ind = (industry or "").strip().lower()
    if symbol in CPSE_SYMS:
        return "CPSE"
    if "pharma" in ind or "pharma" in (company or "").lower():
        return "PHARMA"
    if "bank" in ind or "bank" in (company or "").lower() or symbol in PSU_BANKS or symbol in NIFTY_BANK_MAJORS:
        if symbol in PSU_BANKS or "psu" in (company or "").lower():
            return "PSUBANK"
        if symbol in NIFTY_BANK_MAJORS:
            return "BANK"
        return "PVTBANK"
    if "public sector" in ind or symbol.endswith("PSU"):
        return "PSE"
    return SECTOR_FROM_INDUSTRY.get(ind, "CONSUMPTION")


def yahoo_symbol(nse_sym):
    return f"{nse_sym}.NS"


def extract_ohlcv(raw, ticker):
    if raw is None or getattr(raw, "empty", True):
        return None
    df = None
    if isinstance(raw.columns, pd.MultiIndex):
        lvl0 = set(map(str, raw.columns.get_level_values(0)))
        lvl1 = set(map(str, raw.columns.get_level_values(1)))
        if ticker in lvl0:
            df = raw[ticker].copy()
        elif ticker in lvl1:
            df = raw.xs(ticker, axis=1, level=1).copy()
        else:
            return None
    else:
        df = raw.copy()
    rename = {str(c).strip().title(): str(c).strip().title() for c in df.columns}
    df = df.rename(columns=rename)
    # yfinance sometimes uses lowercase
    colmap = {c.lower(): c for c in df.columns}
    need = {}
    for key in ("open", "high", "low", "close", "volume"):
        if key in colmap:
            need[key.capitalize()] = colmap[key]
        else:
            return None
    out = pd.DataFrame({k: pd.to_numeric(df[v], errors="coerce") for k, v in need.items()})
    out.index = pd.to_datetime(df.index, utc=True, errors="coerce").tz_localize(None)
    out = out.dropna(subset=["Open", "High", "Low", "Close"])
    if len(out) < 40:
        return None
    return out.sort_index()


def resample_ohlc(df, rule):
    res = (
        df.resample(rule, label="right", closed="right")
        .agg({"Open": "first", "High": "max", "Low": "min", "Close": "last", "Volume": "sum"})
        .dropna(subset=["Open", "High", "Low", "Close"])
    )
    return res if len(res) >= 20 else None


def wilder_atr(high, low, close, n=ATR_LEN):
    high = np.asarray(high, dtype=float)
    low = np.asarray(low, dtype=float)
    close = np.asarray(close, dtype=float)
    prev = np.roll(close, 1)
    tr = np.maximum(high - low, np.maximum(np.abs(high - prev), np.abs(low - prev)))
    tr[0] = high[0] - low[0]
    atr = np.full(len(tr), np.nan, dtype=float)
    if len(tr) < n:
        return atr
    atr[n - 1] = tr[:n].mean()
    alpha = 1.0 / n
    for i in range(n, len(tr)):
        atr[i] = atr[i - 1] * (1 - alpha) + tr[i] * alpha
    return atr


def ema(values, n=20):
    values = np.asarray(values, dtype=float)
    out = np.full(len(values), np.nan)
    if len(values) < n:
        return out
    out[n - 1] = values[:n].mean()
    k = 2.0 / (n + 1.0)
    for i in range(n, len(values)):
        out[i] = values[i] * k + out[i - 1] * (1 - k)
    return out


def grade_of(score):
    if score >= 9.0:
        return "A+"
    if score >= 7.0:
        return "A"
    if score >= 5.5:
        return "B"
    return "C"


def calc_score(tests, body, atr, is_no_base, has_gap, aligned_ema, body_pct):
    f_score = 3.0 if tests == 0 else 1.5 if tests == 1 else 0.0
    if (atr and body >= atr * 1.5) or has_gap:
        s_score = 2.0
    elif atr and body >= atr * 0.45:
        s_score = 1.5
    else:
        s_score = 1.0
    b_score = 1.5 if is_no_base else 2.0
    fsb7 = f_score + s_score + b_score
    bonus = 0.0
    if aligned_ema:
        bonus += 1.0
    if has_gap:
        bonus += 1.0
    if atr and body >= atr * 1.2:
        bonus += 1.0
    if body_pct >= IMPULSE_BODY_PCT:
        bonus += 0.5
    tot = min(fsb7 + min(bonus, 3.0), 10.0)
    return round(tot, 1), grade_of(tot)


def detect_active_zones(df, impulse_body_pct=55.0):
    """
    Indicator 3.15 strict 1-bar turn:
      Demand = previous red impulsive + current green impulsive + close > prev close
      Supply = previous green impulsive + current red impulsive + close < prev close
    Prox/Dist = below-wick-to-lower-body / above-wick-to-higher-body
    keepInvalid = False
    """
    if df is None or len(df) < ATR_LEN + 5:
        return [], []
    o = df["Open"].to_numpy(float)
    h = df["High"].to_numpy(float)
    l = df["Low"].to_numpy(float)
    c = df["Close"].to_numpy(float)
    atr = wilder_atr(h, l, c)
    ema20 = ema(c, 20)
    start = max(len(df) - LOOKBACK_BARS, ATR_LEN + 1)

    demands = []
    supplies = []

    def overlap(zones, prox, dist):
        for z in zones:
            lo1, hi1 = min(z["prox"], z["dist"]), max(z["prox"], z["dist"])
            lo2, hi2 = min(prox, dist), max(prox, dist)
            ov = min(hi1, hi2) - max(lo1, lo2)
            sz = max(hi2 - lo2, 1e-9)
            if ov > 0 and ov / sz > 0.50:
                return True
        return False

    for i in range(start, len(df)):
        a = atr[i]
        if not np.isfinite(a) or a <= 0:
            continue
        body = abs(c[i] - o[i])
        body1 = abs(c[i - 1] - o[i - 1])
        rng = max(h[i] - l[i], 1e-9)
        body_pct = body / rng * 100.0
        is_green0 = c[i] > o[i]
        is_red0 = c[i] < o[i]
        is_green1 = c[i - 1] > o[i - 1]
        is_red1 = c[i - 1] < o[i - 1]
        has_gap_up = (o[i] - h[i - 1]) >= c[i] * 0.003
        has_gap_dn = (l[i - 1] - o[i]) >= c[i] * 0.003
        aligned_up = np.isfinite(ema20[i]) and c[i] > ema20[i]
        aligned_dn = np.isfinite(ema20[i]) and c[i] < ema20[i]
        impulse_ok = (
            body >= a * IMPULSE_ATR
            and body1 >= a * IMPULSE_ATR
            and body_pct >= impulse_body_pct
        )

        if impulse_ok and is_red1 and is_green0 and c[i] > c[i - 1]:
            prox = float(min(c[i - 1], o[i]))
            dist = float(min(l[i - 1], l[i]))
            height = prox - dist
            if a * MIN_ZONE_ATR <= height <= a * MAX_ZONE_ATR and not overlap(demands, prox, dist):
                score, grade = calc_score(0, body, a, True, has_gap_up, aligned_up, body_pct)
                demands.append(
                    {
                        "side": "DEMAND",
                        "pat": "DR DEMAND",
                        "prox": round(prox, 2),
                        "dist": round(dist, 2),
                        "born": i,
                        "tests": 0,
                        "score": score,
                        "grade": grade,
                        "body_pct": round(body_pct, 1),
                    }
                )
                if len(demands) > MAX_ZONES:
                    demands.pop(0)

        if impulse_ok and is_green1 and is_red0 and c[i] < c[i - 1]:
            prox = float(max(c[i - 1], o[i]))
            dist = float(max(h[i - 1], h[i]))
            height = dist - prox
            if a * MIN_ZONE_ATR <= height <= a * MAX_ZONE_ATR and not overlap(supplies, prox, dist):
                score, grade = calc_score(0, body, a, True, has_gap_dn, aligned_dn, body_pct)
                supplies.append(
                    {
                        "side": "SUPPLY",
                        "pat": "RD SUPPLY",
                        "prox": round(prox, 2),
                        "dist": round(dist, 2),
                        "born": i,
                        "tests": 0,
                        "score": score,
                        "grade": grade,
                        "body_pct": round(body_pct, 1),
                    }
                )
                if len(supplies) > MAX_ZONES:
                    supplies.pop(0)

        # tests + invalidation on later bars
        last = i == len(df) - 1
        # we apply tests/invalid only using the current bar against already born zones
        keep_d = []
        for z in demands:
            if i <= z["born"]:
                keep_d.append(z)
                continue
            touch_now = l[i] <= z["prox"] and l[i] >= z["dist"]
            touch_prev = l[i - 1] <= z["prox"] and l[i - 1] >= z["dist"] if i - 1 > z["born"] else False
            if touch_now and not touch_prev:
                z["tests"] += 1
                z["score"], z["grade"] = calc_score(
                    z["tests"], body, a, True, False, aligned_up, z.get("body_pct", 0)
                )
            if c[i] < z["dist"]:
                continue  # invalidated, drop
            keep_d.append(z)
        demands = keep_d[-MAX_ZONES:]

        keep_s = []
        for z in supplies:
            if i <= z["born"]:
                keep_s.append(z)
                continue
            touch_now = h[i] >= z["prox"] and h[i] <= z["dist"]
            touch_prev = h[i - 1] >= z["prox"] and h[i - 1] <= z["dist"] if i - 1 > z["born"] else False
            if touch_now and not touch_prev:
                z["tests"] += 1
                z["score"], z["grade"] = calc_score(
                    z["tests"], body, a, True, False, aligned_dn, z.get("body_pct", 0)
                )
            if c[i] > z["dist"]:
                continue
            keep_s.append(z)
        supplies = keep_s[-MAX_ZONES:]

        _ = last  # reserved

    return demands, supplies


def zone_relation(ltp, zone):
    if not zone:
        return "NONE", 999.0
    prox, dist = zone["prox"], zone["dist"]
    lo, hi = (min(prox, dist), max(prox, dist))
    if lo <= ltp <= hi:
        return "IN", 0.0
    if zone["side"] == "DEMAND":
        # just above proximal = approaching demand
        if hi < ltp <= hi * (1 + NEAR_PCT):
            return "NEAR", (ltp - hi) / hi * 100.0
        if ltp < lo:
            return "BROKEN", (lo - ltp) / lo * 100.0
        return "AWAY", (ltp - hi) / hi * 100.0
    # supply
    if lo * (1 - NEAR_PCT) <= ltp < lo:
        return "NEAR", (lo - ltp) / lo * 100.0
    if ltp > hi:
        return "BROKEN", (ltp - hi) / hi * 100.0
    return "AWAY", (lo - ltp) / lo * 100.0


def fmt_zone(zone):
    if not zone:
        return "— NO ACTIVE ZONE —"
    lo, hi = sorted((zone["dist"], zone["prox"]))
    return f"{lo} - {hi} {zone['pat']}"


def fmt_score(zone):
    if not zone:
        return "—"
    return f"{zone['score']:.1f} {zone['grade']}"


def freshness(tests):
    if tests <= 0:
        return "🟢 0 TESTS (FRESH)"
    if tests == 1:
        return "🟡 1 TEST (TESTED)"
    return f"🔴 {tests} TESTS (WEAK)"


def weekly_trend(wdf):
    if wdf is None or len(wdf) < 25:
        return "1W DATA THIN"
    c = wdf["Close"].to_numpy(float)
    e = ema(c, 20)
    last_c, last_e = c[-1], e[-1]
    prev_e = e[-2] if np.isfinite(e[-2]) else last_e
    if not np.isfinite(last_e):
        return "1W EMA FLAT"
    if last_c > last_e and last_e >= prev_e:
        return "1W UP • 20 EMA BULLISH"
    if last_c < last_e and last_e <= prev_e:
        return "1W DOWN • 20 EMA BEARISH"
    return "1W SIDEWAYS • 20 EMA FLAT"


def volume_status(df):
    if df is None or "Volume" not in df.columns or len(df) < 21:
        return "NORMAL VOL", 1.0
    vol = df["Volume"].to_numpy(float)
    avg = np.nanmean(vol[-21:-1]) if np.nanmean(vol[-21:-1]) > 0 else 1.0
    ratio = float(vol[-1] / avg)
    if ratio >= 2.5:
        return "🔥 2.5x VOL EXPLOSION", round(ratio, 2)
    if ratio >= 1.8:
        return "🔥 1.8x HIGH VOL", round(ratio, 2)
    return "NORMAL VOL", round(ratio, 2)


def pick_best_side(ltp, demands, supplies):
    """Choose the most relevant active zone for TODAY's filter."""
    candidates = []
    for z in demands[-3:]:
        rel, dist_pct = zone_relation(ltp, z)
        if rel in ("IN", "NEAR"):
            candidates.append((z, rel, dist_pct))
    for z in supplies[-3:]:
        rel, dist_pct = zone_relation(ltp, z)
        if rel in ("IN", "NEAR"):
            candidates.append((z, rel, dist_pct))
    if not candidates:
        return None, "NONE", 999.0
    # prefer IN over NEAR, then higher score, then closer
    candidates.sort(key=lambda t: (0 if t[1] == "IN" else 1, -t[0]["score"], t[2]))
    z, rel, dist_pct = candidates[0]
    return z, rel, dist_pct


def download_batches(tickers):
    frames = {}
    for i in range(0, len(tickers), BATCH_SIZE):
        batch = tickers[i : i + BATCH_SIZE]
        log(f"[*] Downloading batch {i // BATCH_SIZE + 1}/{(len(tickers) - 1) // BATCH_SIZE + 1} ({len(batch)} tickers)")
        try:
            raw = yf.download(
                tickers=batch,
                period=OHLC_PERIOD,
                interval="1d",
                group_by="ticker",
                auto_adjust=True,
                threads=True,
                progress=False,
                timeout=60,
            )
        except Exception as exc:
            log(f"    batch error: {exc}")
            raw = None
        if raw is None or getattr(raw, "empty", True):
            time.sleep(1.0)
            continue
        for tkr in batch:
            df = extract_ohlcv(raw, tkr)
            if df is not None:
                frames[tkr] = df
        time.sleep(0.4)
    return frames


def build_sector_cards(stock_rows, index_ltps):
    by_sec = defaultdict(list)
    for row in stock_rows:
        by_sec[row["sector"]].append(row["type"])

    def card(sec_id, name, desc_default):
        types = by_sec.get(sec_id, [])
        dem = sum(1 for t in types if t == "DEMAND")
        sup = sum(1 for t in types if t == "SUPPLY")
        if dem > sup and dem > 0:
            status, typ, score, bonus = "IN DEMAND", "DEMAND", "9.0 A+", "+2.0 PTS DEMAND"
        elif sup > dem and sup > 0:
            status, typ, score, bonus = "NEAR SUPPLY", "SUPPLY", "8.5 A", "0.0 PTS (Supply Near)"
        else:
            status, typ, score, bonus = "EQUILIBRIUM", "NEUTRAL", "8.0 A", "+1.0 PT TREND"
        ltp = index_ltps.get(sec_id)
        ltp_txt = f"{ltp:,.2f}" if isinstance(ltp, (int, float)) else "—"
        desc = (
            f"{name}: {dem} demand / {sup} supply zone stocks in today's auto-filter. {desc_default}"
        )
        return {
            "id": sec_id,
            "name": name,
            "status": status,
            "type": typ,
            "score": score,
            "ltp": ltp_txt,
            "desc": desc,
            "bonus": bonus,
        }

    cards = [
        {
            "id": "ALL",
            "name": "ALL SECTORS",
            "status": "AUTO FILTER",
            "type": "NEUTRAL",
            "score": "LIVE",
            "ltp": "NSE 500",
            "desc": f"Only stocks sitting IN/NEAR a valid unmitigated 1D or 1M GTF zone on {today_ist().strftime('%Y-%m-%d')}.",
            "bonus": "AUTO",
        },
        card("BANK", "NIFTY BANK", "Banking heavyweights."),
        card("OIL", "NIFTY ENERGY", "Oil & energy names."),
        card("IT", "NIFTY IT", "IT / tech names."),
        card("AUTO", "NIFTY AUTO", "Auto & auto-ancillary."),
        card("METAL", "NIFTY METAL", "Metals & mining."),
        card("PHARMA", "NIFTY PHARMA", "Pharma names."),
        card("FMCG", "NIFTY FMCG", "FMCG / staples."),
        card("REALTY", "NIFTY REALTY", "Realty names."),
        card("INFRA", "NIFTY INFRA", "Infra / capital goods."),
        card("PSE", "NIFTY PSE", "Public sector enterprises."),
        card("FINSERVICE", "NIFTY FIN SERVICE", "NBFCs & financials."),
        card("PSUBANK", "NIFTY PSU BANK", "PSU banks."),
        card("PVTBANK", "NIFTY PVT BANK", "Private banks."),
        card("CONSUMPTION", "NIFTY CONSUMPTION", "Consumption theme."),
        card("HEALTHCARE", "NIFTY HEALTHCARE", "Hospitals & healthcare."),
        card("CPSE", "NIFTY CPSE", "Central PSUs."),
    ]
    return cards


def make_top3(stock_rows):
    picks = [
        r
        for r in stock_rows
        if r["type"] == "DEMAND"
        and r.get("rel") in ("IN", "NEAR")
        and "UP" in (r.get("w_trend") or "")
        and r.get("tests_count", 99) <= 1
    ]
    picks.sort(key=lambda r: (-_combo_num(r.get("combo")), -_score_num(r.get("s1d")), r.get("sym")))
    top = []
    for r in picks[:3]:
        ltp = float(r["ltp"])
        # recover daily demand proximal/distal from z1d text
        nums = re.findall(r"(\d+(?:\.\d+)?)", r.get("z1d") or "")
        if len(nums) >= 2:
            lo, hi = float(nums[0]), float(nums[1])
        else:
            lo, hi = round(ltp * 0.97, 2), round(ltp * 0.995, 2)
        entry_hi = hi
        entry_lo = round((lo + hi) / 2.0, 2)
        sl = round(lo * 0.997, 2)
        risk = max(entry_hi - sl, 0.1)
        qty = max(int(round(1000.0 / risk)), 1)
        t1 = round(entry_hi + 2 * risk, 2)
        t2 = round(entry_hi + 3 * risk, 2)
        move = max(int(round(3000.0 / max(qty, 1))), 1)
        top.append(
            {
                "sym": r["sym"],
                "comp": r["comp"],
                "sector": r["sector"],
                "ltp": ltp,
                "combo": r["combo"],
                "z1d": r["z1d"],
                "z1m": r["z1m"],
                "s1d": r["s1d"],
                "s1m": r["s1m"],
                "entryLo": entry_lo,
                "entryHi": entry_hi,
                "sl": sl,
                "t1": t1,
                "t2": t2,
                "qty": qty,
                "moveFor3k": move,
                "secBonus": r.get("secBonus", ""),
            }
        )
    return top


def _combo_num(txt):
    m = re.search(r"(\d+(?:\.\d+)?)\s*/\s*10", str(txt or ""))
    return float(m.group(1)) if m else 0.0


def _score_num(txt):
    m = re.search(r"(\d+(?:\.\d+)?)", str(txt or ""))
    return float(m.group(1)) if m else 0.0


def scan():
    now = today_ist()
    today_str = now.strftime("%Y-%m-%d")
    log("=" * 88)
    log(f"  GTF NSE 500 AUTO-FILTER SCANNER  {VERSION}")
    log(f"  Date: {today_str} IST | Password ref: {PASSWORD_REF}")
    log("=" * 88)

    universe = fetch_nifty500_universe()
    # persist latest universe so next run / Actions has it offline
    uni_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "nifty500_universe.csv")
    with open(uni_path, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=["symbol", "company", "industry", "series", "isin"])
        w.writeheader()
        w.writerows(universe)

    meta = {r["symbol"]: r for r in universe}
    tickers = [yahoo_symbol(r["symbol"]) for r in universe]
    log(f"[*] Fetching 3Y daily OHLC for {len(tickers)} Yahoo symbols...")
    frames = download_batches(tickers)
    log(f"[*] OHLC received for {len(frames)} / {len(tickers)} symbols")

    # optional index LTPs
    index_ltps = {}
    try:
        idx_raw = yf.download(
            tickers=list(INDEX_TICKERS.values()),
            period="5d",
            interval="1d",
            group_by="ticker",
            auto_adjust=True,
            threads=True,
            progress=False,
            timeout=40,
        )
        for sec, tkr in INDEX_TICKERS.items():
            df = extract_ohlcv(idx_raw, tkr)
            if df is not None:
                index_ltps[sec] = float(df["Close"].iloc[-1])
    except Exception as exc:
        log(f"[!] Index LTP fetch skipped: {exc}")

    scanned = 0
    failed = 0
    stock_rows = []

    for nse_sym, info in meta.items():
        ysym = yahoo_symbol(nse_sym)
        df = frames.get(ysym)
        if df is None:
            failed += 1
            continue
        scanned += 1
        ltp = float(df["Close"].iloc[-1])
        if not math.isfinite(ltp) or ltp <= 0:
            failed += 1
            continue

        daily_d, daily_s = detect_active_zones(df, impulse_body_pct=60.0)
        wdf = resample_ohlc(df, "W-FRI")
        mdf = resample_ohlc(df, "ME")
        # monthly candles are fatter — slightly looser body% so 1M zones still form
        month_d, month_s = detect_active_zones(mdf, impulse_body_pct=50.0) if mdf is not None else ([], [])
        week_d, week_s = detect_active_zones(wdf, impulse_body_pct=55.0) if wdf is not None else ([], [])

        z1d, rel_d, _ = pick_best_side(ltp, daily_d, daily_s)
        z1m, rel_m, _ = pick_best_side(ltp, month_d, month_s)

        # stock qualifies only if TODAY price is IN/NEAR a valid 1D or 1M zone
        if (rel_d not in ("IN", "NEAR")) and (rel_m not in ("IN", "NEAR")):
            continue

        # decide displayed type from the tighter / more relevant zone
        chosen = None
        chosen_rel = "NONE"
        if rel_d in ("IN", "NEAR") and rel_m in ("IN", "NEAR"):
            if z1d["side"] == z1m["side"]:
                chosen, chosen_rel = z1d, rel_d
            else:
                # conflict — equilibrium / wait
                chosen, chosen_rel = (z1d if rel_d == "IN" else z1m), "CONFLICT"
        elif rel_d in ("IN", "NEAR"):
            chosen, chosen_rel = z1d, rel_d
        else:
            chosen, chosen_rel = z1m, rel_m

        if chosen_rel == "CONFLICT":
            zone_type = "EQUILIBRIUM"
        else:
            zone_type = chosen["side"]

        sector = map_sector(nse_sym, info.get("company"), info.get("industry"))
        w_tr = weekly_trend(wdf)
        vol_txt, _vol_r = volume_status(df)

        tests = int(chosen["tests"]) if chosen else 0
        s1d = z1d["score"] if z1d else 0.0
        s1m = z1m["score"] if z1m else 0.0
        combo = max(s1d, s1m)
        if z1d and z1m and z1d["side"] == z1m["side"] and rel_d in ("IN", "NEAR") and rel_m in ("IN", "NEAR"):
            combo = min(combo + 1.5, 11.5)
        if zone_type == "DEMAND" and "UP" in w_tr:
            combo = min(combo + 1.0, 11.5)
        if zone_type == "SUPPLY" and "DOWN" in w_tr:
            combo = min(combo + 1.0, 11.5)
        sec_bonus = "+0.0 PTS"
        if zone_type == "DEMAND" and sector in {
            "BANK", "OIL", "AUTO", "METAL", "REALTY", "INFRA", "FINSERVICE",
            "PSUBANK", "PVTBANK", "CPSE", "FMCG",
        }:
            combo = min(combo + 2.0, 11.5)
            sec_bonus = "+2.0 PTS"
        elif zone_type == "DEMAND":
            combo = min(combo + 1.0, 11.5)
            sec_bonus = "+1.0 PT"

        if zone_type == "DEMAND" and chosen_rel == "IN" and combo >= 9.0:
            sig = "BUY READY"
        elif zone_type == "DEMAND" and chosen_rel == "NEAR":
            sig = "APPROACHING DEMAND"
        elif zone_type == "SUPPLY" and chosen_rel == "IN":
            sig = "SUPPLY TEST"
        elif zone_type == "SUPPLY":
            sig = "NEAR SUPPLY"
        else:
            sig = "WAIT FOR PULLBACK"

        if combo >= 11:
            combo_txt = f"{combo:.1f} / 10 SUPER COMBO"
        elif combo >= 9.5:
            combo_txt = f"{combo:.1f} / 10 HIGH COMBO"
        elif zone_type == "SUPPLY":
            combo_txt = f"{combo:.1f} / 10 SUPPLY"
        else:
            combo_txt = f"{combo:.1f} / 10 SOLID"

        # show most recent 1D / 1M zone even if the qualifier was the other TF
        show_d = z1d or (daily_d[-1] if daily_d else None)
        show_m = z1m or (month_d[-1] if month_d else None)
        # if chosen is supply, prefer that side's leftover zone for the empty TF
        if not show_d and daily_s:
            show_d = daily_s[-1]
        if not show_m and month_s:
            show_m = month_s[-1]

        stock_rows.append(
            {
                "sym": nse_sym,
                "comp": info.get("company") or nse_sym,
                "sector": sector,
                "type": zone_type,
                "ltp": round(ltp, 2),
                "z1d": fmt_zone(show_d),
                "s1d": fmt_score(show_d),
                "z1m": fmt_zone(show_m),
                "s1m": fmt_score(show_m),
                "w_trend": w_tr,
                "tests_count": tests,
                "fresh_badge": freshness(tests),
                "vol_expl": vol_txt,
                "secBonus": sec_bonus,
                "combo": combo_txt,
                "sig": sig,
                "watch": True,
                "rel": chosen_rel,
                "industry": info.get("industry") or "",
            }
        )

    # sort: BUY READY first, then combo
    rank = {"BUY READY": 0, "APPROACHING DEMAND": 1, "WAIT FOR PULLBACK": 2, "NEAR SUPPLY": 3, "SUPPLY TEST": 4}
    stock_rows.sort(key=lambda r: (rank.get(r["sig"], 9), -_combo_num(r["combo"]), r["sym"]))

    top3 = make_top3(stock_rows)
    for p in top3:
        send_telegram_alert(
            f"GTF AUTO-FILTER: {p['sym']} @ ₹{p['ltp']} | {p['combo']} | {p['z1d']}"
        )

    sector_cards = build_sector_cards(stock_rows, index_ltps)
    stats = {
        "universe": len(universe),
        "ohlcOk": len(frames),
        "scanned": scanned,
        "failed": failed,
        "inZone": len(stock_rows),
        "demand": sum(1 for r in stock_rows if r["type"] == "DEMAND"),
        "supply": sum(1 for r in stock_rows if r["type"] == "SUPPLY"),
        "equilibrium": sum(1 for r in stock_rows if r["type"] == "EQUILIBRIUM"),
        "buyReady": sum(1 for r in stock_rows if r["sig"] == "BUY READY"),
    }

    # strip helper keys before JSON
    public_rows = []
    for r in stock_rows:
        public_rows.append({k: v for k, v in r.items() if k not in ("industry",)})

    payload = {
        "date": today_str,
        "timestamp": now.strftime("%Y-%m-%d %H:%M:%S IST"),
        "passwordRef": PASSWORD_REF,
        "version": VERSION,
        "scanStats": stats,
        "top3": top3,
        "sectorIndices": sector_cards,
        "stockData": public_rows,
    }

    with open("gtf_live_data.json", "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2, ensure_ascii=False)

    export_rows = []
    for r in stock_rows:
        export_rows.append(
            {
                "Symbol": r["sym"],
                "Company": r["comp"],
                "Sector": r["sector"],
                "Type": r["type"],
                "CMP (INR)": r["ltp"],
                "1D Execution Zone": r["z1d"],
                "1D Score": r["s1d"],
                "1M Supporting Zone": r["z1m"],
                "1M Score": r["s1m"],
                "1W Intermediate Trend": r["w_trend"],
                "Zone Freshness": r["fresh_badge"],
                "Volume Status": r["vol_expl"],
                "Sector Bonus (Ep 16)": r["secBonus"],
                "GTF Combo Score": r["combo"],
                "Setup Status": r["sig"],
                "Relation": r.get("rel", ""),
            }
        )
    df_out = pd.DataFrame(export_rows)
    df_out.to_excel("NIFTY500_GTF_Dashboard.xlsx", index=False)
    df_out.to_csv("NIFTY500_GTF_Dashboard.csv", index=False)

    log("")
    log("=" * 88)
    log(
        f"  SCAN DONE  universe={stats['universe']}  ohlc={stats['ohlcOk']}  "
        f"IN/NEAR zone={stats['inZone']}  DEMAND={stats['demand']}  "
        f"SUPPLY={stats['supply']}  BUY READY={stats['buyReady']}"
    )
    log("=" * 88)
    if not stock_rows:
        log("  No stock is currently sitting inside a valid GTF zone.")
    else:
        show = df_out.head(15)
        log(show.to_string(index=False))
    log("[✓] Wrote gtf_live_data.json, NIFTY500_GTF_Dashboard.xlsx, NIFTY500_GTF_Dashboard.csv")
    return payload


if __name__ == "__main__":
    try:
        scan()
    except Exception as exc:
        log(f"[FATAL] {exc}")
        raise
