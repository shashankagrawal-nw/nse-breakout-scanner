"""Deterministic breakout pipeline.

Stage 0  universe filter (EQ only, no ETFs/index funds, min turnover)
Stage 1  pre-screen on feed fields (red close, close off high, base too short)
Stage 2  fetch daily OHLCV (yfinance), splice NSE's official close/high for
         today when Yahoo's same-day bar is still null (their India EOD lag)
Stage 3  weekly confirmation  — all four must hold:
           c1 weekly close > max weekly close of prior 52 weeks
           c2 weekly close in the upper third of the weekly range
           c3 weekly volume >= 1.5x the 10-week average
           c4 close above a rising 30-week MA
Stage 4  monthly qualification — scored:
           base >= 3 months | monthly close > base's max monthly close |
           <= +25% above 10-month MA | base drawdown <= 50%
Stage 5  CONFIRMED (S3 all + S4 >= 3/4) | WATCH (S3 only) | REJECTED
"""
import datetime as dt
import re
import time
import warnings

import numpy as np
import pandas as pd

warnings.filterwarnings("ignore")

MIN_TURNOVER_LAKH = 500        # Rs 5 crore
BASE_MIN_DAYS = 10
OFF_HIGH_MAX_PCT = 3.0
VOL_X_MIN = 1.5
EXT_MAX_PCT = 25.0
BASE_DD_MAX_PCT = 50.0

FUND_NAME_RE = re.compile(
    r"\b(ETF|BeES|Mutual Fund|Index Fund|Nifty|Sensex|FOF)\b", re.I)
FUND_SYM_RE = re.compile(r"(ETF|BEES|IETF|NN50|N50|SETF|LIQUID|INDEX)", re.I)


def _f(x):
    try:
        v = float(str(x).replace(",", "").strip())
        return v if np.isfinite(v) else None
    except (TypeError, ValueError):
        return None


def _date(x):
    if not x:
        return None
    for fmt in ("%d-%b-%Y", "%d-%B-%Y", "%d-%m-%Y"):
        try:
            return dt.datetime.strptime(str(x).strip(), fmt).date()
        except ValueError:
            continue
    return None


# ---------------------------------------------------------------- candidates
def build_candidates(feeds: dict) -> dict:
    """Merge the three feeds into {symbol: candidate}. Feed values may be None."""
    cands = {}

    def get(sym):
        return cands.setdefault(sym, {
            "symbol": sym, "sources": set(), "series": None, "name": None,
            "ltp": None, "pchng": None, "day_high": None, "day_low": None,
            "day_open": None, "prev_high_date": None, "turnover_lakh": None,
            "vol_x_1w": None,
        })

    js = feeds.get("high52w")
    if js and js.get("data"):
        for r in js["data"]:
            c = get(str(r.get("symbol", "")).strip())
            c["sources"].add("52w_high")
            c["series"] = r.get("series") or c["series"]
            c["name"] = r.get("comapnyName") or r.get("companyName") or c["name"]
            c["ltp"] = _f(r.get("ltp")) or c["ltp"]
            c["pchng"] = _f(r.get("pChange")) if _f(r.get("pChange")) is not None else c["pchng"]
            c["day_high"] = _f(r.get("new52WHL")) or c["day_high"]
            c["prev_high_date"] = _date(r.get("prevHLDate")) or c["prev_high_date"]

    js = feeds.get("volume_gainers")
    if js and js.get("data"):
        for r in js["data"]:
            c = get(str(r.get("symbol", "")).strip())
            c["sources"].add("volume_gainer")
            c["name"] = c["name"] or r.get("companyName")
            c["ltp"] = c["ltp"] or _f(r.get("ltp"))
            if c["pchng"] is None:
                c["pchng"] = _f(r.get("pChange"))
            c["turnover_lakh"] = _f(r.get("turnover")) or c["turnover_lakh"]
            c["vol_x_1w"] = _f(r.get("week1volChange"))

    js = feeds.get("price_gainers")
    if js:
        rows = (js.get("allSec") or {}).get("data") or js.get("data") or []
        for r in rows:
            c = get(str(r.get("symbol", "")).strip())
            c["sources"].add("price_gainer")
            c["series"] = c["series"] or r.get("series")
            c["ltp"] = c["ltp"] or _f(r.get("ltp"))
            if c["pchng"] is None:
                c["pchng"] = _f(r.get("perChange"))
            c["day_high"] = c["day_high"] or _f(r.get("high_price"))
            c["day_low"] = _f(r.get("low_price")) or c["day_low"]
            c["day_open"] = _f(r.get("open_price")) or c["day_open"]
            c["turnover_lakh"] = c["turnover_lakh"] or _f(r.get("turnover"))

    cands.pop("", None)
    return cands


# ----------------------------------------------------------------- prescreen
def prescreen(cands: dict, asof: dt.date):
    survivors, rejects = [], []

    def reject(c, code, detail):
        rejects.append({"symbol": c["symbol"], "stage": code, "detail": detail})

    for c in cands.values():
        sym, name = c["symbol"], c["name"] or ""
        if c["series"] is not None and c["series"] != "EQ":
            reject(c, "S0_series", c["series"]); continue
        if FUND_SYM_RE.search(sym) or FUND_NAME_RE.search(name):
            reject(c, "S0_fund", "ETF/index fund"); continue
        if c["turnover_lakh"] is not None and c["turnover_lakh"] < MIN_TURNOVER_LAKH:
            reject(c, "S0_turnover", f"Rs {c['turnover_lakh']/100:.1f} cr"); continue
        if c["pchng"] is not None and c["pchng"] <= 0:
            reject(c, "S1_red_close", f"{c['pchng']:+.2f}% on the day"); continue
        if c["ltp"] and c["day_high"]:
            off = (c["day_high"] - c["ltp"]) / c["day_high"] * 100
            c["off_high_pct"] = round(off, 2)
            if off > OFF_HIGH_MAX_PCT:
                reject(c, "S1_off_high", f"closed {off:.1f}% off day high"); continue
        if c["prev_high_date"]:
            bd = (asof - c["prev_high_date"]).days
            c["base_days"] = bd
            if bd <= BASE_MIN_DAYS:
                reject(c, "S1_base_short", f"base {bd}d — rolling high"); continue
        survivors.append(c)
    return survivors, rejects


# -------------------------------------------------------------- confirmation
def _fetch_daily(symbol: str, asof: dt.date):
    import yfinance as yf
    start = (asof - dt.timedelta(days=760)).isoformat()
    end = (asof + dt.timedelta(days=2)).isoformat()
    d = yf.Ticker(symbol + ".NS").history(start=start, end=end,
                                          auto_adjust=False, raise_errors=False)
    if d is None or d.empty:
        return None
    d.index = d.index.tz_localize(None).normalize()
    return d[["Open", "High", "Low", "Close", "Volume"]]


def confirm(survivors, asof: dt.date, sleep_s: float = 0.4):
    results, rejects = [], []
    asof_ts = pd.Timestamp(asof)
    weekly_complete = asof.weekday() >= 4  # Fri or later => weekly candle final

    for c in survivors:
        sym = c["symbol"]
        time.sleep(sleep_s)
        try:
            d = _fetch_daily(sym, asof)
        except Exception as e:
            d = None
            err = type(e).__name__
        if d is None or len(d) < 260:
            rejects.append({"symbol": sym, "stage": "S2_data",
                            "detail": f"DATA UNAVAILABLE ({0 if d is None else len(d)} bars)"})
            continue

        # Splice NSE's official numbers when Yahoo's same-day bar lags.
        spliced = False
        if d.index[-1] == asof_ts and pd.isna(d["Close"].iloc[-1]) and c["ltp"]:
            d.loc[asof_ts, "Close"] = c["ltp"]
            d.loc[asof_ts, "High"] = c["day_high"] or c["ltp"]
            d.loc[asof_ts, "Low"] = c["day_low"] or min(c["ltp"], d["Close"].iloc[-2])
            d.loc[asof_ts, "Open"] = c["day_open"] or c["ltp"]
            spliced = True
        elif d.index[-1] < asof_ts and c["ltp"]:
            d.loc[asof_ts] = [c["day_open"] or c["ltp"], c["day_high"] or c["ltp"],
                              c["day_low"] or c["ltp"], c["ltp"],
                              d["Volume"].tail(5).mean()]
            spliced = True
        d = d.dropna(subset=["Close"])
        if d.index[-1] != asof_ts:
            rejects.append({"symbol": sym, "stage": "S2_data",
                            "detail": f"last bar {d.index[-1].date()}, not as-of date"})
            continue

        today = d.loc[asof_ts]
        prior = d.iloc[:-1].tail(252)

        # Deferred pre-screen checks for feeds that lacked the fields.
        if c.get("off_high_pct") is None and today["High"] > 0:
            off = (today["High"] - today["Close"]) / today["High"] * 100
            c["off_high_pct"] = round(off, 2)
            if off > OFF_HIGH_MAX_PCT:
                rejects.append({"symbol": sym, "stage": "S1_off_high",
                                "detail": f"closed {off:.1f}% off day high"}); continue
        if c["turnover_lakh"] is None:
            t_lakh = today["Close"] * today["Volume"] / 1e5
            c["turnover_lakh"] = round(t_lakh, 1)
            if t_lakh < MIN_TURNOVER_LAKH:
                rejects.append({"symbol": sym, "stage": "S0_turnover",
                                "detail": f"Rs {t_lakh/100:.1f} cr"}); continue
        if "52w_high" not in c["sources"]:
            prior_max_hi = prior["High"].max()
            if today["High"] < prior_max_hi:
                rejects.append({"symbol": sym, "stage": "S2_no_new_high",
                                "detail": f"day high {today['High']:.2f} < 52w high {prior_max_hi:.2f}"})
                continue
        if c.get("base_days") is None:
            base_date = prior["High"].idxmax().date()
            c["prev_high_date"] = base_date
            c["base_days"] = (asof - base_date).days
        if c["base_days"] <= BASE_MIN_DAYS:
            rejects.append({"symbol": sym, "stage": "S1_base_short",
                            "detail": f"base {c['base_days']}d — rolling high"}); continue

        # Weekly / monthly frames.
        W = d.resample("W-FRI").agg({"Open": "first", "High": "max", "Low": "min",
                                     "Close": "last", "Volume": "sum"}).dropna()
        M = d.resample("ME").agg({"Open": "first", "High": "max", "Low": "min",
                                  "Close": "last", "Volume": "sum"}).dropna()
        cw, priorW = W.iloc[-1], W.iloc[:-1]

        level = priorW["Close"].tail(52).max()
        c1 = bool(cw["Close"] > level)
        rng = cw["High"] - cw["Low"]
        close_pos = (cw["Close"] - cw["Low"]) / rng if rng > 0 else 1.0
        c2 = bool(close_pos >= 0.667)
        v10 = priorW["Volume"].tail(10).mean()
        vol_x = cw["Volume"] / v10 if v10 > 0 else np.nan
        c3 = bool(vol_x >= VOL_X_MIN)
        ma30 = W["Close"].rolling(30).mean()
        c4 = bool(len(ma30.dropna()) >= 5 and cw["Close"] > ma30.iloc[-1]
                  and ma30.iloc[-1] > ma30.iloc[-5])
        s3 = bool(c1 and c2 and c3 and c4)

        bstart = pd.Timestamp(c["prev_high_date"])
        base_d = d.loc[bstart:]
        base_months = round(c["base_days"] / 30.44, 1)
        mm = M[(M.index > bstart) & (M.index < M.index[-1])]
        m2 = bool(cw["Close"] > mm["Close"].max()) if len(mm) else True
        ma10m = M["Close"].rolling(10).mean().iloc[-1]
        ext = (cw["Close"] / ma10m - 1) * 100 if pd.notna(ma10m) else np.nan
        m3 = bool(ext <= EXT_MAX_PCT)
        base_dd = (1 - base_d["Low"].min() / base_d["High"].iloc[0]) * 100 \
            if len(base_d) else np.nan
        m4 = bool(base_dd <= BASE_DD_MAX_PCT)
        s4 = sum([base_months >= 3, m2, m3, m4])

        bucket = "CONFIRMED" if (s3 and s4 >= 3) else ("WATCH" if s3 else "REJECTED")
        fail = None
        if not s3:
            fail = ("c1 close below prior 52w weekly close high" if not c1 else
                    "c2 weak close in weekly range" if not c2 else
                    f"c3 volume {vol_x:.1f}x < 1.5x" if not c3 else
                    "c4 30-week MA not rising / close below it")

        results.append({
            "symbol": sym, "name": c["name"], "bucket": bucket,
            "close": round(float(cw["Close"]), 2),
            "breakout_level": round(float(level), 2),
            "base_months": base_months,
            "base_start": str(c["prev_high_date"]),
            "vol_x_10wk": round(float(vol_x), 1) if np.isfinite(vol_x) else None,
            "weekly_close_pos": round(float(close_pos), 2),
            "ext_vs_10m_ma_pct": round(float(ext), 1) if np.isfinite(ext) else None,
            "base_drawdown_pct": round(float(base_dd), 1) if np.isfinite(base_dd) else None,
            "off_high_pct": c.get("off_high_pct"),
            "turnover_cr": round(c["turnover_lakh"] / 100, 1) if c["turnover_lakh"] else None,
            "sources": "+".join(sorted(c["sources"])),
            "weekly_candle_final": weekly_complete,
            "spliced_today": spliced,
            "s3_pass": s3, "s4_score": s4, "s3_fail": fail,
        })
    return results, rejects
