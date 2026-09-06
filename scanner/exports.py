"""Watchlist exports for third-party charting tools.

TradingView publishes no API for watchlists. Its only supported ingestion
route is the Import option in the watchlist menu, which takes a .txt file of
comma-separated symbols carrying an exchange prefix. So this module just
writes that file; nothing here touches the network or any credential.

  output/<date>/tradingview.txt   confirmed + watch, NSE: prefixed
  output/latest/tradingview.txt   stable mirror, copied by run.py

Whether Import appends to the open watchlist or replaces it has changed
between TradingView releases — check what yours does on the first load, and
either way point it at a list kept for this purpose rather than your main one.

The file is written only when there is at least one symbol. An empty .txt is
worse than an absent one: TradingView rejects it, and because run.py rebuilds
output/latest from scratch every run, absence is unambiguous.
"""
from pathlib import Path

EXCHANGE = "NSE"


def tradingview_symbols(*buckets) -> list:
    """Prefixed symbols across the given result buckets, order preserved,
    de-duplicated. Buckets are disjoint today, but a symbol appearing twice
    would make TradingView show it twice, so we guard anyway."""
    out, seen = [], set()
    for rows in buckets:
        for r in rows or []:
            sym = (r.get("symbol") or "").strip().upper()
            if sym and sym not in seen:
                seen.add(sym)
                out.append(f"{EXCHANGE}:{sym}")
    return out


def write_tradingview(path, confirmed, watch) -> int:
    """Write the import file. Returns how many symbols it holds (0 = no file
    written)."""
    symbols = tradingview_symbols(confirmed, watch)
    if not symbols:
        return 0
    Path(path).write_text(",".join(symbols) + "\n", encoding="utf-8")
    return len(symbols)

