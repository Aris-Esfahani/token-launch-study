# Memecoin Signal Agent for Padre: Build Spec

Handoff doc for Claude Code. Read fully before writing code.

**What this is:** a local terminal app that scores brand-new pump.fun tokens and gives instant analysis on any mint the user is looking at in **Padre** (trade.padre.gg / terminal.pump.fun). The human trades manually on Padre. **The agent never places trades and never touches private keys.**

> **Research track** *(2026-11-13, decided by the user)*. The project is now a data science study, not a tool for trading. Its question: **using only a new pump.fun coin's first 30 s to 5 min of on-chain activity, how well can we predict whether it dies, rugs, graduates or doubles, and would any of that survive fees?** The data is the backfilled history (4.1); the deliverable is an honest, reproducible evaluation and write-up. What was built for live use (engine, live signals, screen, local API) stays as a working demo; the rest of the live side (paper trading, real-trade tracking, enrichers, packaging; parts of 1, 3, 11-14) is paused. Build order: 15.3. Evaluation plan: 15.4. Sections that describe the practical track are left as they were, so it can be resumed.

---

## 0. Locked decisions

| Decision | Choice |
|---|---|
| Project track *(2026-11-13, research track)* | **Research:** a data science study on backfilled history (see the note above). The rows below still describe the practical track, which is paused. |
| Trading venue | **Padre** (manual execution by the user). Padre has no public API. |
| Chain / tokens | Solana, pump.fun bonding curve (and PumpSwap after graduation) |
| Trading window | First **0-15 min** after token creation |
| Interface | **Terminal app (TUI)**: live alert feed + instant on-demand analysis + clipboard watcher |
| Remote alerts | Optional later: Telegram/Discord, alerts only (for when away from laptop) |
| Budget | **Free tiers only** |
| Language / stack | Python 3.11+, asyncio, Textual (TUI), DuckDB + Parquet, LightGBM |
| LLM | Claude, only for slow, optional narrative/copycat/"why" explanations. Never in the fast path. |
| Runs on | User's laptop (or an always-on machine for `agentd`); packaged so others can install it (section 14) |
| OS | **Windows** (user's machines are Windows PCs). Build and test on Windows first; native Python on Windows, not WSL, so the clipboard watcher and auto-start work. Keep code cross-platform (pathlib, no shell-specific scripts) so macOS/Linux still work. |
| Training data priority | **Historical first.** Train a strong baseline on backfilled history (Old Faithful, last 2-3 months) + public datasets **before** relying on live recordings. The live recorder adds fresh data on top. *(Updated 2026-11-12: 2-3 months is the target. The actual window is set by the Phase 3 pilot, starting with 2-4 weeks; see 4.1.)* |
| User timeline | User is not trading soon. No rush to go live; prioritize a well-trained, well-tested baseline. |
| Prediction stages (build in order) | 1. Rug/bundle filter → 2. Graduation → 3. 2x-before-(-50%) → 4. Filter-then-rank |

---

## 1. How it fits the Padre workflow

```
Padre (browser)                         Agent (terminal, beside Padre)
─────────────────                       ──────────────────────────────
Trenches shows a new coin  ──copy mint──>  clipboard watcher detects mint
                                           fast verdict in < 1-2s
                                           enrichment follows a few s later
User decides on Padre      <───────────  risk / grad / 2x scores + reasons
Buys with Padre preset,
sets TP / SL / limit orders
                                         agent also pushes its own alerts
                                         (mint first, so it can be pasted into Padre)
```

Padre-specific rules:
- **Mint address first** in every alert and analysis, so it can be pasted straight into Padre's search.
- **Mirror Padre's mental model:** show the same kinds of signals Padre's Trenches filters use (liquidity/curve progress, holder growth, dev history, insider/top-holder concentration, bundles), so the user can cross-check the agent against Padre's screen.
- **Exit rules in backtests and paper trading mirror the user's Padre settings** (take-profit, stop-loss, time stop, slippage, priority fee). Keep them in `config/padre_presets.yaml`.
- **Do not automate, scrape, or reverse-engineer Padre's UI or private endpoints.** It has no public API, it can break without warning, and it risks the user's account. All data comes from the chain and public APIs.
- **User's own Padre trades as labels:** Padre trades come from the user's own wallet, so pull them from the chain using the wallet's **public address** (in `.env`, never a private key). No manual logging needed.
- Official Padre domains only in any links: `trade.padre.gg`, `terminal.pump.fun`, `padre.gg`, `app.padre.gg`.

---

## 2. Goals and non-goals

**Goals: research track** *(2026-11-13, research track; these replace the practical goals below while the project is on this track)*
- Build a complete, recent historical dataset from the backfill (4.1): every pump.fun coin and trade, bonding curve and PumpSwap.
- Label every coin (section 7) and compute its features at T+30/60/120/300 s with the shared, no-lookahead feature code (6, 8).
- Describe the market: how often coins die, rug, graduate and double; how that varies by hour, by creator history and for mayhem coins.
- Predict the section 7 outcomes and measure honestly: time-split evaluation fixed in advance (15.4), against base rates, the hand-written rules and logistic regression; calibration; leakage checks.
- Simulate whether any signal would make money after fees, price impact and entry delay (10), as analysis only.
- Write it up so the results can be reproduced from the data and the commands, with their limits stated.

**Goals: practical track** *(paused 2026-11-13, research track; kept for reference)*
- Build a large, recent historical training set (backfill) before going live.
- Record every new pump.fun token and bonding-curve trade in real time. *(Updated 2026-11-12, after Phase 1. PumpSwap trading is not recorded live: its stream is about 83 GB/day, four times the bonding-curve stream. PumpSwap history comes from the backfill; live PumpSwap for freshly graduated tokens can be added later with one small subscription per new pool.)*
- Keep live, in-memory features for every token under 15 min old.
- Answer "analyze this mint" in **< 2 seconds** from local data.
- Predict rug/bundle risk, graduation probability, and 2x-before-(-50%) probability.
- Push ranked alerts to the terminal feed.
- Measure honestly: backtest with realistic costs → paper trade live → compare to the user's real Padre results.

**Non-goals (for now)**
- No auto-execution, no private keys, no Padre automation.
- No reinforcement-learning market simulator.
- No paid data.
- *(2026-11-13, research track.)* No further live-tool work (always-on uptime, paper ledger, alerts, packaging) while on the research track; the live recorder's data is used only to describe the live feed, not for training.

---

## 3. Architecture

Two processes, so the UI can restart without losing live state:

```
┌─────────────────────────── agentd (daemon, always on) ───────────────────────────┐
│                                                                                 │
│  Solana WS ──> recorder ──> raw events (parquet, append-only)                   │
│                    │                                                            │
│                    v                                                            │
│              live state (in memory): rolling features for tokens < 15 min old    │
│                    │                                                            │
│   background enrichers (rate-limited, cached): RugCheck · metadata/socials ·    │
│   website check · wallet funding · copycat · optional LLM tags                  │
│                    │                                                            │
│                    v                                                            │
│              models (loaded once) ──> scorer ──> alert engine ──> paper trader   │
│                    │                                                            │
│   local API (localhost websocket, 127.0.0.1 only): analyze / subscribe / ask    │
└────────────────────┬────────────────────────────────────────────────────────────┘
                     │
        ┌────────────┴────────────┐
        v                         v
   agent-tui (Textual)       optional: telegram/discord notifier (alerts only)
   - alert feed
   - paste box + clipboard watcher
   - analysis panel
   - ask <mint> <question>
```

- **Training data comes from two places that share one parser:** (1) the **backfill** of recent history (section 4.1), which is the priority for building the baseline, and (2) the live **recorder**, which keeps the data current. Backfilled and recorded events must go through the **same parser and the same schema**, so the model can't tell them apart.
- *(Updated 2026-11-12, after Phase 1.)* Record in bursts (for example 24-hour runs) until Phase 12, then continuously from live scoring and paper trading onward. Recording is not free: about 22-54 GB of network data per day and a PC that stays on and awake. Every off period is in the gap log, and the backfill can fill any period that was not recorded (the archive is about a day behind).
- The daemon loads models once at startup. The TUI never starts Python or loads models per request.
- Bind the local API to `127.0.0.1` only.

### 3.1 The three parts

| Part | Command | Role | When it runs |
|---|---|---|---|
| **Engine** | `agentd` | Records live data **and** does all live analysis (live state, enrichment, scoring, alerts, paper trades). This is the "brain". | During recording bursts; always on from Phase 12 (updated 2026-11-12) |
| **Screen** | `agent` | TUI only: shows alerts, takes pasted/copied mints, shows analysis, passes commands to `agentd`. Holds no data. | While trading/monitoring |
| **Trainer** | `agent train` | Offline: reads backfilled + recorded data, builds features/labels, trains and evaluates models, writes versioned model files. `agentd` then loads them (`agentd reload-models`). | Weekly, or manually |

Training never happens inside the live engine.

### 3.2 Controlling `agentd`

```
agentd start              # turn on (recording + analysis)
agentd stop               # turn off
agentd status             # running? ws connected? lag, tokens/hr, credits used, gaps today, model version
agentd pause-recording    # keep analysis running, stop writing new raw events
agentd resume-recording
agentd reload-models      # load newest model files without restarting
```

- **Gap log:** every period when recording was off or disconnected is written to `data/gaps.parquet` (start_ts, end_ts, reason). Training and labeling must exclude or flag tokens affected by gaps (e.g., a token created during a gap has incomplete early trades). Never treat a gap as "no tokens were launched". *(Updated 2026-11-12, step B.)* With several live connections at once, a gap is on-chain time that no connection delivered: one connection dropping while another keeps delivering is not a gap, but a connection that is up and running late does not cover time it never reaches before it drops.
- **Auto-start (optional):** a setting to start `agentd` on login. Windows first: Task Scheduler task created by `agentd install-autostart` (and removed by `agentd uninstall-autostart`). launchd/systemd later if needed.
- **Sleep warning:** if the PC sleeps, recording stops. On Windows, document how to set "Sleep: Never" while plugged in (Settings → System → Power), and have `agentd status` warn if the system slept since the last check. `agentd status` and the TUI status bar must show when the last gap happened. Recommend disabling sleep while recording, or running `agentd` on an always-on machine (old PC, mini PC, or a free-tier cloud VM; verify current free-tier terms first).
- When `agentd` is off, the TUI shows "engine offline" and offers to start it.

---

## 4. Data sources (free tier)

| Source | Use | Notes / limits (verify current docs before coding) |
|---|---|---|
| **Public Solana RPC WebSocket** (`api.mainnet.solana.com`, no key) | **Primary live feed:** `logsSubscribe` on the pump.fun program; parse create/buy/sell | *(Added 2026-11-12.)* Free, but "not for production": it can throttle or block without notice, so the recorder must measure its own miss rate. Limits: 100 requests / 10 s per IP, 40 connections, 100 MB / 30 s. The live source is pluggable (ordered list in `settings.yaml`). |
| **Helius** (free plan) | RPC lookups: wallet funding, user wallet history, gap repair, audits. **Not the live firehose.** | *(Updated 2026-11-12.)* 1M credits/month, 10 RPC req/s. WebSocket data is billed at 2 credits per 0.1 MB; the pump.fun log stream measured ~18.6 GB/day (~11M credits/month), so it does not fit. Avoid `getProgramAccounts`; cache everything. |
| **Bitquery** free sample | Historical pump.fun Parquet sample for bootstrapping and schema design | Sample only; not enough to train alone. |
| **RugCheck** `api.rugcheck.xyz` | Mint/freeze authority, risk flags, top holders, creator balance | Free reports may be cached; respect rate limits. |
| **DexPaprika** | Backup price/OHLCV, post-graduation pool data | Up to ~15s delayed. Analysis only. |
| **GeckoTerminal API** | Post-graduation pool data, sanity checks | 30 calls/min keyless. |
| **Token metadata URI** (from create event) | Name, symbol, image, description, website/X/Telegram links | Fetch once per token. |
| **Website check** (own code) | Resolves? HTTP 200? hosting type, domain age (WHOIS) | Rate-limit; cache by domain. |
| **X/Twitter** | Narrative | No free tier. Phase 1: link presence only. Later: optional small curated KOL list. |
| **OpenChainBench** | Pick the lowest-latency RPC | One-time research. |

Program IDs (verify on-chain before use):
- pump.fun: `6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P`
- PumpSwap: `pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA`

**Do not hardcode bonding-curve constants or fee rates.** pump.fun has changed them over time. Read them from the program's global config account, store them with a timestamp, and use the values that applied at each trade's time.

*(Updated 2026-11-12.)* Fee tiers now live in a separate fee program (`pfeeUxB6jkeY1Hxd7CsFCAjcbHA9rWtchMGdZ6VojVZ`, `FeeConfig` account), and curve parameters in the pump.fun `Global` account. Store both as timestamped snapshots read from chain (`data/config_snapshots/`), not as a hand-edited YAML file. Each trade event also records the fee rates and amounts actually charged; prefer those per-trade values wherever they exist.

### 4.1 Historical training data (priority for the baseline)

| Source | What it gives | Role | Notes |
|---|---|---|---|
| **Old Faithful** (Triton, `files.old-faithful.net`) + **Jetstreamer** (now maintained by Vaultic, `vaultic-win/jetstreamer`) | Every Solana block and transaction, one CAR archive per epoch, free | **Main baseline training set.** Backfill recent history (target 2-3 months; window set by the Phase 3 pilot), keeping only pump.fun + PumpSwap program transactions | Archives are very large. **Test one epoch first** to measure download size, disk, and parse time. Stream and filter; never store full blocks. Free hosting is "subject to change"; verify before relying on it. *(Updated 2026-11-12: an epoch is now ~1.07 days and 290-670 GB, so 2-3 months is roughly 20-40 TB of streaming. There is no server-side program filter. Jetstreamer on native Windows is unverified.)* *(Updated 2026-11-12, Phase 3: read with our own Python reader (`ingest/car.py`) instead of Jetstreamer, which needs Rust, MSVC build tools and ClickHouse and is not built for Windows. The reader uses the same per-slot index (`epoch-<E>-slot-ranges.raw`) and HTTP range requests, checks every node against its CID, and reads legacy, v0 and v1 (SIMD-0385) transactions.)* |
| **MemeTrans** (arXiv 2602.13480, GitHub `git-disl/MemeTrans`) | 41k+ Solana memecoin launches, 200M+ transactions, bundled-account traces | Prototype and validate **Stage 1 bundle features**; cross-check our bundle-cluster logic against their labels | Older data; check license and collection dates before use. |
| **Kaggle: Solana Skill Sprint "memecoin graduation"** | Labeled pump.fun graduation data | Prototype and test the **Stage 2** pipeline end to end; review top public solutions for feature ideas | Check competition data rules and dates. |
| **Bitquery free Parquet sample** (`github.com/bitquery/blockchain-cloud-data-dump-sample`) | Sample pump.fun creations, migrations, trades, pools, OHLCV | Schema design and parser tests | Sample only. |
| **Google BigQuery** public Solana data + decoded pump.fun tables (Token Terminal) | SQL access to decoded pump.fun data; 1 TB free queries/month | Optional extra volume, spot-checks of our parsed data | Check freshness first: the main Solana table has had update delays/stalls. Always filter by date to stay within the free tier. |
| **Dune** (free tier) | Decoded pump.fun tables | Exploration and sanity checks | Free exports are limited. |
| **MemeChain** (arXiv 2601.22185, Zenodo, CC-BY 4.0) | Off-chain artifacts (websites, logos, socials) | Feature ideas only (section 8.5) | Not used as training data (section 17). |

Do **not** use for training: Coin-Meme / CoinVibe (graduated tokens only, survivorship bias), small paid "chart" datasets, small sentiment datasets.

**Backfill rules**
- Same parser and schema as the live recorder (section 5). Tag every row with `source = backfill | recorder | external`.
- **Recency weighting:** pump.fun fees, curve parameters, migration target (Raydium → PumpSwap), and instruction formats have changed over time. Train on the most recent 2-3 months; if older data is added, down-weight it and include a "fee/curve regime" feature from the versioned config.
- External datasets (MemeTrans, Kaggle) are for prototyping and validation, and stay out of the main training set unless their dates and mechanics match the current regime.
- Run a **parity test**: for a period covered by both the backfill and the live recorder, the two must produce identical trades and features. *(Made precise 2026-11-12, Phase 3.)* The live feed drops some transactions and delivers a few that never landed, so the two never hold exactly the same set. The test is: (1) every row both captured (same `signature` + `event_index`) is identical in every shared column except `recv_ts` and `source`; (2) every row only the recorder has belongs to a transaction the chain has no record of; (3) rows only the backfill has are recorder misses, reported inside and outside the recorder's logged gaps but not a failure. PumpSwap rows are compared only for transactions that mention pump.fun (the live feed follows pump.fun only). Backfill rows are kept in `data/raw_backfill/` until Phase 4 decides how to merge them. *(Updated 2026-11-13, closing Phase 3; approved by the user.)* Two readings, from the pilot epoch 1084: (a) a row both captured whose slot differs counts as matching when the chain confirms the backfill's slot: the live feed reported the transaction from a block that was dropped, and it landed again a few slots later (29 trades, all 29 confirmed); (b) when checking every recorder-only transaction is too slow on the public RPC (about 1.3 s each with the full history), a random sample may stand in, reported with its 95% upper bound on how many could be on chain (1,000 of 28,468 checked, none on chain: at most 0.30%), with control transactions checked before and after. The pilot also found that the live feed delivers about 1.3% of pump.fun trades that never land, even at commitment `confirmed`; live signals include them, backfilled training data does not.
- *(Added 2026-11-12)* **Window:** start with the most recent 2-4 weeks, then extend toward 2-3 months only if the pilot shows the bandwidth, time, and disk cost are acceptable. Ask the user before each extension (large download).
- *(Added 2026-11-12)* **Regime changes inside the window:** pump.fun changed layouts on 2026-09-12 (holder-rewards fields), 2026-09-30 (PumpSwap negative virtual reserves), 2026-10-07 (v3 trades, fees kept on the curve, synthetic migration, pump coins as quote mints), and 2026-10-13 (depth-2 quote coins). The parser must decode every regime in the window, with a test transaction from each.
- *(Added 2026-11-12)* **Backfill-only fields:** priority fee, Jito tip, and position within a slot exist only in full transactions, not in the live log stream. Store them as nullable, tag them backfill-only, and keep them out of model features unless the live path can supply the same values (otherwise training and live scoring would disagree). *(Updated 2026-11-13, research track.)* With no live scoring, they may be used as features, still known by each cutoff, tagged backfill-only, with every model also reported without them (what a live tool could use).
- *(Added 2026-11-13, research track)* **Training data is the backfill only.** Recorder rows are not merged into it: the live feed misses some transactions and delivers about 1.3% of trades that never land (above). The recorder's data is used to describe the live feed.

---

## 5. Data model (raw events)

Append-only Parquet, partitioned by date. Query with DuckDB.

*(Updated 2026-11-12, after Phase 1.)* Each on-chain event has its own table, and its columns keep the names pump.fun uses in its IDL files, so the parser holds no layout of its own. One rename: `user` is stored as `wallet` (a reserved word in SQL). Every event table also has `signature`, `event_index` (position among the transaction's pump.fun events), `slot`, `ts` (on-chain event time; use it for cutoffs), `recv_ts` (when the recorder saw it; empty for backfill), `source`, `event`, and `extra_hex` (bytes the IDL copy does not describe yet).

- `tokens` (CreateEvent): mint, creator, name, symbol, uri, bonding_curve, quote_mint, starting reserves, token program, mayhem and holder-reward flags, creator_fee_bps. Metadata contents and socials come later from the metadata enricher (8.5).
- `trades` (bonding-curve TradeEvent): mint, wallet, is_buy, sol_amount, quote_mint, quote_amount, token_amount, virtual and real reserves after the trade, fee and creator-fee rates and amounts as charged, ix_name, and derived `is_creator`. priority_fee and jito_tip are backfill-only (nullable; see 4.1).
  - *(Updated 2026-11-12.)* `quote_mint` + `quote_amount` because coins can now be paired with SOL, USDC, or another pump coin (`sol_amount` is 0 for those). Record every coin; build labels and models on SOL-paired coins first.
- `curve_completes` (CompleteEvent) and `post_complete_buys` (PostCompleteBuyEvent): the bonding curve finishing, and the part of a completing buy priced on the future pool.
- `amm_trades` (PumpSwap BuyEvent and SellEvent, with derived `is_buy`) and `amm_pools` (CreatePoolEvent): trading after graduation, kept apart from bonding-curve trades.
- `truncated_txs`: transactions whose logs the node cut short; their events may be incomplete and are repaired from the backfill.
- `wallet_funding`: wallet, first_funder, funding_ts, funding_amount (cached)
- `enrichment`: mint, rugcheck_json, website_status, domain_age_days, host_type, fetched_ts
- `migrations` (CompletePumpAmmMigrationEvent): mint, pool, bonding_curve, timestamp (the graduation time), amounts moved into the pool
- `my_trades`: the user's own wallet trades (pulled from chain via public address)
- `alerts` and `analyses`: everything the agent showed, with timestamps and scores (for later evaluation)

- `gaps`: start_ts, end_ts, reason (recorder off, disconnect, backfill missing range)

Every row needs a timestamp so features can be cut off at decision time, plus a `source` column (`backfill | recorder | external`).

---

## 6. Decision times and lookahead rule

**Training snapshots:** one feature snapshot per token at **T+30s, T+60s, T+120s, T+300s** after creation (T = create slot time).

**On-demand analysis:** uses the live state at the moment of the request; record `token_age_s` and pass it to the model as a feature so it matches training.

**Hard rule:** a snapshot at T+k may only use events with `ts <= T+k`. Enrichment fetched after T+k is not allowed for that snapshot unless it is static and known at creation (e.g., metadata URI contents). Write a unit test that fails if any feature reads data past its cutoff.

---

## 7. Labels

All thresholds live in `config/labels.yaml`.

**Stage 1: rug / bundle**
- `is_rug`: within 60 min of creation, creator + linked wallets sell ≥ 50% of their holdings **and** price falls ≥ 80% from peak.
- `is_dead`: no trades after T+30 min (pump.fun-speed version of the MemeChain paper's "One-Day Meme Coin").
- `is_bundled`: at T+60s, the bundle cluster (8.2) holds ≥ 20% of supply.
  - *(Updated 2026-11-13, research track.)* **Stand-in first.** The bundle cluster needs to know who sent SOL to the early buyers, and the backfill keeps only pump.fun and PumpSwap transactions, so that is not in the data. Until it is (by widening the backfill to the early buyers' funding transfers, or by checking against MemeTrans, Phase 5), the label is `is_bundled_proxy`: at T+60s, the wallets other than the creator that bought in the first second (thresholds in `config/labels.yaml`) hold ≥ 20% of supply. The write-up names it a stand-in.
- Target: `bad = is_rug OR is_dead OR is_bundled`. Option later: survival model (time-to-death).
- *(Added 2026-11-13, research track.)* Label rates are reported separately for mayhem and ordinary coins (8.10). R1 checks whether the mayhem agent's own trades keep coins from counting as dead, and reports `is_dead` both with and without them.

**Stage 2: graduation**
- `graduates_1h`: completes the bonding curve within 60 min of creation.
- *(Added 2026-11-14, closing R3, research track; evidence in `docs/PROGRESS.md`, "R3: exploration")* **Predicted only for coins still on the curve.** Most coins that graduate within the hour have done so before the first decision time (in R3, 60% of ordinary ones by T+30 s, most in their launch second), so for them the outcome is already known. At each decision time stage 2 predicts "graduates later in the hour" (`graduates_1h`, with the curve not complete at the decision time: the dataset's `completed` signal is false) for coins still on the bonding curve. Coins already complete at the decision time are counted and reported, not predicted.

**Stage 3: 2x before -50%**
- Entry price = curve price at decision time **+ latency delay** (default 5s) **+ price impact of the position size**.
- `hit_2x`: price reaches 2x entry before 0.5x entry within 60 min; otherwise 0.

**Stage 4: filter-then-rank**
- Not a new label. Score = P(not bad) × expected value from stages 2-3, after costs. Evaluate as a ranking per hour.

**How the labels are computed** *(added 2026-11-14, closing R1, research track; evidence in `docs/PROGRESS.md`, "R1: labels")*
1. **Prices are execution prices:** quote paid or received per token, before fees. They mean the same on the curve and in the PumpSwap pool. (Almost every graduated coin's pool has a virtual quote reserve, so prices from reserves would depend on that formula.)
2. **Price-based labels (`is_rug`, `hit_2x`) cover SOL-paired coins only.** Coins priced in another coin get them empty: their units do not line up across graduation.
3. **The mayhem agent's own trades are left out of the prices the labels use** (`prices.mayhem_agent` in `config/labels.yaml`); `hit_2x` is also stored with them included. Its trades still count as activity for `is_dead`.
4. **Trades under 0.001 SOL are not used as prices** (setting): rounding noise.
5. **The Stage 3 entry price includes the position's price impact but not fees.** On the curve the buy is simulated with the curve math. For a coin that graduated before the entry, the entry is the last traded price, without a simulated impact (the pool's reserves include a virtual part). Fees are applied in the backtest (10).
6. **`is_rug` uses the creator's wallet only.** Finding linked wallets needs the same funding transfers as the bundle cluster, which the backfill does not keep.
- Labels come from the backfill only. A coin whose first 65 minutes (the last entry at T+305 s plus one hour) are not fully covered keeps a row with empty labels.
- *(Added 2026-11-21, closing R6.)* The note in `labels/make_labels.py` that mayhem coins' entry prices are "less exact" because their curves are reset between trades is out of date: in the backfill the reserves after the last recorded trade are exact for them too (8.10). The labels and the module are unchanged (its file's hash is recorded with the dataset); only this reading of them is corrected.

---

## 8. Features (computed at the cutoff)

### 8.1 Dev / creator
- Prior pump.fun launches: count, % rugged/died, % graduated, median lifetime
- Creator buy at launch (SOL, % of supply), current holding %, sold yet, % sold
- Wallet age and funding source (fresh wallet funded minutes ago = red flag)

### 8.2 Bundlers / snipers
- Buys in the create slot; buys in the first 1-3 slots. *(Updated 2026-11-12: slots are now ~215 ms, not 400 ms, so also compute these over fixed time windows: first 0.5 s, 1 s, 2 s.)*
- Bundle cluster: early buyers sharing a funder within 24h, or funded with identical SOL amounts from one source
- Cluster supply %, cluster size, Jito-tip presence on early buys *(backfill-only: offline analysis, not a model feature; see 4.1)*
- Known sniper-bot wallets (from the wallet label DB, 8.6)

### 8.3 Holders
- Unique holders, top-1 / top-10 %, holder growth rate, fresh-wallet share

### 8.4 Trade flow and curve
- Buy/sell counts and SOL volume (total and last 30s), net SOL flow
- Buy-size distribution (median, max), unique buyers per minute
- Curve progress %, progress velocity, price change since creation
- Largest single sell as % of supply

### 8.5 Off-chain effort (from the MemeChain paper)
- Has image / description / website / X / Telegram
- Website is a real domain vs. a redirect to Telegram/X/pump.fun
- Resolves and returns HTTP 200; hosting type (custom domain vs. vercel.app / canva.site / other builders)
- Domain age, registrar
- Domain or X handle reused by other recent tokens (serial-scam / copycat signal)

### 8.6 Wallet label DB (the long-term edge)
`wallet_labels(wallet, label, evidence, updated_ts)` with labels like `rugger`, `bundler_cluster:<id>`, `sniper_bot`, `profitable_trader`, `kol`. When used as features, labels may only use evidence from before the snapshot cutoff.

**Seed it from the backfill.** Months of history let the agent recognize serial ruggers, repeat devs, bundler clusters, and consistently profitable wallets before going live. This is the single biggest boost to the baseline. Store each label with the date it first became knowable, so training only uses labels that existed at each snapshot time.

### 8.7 Copycat / narrative
- Name/ticker similarity to tokens created in the last 6h (near-duplicate count, position in its "family")
- Image perceptual-hash similarity to recent tokens
- Meme keyword category (animal, AI, political, celebrity, etc.) and that category's recent launch/graduation rate
- Optional LLM tag (narrative + "riding a current trend?"), cached by mint, never required

### 8.8 Market context
- SOL price change over 1h; pump.fun launches and graduations per hour

### 8.9 Trusted wallets (user-supplied traders and friends)

The user will later supply public wallet addresses of traders they believe are consistently profitable, including friends who agree to share. Build this in **Phase 6**, after the backfill, so every wallet can be verified against history.

**File:** `config/trusted_wallets.yaml` (gitignored; never shipped with the app)
```yaml
- address: <PUBLIC_WALLET_ADDRESS>
  name: friend_a            # nickname only
  source: friend            # friend | kol | found_onchain | other
  style: sniper             # sniper | holder | mixed | unknown
  consent: true             # required for friends
  added: 2026-11-12
  notes: "trades first 15 min, tight stops"
```
- **Public addresses only.** Never accept, request, or store private keys or seed phrases.
- Optional companion file `config/trusted_wallet_notes.csv` (mint, wallet, side, ts, reason) for trades where the trader wrote down *why* they bought or sold.

**Verification (`agent wallets verify`)**, run against backfilled + recorded data, before a wallet is used anywhere:
- Number of pump.fun trades, win rate, median and total after-cost P&L, max drawdown
- Consistency: profitable across most weeks or months, not driven by 1-3 outlier wins
- Style check: typical hold time and entry age (seconds after launch) vs. our 0-15 min window
- Behavior flags: buys that move the price a lot (being followed / exit-liquidity risk), signs of wallet rotation or coordinated wallets
- Output: `verified | unverified | rejected`, with the stats, stored in `wallet_labels` with a "knowable-from" date

**Discovery (`agent wallets discover`):** rank all wallets in the backfill by consistent after-cost profitability within the 0-15 min window. The user's list is a seed and cross-check; discovered wallets are proposed to the user for approval, never auto-trusted.

**How verified wallets are used**
1. **Features:** count of verified trusted wallets that bought before the snapshot cutoff, their combined SOL size, how early they entered, and whether any have already sold.
2. **Labels:** their entries and exits as extra weak labels ("a skilled trader bought here") alongside the main labels. Down-weight so no single trader dominates.
3. **Later (optional):** a per-trader "trades like X" model, only if that trader has enough trades (several hundred+) in our style.

**Rules**
- Lookahead: a wallet's "verified" status and stats may only use trades before each snapshot's cutoff.
- Privacy: friends' wallets are only used with consent, stay out of logs shared with others, and are never included in a shared wallet-label snapshot (section 14) without permission.
- Re-verify monthly; demote wallets whose performance falls off.

### 8.10 Mayhem-mode coins *(added 2026-11-12, after Phase 2)*

About a fifth of new coins are in pump.fun's mayhem mode: pump.fun's own agent wallet trades them (about 22% of all bonding-curve trades in Phase 2's sample), fee-free and priced by the mayhem program, and resets their reserves between trades.
- **Their own category:** `is_mayhem` (from the create event) is a feature, and label rates and model metrics are reported separately for mayhem and ordinary coins. If the two behave very differently, train a separate model for mayhem coins.
- **The agent's trades stay in** the data, the features and the labels. Each trade is flagged if the mayhem agent made it (the agent's wallet comes from the IDL, never hardcoded), so features can also show how much of a coin's activity is the agent's (for example its share of buy volume).
- Curve state for mayhem coins uses the recorded reserve resets (`mayhem_params`).
  *(Updated 2026-11-21, closing R6.)* In the backfill this needs no record of the resets: every trade by a person starts exactly from the reserves the previous recorded trade reported (all 14.9 million such trades on Nov 12-17, 3.8 million of them on mayhem coins), because the resets come with the mayhem agent's own trades. Curve state is the reserves after the last recorded trade, the agent's included. The backfill has no `mayhem_params` table.

---

## 9. Modeling

- One LightGBM model per stage, with `token_age_s` / snapshot time as a feature.
- **Split by time, never randomly.** Train on older weeks, validate on the next, test on the most recent. Walk-forward retraining.
- Class weights for imbalance; isotonic calibration on validation.
- Metrics:
  - Stage 1: PR-AUC, recall of bad tokens at a fixed false-positive rate
  - Stages 2-3: PR-AUC, precision at top-k alerts per hour, calibration curve
  - Stage 4: average after-cost P&L per alert, hit rate, max drawdown on the paper ledger
- Baselines to beat: (a) "everything that passes RugCheck", (b) a hand-written rules filter. If the model can't beat the rules, ship the rules.
- SHAP for every model; a suspicious top feature usually means leakage.
- Export models as files with a version + training-date manifest, so they can ship with the app (section 14).

---

## 10. Backtest rules

- Fills use **exact bonding-curve math** from reserves at fill time (price impact of the simulated size).
- Apply pump.fun fees (the per-trade fee fields recorded on chain, or the timestamped chain snapshots; see section 4), Padre-side settings from `padre_presets.yaml` (slippage, priority fee), and a failed-transaction rate.
- Entry delay: default 5s after the alert/analysis; test 2s / 5s / 10s.
- Exits mirror the user's Padre presets: take-profit, stop-loss, time stop.
- Report weekly to check stability.
- *(After Phase 2.)* Mayhem coins (8.10): the mayhem agent's trades stay in the simulated market, curve state uses the recorded reserve resets, and results are reported separately for mayhem and ordinary coins.
- *(Added 2026-11-21, closing R6, research track; evidence in `docs/PROGRESS.md`, "R6: backtest as analysis".)* **How the backtest is run:**
  - **Reserves:** a simulated trade lands on the reserves after the last recorded trade at or before its second, on the curve or, for a graduated coin, in its PumpSwap pool. Mayhem coins need no recorded resets (8.10). A pool's price follows its token reserve and its quote reserve plus its virtual quote reserve. A sale on the curve never pays out more than the curve holds.
  - **Our own buy, two views, both reported:** it leaves no trace in the market (strict, the headline: on a new curve this understates a 1 SOL sale by about 6% of the stake), or the SOL it put in stays in the reserves (generous: exact when nobody else trades). The truth is between the two.
  - **Signals:** a buy signal is a score at or above a cut fixed in advance. "The top 5 of the hour" cannot be traded, so each cut is the score that gave that many picks per hour on the validation day; no profit or loss is used to choose it (`config/backtest.yaml`).
  - **Settings added to `config/padre_presets.yaml`** (test settings, not measured costs): the share of orders that fail (5%; 0% and 10% also shown), the age of the quote the slippage limit is measured against, how often a failed sell is tried again, and Padre's own fee (1% in the headline, the user's cautious choice; 0% as a what-if). A sell lands one entry delay after its trigger.
  - **The test days are simulated once** (`agent backtest test` refuses a second run and any change after the cuts were frozen). Padre's fee changes no simulated trade, so a report may be built again from the saved trades after a change to it (`--resummarize`).
  - "Report weekly" is, with nine days of history, a report per day.

---

## 11. Paper trading and real-trade feedback

- Every alert gets a simulated entry (delay + impact) and simulated exits under the Padre presets.
- Ledger: `paper_trades(mint, alert_ts, entry_ts, entry_price, exit_ts, exit_price, reason, pnl_sol)`.
- Pull the user's real Padre trades from chain (`my_trades`) and join them to what the agent showed at the time.
- Daily summary in the TUI: **backtest vs. paper vs. real** P&L per trade. The gaps between these are the key health metrics.

---

## 12. Terminal app (TUI)

### 12.1 Layout
```
┌ ALERTS (live) ───────────────────────────────────────────────────────────┐
│ 12:04:31  <MINT>  $TICK  T+60s  risk .12  grad .41  2x .28  #2/hr         │
│ 12:03:58  <MINT>  $FOO   T+120s risk .09  grad .55  2x .31  #1/hr         │
├ ANALYZE ─────────────────────────────────────────────────────────────────┤
│ > paste mint (or just copy it in Padre)                                   │
│                                                                           │
│ <MINT>  $TICK · age 74s · curve 34% · data as of 2s ago                   │
│ RISK 0.12 (low)   GRAD 0.41   2x 0.28                                     │
│ + dev: 0 prior rugs, 3 graduations, has not sold                          │
│ + 6 profitable wallets in                                                 │
│ - top10 holds 38% · bundle cluster 11%                                    │
│ … enrichment: RugCheck ok · mint/freeze revoked · no website (X only)     │
├ STATUS ──────────────────────────────────────────────────────────────────┤
│ ws: connected · lag 0.4s · tokens/hr 1,820 · credits used 41% · model v3  │
└──────────────────────────────────────────────────────────────────────────┘
```

### 12.2 Two-stage on-demand analysis
1. **Fast reply (target < 1-2s):** read live state from memory → score with loaded models → show scores + top reasons. No network calls.
2. **Enrichment reply (a few seconds):** RugCheck, missing wallet-funding lookups, website/socials check, optional LLM take. Runs in parallel and updates the panel when ready.
- Always show **data age** ("as of 2s ago"); first-minute data goes stale fast.
- **Unknown mint** (created before the recorder started, or missed during a disconnect): show "backfilling…", fetch its trades from RPC, then score. Show a warning that this is slower and uses credits.

### 12.3 Clipboard watcher
- Polls the clipboard (e.g., every 200ms) and auto-analyzes when it sees a valid Solana mint address (base58, 32-44 chars). *(Updated 2026-11-12, after Phase 1. Ending in `pump` is only a weak hint: in a sample only 56% of new pump.fun mints did. Confirm a mint against local data or the chain.)*
- Toggle on/off with a hotkey. Ignore repeats of the same mint.
- **Windows first** (user's OS). Use a library that works natively on Windows (e.g., `pyperclip` or `pywin32`); test that it doesn't conflict with the browser running Padre. macOS/Linux support is a later nice-to-have.

### 12.4 Commands
- `<mint>`: analyze
- `ask <mint> <question>`: slow LLM explanation using the current features (e.g., "why is risk high?")
- `watch <mint>`: re-score every few seconds and flag big changes (dev sold, cluster dumped)
- `mute <mint>` / `filter <expr>`: control the alert feed
- `stats`: backtest vs. paper vs. real summary

### 12.5 Speed rules
- Keep rolling features for all tokens < 15 min old updated in memory on every trade.
- Pre-fetch wallet funding for early buyers in the background **before** the user asks.
- No LLM, no outside API calls in the fast path.
- Log end-to-end latency for every analysis (request → fast reply, request → enrichment) and show the median in the status bar.

### 12.6 Optional remote alerts (later)
- A small notifier that subscribes to the daemon's alert stream and forwards to Telegram or Discord, for when the user is away from the laptop. Alerts only, mint first.

---

## 13. Repo layout

```
memecoin-agent/
  config/          settings.yaml, labels.yaml, padre_presets.yaml, readiness.yaml,
                   trusted_wallets.yaml + trusted_wallet_notes.csv (gitignored, never shipped)
  src/agent/
    daemon/        main.py, local_api.py, live_state.py
    ingest/        ws_client.py (pluggable live source), logs.py, parsers.py (shared by live + backfill), recorder.py, backfill.py (RPC gap fill),
                   car.py (Old Faithful CAR reader), oldfaithful_extract.py + oldfaithful_backfill.py (range download → filter pump.fun → parsers; updated 2026-11-12), gaps.py
    datasets/      memetrans.py, kaggle_graduation.py, bitquery_sample.py, bigquery_export.py (importers to our schema)
    cli/           agentd commands (start/stop/status/pause/resume/reload-models), agent train, agent backfill
    enrich/        rugcheck.py, metadata.py, website.py, funding.py, copycat.py, llm_tags.py
    features/      build_snapshot.py, feature_defs.py, live_features.py
    labels/        make_labels.py
    models/        train.py, evaluate.py, calibrate.py, registry.py
    sim/           curve_math.py, backtest.py, paper_trader.py
    tui/           app.py, widgets.py, clipboard.py
    notify/        telegram.py (optional)
    db/            duckdb helpers, wallet_labels.py, my_trades.py
  models/          shipped model files + manifest.json
  data/            raw/, features/, labels/, config_snapshots/ (fee + curve parameters read from chain) (gitignored)
  notebooks/       EDA only
  tests/           test_no_lookahead.py, test_curve_math.py, test_parsers.py, test_mint_detect.py,
                   test_backfill_parity.py (backfill vs recorder produce identical rows), test_gaps.py
  .env.example     HELIUS_API_KEY, MY_WALLET_PUBLIC_ADDRESS, ANTHROPIC_API_KEY (optional), TELEGRAM_* (optional)
  pyproject.toml   console scripts: agentd, agent (incl. agent train, agent backfill)
```

Never commit `.env`, data, or anyone's keys.

*(Added 2026-11-14, closing R2, research track.)* The research dataset (15.3, R2) is built by `src/agent/dataset/build.py` (`agent dataset`), not `features/build_snapshot.py`: nothing under `features` may import the labels (7), and the builder joins the two. It writes `data/dataset/` (`dataset.parquet` and `manifest.json`) rather than `data/features/`. The backfill's coverage, which the labels and the dataset both use, is in `ingest/backfill_coverage.py`.

*(Added 2026-11-21, R5, research track.)* The models (15.3, R5) are in `models/`: `frame.py` (the rows and columns a model may read), `train.py` (baselines, logistic regression, LightGBM, the checks, `agent train` and `agent score-test`), `evaluate.py` (the plan, the split and the metrics) and `report.py` (writes `docs/R5_RESULTS.md`). There is no separate `calibrate.py` or `registry.py`: calibration is part of `train.py`, and the model files with their manifest go to `data/models/r5/`. Backfill-only signals are in `features/backfill_only.py`.

*(Added 2026-11-21, closing R6, research track.)* The backtest (15.3, R6) is `sim/backtest.py` (one simulated trade and its profit or loss), `sim/market.py` (a coin's reserves and fees after every trade, from the backfill), `sim/pool_math.py` (PumpSwap pool math and its check against recorded trades) and `backtest/run.py` + `backtest/report.py` (signals, cuts, the ledger, the report, `agent backtest`; they write `data/backtest/r6/` and `docs/R6_RESULTS.md`). The run code is its own package because nothing under `sim` may import the dataset. There is no `paper_trader.py` on this track.

---

## 14. Packaging and sharing

- Install: `pipx install git+<repo-url>` (or clone + `pip install -e .`). Two commands: `agentd` (daemon) and `agent` (TUI).
- First run: an `agent setup` wizard creates `.env`, checks the Helius key and the WebSocket connection, and creates the data folders.
- **Each user brings their own free API keys.** Never ship or share keys; free-tier limits are per key.
- **Each user records their own data** on their own machine.
- Ship with: trained model files + manifest (version, training dates, feature list), and optionally a wallet-label snapshot so new users get useful scores on day one. **Exclude** `trusted_wallets.yaml`, friends' wallets, and the user's own wallet from anything shipped. Auto-check for newer model files on startup (optional).
- No private keys anywhere, ever. This is what makes the app safe to hand to others.
- Include a clear **"not financial advice"** notice in the README and on first launch.
- Keep distribution private or small: if many people act on the same alerts, they crowd each other out and the edge shrinks. If it's ever sold as a paid signal service, check the regulatory side first.

---

## 15. Phases and acceptance criteria

Priority order: **parser → recorder → historical backfill → train a strong baseline → paper trade → go live.** Live-recorded data supplements the baseline; it is not the starting point.

*(2026-11-13, research track.)* On the research track the build order is 15.3 and the evaluation plan 15.4; 15.0-15.2 describe the practical track, which is paused.

### 15.0 Build order: fast path *(updated 2026-11-12, during Phase 3; approved by the user. Paused 2026-11-13, research track: see 15.3)*

The phases below keep their numbers and "done when" checks, but are built in this order so rules-based alerts go live and are paper-tracked while the backfill and models are built. The rules filter is the baseline that section 9 requires anyway; a model replaces it only when it beats it. The readiness criteria (15.1) for real money are unchanged, and every step ends with tests, its "done when" results and the user's OK.

| Step | Builds | Done when |
|---|---|---|
| **A** | Close Phases 1 and 3 | Their "done when" checks below |
| **B** | Engine always on (part of 12): continuous recording, a second feed connection with duplicates removed by signature | 24 h continuous run with recorder uptime ≥ 95% (the 15.1 operations threshold) |
| **C** | Live signals + rules verdict (parts of 8 and 9): the chain-only features of 8.1-8.4, 8.8 and 8.10, computed by one piece of code that runs both live and when replaying recorded/backfilled data; hand-written rules in `config/rules.yaml` | No-lookahead test passes; a token's snapshot is identical live and replayed; every new token gets a verdict with reasons at T+30/60/120/300s |
| **D** | Screen (part of 12): alert feed, paste box + clipboard watcher | Copy-to-verdict median < 2 s; mint first in every alert |
| **E** | Paper ledger + `my_trades` (13) with `padre_presets.yaml` | Every alert gets a simulated entry and exit; daily paper vs. real summary |
| *background* | Phase 4 full backfill, started after A | As in Phase 4 |
| then | Phases 5-7, the rest of 8, 9-11, 14-16 | As below; live features and the screen are extended as models, enrichers and wallet labels arrive |

*(Step B, built 2026-11-12.)* Both connections use Solana's free public server (no free alternative carried the stream: PublicNode sent nothing, dRPC's free plan refuses `logsSubscribe`, Helius bills the data). The server limits each IP address and refuses new connections for up to a minute after some drops, while open ones keep streaming. Two connections use about 130 GB of network data a day.

| Phase | Build | Done when |
|---|---|---|
| **0. Setup** | Repo, config, `.env`, Helius key (optional; the live feed uses the public WebSocket), `agentd` start/stop/status | Daemon connects and prints live create/trade events |
| **1. Parser + recorder** | One shared parser for pump.fun + PumpSwap; live recorder on the bonding curve (PumpSwap from the backfill; updated 2026-11-12); store tokens/trades; reconnect; gap log | 24h run with < 1% missed tokens (spot-check against an explorer); gaps logged |
| **2. Curve math + tests** | Price/impact from reserves; fee/curve versioning | Simulated prices match on-chain trade prices within tolerance |
| **3. Backfill pilot** | Old Faithful for **one epoch** (own reader instead of Jetstreamer; updated 2026-11-12), filtered to pump.fun/PumpSwap, through the shared parser | Measured download size, disk use, and parse time per epoch; **parity test** passes against the recorder for an overlapping period |
| **4. Full backfill** | Window chosen from the pilot: most recent 2-4 weeks first, extended toward 2-3 months if the cost allows | Complete coverage with gaps logged; storage and run time documented |
| **5. External datasets** | Importers for MemeTrans, Kaggle graduation, Bitquery sample | Bundle-cluster logic checked against MemeTrans labels; Stage 2 pipeline runs end to end on Kaggle data |
| **6. Wallet-label DB + trusted wallets** | Seed from backfill: ruggers, repeat devs, bundler clusters, profitable wallets (with "knowable-from" dates). Add `trusted_wallets.yaml` support, `agent wallets verify` and `agent wallets discover` (section 8.9) | Labels available with no future leakage; user-supplied wallets verified with stats; discovered wallets proposed to the user |
| **7. Enrichers** | RugCheck, metadata, website, funding (with background pre-fetch), copycat | Cached; inside free-tier limits. Note: historical tokens get only the enrichment still recoverable (on-chain + metadata URI); mark live-only features as missing in training |
| **8. Features + labels** | Snapshots at T+30/60/120/300s; labels per section 7 | No-lookahead test passes; label rates look plausible |
| **9. Stage 1 model** | Rug/bundle filter, trained on the backfill | Walk-forward results meet the readiness criteria (15.1) for Stage 1 |
| **10. Stages 2-3** | Graduation and 2x models | Calibrated; beat baselines on the most recent month |
| **11. Backtest** | Full cost model with Padre presets | Monthly and weekly results with delay sensitivity |
| **12. Live state + TUI** | In-memory rolling features, terminal app, clipboard watcher, two-stage analysis | Copy-to-verdict < 1-2s; enrichment arrives in a few seconds |
| **13. Paper trading + real-trade tracking** | Alert feed, paper ledger, `my_trades` from chain | Runs **4-8 weeks** across different market conditions |
| **14. Stage 4 ranker** | Filter-then-rank | Meets all readiness criteria (15.1) |
| **15. Packaging** | `pipx` install, setup wizard, model manifest, README | Fresh install works on a clean Windows PC (macOS optional) |
| **16. Ongoing** | Weekly retrain (backfill + recorder data, recency-weighted), wallet-label growth, optional remote alerts | Drift monitored (feature distributions, hit rate, latency) |

### 15.1 Readiness criteria (decide before seeing results; do not loosen later) *(practical track; paused 2026-11-13, research track: see 15.4)*

The agent is "ready for real money" only when **all** hold on out-of-time data (the most recent month that the model never trained on) **and** during paper trading:

- **Stage 1:** catches ≥ 90% of bad tokens (rug/dead/bundled) while blocking ≤ 30% of tokens that turned out fine.
- **Stages 2-3:** beat the rules baseline on PR-AUC and on precision at the top alerts per hour; calibration error within a set tolerance.
- **Backtest vs. paper gap:** paper P&L per alert is at least half of backtest P&L per alert (a bigger gap means the backtest is lying).
- **Paper trading:** positive after-cost P&L per alert for **4+ consecutive weeks**, with max drawdown inside a set limit.
- **Stability:** no single week accounts for most of the profit; results hold in both busy and quiet weeks.
- **Operations:** recorder uptime ≥ 95% over the paper period; copy-to-verdict median < 2s.

Store these thresholds in `config/readiness.yaml`; `agent train` and the TUI `stats` command report pass/fail for each.

### 15.2 Suggested timeline

| When | What |
|---|---|
| Week 1 | Phases 0-2; one 24-hour recording run (updated 2026-11-12: bursts, not continuous) |
| Weeks 2-3 | Phase 3 (backfill pilot), then start Phase 4 (full backfill) |
| Weeks 3-5 | Phases 5-8: external datasets, wallet-label DB, enrichers, features + labels |
| Weeks 5-7 | Phases 9-11: train stages 1-3, walk-forward tests, backtest |
| Weeks 7-8 | Phase 12: terminal app |
| Weeks 8-16 | Phases 13-14: paper trading + weekly retrains; check readiness criteria |
| After | If all criteria pass: small real trades on Padre; keep retraining |

*(Updated 2026-11-12, after Phase 1.)* The recorder runs in bursts until Phase 12 and continuously from then on, so paper trading adds several weeks of continuous live data on top of the backfill.

*(Fast path, 2026-11-12.)* With the build order in 15.0, rules-based alerts and paper trading start in about week 2 instead of weeks 7-8, and continuous recording starts at step B. The rest of this table still gives the order of the model work.

*(2026-11-13, research track.)* This table is for the practical track. On the research track the order is R0-R7 in 15.3, with no fixed dates.

### 15.3 Build order: research track *(2026-11-13, research track; decided by the user)*

This replaces the fast path (15.0). Every step still ends with the tests, its "done when" results, an update to `docs/PROGRESS.md` and the user's OK. The no-lookahead rule (6) and the parity test (4.1, passed in Phase 3) stay mandatory. Large downloads are asked for first.

| Step | Builds | Done when |
|---|---|---|
| **R0** | Close the live side: Phase 1's 24 h audit; stop the engine; bring branch `step-b` into `main` (with the user's OK) | Audit result recorded against Phase 1's check; engine stopped; recorded data kept |
| **R1** | Labels (section 7; the label half of Phase 8): `config/labels.yaml`; labels from backfilled bonding-curve and PumpSwap trades up to 60 min after creation | A test for each label on built examples; labels are computed apart from the features and never feed them; label rates per day, mayhem vs. ordinary, look plausible |
| **R2** | Dataset (the feature half of Phase 8): one row per coin per cutoff (T+30/60/120/300 s), from the shared feature code replayed on the backfill, joined to the labels; saved as Parquet with a manifest (code version, rules version, epochs) | No-lookahead test passes on the builder; one row per coin and cutoff; rebuilding gives identical output |
| **R3** | Exploration (notebook): outcome rates, survival over time, hour of day, mayhem vs. ordinary, creator history, feature distributions; the current rules scored as the first baseline | Notebook runs top to bottom from the dataset; findings in `docs/PROGRESS.md` |
| **R4** | More history (a smaller Phase 4): 7 more days first, the most recent archived epochs (about 2.7 TB to download, about 12 h at the pilot's speed, about 37 GB kept), more only if R5 needs it; asked for first | Complete coverage, gaps logged; download, disk and time documented |
| **R5** | Models (Phases 9-10): stages 1-3, each as rules → logistic regression → LightGBM (9), with the evaluation plan (15.4) | The 15.4 report, with the test days scored once; SHAP and leakage checks |
| **R6** | Backtest as analysis (Phase 11): simulated 1 SOL entries with a 5 s delay (2 s and 10 s tested), exact curve math, recorded fees, the exit plans in `padre_presets.yaml` as test settings | After-cost P&L per signal with delay sensitivity, per day, mayhem vs. ordinary |
| **R7** | Write-up: question, data, method, results, limits (including the live-feed findings of Phases 1 and 3) | Someone else could reproduce the results from the data and the commands |
| *optional* | Phase 5 (MemeTrans as a check on the bundle stand-in), Phase 6 (creator and wallet history), 8.7 copycat names (chain-only), stage 4 ranking as analysis | Their "done when" checks in 15.0, where they apply |

Paused on this track: steps B (24 h uptime), E (paper ledger), and Phases 7, 12-16, apart from what is already built.

### 15.4 Evaluation plan: research track *(2026-11-13, research track; decide before seeing results; do not loosen later)*

Written into `config/evaluation.yaml` before any model is scored on the test days.
- **Time split, never random:** the most recent quarter of the days (at least 2 days) is the **test set**, scored once at the end; the day before it is the **validation set**, for tuning and calibration; the rest is for training, with walk-forward splits inside it. All days are whole UTC days.
- **Baselines every model must be compared against:** the base rate (always predicting the most common outcome), the hand-written rules (`config/rules.yaml`) and logistic regression.
- **Metrics:** stage 1: PR-AUC, and recall of bad coins when blocking at most 30% of fine ones (the practical track's 90% / 30% target, as a reference); stages 2-3: PR-AUC, precision of the top 5 and top 20 coins per hour, calibration (Brier score and a calibration curve).
- **Uncertainty:** 95% intervals by resampling whole hours (coins in the same hour are not independent).
- **Reported separately:** mayhem vs. ordinary coins; each test day; with and without backfill-only features (4.1).
- **Every result is reported,** including models that lose to the baselines.
- *(Added 2026-11-14, closing R3.)* **Written down:** `config/evaluation.yaml` (2026-11-14, before any model), read by `src/agent/models/evaluate.py`. "The most recent quarter of the days" rounds up (9 whole days give 3 test days). Exploration (R3) and training use only rows launched before the validation days. With fewer than 4 whole days (2 test, 1 validation, 1 training) nothing is held out yet. Stage 2 is scored on coins still on the curve at each decision time (7).
- *(Added 2026-11-21, start of R5; decided by the user after the leak review and before any model was trained; evidence in `docs/PROGRESS.md`, "R5: models".)* **Tightened for the models:**
  - **Whole days only.** Models train on Nov 12-16, tune on Nov 17 and are tested on Nov 18-19 (UTC). The part-days at the edges of the backfill (Nov 11, Nov 20) are in no set.
  - **No label reads into a later set.** Labels look up to 65 min past launch (3,905 s). Coins launched that close before the validation or the test days are left out of the set they are in (`split.drop_label_overlap_s` in `config/evaluation.yaml`).
  - **Creator history over a fixed window.** Models read `dev_launches_24h` (the creator's launches in the 24 hours before the coin) instead of `dev_prior_launches`, which counts since the backfill began and so grows with the calendar. The dataset keeps the old column.
  - **Stage 1 is reported on two targets:** `bad` as defined in 7 (the main result), and `dead or rug` alone, because the bundled stand-in is already decided at T+60 s (one signal matches it 98.7-99.7% of the time from then on).
  - **Models cover SOL-paired coins** (5); coins priced in another coin are counted, not modeled.
  - **Backfill-only features (4.1)** are the priority fee, block position and compute units of the launch and of the buys so far (`features/backfill_only.py`; Jito tips are not stored). The models without them are the main results.
  - **Scored once, enforced:** `agent score-test` refuses to run when a test report exists, and `agent train` refuses to replace models the test days were scored with.

---

## 16. Known risks and pitfalls

- **Lookahead leakage** is the #1 failure mode. Enforce cutoffs in code and tests.
- **Survivorship bias:** keep every token, especially the dead ones.
- *(Added 2026-11-13, research track.)* **Few days, one market:** a week or two of history is one market regime, and coins launched in the same hour move together. Report results per day, give intervals that resample whole hours (15.4), and do not claim more than the days support.
- **Latency on free tier:** the agent will lag paid bots by seconds. Measure it; don't trust backtest fills blindly. *(Measured 2026-11-12, step B.)* The free public feed itself often delivers events 5-77 s after they happen (median about 6 s) and cuts the connection when far behind; two connections run late together, so a second one helps with drops, not with lateness. A verdict waits until the data up to its cutoff has arrived, so it comes late too; each verdict records how late (`wait_s`), and the paper ledger (step E) must enter at that time, not at the cutoff.
- **PC must be on and awake** while recording (in bursts now, continuously from Phase 12); gaps in recording mean missing data and slow backfills. Log every gap. *(Updated 2026-11-12, after Phase 1.)*
- **Stale history:** older pump.fun data reflects older fees, curves, and migration paths. Backfill recent months; down-weight older data.
- **Backfill vs. live mismatch:** if the backfill parser and live parser differ even slightly, the model learns patterns that don't exist live. The parity test is mandatory.
- **Missing enrichment for historical tokens:** websites and some APIs can't be reconstructed for past dates. Mark those features as missing in training instead of filling them with today's values (that would be lookahead).
- **Backfill size:** Old Faithful archives are very large; stream and filter, measure on one epoch first, and check free disk space.
- **Adversarial drift:** ruggers and bundlers adapt. Retrain often and watch for drops.
- **Free-tier limits:** cache aggressively; skip enrichment instead of crashing.
- **pump.fun fee/curve/instruction changes:** versioned config and parsers, never hardcoded.
  *(Added 2026-11-21, closing R4.)* **A program update inside the data:** pump.fun and
  PumpSwap were both updated on Nov 15, 2026 at about 9:20 AM PST (slot 469,811,025). The
  event layouts did not change (no unknown events and no new extra bytes across the
  backfill), but trading behaviour or fees may have. Training has about 31 h after the
  update; validation and test are entirely after it. The backtest (R6) uses fees read from
  the chain for after the update.
- **Padre changes:** irrelevant to the agent by design (no Padre dependency), but keep `padre_presets.yaml` in sync with the user's real settings.
- High-risk market. Real money only after a sustained positive paper record, and only amounts the user can afford to lose.

---

## 17. Reference: MemeChain paper (arXiv 2601.22185)

Mongardini & Mei, *MemeChain: A Multimodal Cross-Chain Dataset for Meme Coin Forensics and Risk Analysis* (2026). Dataset on Zenodo, CC-BY 4.0.

Used for:
- Off-chain effort features (8.5): missing logo, no real website, unreachable site, cheap hosting, budget registrars, domain reuse across tokens.
- "One-Day Meme Coin" as a failure label and survival analysis as an option (7, Stage 1).
- Meme keyword categories for narrative features (8.7).

Not used as training data: single Oct 2024 snapshot, aggregator-sourced (survivor-biased for Solana: only 0.09% one-day coins), no trade-level data. Note: Table 4 swaps the Ethereum and Solana token counts relative to the text.

---

## 18. Open items to verify before coding

- [ ] Helius free-tier credits/RPS, and whether `logsSubscribe` on the pump.fun program fits within them
- [ ] Current pump.fun create/buy/sell instruction layouts and event formats (versions such as `create_v2`)
- [ ] RugCheck free endpoints and rate limits
- [ ] Bitquery free sample scope
- [ ] Old Faithful: current free hosting status, per-epoch CAR size, Jetstreamer version and filtering options, download speed on the user's connection
- [ ] MemeTrans and Kaggle dataset licenses, date ranges, and whether their pump.fun mechanics match the current regime
- [ ] BigQuery Solana/pump.fun tables: current freshness and table names
- [ ] Free disk space on the machine running `agentd`, and whether to use an always-on machine for recording
- [ ] pump.fun fee schedule and curve parameters from the on-chain global account
- [ ] Clipboard library behavior on Windows (primary); macOS/Linux later
- [ ] Windows Terminal rendering of the Textual TUI; Task Scheduler auto-start; long-path and file-lock behavior for Parquet/DuckDB on Windows
- [ ] User's Padre settings to fill `padre_presets.yaml` (slippage, priority fee, typical TP/SL, typical buy size)

---

## 19. Change log

**2026-11-12** (after the section 18 verification; evidence in `docs/PROGRESS.md`). Every edit is marked in place with this date.

1. **Live feed (3, 4, 13, 15):** the public Solana WebSocket is the primary live source; Helius is for RPC lookups only, because its free credits cannot carry the pump.fun log stream.
2. **Backfill window (0, 4.1, 15):** 2-3 months stays the target, but the Phase 3 pilot sets the actual window, starting with 2-4 weeks. Jetstreamer is now maintained by Vaultic.
3. **Trade schema (5):** `sol_amount` became `quote_mint` + `quote_amount`; per-trade fee fields, instruction name and venue added. SOL-paired coins are modeled first.
4. **Backfill-only fields (4.1, 5, 8.2):** priority fee, Jito tip and in-slot position are nullable and kept out of model features.
5. **Regime changes (4.1):** the four pump.fun layout changes between 2026-09-12 and 2026-10-13 are listed; the parser must cover each.
6. **Slot timing (8.2):** early-buy features are also computed over fixed time windows, since slots are now ~215 ms.
7. **Fees (4, 10, 13):** fee and curve parameters are timestamped snapshots read from chain, not a hand-edited `fees.yaml`; per-trade recorded fees are preferred.

**2026-11-12, after Phase 1** (approved by the user; evidence in `docs/PROGRESS.md`, Phase 1). Marked in place with "after Phase 1" or "updated 2026-11-12".

8. **Event tables (5):** each on-chain event has its own table with pump.fun's IDL column names (`user` stored as `wallet`). Bonding-curve and PumpSwap trades are separate tables (`trades`, `amm_trades`) instead of one table with a venue column.
9. **Live coverage (2, 15):** the live recorder covers the bonding curve only; PumpSwap history comes from the backfill, because its live stream is about 83 GB/day.
10. **Mint check (12.3):** a mint ending in `pump` is a weak hint, not a rule (56% in a sample).
11. **Recorder schedule (3, 3.1, 15.2, 16):** record in bursts until Phase 12 and continuously from then on, since recording costs 22-54 GB of network data per day.

**2026-11-12, Phase 3** (approved by the user with the Phase 3 plan; evidence in `docs/PROGRESS.md`, Phase 3). Marked in place with "Phase 3".

12. **Backfill reader (4.1, 13, 15):** Old Faithful is read with the project's own Python reader instead of Jetstreamer (Rust, MSVC build tools and ClickHouse; not built for Windows). Same archive, same per-slot index, HTTP range requests.
13. **Parity test (4.1):** "identical trades" made precise: identical shared columns for rows both captured, recorder-only rows must be off chain, backfill-only rows reported as recorder misses.

**2026-11-12, after Phase 2: mayhem coins** (decided by the user; evidence in `docs/PROGRESS.md`, Phase 2). Marked in place with "after Phase 2".

14. **Mayhem coins (8.10, 10):** treated as their own category (`is_mayhem` feature, results reported separately, a separate model if needed). The mayhem agent's trades are kept everywhere, flagged per trade; the proposal to leave them out of backtests was declined.

**2026-11-12, during Phase 3: fast path** (decided by the user, who wants live trading feedback as soon as possible). Marked in place with "fast path".

15. **Build order (15.0):** after Phases 1 and 3, the engine goes always-on and rules-based live signals, the screen and the paper ledger (parts of 8, 9, 12, 13) are built before the models, while the full backfill runs in the background. Recording becomes continuous at step B instead of Phase 12 (3, 3.1, 15.2). Phase numbers, "done when" checks and the readiness criteria are unchanged.

**2026-11-12, step B (fast path)** (built in branch `step-b`; evidence in `docs/PROGRESS.md`, Step B). Marked in place with "step B".

16. **Gaps with several connections (3.2):** a gap is on-chain time no connection delivered. **Feed lateness (16):** measured, and verdicts record how late they came; the paper ledger enters at the verdict time. **Second connection (15.0):** on the same public server; about 130 GB a day. No requirement or "done when" check changed.

**2026-11-13, closing Phase 3** (approved by the user; evidence in `docs/PROGRESS.md`, Phase 3, "Pilot epoch 1084"). Marked in place with "closing Phase 3".

17. **Parity test (4.1):** rows whose slot differs count as matching when the chain confirms the backfill's slot (fork re-lands), and the recorder-only chain check may use a random sample with a stated 95% bound when checking all is too slow on the public RPC.

**2026-11-13, research track** (decided by the user: the project becomes a data science study rather than a tool to trade with). Marked in place with "research track".

18. **Purpose and goals (intro, 0, 2):** the project answers a research question on backfilled history and ends in a reproducible evaluation and write-up. The live tools built so far stay as a demo; paper trading, real-trade tracking, enrichers and packaging are paused. The practical track's text is kept so it can be resumed.
19. **Build order (15, 15.2, 15.3):** R0-R7 (close the live side, labels, dataset, exploration, 7 more days of history, models, backtest as analysis, write-up) replace the fast path. 15.0 and 15.1 keep their numbers and are marked paused.
20. **Evaluation (15.4):** a plan fixed in advance replaces the readiness criteria: time split with the test days scored once, base rate / rules / logistic regression baselines, set metrics, intervals by resampling whole hours, results reported separately for mayhem coins, test days, and with and without backfill-only features.
21. **Data (4.1, 16):** training uses the backfill only; recorder data describes the live feed. Backfill-only fields may be features, with every model also reported without them.
22. **Bundled label (7):** reported as a gap. The bundle cluster needs the early buyers' funding transfers, which the backfill does not keep, so `is_bundled_proxy` (other wallets' first-second buys holding ≥ 20% of supply at T+60s) stands in and is named as such. `is_dead` is reported with and without the mayhem agent's trades.

**2026-11-14, closing R1** (approved by the user; evidence in `docs/PROGRESS.md`, "R1: labels"). Marked in place with "closing R1".

23. **How labels are computed (7):** execution prices; price labels for SOL-paired coins only; the mayhem agent's trades left out of prices (`hit_2x` also stored with them); trades under 0.001 SOL not used as prices; the entry price includes impact on the curve but not fees; `is_rug` from the creator's wallet only; coins without 65 covered minutes get empty labels. No threshold changed.

**2026-11-14, closing R2** (approved by the user; evidence in `docs/PROGRESS.md`, "R2: dataset"). Marked in place with "closing R2".

24. **Repo layout (13):** the dataset builder is `src/agent/dataset/build.py` and writes `data/dataset/`, because the feature code must never import the labels. No requirement or "done when" check changed.

**2026-11-14, closing R3** (approved by the user; evidence in `docs/PROGRESS.md`, "R3: exploration"). Marked in place with "closing R3".

25. **Graduation target (7, 15.4):** stage 2 predicts graduating later in the hour, for coins still on the bonding curve at each decision time; coins that already graduated are counted and reported, not predicted. **Evaluation plan (15.4):** written to `config/evaluation.yaml`; the test quarter rounds up; exploration and training use only rows before the validation days. No threshold or "done when" check changed.

**2026-11-21, closing R4** (approved by the user; evidence in `docs/PROGRESS.md`, "R4: more history"). Marked in place with "closing R4".

26. **Program update (16):** pump.fun and PumpSwap were updated on Nov 15, 2026, inside the backfilled days. Event layouts unchanged; training has about 31 h after the update, validation and test none before it; the backtest uses fees read from the chain for after it. No threshold or "done when" check changed.

**2026-11-21, start of R5** (decided by the user after the leak review, before any model was trained; evidence in `docs/PROGRESS.md`, "R5: models"). Marked in place with "start of R5" or "R5".

27. **Evaluation plan tightened (15.4):** models use whole days only; coins launched within 65 min before the validation or the test days are left out, so no label reads into a later set; `dev_launches_24h` replaces `dev_prior_launches` in the models; stage 1 is also reported as dead or rug alone; SOL-paired coins are modeled; the backfill-only features are named; scoring the test days once is enforced by the commands. **Repo layout (13):** the model code's files. No threshold or "done when" check changed.

**2026-11-21, closing R6** (approved by the user; evidence in `docs/PROGRESS.md`, "R6: backtest as analysis"). Marked in place with "closing R6".

28. **Backtest (8.10, 10, 13, 7):** mayhem curve state is the reserves after the last recorded trade (the backfill has no reset records and needs none); results are reported under two views of our own buy, the strict one as the headline; signals buy at cuts fixed on the validation day by picks per hour; the new test settings in `padre_presets.yaml` and `config/backtest.yaml` are named, with Padre's fee at 1% as a test setting; the test days are simulated once; the backtest's files are listed; the labels' note on mayhem entry prices is corrected in wording only. No threshold or "done when" check changed.
