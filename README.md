# Memecoin signal agent

A local tool that watches new pump.fun tokens and (in later phases) scores them, so you can
decide what to do on Padre yourself. Design: [docs/SPEC.md](docs/SPEC.md). Progress:
[docs/PROGRESS.md](docs/PROGRESS.md).

**Not financial advice.** This is a high-risk market. The agent only analyzes and alerts.

Built with [Claude Code](https://claude.com/claude-code), Anthropic's coding agent.

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
.venv\Scripts\agentd start                # record in the background
.venv\Scripts\agentd status               # running? for how long? progress, gaps, disk
.venv\Scripts\agentd pause-recording      # keep running but stop writing (logged as a gap)
.venv\Scripts\agentd resume-recording
.venv\Scripts\agentd stop                 # writes what is buffered, then exits
```

`agentd start` asks Windows itself to run the engine, so it belongs to no window or app:
you can close the PowerShell window, and Claude, and it keeps recording. It prints
"You can close this window" when that worked. If it prints "Keep this window open"
instead, Windows refused, and closing the window may stop the engine.

The PC must stay awake while the engine runs: Settings > System > Power > Sleep: Never.
The live feed uses about 20-35 GB of network data per day.

Three lines of `agentd status` tell you where a recording stands:

```
running:   15 m 08 s, since 04:46 today            this run of the engine
saved:     54 m 09 s recorded out of 58 m 04 s since 04:03 today; 3 gaps, 3 m 55 s off
burst:     [#-----------------------------]   4% of 24 h; 23 h 05 m to go, done about 04:07 tomorrow
```

The bar counts recorded time toward one burst (`goal_hours` in `config/settings.yaml`,
24 by default). Restarts and short breaks count toward the same burst; a break longer
than an hour starts a new one. At 100% it says to run the audit and then stop.

A fourth line, `history:`, shows progress toward all the history needed for a first model
(`dataset.target_days`, 28 days by default): days done and to go, and GB downloaded out of
the estimated total. The total assumes the missing days come from the backfill archive at
`dataset.backfill_gb_per_day` (310 GB, estimated from the archive's file sizes until the
Phase 3 pilot measures it). Downloads are counted across runs in `run/downloads.jsonl`.

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

## History from the archive (backfill)

Old Faithful (`files.old-faithful.net`) keeps every Solana block, one file per epoch
(about a day, about 400 GB). It has no filter, so the whole epoch is downloaded and only
the pump.fun and PumpSwap rows are kept (a few GB). Nothing else is written to disk.

```powershell
.venv\Scripts\agent backfill --epoch 1084                # shows the size, asks, then downloads
.venv\Scripts\agent backfill --epoch 1084 --wait --yes   # waits for the epoch to be published
.venv\Scripts\agent backfill --epoch 1084 --status       # what is done, sizes and speeds
.venv\Scripts\agent parity --epoch 1084                  # compare with the live recording
```

- Keep the window open while it runs (it does not run in the background like `agentd`).
  Ctrl+C stops it; the same command continues where it stopped.
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

## Where the data goes

Everything is under `DATA_DIR` from `.env` (default: `./data`):

```
raw/<table>/date=YYYY-MM-DD/part-*.parquet   recorded events, one folder per UTC day
raw_backfill/<table>/date=YYYY-MM-DD/e*.parquet  the same tables from the archive
gaps/gap-*.parquet                           every period when recording was off or unreliable
backfill/epoch-<E>/                          archive index, progress and report per epoch
backfill/gaps/epoch-<E>.parquet              archive pieces that could not be read
audits/audit-*.json                          audit results
run/                                         engine log, status and state files
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
against the recording. Models come later; see [docs/PROGRESS.md](docs/PROGRESS.md).
