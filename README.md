# Token launch study

A data science study of brand-new pump.fun coins: using only a coin's first 30 seconds to
5 minutes of on-chain activity, how well can we predict whether it dies, rugs, graduates
or doubles, and would any of that survive fees? It started as a local tool to score coins
for trading on Padre; that live tool still works as a demo. Design:
[docs/SPEC.md](docs/SPEC.md). Progress: [docs/PROGRESS.md](docs/PROGRESS.md).

**Not financial advice.** This is a high-risk market. The agent only analyzes and alerts.

Built with Claude Code

## Rules this project never breaks

- It never places trades and never automates or scrapes Padre.
- It never asks for or stores private keys or seed phrases. Public wallet addresses only.
- API keys live in `.env` only. They are never printed, logged, or committed.
- `config/trusted_wallets.yaml` and recorded data are git-ignored.

## Setup (Windows, PowerShell, from the project folder)

```powershell
python -m venv .venv
.venv\Scripts\pip install -e ".[dev]"
copy .env.example .env      # then edit .env (all values are optional for now)
.venv\Scripts\pytest
```

## Running the engine

```powershell
.venv\Scripts\agentd start --foreground   # record and print live events; Ctrl+C to stop
.venv\Scripts\agentd start --foreground --show alerts   # ...or print only tokens that pass
.venv\Scripts\agentd start                # record in the background
.venv\Scripts\agentd status               # running? feeds, uptime, verdicts, gaps, disk
.venv\Scripts\agentd alerts --follow      # tokens that pass the rules, mint first; Ctrl+C
.venv\Scripts\agentd pause-recording      # keep running but stop writing (logged as a gap)
.venv\Scripts\agentd resume-recording
.venv\Scripts\agentd stop                 # writes what is buffered, then exits
```

`agentd start` asks Windows itself to run the engine, so it belongs to no window or app:
you can close the PowerShell window, and Claude, and it keeps recording. It prints
"You can close this window" when that worked. If it prints "Keep this window open"
instead, Windows refused, and closing the window may stop the engine.

The PC must stay awake while the engine runs: Settings > System > Power > Sleep: Never.
The engine keeps two connections to Solana's free public feed open at once
(`live.sources` in `config/settings.yaml`), so a drop on one is covered by the other.
Together they use about 130 GB of network data per day.

A few lines of `agentd status` tell you how the recording is going:

```
feed 1:    solana-public, connected, last message 0.1s ago, 4.2s behind, first for 58%, 12 drops
feed 2:    solana-public-legacy, connected, last message 0.0s ago, 3.9s behind, first for 42%, 9 drops
saved:     21 h 40 m recorded out of 22 h 05 m since 6:10 AM today; 14 gaps, 25 m off
uptime:    98.1% of the last 24 h recorded; meets the 95% target
verdicts:  9,120 made (avoid 4,010, caution 5,090, pass 20); 31 with gaps; 410 tokens tracked; ...
```

- `feed N`: one line per connection. "behind" is how late its events arrive; "first" is
  how often it delivered a transaction before the other one did.
- `uptime`: the share of the last 24 hours with no gap (spec 15.1 asks for 95%). A gap
  is time no connection covered; one connection dropping while the other runs is not one.
- `verdicts`: see "Live verdicts" below.

The engine records continuously (`recorder.goal_hours: 0`). With `goal_hours` above 0 it
also shows a `burst:` bar toward a recording of that many hours.

Pause or stop?

- **Pause** keeps the engine connected but writes nothing. Use it for a short break.
  It stays paused, even after a restart, until you run `resume-recording`.
- **Stop** shuts the engine down. Use it when you are done for now. `agentd start`
  continues later; the time in between is written to the gap log automatically.

If something goes wrong:

```powershell
Get-Content D:\memecoin-agent-data\run\agentd.log -Tail 30   # what the engine last did
.venv\Scripts\agentd status                                  # shows the engine's pid
Stop-Process -Id <pid>                                       # only if `agentd stop` fails
```

The engine does not start by itself after the PC restarts. Run `agentd start` again.

## Checking the recording

```powershell
.venv\Scripts\agent audit --hours 24 --trades-sample 20
```

Lists every pump.fun token created on chain in that period, compares it with what was
recorded, and reports anything missed (separately for periods the gap log already covers).
`--trades-sample` also checks every trade of that many random tokens.

## Price and fee math

```powershell
.venv\Scripts\agent snapshot-config        # save pump.fun's curve and fee settings from chain
.venv\Scripts\agent verify-curve --hours 24  # re-run recorded trades with our math
```

`src/agent/sim/curve_math.py` prices buys and sells on a bonding curve exactly as
pump.fun does (fees included), and `verify-curve` checks that against every recorded
trade. The engine also saves a settings snapshot at start-up and every 6 hours
(`data/config_snapshots/`); fee rates and curve parameters come from those snapshots or
from the trades themselves, never from the code.

## Live verdicts

While it records, the engine judges every new token 30, 60, 120 and 300 seconds after its
launch with the rules in `config/rules.yaml` (avoid / caution / pass, with reasons). A
verdict waits until the data up to that moment has arrived, so when the feed runs late
the verdict comes late too (`agentd status` shows the median delay). Tokens that pass go
to `run/alerts.log`; `agentd alerts --follow` shows them as they come. Edits to
`config/rules.yaml` are picked up within 10 seconds.

Every verdict, with all the signals it saw, is saved in `live/verdicts/`. To check that
the live signals match a replay of the recorded data (they must be identical):

```powershell
.venv\Scripts\agent check-live --hours 2
```

The rules are starting guesses, not advice: tokens that pass are not yet known to do
better than any others.

### Model scores beside the verdicts (analysis only)

With the saved models from `agent train` on disk, the engine also scores each coin at
the same four moments (spec 15.3, step L1): the chance that it is bad (rug, dead or
bundled), that it graduates later in the hour, and that it doubles before it halves.
**This is analysis, not advice: in the backtest no signal made money after costs**
(`docs/R6_RESULTS.md`). Prepare the models once (needs the research packages):

```powershell
.venv\Scripts\agent export-live-models
```

It writes the models' calibration as plain numbers to `models/r5_live/bundle.json` and
checks, on the validation day, that the engine's scoring gives exactly the numbers the
research code gives. At start-up the engine checks the model files against the hashes
recorded when they were trained. `agentd status` has a `models:` line; without the
research packages or the files the engine runs as before and that line says why scores
are off.

What to know when reading a score:
- The models were trained on Nov 12-16, 2026. The market drifts, so they age.
- A score is made only for the kind of coin the models were trained on: paired with SOL,
  no recording gap in its life so far. Otherwise the verdict says why there is none.
- The models read how many coins launched in the hour before, and how many the same
  creator launched in the 24 hours before. The engine only knows the launches it
  recorded (plus the backfill). With less than 95% of the last hour recorded there is no
  score (`live_models.min_hour_covered` in `config/settings.yaml`); with less than the
  full 24 hours the coin is scored and marked, because the creator's count is then a
  lower bound. After a fresh start, scores begin about an hour in.
- The free live feed often runs 5 to 77 seconds late, and a score waits for its data, so
  a "T+30 s" score can arrive a minute after launch. The screen says so when a score
  came 10 seconds late or more, and the every-number view (F3) always shows the delay.

`agent check-live` also makes the saved scores again from the recorded data with the same
code; they must be identical to the last digit.

## The screen

```powershell
.venv\Scripts\agent screen
```

The window to keep beside Padre while you trade. It needs the engine running.

It is minimal and in plain English:

```
NEW COINS that look OK · click one for details
  Lazy Otter ($OTTER) · 2 min old · looked OK at 1 min · 25 buyers · worth 32 SOL

DETAILS
  Lazy Otter ($OTTER) · 2 min old · data from 2 sec ago
  LOOKS OK: no warning sign so far
    25 people bought it. The 10 biggest holders own 22%.
    The creator holds 3% and has not sold.
    Worth about 32 SOL, 12% of the way to graduating.
  The model's guess at 30 sec (analysis, not advice):
    goes bad 81% (usually 87%) · graduates later 2.1% (usually 1.1%) · doubles before halving 6.1% (usually 2.4%)
    Trained on Nov 12-16, 2026. In the backtest, no signal made money after costs.
  Address (copied, paste it in Padre): <the coin's address>

Engine on · data 1 sec behind · model scores on · clipboard watching on (F2)
```

- **New coins** (top): every coin that passes the rules, one line each, by its name. The
  age keeps counting, and a later check of the same coin updates its line. Click a coin
  (or pick it with the arrow keys and press Enter) to see its details; **its address is
  copied**, ready to paste into Padre's search.
- **Details** (middle): also shown when you copy a coin's address in Padre (the
  clipboard watcher), or paste one into the box and press Enter. A coin's bonding-curve
  address works too. You get the rules' verdict in words with its reasons, three
  sentences of facts, and the models' newest guess, each number beside how often that
  outcome usually happened (on the test days, for the same moment and kind of coin), or
  the reason there is no guess. **F3** switches to every number (buyers, holders, top
  10, dev, snipers, buy/sell flow, the scores at each of the four moments) and back.
- **Status** (bottom): what is on, how far behind the data is, then only what is wrong
  (a feed down, not recording, checks running late, the last interruption), and how
  fast the screen answers (the median).

Commands in the box: an address, `more` (or F3), `mute <address>` / `unmute <address>`,
`clipboard` (or F2) to turn the watcher on and off, `start` to start the engine, `help`,
`quit`. Closing the screen does not stop the engine.

The clipboard watcher only reacts to text that is exactly one Solana address. Anything
else on the clipboard is ignored and never stored, logged or sent anywhere, and long
text is not even read. The only thing the screen ever writes to the clipboard is the
address of a coin you clicked. Only coins launched while the engine runs, and in the
last 15 minutes, can be looked up.

The screen talks to the engine over a local connection (`local_api.port` in
`config/settings.yaml`, 8765). It listens on this PC only and refuses web pages.

## Signals and rules (first version)

```powershell
.venv\Scripts\agent signals --hours 8 --at 60   # judge recorded tokens 60 s after launch
```

Replays recorded tokens through the same signals the engine will use live (holders, dev
buys and sells, snipers, buy/sell flow, curve progress, mayhem share) and through the
hand-written rules in `config/rules.yaml`, then shows each verdict (avoid / caution /
pass, with reasons) and how often tokens with each verdict died or graduated. The rules
are starting guesses to be tested, not advice. Signals only use events up to the moment
they describe. Only the live recording is replayed, not the backfilled history; a
creator's earlier coins are counted from both.

## History from the archive (backfill)

Old Faithful (`files.old-faithful.net`) keeps every Solana block, one file per epoch
(about a day, about 400 GB). It has no filter, so the whole epoch is downloaded and only
the pump.fun and PumpSwap rows are kept (a few GB). Nothing else is written to disk.

```powershell
.venv\Scripts\agent backfill --epoch 1084                # shows the size, asks, then downloads
.venv\Scripts\agent backfill --epoch 1084 --wait --yes   # waits for the epoch to be published
.venv\Scripts\agent backfill --epoch 1084 --status       # what is done, sizes and speeds
.venv\Scripts\agent backfill --epoch 1085-1091 --wait    # several epochs, one after another
.venv\Scripts\agent parity --epoch 1084                  # compare with the live recording
```

- Keep the window open while it runs (it does not run in the background like `agentd`).
  Ctrl+C stops it (every epoch left in the command); the same command continues where it
  stopped.
- With `--wait`, it checks every 10 minutes until the epoch is published, and it also
  waits out short archive or network trouble (rate limits, server errors, dropped
  connections) instead of stopping.
- Speed limit and parallel downloads: `backfill:` in `config/settings.yaml`, or
  `--max-mb-s` and `--workers`. The default (80 MB/s) leaves room for the live recorder.
- A piece that keeps failing is written to `backfill/gaps/` and tried again next run.
- Rows go to `raw_backfill/`, separate from the live recording. They have the same
  tables and columns, plus a few `bf_` columns that only the archive has (position in
  the block, fee, compute units); those are not for model features.

`agent parity` is the check the spec requires: every row both the recorder and the
backfill captured must be identical. It also counts what the recorder missed and asks
the RPC about any row only the recorder has (those must be transactions that never
landed on chain).

## Labels (research track)

```powershell
.venv\Scripts\agent labels      # label every backfilled coin; prints rates and examples
```

What happened to each coin in its first hour (spec 7): dead, rugged, bundled (a stand-in),
graduated, and whether a simulated 1 SOL buy 5 s after T+30/60/120/300 s saw the price
double before it halved. Every threshold is in `config/labels.yaml`; change one and run
the command again (about 3.5 minutes per day of history). Labels look into the future, so
they are only for training and evaluating models, never inputs. They come from the
backfill only, and coins launched in the last 65 minutes of the data get empty labels.

## Dataset (research track)

```powershell
.venv\Scripts\agent dataset     # one row per coin and decision time; prints counts
```

Builds `dataset\dataset.parquet` in the data folder: one row per backfilled coin at
T+30, 60, 120 and 300 s, with the signals the engine computes live (replayed on the
backfill only), the verdict of the rules in `config/rules.yaml`, and the coin's labels.
If the labels are missing or out of date (other settings in `labels.yaml`, or a changed
backfill), it rebuilds them first. About 1 minute per day of history, plus the labels'
time when they are rebuilt.

- Columns starting with `label_` look into the future: never use them as inputs.
- Use rows where `has_gap` is false (the backfill covers the coin up to the cutoff) and
  `labels_complete` is true.
- `manifest.json` next to it lists the column groups (ids, signals, rules, labels) and
  what made the file: code version, rules and label settings, epochs, and a hash of the
  file. The same data and code always give the same file.

## Exploration notebook (research track)

Needs the research packages once (pandas, matplotlib, scikit-learn, JupyterLab; about
140 MB from PyPI):

```powershell
.venv\Scripts\pip install -e ".[research]"
```

`requirements-research.txt` lists the exact versions used, with their hashes. Then open
the notebook and run it (Run > Run All):

```powershell
.venv\Scripts\jupyter lab notebooks\r3_exploration.ipynb
```

Or run it all without opening it, saving a page to read in the browser:

```powershell
.venv\Scripts\jupyter nbconvert --to html --execute notebooks\r3_exploration.ipynb --output-dir D:\memecoin-agent-data\notebooks
```

It reads the dataset (`agent dataset`) and leaves out the days the evaluation plan
(`config/evaluation.yaml`, spec 15.4) holds back for testing models. Findings, with
numbers, are in `docs/PROGRESS.md`.

## Backtest as analysis (research track)

What a 1 SOL buy after each signal would have made or lost after costs. Nothing is
traded: it replays recorded trades.

```powershell
.venv\Scripts\agent backtest validation   # chooses the score cuts on the validation day and freezes them
.venv\Scripts\agent backtest train        # the unfitted signals (every coin, the rules) on the training days
.venv\Scripts\agent backtest test         # the test days, once; refused a second time
```

- **Signals** (`config/backtest.yaml`): every coin, the rules, and the saved models
  (`agent train`) with score cuts chosen on the validation day by picks per hour, never by
  profit.
- **A trade** (`config/padre_presets.yaml`): 1 SOL bought 5 s after the decision time
  (also 2 s and 10 s), exact bonding-curve or PumpSwap pool math on the reserves at that
  second, fees read from the chain data, a 20% slippage limit, a priority fee, a share
  of orders that fail, and three exit plans (take-profit, stop-loss, time stop).
- **Results:** `docs/R6_RESULTS.md`: profit or loss per signal for each delay, day and
  exit plan, for ordinary and mayhem coins, with 95% intervals. They are shown under two
  views of our own buy: it leaves no trace in the market (strict, the headline), or its
  SOL stays in the reserves (generous). The truth is between the two.
- `test` refuses to run when the settings, the cuts, the code or the models changed
  after the cuts were frozen.
- Padre's own fee (`platform_fee_pct`, 1% as a cautious test setting; the real fee has
  not been checked) changes no simulated trade, only what is taken off each result. After
  changing it, `agent backtest <days> --resummarize` builds the report again from the
  saved trades without simulating anything, and keeps the report it replaces.

## Where the data goes

Everything is under `DATA_DIR` from `.env` (default: `./data`):

```
raw/<table>/date=YYYY-MM-DD/part-*.parquet   recorded events, one folder per UTC day
live/verdicts/date=YYYY-MM-DD/verdict-*.parquet  the engine's verdicts, with their signals
                                             and the models' scores
models/r5/                                   the saved models (`agent train`), their manifest
                                             and reports
models/r5_live/bundle.json                   the models' calibration as plain numbers, for
                                             the engine (`agent export-live-models`)
raw_backfill/<table>/date=YYYY-MM-DD/e*.parquet  the same tables from the archive
gaps/gap-*.parquet                           every period when recording was off or unreliable
backfill/epoch-<E>/                          archive index, progress and report per epoch
backfill/gaps/epoch-<E>.parquet              archive pieces that could not be read
audits/audit-*.json                          audit results
labels/coins.parquet, entries.parquet        labels (`agent labels`), with manifest.json
dataset/dataset.parquet                      the research dataset (`agent dataset`), with
                                             manifest.json
backtest/r6/                                 the backtest (`agent backtest`): cuts.json,
                                             frozen.json, a report per set of days, and
                                             ledger/<set>/*.parquet (every simulated trade)
notebooks/                                   notebooks run with their results, and pages
duckdb_tmp/                                  DuckDB's overflow files, kept off drive C:
run/                                         engine log, status, alerts, state files and
                                             the screen's copy-to-verdict times
```

Tables: `tokens` (creates), `trades` (bonding-curve buys and sells), `curve_completes`,
`post_complete_buys`, `migrations`, `amm_trades` and `amm_pools` (PumpSwap), and
`truncated_txs`. Each table mirrors one on-chain event: columns keep the names pump.fun
uses in its IDL files (`src/agent/ingest/idl/`), except `user`, which is stored as `wallet`.
Every row has `ts` (on-chain time, use it for cutoffs), `slot`, `signature` and `source`.

Query with DuckDB, for example:

```sql
SELECT date, count(*) FROM read_parquet('D:/memecoin-agent-data/raw/trades/*/*.parquet',
                                        union_by_name=true) GROUP BY date;
```

## Status

Phases 0-2: the engine records live pump.fun events to Parquet with a gap log, `agent
audit` measures what was missed, and the curve math reproduces every recorded trade.
Phase 3: `agent backfill` reads history from the archive and `agent parity` checks it
against the recording. Fast path steps B and C: two live connections, continuous
recording, and live rule verdicts for every new token. Step D: the screen (`agent
screen`). Step L1 (2026-11-21): scores from the saved models beside each live verdict, as
analysis only.

Since 2026-11-13 the project is on the research track (spec 15.3): labels, a dataset built
from the backfilled history, exploration, models with an evaluation plan fixed in advance
(spec 15.4), a backtest as analysis, and a write-up. See
[docs/PROGRESS.md](docs/PROGRESS.md).
