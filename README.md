# NSE Breakout Scanner

Scheduled automation that pulls three NSE live-analysis feeds every trading
evening — 52-week highs, volume gainers, price gainers — merges them into one
candidate list, runs a deterministic breakout-confirmation pipeline on weekly
and monthly OHLC, and writes a CSV of confirmed breakout tickers plus a
Claude-written analysis note.

## How the intelligence is split

The identification and confirmation math is plain Python, not an LLM call.
IF a filter is arithmetic (close off high, volume multiple, base length,
moving averages), THEN it runs in code so the same stock passes or fails
identically every night and costs nothing. Claude (Application Programming
Interface call, model `claude-sonnet-4-6`) is used only for the final
analysis note, and it is given the pre-computed metrics as its only source
of truth — it is instructed to never invent a price or level. That makes the
narrative useful and the numbers un-hallucinatable.

## Pipeline

1. Stage 0 — universe: EQ series only, no ETFs/index funds, turnover >= Rs 5 cr
2. Stage 1 — pre-screen: reject red closes, closes >3% off the day high, and
   bases <= 10 days (rolling highs, i.e. already trending)
3. Stage 2 — fetch ~2 years of daily OHLCV per survivor (yfinance); NSE's
   official close/high is spliced in when Yahoo's same-day India bar is null
4. Stage 3 — weekly confirmation: close above the prior 52-week max weekly
   close, close in the upper third of the week's range, volume >= 1.5x the
   10-week average, price above a rising 30-week MA (all four must pass)
5. Stage 4 — monthly qualification: base >= 3 months, monthly close above the
   base's max monthly close, <= +25% over the 10-month MA, base drawdown <= 50%
6. Stage 5 — CONFIRMED (Stage 3 all + Stage 4 >= 3/4), WATCH, or REJECTED

Runs before Friday's close are marked `weekly_candle_final = False` because
the weekly candle is still forming; Friday runs are final.

## Setup (GitHub Actions) — no credentials needed

1. Create a repo and push this folder.
2. (Optional) Repo Settings -> Secrets and variables -> Actions -> add
   `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` for the evening summary ping.
   `ANTHROPIC_API_KEY` / `CLAUDE_CODE_OAUTH_TOKEN` are NOT required — see
   "No API key?" below.
3. (Optional) Add email delivery — see "Delivery" below.
4. Actions tab -> NSE Breakout Scanner -> Run workflow (manual test).
5. The schedule (`30 12 * * 1-5` = 18:00 IST weekdays) then runs itself.
   The scanner reads the feed timestamp and dates the output folder to the
   trading day, so a Saturday run of Friday's frozen data files correctly.

## Delivery

Two optional channels, both fail-soft: if a channel's secrets are missing,
or its network call errors, the run prints one line and continues. The CSVs
are committed to the repo either way, so delivery is never load-bearing.

| Channel | Secrets |
| --- | --- |
| Telegram | `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` |
| Slack | `SLACK_WEBHOOK_URL` |

**Slack** — in the Slack app directory add *Incoming Webhooks*, pick the
target channel, copy the generated `https://hooks.slack.com/services/...`
URL, and add it under Settings -> Secrets and variables -> Actions as
`SLACK_WEBHOOK_URL`.

That URL is a bearer credential: anyone holding it can post into the
channel, and there is no second factor. It must never be committed. This
repo is public, so a hardcoded webhook would be scraped within hours —
GitHub's secret scanning also reports leaked Slack webhooks to Slack, which
revokes them, so a committed URL tends to break the integration as well as
expose it. If one is ever pushed, revoking and regenerating is the only fix;
deleting the line does not help, since the value stays in git history.

The message is deliberately just the two signal tables: a header, the
confirmed breakouts, the watch list, and a link to `output/latest`. The
funnel, reject reasons and Claude note are still written to `output/` on
every run — they are simply not posted to the channel.

Slack's ceilings are enforced when building the payload — 50 blocks per
message, 3000 characters per section, 150 per header — because breaching
them either returns `400 invalid_blocks` or truncates silently with no
error. If a block payload is rejected anyway, the sender retries once as
plain text so the alert still lands.

Set `SLACK_ONLY_ON_HITS=1` to stay quiet on days with no confirmed and no
watch names. Note that a day with zero prescreen survivors already exits
before the notification block, so neither channel fires on those days.

Each run commits `output/YYYY-MM-DD/` back to the repo:

- `breakouts_confirmed.csv` — the tickers you asked for, with metrics
- `watchlist.csv`, `rejects.csv`, `funnel.txt`, `all_results.json`
- `analysis_prompt.txt` — paste into claude.ai for the analysis note
  (becomes `analysis.md` automatically if you ever add Claude credentials)
- `raw/*.json` — the untouched NSE payloads, for audit

`output/latest/` always mirrors the most recent scan, so
`.../output/latest/analysis_prompt.txt` is a stable, bookmarkable path — the
Telegram summary includes the link automatically. IF the repo is public,
THEN you can also just give Claude in claude.ai the raw link
(`https://raw.githubusercontent.com/<user>/<repo>/main/output/latest/analysis_prompt.txt`)
and ask it to fetch and analyse — no copy-paste at all.

## Watchlists and alerts

Neither broker lets you write to a watchlist. Zerodha's own staff say Kite
Connect is an order-execution platform and marketwatch items must be added by
hand; TradingView publishes no watchlist API at all, and every library that
claims one drives undocumented internal endpoints with your session cookie.
So the scan writes what each platform *will* accept:

- `tradingview.txt` — comma-separated `NSE:` symbols for TradingView's
  Import option (watchlist menu -> Import). Confirmed + watch, written only
  when non-empty, mirrored at `output/latest/tradingview.txt`.
- **Kite price alerts** — Kite Connect *does* expose an Alerts API, so if
  `KITE_API_KEY` and `KITE_ACCESS_TOKEN` are set, each run replaces last
  run's alerts with today's. Absent, the step is skipped like any other
  sender. Only `type=simple` alerts are sent; the API's ATO variant places a
  real trade and is never used here.

Alert direction is derived per stock: above its `breakout_level` gets `<=`
(the level is now invalidation), still below gets `>=` (the level is still
the trigger). `KITE_ALERTS_DRY_RUN=1` prints the plan without sending;
`KITE_ALERTS_KEEP=1` stops it pruning. Pruning only removes alerts named
`BRK-*`, so anything you made in Kite by hand is untouched.

The one thing that cannot be automated is the login. Zerodha flushes access
tokens around 07:30 IST daily and requires a manual login at least once a
day, so run `python tools/kite_login.py --repo <owner>/<name>` each morning:
it mints a token and pushes it to the repo secret the 18:00 scan reads.
Automating that login with a stored TOTP secret is against Zerodha's
guidance and risks the account.

## No API key? Two free options

The scanner never needed the key for the actual breakout logic — that is
deterministic code. The key only produced the written analysis note. IF you
do not have an API key, THEN pick one of these:

1. **Claude subscription token (best if you are on the Pro or Max plan).**
   Run `claude setup-token` once on your own machine (requires the Claude
   Code command line tool), copy the token it prints, and add it as a GitHub
   secret named `CLAUDE_CODE_OAUTH_TOKEN`. The workflow detects it, installs
   Claude Code on the runner, and writes the analysis using your existing
   subscription quota — no API account, no separate billing.
2. **Manual paste (zero setup).** With no credentials at all, each run writes
   `analysis_prompt.txt` next to the tickers CSV. Open it, paste the contents
   into claude.ai, and you get the identical note — it contains the exact
   same instructions the API call would have used.

IF both secrets are absent, THEN nothing fails: the confirmed tickers CSV,
watchlist, rejects log, and Telegram summary are all produced regardless.

## Offline / backtest mode

Feed it the CSVs downloaded manually from the NSE website:

```
python run.py --offline --csv52 52WeekHigh.csv \
  --csvvol LA-Volume-Gainers-07-Aug-2026.csv --asof 2026-08-07
```

## Known failure modes

- **NSE blocking.** NSE's Akamai sometimes 403s datacenter IPs. The fetcher
  bootstraps cookies, retries with backoff, and refreshes the session; the
  API often responds even when the homepage 403s. IF runs start failing
  consistently with all-feeds-failed, THEN switch to a machine on a
  residential IP — on Windows:
  `schtasks /Create /SC WEEKLY /D MON,TUE,WED,THU,FRI /TN "NSE Breakout Scan" /TR "cmd /c cd /d C:\path\nse-breakout-scanner && set PYTHONUTF8=1 && python run.py" /ST 18:00`
- **Yahoo EOD lag.** Yahoo's India daily bar is often null on the evening of
  the trading day; the pipeline splices NSE's official LTP/high so the tests
  still run on real closes. Volume on that bar is Yahoo's own live figure.
- **Holidays.** Feeds return the previous session; the timestamp-based
  dating means the run just re-writes that day's folder with no side effects.
- **Field names.** NSE's 52-week-high API really does spell the field
  `comapnyName`. The parser handles both spellings in case they fix it.

Outputs are technical classification, not investment advice.
