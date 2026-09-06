"""Kite Connect price alerts — optional, opt-in, fail-soft.

Zerodha exposes no API for the Kite marketwatch: their own staff say Kite
Connect is an order-execution platform and watchlist items must be added by
hand. Alerts, however, do have one, and alerts created here are delivered
exactly like ones set up in Kite web or the app.

  POST   /alerts             create
  GET    /alerts             list
  DELETE /alerts?uuid=<u>    delete
  https://kite.trade/docs/connect/v3/alerts/   (cap: 500 active per user)

Environment (all absent -> this module no-ops, like every other sender here):
  KITE_API_KEY           Kite Connect app key
  KITE_API_SECRET        not used here; see tools/kite_login.py
  KITE_ACCESS_TOKEN      flushed ~07:30 IST daily, so regenerate each morning
  KITE_ALERTS_DRY_RUN=1  log the plan, call nothing
  KITE_ALERTS_KEEP=1     keep previous runs' alerts instead of pruning

Only type=simple alerts are created. The API also supports ATO ("alert
triggers order"), which places a real trade when it fires. This module never
sends that, deliberately — an alert should tell you something, not buy
something.

Direction is derived per stock rather than assumed. A name already trading
above its breakout_level gets a <= alert, because for it the level has become
the invalidation mark the analysis note already keys off. A name still below
gets a >= alert, because for it the level is still the trigger.

Pruning only ever touches alerts whose name starts with PREFIX, so anything
you created by hand in Kite is left alone.
"""
import os
import time

import requests

BASE = "https://api.kite.trade"
PREFIX = "BRK-"
TIMEOUT = 20
MAX_ALERTS = 500     # Kite's per-user cap on active alerts
PAUSE = 0.35         # be polite; Kite rate-limits per-endpoint


def _creds():
    key = os.environ.get("KITE_API_KEY")
    token = os.environ.get("KITE_ACCESS_TOKEN")
    return (key, token) if key and token else (None, None)


def _headers(key, token):
    return {"X-Kite-Version": "3",
            "Authorization": f"token {key}:{token}"}


def _explain(r) -> str:
    """Kite returns a typed error body; surface the useful part."""
    try:
        body = r.json()
        etype = body.get("error_type", "")
        msg = body.get("message", "")
    except ValueError:
        return f"{r.status_code}: {r.text[:200]}"
    if etype == "TokenException":
        msg += "  (access token expired — regenerate, they are flushed daily)"
    elif etype == "PermissionException":
        msg += ("  (alerts need an active Kite Connect subscription; a stale "
                "token can also cause this — try regenerating it)")
    return f"{r.status_code} {etype}: {msg}"


def _plan(confirmed, watch) -> list:
    """One alert spec per stock. Skips rows missing a usable level."""
    out = []
    for rows, bucket in ((confirmed, "CONFIRMED"), (watch, "WATCH")):
        for r in rows or []:
            sym = (r.get("symbol") or "").strip().upper()
            level = r.get("breakout_level")
            close = r.get("close")
            if not sym or level in (None, ""):
                continue
            level = round(float(level), 2)
            above = close is not None and float(close) > level
            out.append({
                "name": f"{PREFIX}{sym}",
                "lhs_exchange": "NSE",
                "lhs_tradingsymbol": sym,
                "lhs_attribute": "LastTradedPrice",
                "operator": "<=" if above else ">=",
                "rhs_type": "constant",
                "rhs_constant": level,
                "type": "simple",
                "_bucket": bucket,
            })
    return out


def _list_ours(sess, key, token) -> list:
    r = sess.get(f"{BASE}/alerts", headers=_headers(key, token),
                 timeout=TIMEOUT)
    if r.status_code != 200:
        print(f"  kite: list failed {_explain(r)}")
        return []
    data = r.json().get("data") or []
    return [a for a in data if str(a.get("name", "")).startswith(PREFIX)]


def sync_alerts(confirmed, watch) -> int:
    """Prune this scanner's previous alerts, then create today's.

    Returns the number created. Never raises: a broken alert sync must not
    fail a scan whose real output is already committed.
    """
    key, token = _creds()
    if not key or not token:
        return 0

    plan = _plan(confirmed, watch)
    if not plan:
        print("  kite: nothing to alert on")
        return 0

    dry = os.environ.get("KITE_ALERTS_DRY_RUN") == "1"
    for p in plan:
        print(f"  kite: {p['_bucket']:9} {p['lhs_tradingsymbol']:12} "
              f"{p['operator']} {p['rhs_constant']}")
    if dry:
        print(f"  kite: DRY RUN — {len(plan)} alerts not sent")
        return 0

    sess = requests.Session()
    try:
        existing = _list_ours(sess, key, token)

        if os.environ.get("KITE_ALERTS_KEEP") != "1":
            gone = 0
            for a in existing:
                uuid = a.get("uuid")
                if not uuid:
                    continue
                r = sess.delete(f"{BASE}/alerts", params={"uuid": uuid},
                                headers=_headers(key, token), timeout=TIMEOUT)
                gone += r.status_code == 200
                time.sleep(PAUSE)
            if gone:
                print(f"  kite: pruned {gone} alert(s) from previous runs")
            existing = []

        headroom = MAX_ALERTS - len(existing)
        if len(plan) > headroom:
            print(f"  kite: {len(plan)} alerts but only {headroom} slots "
                  f"under the {MAX_ALERTS} cap — sending the first {headroom}")
            plan = plan[:max(headroom, 0)]

        made = 0
        for p in plan:
            body = {k: v for k, v in p.items() if not k.startswith("_")}
            r = sess.post(f"{BASE}/alerts", data=body,
                          headers=_headers(key, token), timeout=TIMEOUT)
            if r.status_code == 200:
                made += 1
            else:
                print(f"  kite: {p['lhs_tradingsymbol']} failed "
                      f"{_explain(r)}")
                if r.status_code in (401, 403):
                    break        # token or subscription — the rest will fail
            time.sleep(PAUSE)
        print(f"  kite: {made}/{len(plan)} alert(s) created")
        return made
    except requests.RequestException as e:
        print(f"  kite error: {type(e).__name__}: {e}")
        return 0

