"""NSE breakout scanner — orchestrator.

Live (scheduled):   python run.py
Offline (backtest): python run.py --offline --csv52 path.csv --csvvol path.csv \
                        --asof 2026-08-07
"""
import argparse
import csv
import datetime as dt
import io
import json
import os
import shutil
import sys
from collections import Counter
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT))

from scanner import pipeline  # noqa: E402
from scanner.alerts_kite import sync_alerts  # noqa: E402
from scanner.analyse_claude import (SYSTEM, build_user_content,  # noqa: E402
                                    write_analysis)
from scanner.exports import write_tradingview  # noqa: E402
from scanner.notify import send_slack, send_telegram  # noqa: E402
from scanner.report import build_slack_blocks  # noqa: E402


def load_website_csvs(csv52, csvvol):
    """Map NSE website CSV downloads into the API feed shape (offline mode)."""
    feeds = {"high52w": None, "volume_gainers": None, "price_gainers": None}

    def read(path):
        text = Path(path).read_text(encoding="utf-8-sig")
        rows = list(csv.DictReader(io.StringIO(text)))
        return [{k.strip(): (v.strip() if isinstance(v, str) else v)
                 for k, v in r.items()} for r in rows]

    if csv52:
        feeds["high52w"] = {"data": [{
            "symbol": r.get("Symbol"), "series": r.get("Series"),
            "comapnyName": r.get("Security Name") or r.get("Symbol"),
            "ltp": r.get("LTP"), "pChange": r.get("%chng"),
            "new52WHL": r.get("New 52W/H price"),
            "prevHLDate": r.get("Prev. High Date"),
        } for r in read(csv52)]}
    if csvvol:
        feeds["volume_gainers"] = {"data": [{
            "symbol": r.get("SYMBOL"), "companyName": r.get("SECURITY"),
            "ltp": r.get("TODAY - LTP"), "pChange": r.get("TODAY - % CHNG"),
            "week1volChange": r.get("1 WEEK - CHANGE"),
            # website CSV turnover is in rupees; API uses lakhs
            "turnover": (float(r["TODAY - TURNOVER"]) / 1e5
                         if r.get("TODAY - TURNOVER") else None),
        } for r in read(csvvol)]}
    return feeds


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--offline", action="store_true")
    ap.add_argument("--csv52")
    ap.add_argument("--csvvol")
    ap.add_argument("--asof", help="YYYY-MM-DD (offline mode)")
    args = ap.parse_args()

    asof = (dt.date.fromisoformat(args.asof) if args.asof
            else dt.date.today())
    day_dir = ROOT / "output" / asof.isoformat()
    day_dir.mkdir(parents=True, exist_ok=True)

    print(f"== NSE breakout scan | as-of {asof} ==")
    if args.offline:
        feeds = load_website_csvs(args.csv52, args.csvvol)
    else:
        from scanner.fetch_nse import fetch_all
        feeds = fetch_all(day_dir / "raw")
        if all(v is None for v in feeds.values()):
            print("FATAL: all NSE feeds failed — likely IP blocked or site down")
            sys.exit(1)
        ts = (feeds.get("high52w") or {}).get("timestamp", "")
        if ts:
            feed_date = ts.split(" ")[0]
            try:
                asof = dt.datetime.strptime(feed_date, "%d-%b-%Y").date()
                print(f"  feed timestamp {ts} -> as-of {asof}")
                day_dir = ROOT / "output" / asof.isoformat()
                day_dir.mkdir(parents=True, exist_ok=True)
            except ValueError:
                pass

    # Stages 0-1
    cands = pipeline.build_candidates(feeds)
    n0 = len(cands)
    survivors, rej1 = pipeline.prescreen(cands, asof)
    print(f"  candidates {n0} -> prescreen survivors {len(survivors)}")
    if not survivors:
        (day_dir / "status.txt").write_text(
            f"No prescreen survivors on {asof} ({n0} candidates). "
            "Likely a holiday snapshot or a weak tape.\n", encoding="utf-8")
        print("  nothing to confirm — exiting cleanly")
        return

    # Stages 2-5
    results, rej2 = pipeline.confirm(survivors, asof)
    rejects = rej1 + rej2
    confirmed = [r for r in results if r["bucket"] == "CONFIRMED"]
    watch = [r for r in results if r["bucket"] == "WATCH"]
    confirmed.sort(key=lambda r: (-r["base_months"], -(r["vol_x_10wk"] or 0)))

    # Funnel
    stage_counts = Counter(r["stage"] for r in rejects)
    funnel = [f"candidates {n0}",
              f"prescreen survivors {len(survivors)}",
              f"confirmed {len(confirmed)} | watch {len(watch)} | "
              f"rejected {len(rejects) + len(results) - len(confirmed) - len(watch)}",
              "reject reasons: " + ", ".join(
                  f"{k}={v}" for k, v in stage_counts.most_common())]
    funnel_txt = "\n".join(funnel)
    print(funnel_txt)

    # Outputs
    def write_csv(path, rows):
        if not rows:
            Path(path).write_text("", encoding="utf-8")
            return
        with open(path, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
            w.writeheader()
            w.writerows(rows)

    write_csv(day_dir / "breakouts_confirmed.csv", confirmed)
    write_csv(day_dir / "watchlist.csv", watch)
    write_csv(day_dir / "rejects.csv", rejects)
    (day_dir / "all_results.json").write_text(
        json.dumps(results, indent=1), encoding="utf-8")
    (day_dir / "funnel.txt").write_text(funnel_txt + "\n", encoding="utf-8")

    n_tv = write_tradingview(day_dir / "tradingview.txt", confirmed, watch)
    if n_tv:
        print(f"  tradingview.txt: {n_tv} symbol(s)")

    analysis = write_analysis(results, funnel_txt)
    if analysis:
        (day_dir / "analysis.md").write_text(
            f"# Breakout scan {asof}\n\n{analysis}\n", encoding="utf-8")
    elif confirmed or watch:
        (day_dir / "analysis_prompt.txt").write_text(
            "PASTE EVERYTHING BELOW THIS LINE INTO claude.ai TO GET THE "
            "ANALYSIS NOTE\n" + "=" * 70 + "\n\n" + SYSTEM + "\n\n---\n\n" +
            build_user_content(results, funnel_txt) + "\n", encoding="utf-8")

    # Stable-path mirror so 'latest' is always one bookmark away.
    latest = ROOT / "output" / "latest"
    if latest.exists():
        shutil.rmtree(latest)
    latest.mkdir(parents=True)
    for fn in ("breakouts_confirmed.csv", "watchlist.csv", "funnel.txt",
               "analysis.md", "analysis_prompt.txt", "tradingview.txt"):
        if (day_dir / fn).exists():
            shutil.copy(day_dir / fn, latest / fn)

    repo = os.environ.get("GITHUB_REPOSITORY")
    branch = os.environ.get("GITHUB_REF_NAME", "main")
    server = os.environ.get("GITHUB_SERVER_URL", "https://github.com")
    link = (f"\n{server}/{repo}/tree/{branch}/output/latest" if repo else "")

    tickers = ", ".join(r["symbol"] for r in confirmed) or "none"
    summary = (f"NSE breakout scan {asof}\n"
               f"CONFIRMED ({len(confirmed)}): {tickers}\n"
               f"WATCH ({len(watch)}): "
               f"{', '.join(r['symbol'] for r in watch) or 'none'}\n"
               f"{funnel[3]}{link}")
    if send_telegram(summary):
        print("  telegram sent")

    # Slack: same numbers, laid out as Block Kit.
    if os.environ.get("SLACK_ONLY_ON_HITS") == "1" and not (confirmed
                                                            or watch):
        print("  no hits and SLACK_ONLY_ON_HITS=1 — slack skipped")
    else:
        fallback, blocks = build_slack_blocks(
            asof, confirmed, watch, link.strip())
        if send_slack(fallback, blocks):
            print("  slack sent")

    # Last, and deliberately so: the outputs are on disk and the pings are
    # already out, so nothing above depends on the broker being reachable.
    sync_alerts(confirmed, watch)

    print(f"== done -> {day_dir} ==")


if __name__ == "__main__":
    main()
