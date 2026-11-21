# Progress log

Spec: [SPEC.md](SPEC.md) (moved from the project root on 2026-11-12; edits are listed in its section 19).

## Status

| Phase | State | Notes |
|---|---|---|
| Pre-work: section 18 verification | Done 2026-11-12 | Results below. |
| 0. Setup | Done 2026-11-12 (user OK'd) | Results in "Phase 0" below. |
| 1. Parser + recorder | Done 2026-11-13 (user OK'd) | 24-hour audit: 0.54% of coins missed outside logged gaps (target under 1%). Results in "Phase 1" below. |
| 2. Curve math + tests | Done 2026-11-12 (user OK'd) | Built while Phase 1's 24-hour run continued (user's choice). Results in "Phase 2" below. Mayhem proposals decided 2026-11-12 (spec 8.10). |
| 3. Backfill pilot | Done 2026-11-13 (user OK'd) | Epoch 1084: size, disk and parse time measured; parity passes. Every difference came from the live feed (29 fork re-lands, all confirmed by the chain; 1.3% of trades that never landed, 1,000 of 1,000 sampled), not the archive reader. Results in "Phase 3" below. |

**Build order since 2026-11-13: the research track in spec 15.3** (user's decision: the
project becomes a data science study, not a tool to trade with; spec change log 18-22).
Training data is the backfill; the live tools built so far stay as a demo.

| Research step | State | Notes |
|---|---|---|
| R0. Close the live side | Done 2026-11-13 (user OK'd) | The user stopped the engine at 10:11 AM PST, Nov 13. The audit met Phase 1's check (7:03 PM); Phase 1 closed with the user's OK. Branch `step-b` brought into `main` by fast-forward (user's OK); the main folder's older uncommitted copies of the step C files, all superseded by `step-b`, were set aside in a labeled git stash. |
| R1. Labels | Done 2026-11-14 (user OK'd; branch `r1-labels`) | `agent labels`: 45,439 coins from epoch 1084, 43,022 with every label known. Results in "R1" below. |
| R2. Dataset | Done 2026-11-14 (user OK'd; branch `r1-labels`) | `agent dataset`: 181,756 rows (45,439 coins x 4 cutoffs), 172,088 to model on; rebuilt byte-identical. Results in "R2" below. |
| R3. Exploration | Done 2026-11-14 (user OK'd; branch `r1-labels`) | `notebooks/r3_exploration.ipynb` runs top to bottom on the dataset. Main finding: most graduations happen before the first decision time, so stage 2 now predicts graduating later, for coins still on the curve (spec 7). Results in "R3" below. |
| R4. More history | Done 2026-11-21 (user OK'd; option B; branch `r1-labels`) | Epochs 1085-1091: 2,755 GB downloaded in 11.3 h, 35 GB kept; one unbroken span of 206.6 h with no gaps. Dataset rebuilt: 367,730 coins, 1,464,896 rows to model on. Results in "R4" below. |
| R5. Models | Done 2026-11-21 (user OK'd; branch `r5-models`) | Leak review, baselines and LightGBM for stages 1-3; test days Nov 18-19 scored once. LightGBM beats every baseline in every stage; the backfill-only signals add nothing. Results in "R5" below and `docs/R5_RESULTS.md`. |
| R6. Backtest (analysis) | Done 2026-11-21 (user OK'd; branch `r6-backtest`) | Day plan and lookahead review reported first; test days Nov 18-19 simulated once. No signal made money after costs (every coin: -0.199 SOL per 1 SOL signal under the strict view, -0.068 under the generous one; the best model signal -0.106 and -0.017). Results in "R6" below and `docs/R6_RESULTS.md`. |
| R7. Write-up | Not started | |
| L1. Live model scores (demo) | Done 2026-11-21 (user OK'd; branch `claude/silly-payne-d9066b`) | The engine scores every new coin at T+30/60/120/300 s with the saved R5 LightGBM models; the screen shows the probabilities beside the rules' verdict. Analysis only. On the real feed, 10,602 saved score records over 2.3 h equal their replay to the last digit. Results in "L1" below. |

Build order from 2026-11-12 to 2026-11-13: the fast path in spec 15.0 (user's decision,
now paused). Steps: A close Phases 1 and 3, B engine always on, C live signals + rules
verdict, D screen, E paper ledger; the full backfill (Phase 4) runs in the background after A.

| Fast-path step | State | Notes |
|---|---|---|
| A. Close Phases 1 and 3 | Done 2026-11-13: both closed with the user's OK | See "Phase 1" and "Phase 3" below. |
| B. Engine always on | **Built 2026-11-12 in a separate copy (branch `step-b`); paused 2026-11-13** | Two live connections, gap log for several connections, continuous recording, uptime in `agentd status`. The 24-hour uptime test is not needed on the research track. Results in "Step B" below. |
| C. Live signals + rules verdict | **Built 2026-11-12 (branch `step-b`)** | The engine judges every new token at T+30/60/120/300 s; `agent check-live` compares live with replay. The same feature code builds the research dataset (R2). Results in "Step C" below. |
| D. Screen | **Built 2026-11-12 (branch `step-b`); kept as a demo** | `agent screen`: alert feed, paste box, clipboard watcher, status bar; the engine answers over a local connection. Results in "Step D" below. |
| E. Paper ledger | Paused 2026-11-13 (research track) | `padre_presets.yaml` is kept as settings for the R6 backtest. |

## L1: live model scores (demo, built 2026-11-21)

**Done when** (spec 15.3, step L1): scores saved with every verdict, the model files
checked against the R5 manifest; the screen shows each probability beside its usual rate
and says when a score is missing and why; live scores equal a replay of the recorded data
to the last digit over at least 2 h; a no-lookahead test covers the launch-context signals
in their live form; the engine still runs without the research packages.
**Result: met in every part** (the real-feed check ran 4:21 to 6:40 AM PST, Nov 21).
**Closed 2026-11-21 with the user's OK.**

This reopens a part of the live side that was paused on 2026-11-13. The user OK'd the plan
and the spec note on 2026-11-21 (spec change-log entry 29). **It is analysis, not advice:**
R6 found no signal that makes money after costs, and nothing here places a trade.

Built on branch `claude/silly-payne-d9066b` (cut from `r6-backtest`):
- `src/agent/scoring/launch_context.py`: the three launch-context signals counted live
  (`LaunchIndex`), and how much of the hour and of the 24 hours before a launch the
  history covers (`LaunchHistory`: the recording outside its logged gaps, plus the
  backfill).
- `src/agent/scoring/live.py`: loads the saved LightGBM models, checks every file
  against the SHA-256 in the R5 manifest, and scores one coin at one decision time.
- `src/agent/export_live.py` and `agent export-live-models` (research side): write the
  calibration maps as plain numbers to `models/r5_live/bundle.json`, then compare the
  engine's scoring with R5's own on the validation day.
- `src/agent/scoring/replay_check.py`, and `agent check-live` extended: saved scores are
  made again from the recorded data and must be identical.
- Engine (`daemon/live_state.py`, `daemon/main.py`): each verdict gets a `scores` column
  in `live/verdicts/`; `agentd status` has a `models:` line.
- Screen (`tui/view.py`): the analysis panel lists the scores at each decision time the
  coin has passed, each beside its usual rate, with the limits; the status bar says
  whether model scores are on.
- Setting: `live_models` in `config/settings.yaml` (`enabled`, `min_hour_covered` 0.95).
- Tests: 410 pass (28 new), lint clean.

### The five points settled with the user before building

| Point | What was done |
|---|---|
| Reopening paused live work | Spec note: optional step L1 in 15.3, an exception in the research-track note and the section 2 non-goal, change-log entry 29. |
| The launch-context signals, live | Counted from the launches on disk (recording and backfill) plus the live feed, from launches strictly before the coin's own. Each score saves the share of the hour and of the 24 h before the launch that was recorded. Under 95% of the hour: no score, with the reason. Under 100% of the 24 h: scored and marked "the creator's launch count is a lower bound" (the user's choice over "no score until 24 h exist"). |
| The wall between labels and live code | New package `scoring/`, added to both wall tests. A new test loads the engine, the screen and the scoring code in a fresh Python and fails if any label-side module was loaded. |
| Engine without the research packages | It starts and gives rule verdicts; `agentd status` and the screen say "model scores off" and why (the same test checks that LightGBM, scikit-learn, joblib and pandas are not loaded by starting the engine). |
| Limits shown | On the screen with every analysis: when the models were trained, that the market drifts, that the backtest found no profitable signal, how late each score came, and a mark when the creator history is short. |

### Why the calibration is exported

The R5 folder keeps each stage's calibration (and logistic regression) in
`models.joblib`. That file cannot be opened without loading `agent.models.train` (it
refers to a function there), and that module loads the dataset code and through it the
labels. The LightGBM models are plain text files. So the engine reads the LightGBM files
directly, and `agent export-live-models` writes the calibration maps once, as numbers,
with the hash of the `models.joblib` they came from. The engine checks that hash and the
LightGBM files against the R5 manifest and refuses anything else. The R5 folder is not
changed (a test compares every file's hash before and after). Only LightGBM is scored
live: it beat logistic regression in every stage (R5), and it keeps scikit-learn out of
the engine.

### Checks

| Check | Result |
|---|---|
| Tests | 410 pass (28 new), ruff clean |
| Engine's scoring = R5's own, same rows (validation day, Nov 17) | 4,108 rows through R5's scoring code and through the engine's scorer: 0 differ, raw score and probability, to the last digit |
| Engine's scoring = R5's own, from raw events (validation day) | 540 coins launched 6:00-6:20 AM UTC, Nov 17, replayed from the backfill through the live code (`TokenState`, `LaunchHistory`, the scorer): 2,160 rows, 0 differ; the live launch-context signals equal the dataset's in every row |
| Model files checked against the manifest | Tests: a changed LightGBM file, a changed `models.joblib`, a bundle made from other models, other signals, a broken calibration map and a missing bundle are each refused with a reason |
| No-lookahead, launch-context signals in their live form | Tests: 500 launches at or after a coin's own second change none of its three signals; in the engine, 900 launches arriving before the verdicts change no score's context at any decision time; the live counts equal the training SQL on 4,000 random launches |
| The wall | Tests: nothing in `scoring`, `daemon`, `tui` or `features` imports the labels, the dataset, the training frame, the training code or the backtest; a fresh Python that loads the engine, screen and scoring code holds none of them |
| Engine with and without the models | Tests: a real engine run against a fake feed starts with model scores on when the files are there, and with one model file changed it still starts, scores off, naming the file; no LightGBM or no model files gives a reason; verdicts go on; a scoring error costs no verdict |
| Live scores = replay, recorded fixtures | Tests: real transactions through the recorder and the scorer; `agent check-live` finds every saved score identical to the last digit, and reports a score changed by 0.000000000001 |
| Live scores = replay, real feed, at least 2 h | **Pass.** `agent check-live --hours 3` at 6:40 AM PST: 10,602 verdicts carry a score record, 8,931 of them with probabilities; all 10,602 made again from the recorded data are identical to the last digit, 0 different, over 2.29 h of verdicts. The signals of all 13,034 verdicts of those 3 h are identical too. See "The run on the real feed" |
| Earlier results untouched | No file under `features/`, `models/`, `sim/`, `dataset/`, `labels/` or `backtest/` changed, so the code hashes recorded with the dataset and frozen with the backtest are the same. The test days were not scored or simulated again: the usual rates are read from the saved report |

Dry run on the real recording (read-only, 4:06 AM PST, Nov 21, while the engine of the
main folder was recording): the models load in about 2 s and the launch history (368,175
launches, 329 logged gaps) in about 3.5 s; scoring takes about 0.35 ms per verdict. Of
378 coins launched in the half hour before, 46 (12%) were not paired with SOL and get no
score. The recording had started 30 minutes earlier, so every other coin had under 41% of
its last hour recorded and gets no score yet, as intended; and 5% of its last 24 hours
(the backfill ends Nov 20, 4:52 AM PST).

### What the screen shows

```
<MINT>  $SYM · age 5m 00s · curve 0% · mc 28.0 SOL · data as of 3s ago
CAUTION: <the rules' reasons>
...
verdicts so far: T+30s caution · T+60s caution · T+120s caution · T+300s caution
model scores, as analysis and not advice (usual = how often it happened on the test days Nov 18-19, 2026, same time, same kind of coin):
  T+30s, 5s late: bad 98% (usual 87%) · graduates later 14% (usual 1.2%) · 2x before half 20% (usual 2.8%)
  T+60s, 5s late: bad 94% (usual 87%) · graduates later 0.2% (usual 1.1%) · 2x before half 0.4% (usual 2.4%)
! models trained on Nov 12-16, 2026; the market drifts, and the backtest found no signal that makes money after costs
! creator history: 5% of the 24 h before launch recorded, so the creator's launch count (0) is a lower bound
```

(A real coin of this morning, scored in the dry run with the one-hour minimum switched off
for the preview.) Without a score the line gives the reason, for example
`T+60s, 31s late: no score: only 40% of the hour before its launch was recorded (95%
needed to count the launches in it)`.

### Findings

1. **A short creator history matters.** On the training days only 37% of coins came from
   a creator with no launch in the 24 hours before (median 6 earlier launches, 90th
   percentile 154), and for 48% the 24-hour count is larger than the 1-hour count. Its
   share of the models' SHAP values is 5-6% in stages 1-2 and 0.6% in stage 3. So scores
   in the first day after a start are somewhat off for serial launchers, which is why
   they are marked.
2. **`agent check-live` no longer worked on this data folder.** It read the recording
   together with the backfill, and matching every recorded event against 35 GB of history
   runs out of memory. It now replays the recording only, which is what the engine saw
   (20 minutes of coins in 11 s). `agent signals` reads both in the same way and has the
   same problem; it is not changed here.
3. **About one coin in eight is not paired with SOL** in this morning's sample and gets
   no score (the models cover SOL-paired coins, spec 5).

### Limits

- **Trained on five days** (Nov 12-16, 2026), tested on two. Nothing here measures how
  fast the models age; the live scores are not evaluated against outcomes.
- **Scores come late.** The free feed often runs 5 to 77 s behind, and a score waits for
  its data (`wait_s` in each verdict).
- **Live launches are not exactly the backfill's.** The feed delivers at "confirmed", so
  a few launches it counts never land on chain (Phase 3 found 1.3% of trades), and
  launches missed in gaps are not counted. The coverage shares say how much was recorded,
  not that the count is exact.
- **Only the four decision times.** Between them, and after T+300 s, there is no new score.
- **No profitable signal** (R6). The scores describe; they do not recommend.

### The run on the real feed (Nov 21, 4:21 to 6:40 AM PST; restart OK'd by the user)

The engine that ran the main folder's code (rule verdicts only, since 3:36 AM) was stopped
cleanly at 4:20:42 AM and started from this branch's code. The public server refused new
connections for about 35 s after the stop, so the restart left a 47 s gap (logged).

| | |
|---|---|
| Verdicts made in the run (to 6:40 AM) | 10,690, none touched by a gap (24 reconnects, each covered by the other connection) |
| With probabilities | 9,002 verdicts on 2,280 coins, from 4:34:39 to 6:40:20 AM (2.09 h) |
| Without, and why | Not paired with SOL (about 10% of verdicts); too little of the last hour recorded (the first 13 minutes after the restart: the recording began at 3:36 AM) |
| How late a score came | Median 3.4 s after its decision time, 90% within 5.1 s, the latest 32 s |
| Creator history behind the scores | 5% to 13% of the 24 h before launch recorded, so every score of this run carries the "lower bound" mark |
| `agent check-live --hours 3` | 10,602 score records made again from the recorded data: 10,602 identical to the last digit, 0 different (19 s to run) |
| Network | 5.9 GB in 2 h 19 m, about 2.5 GB an hour |

The scores look like the models' usual output, which is all this says (they are not
checked against outcomes, which lie in the future of the run): the mean predicted chance
of "2x before half" was 7.3%, 4.1%, 2.3% and 1.2% at the four decision times, beside test
day rates of 6.3%, 3.6%, 1.9% and 1.0%; the mean predicted chance of "bad" was 88-90%
(test days 93%). 60% of the scored coins were mayhem coins.

An earlier check, five minutes after the restart, had already compared the first 116
score records, none with probabilities yet: identical, so the launch counts and the
recorded-history shares are the same live and replayed.

The engine was left running on this branch's code (started from the worktree folder, with
a `.env` there that holds only `DATA_DIR`). After this branch is brought into the main
folder, restart it from there: `agentd stop`, then `agentd start`.

## R6: backtest as analysis (research track, built and closed 2026-11-21)

**Done when:** "After-cost P&L per signal with delay sensitivity, per day, mayhem vs.
ordinary." **Result: met in every part; closed 2026-11-21 with the user's OK.** Every number
(all signals, decision times, delays, exit plans, days, ordinary / mayhem, with intervals)
is in [R6_RESULTS.md](R6_RESULTS.md); the tables here are the main ones.

**Short answer: no signal made money after costs on the test days.** Under the strict
view of our own buy every signal loses 5% to 22% of the 1 SOL stake per signal; under the
generous view the best signals lose 1-2%, with intervals from about -0.10 to +0.08. The
models do pick better coins (their buys hit the take-profit 23-31% of the time against 3%
for buying everything), but a stop-loss that is hit 55% of the time costs more than the
winners bring.

Built on branch `r6-backtest`:
- `src/agent/sim/pool_math.py`: PumpSwap pool math (buy with a budget, buy exact tokens,
  sell; three fees), and a check of it against every recorded pool trade.
- `src/agent/sim/market.py`: a coin's reserves after every trade, in exact order, on its
  curve and in its pool, with the fees each trade paid; read from the backfill only.
- `src/agent/sim/backtest.py`: the simulator (buy, exit plans, sell, slippage, failed
  orders, the two views of our own buy) and profit and loss per simulated trade.
- `src/agent/backtest/run.py`, `report.py`: signals, the cuts, the ledger, the report and
  the once-only guard. Command: `agent backtest validation | train | test`.
- `config/backtest.yaml` (signals); new test settings in `config/padre_presets.yaml`
  (failed orders 5%, also 0% and 10%; quote age 2 s; at most 4 failed sells; Padre fee
  1% with 0% as the what-if), as the user chose on 2026-11-21.
- Tests: 382 pass (34 new), lint clean.

### Step 1: day plan and lookahead review, before any simulation (reported to the user first)

Facts checked first, on Nov 12-17 only (no test-day trade was read):

| Check | Result |
|---|---|
| Curve fees people pay | 0.95% + 0.30% creator fee on every training day, before and after the Nov 15 update. Holder rewards (30 bps on about a third of trades) are part of those fees, not on top (buyers' totals are round numbers without them). On the rare cashback coins the 30 bps cashback fee replaces the creator fee and is on top of the 0.95%. |
| Mayhem reserve resets | The backfill has no `mayhem_params` table, and does not need one: all 14.9 million trades by people on Nov 12-17, including 3.8 million on mayhem coins, start exactly from the reserves the previous recorded trade reported. The resets come with the mayhem agent's own trades (97% of its trades start from other reserves than the previous trade left). So a person's fill on a mayhem coin is as exact as on an ordinary one. |
| PumpSwap pools | Trades follow a constant product of the pool's token reserve and its quote reserve **plus the virtual quote reserve**; the fees (liquidity, protocol, creator) are recorded per trade (about 1.2% in total). To be verified on every recorded pool trade when the simulator is built. |
| Solana's base fee | 5,000 lamports per signature (the lowest fee per signature in the data). |

**Proposed day plan** (to keep the test days honest):
1. Models and their settings are frozen (R5). A buy signal is a score at or above a fixed
   cut. "Top 5 per hour" cannot be traded as it is (the hour's top 5 are only known when
   the hour is over), so each cut is the score that gave 5 (and 20) picks per hour on
   average on **Nov 17**, per decision time. No P&L is used to choose anything.
2. The simulator is built, tested and checked on Nov 12-17 only.
3. Cuts, exit plans, delays and costs are written to `config/backtest.yaml`; its hash and
   the code's go in the manifest. Then Nov 18-19 are simulated **once**: `agent backtest
   --test` refuses to run when a test report exists (as `agent score-test` does).
4. Reported: Nov 18-19 (the result); Nov 17 marked as the day the cuts come from; Nov 12-16
   only for the signals that were never fitted (buying everything, the rules), for
   stability across days.
5. If a mistake is found after the test run, both the old and the corrected numbers are
   reported.

**Lookahead review of the simulator design:**

| Part | Rule | Lookahead? |
|---|---|---|
| Signal | Features at T+k read events up to T+k (R2, R5); the cut is fixed beforehand | No |
| Entry | At T+k+delay, on the reserves after the last trade at or before that second (curve, or pool for a graduated coin), exact math, the fees of the last person's trade at or before it | No |
| Slippage check | The quote is the price a little earlier (a setting); the buy fails if the fill is worse than the limit | No |
| Exit trigger | Only trades after the entry second; take-profit / stop-loss on the price after each trade against the price paid | No |
| Exit fill | The sell lands the same delay after its trigger (a time stop at its planned time), on the reserves after the last trade at or before then | No |
| Graduated before the exit | Sold in the PumpSwap pool with the pool formula and the pool's recorded fees | No |

**Limits found, to decide or name:**
- **Our own buy is not in the market.** With 1 SOL on a new curve (30 SOL deep) this is
  not small: selling from reserves that lack our own buy costs about 6% of the position
  (a coin nobody else trades would show -8.6% instead of the true -2.5%). Proposed: report
  two versions side by side, "our buy leaves no trace" (strict, the headline) and "our
  SOL stays in the curve" (exact when nobody else trades; optimistic when others sell).
- Mayhem coins are reported separately (spec 8.10, 10), though their fills are exact.
- Not in the data: failed transactions, Padre's own fee (`platform_fee_pct` is empty), the
  rent of a token account. `padre_presets.yaml` has no failed-transaction rate yet.

**The user's decisions (2026-11-21):** the day plan as proposed; both views of our own
buy, the strict one as the headline; failed orders 5% (also 0% and 10%); a sell lands one
delay after its trigger; **Padre's fee 1% in the headline, 0% as the what-if** (the plan
proposed the reverse). The user did not state Padre's real fee: 1% is a cautious test
setting, not a measured cost.

**The fee headline was switched after the test days had been simulated.** The user's
pick of 1% was made on the plan, in the R5 session, and reached this session only after
the run (which had used 0% as the headline and 1% as the what-if, on the "Yes, go ahead"
given here). Nothing was simulated again: the fee changes no simulated trade, only what
is taken off each result. `agent backtest <days> --resummarize` built the three reports
again from the same ledger files (their hashes are checked; any other change is refused;
tested), and the reports it replaced are kept beside them
(`*_report_before_20261121T11....json`). Both fee settings were in the first report and
are in this one; they only swapped columns.

Decided while building, all before any test-day result (each is in the settings files or
the module notes):
- **Signals:** every coin; the rules (pass; pass or caution); LightGBM stage 1 as a filter
  (not blocked, at the validation day's "at most 30% of fine coins blocked" cut); logistic
  regression and LightGBM for 2x-before-0.5x, and LightGBM for graduation, each at about 5
  and about 20 picks per hour. The "+ backfill-only" models are left out (R5: no gain).
- **Also reported:** a coin's first signal only (buying a coin once).
- **A curve cannot pay out more than it holds** (the SOL it recorded plus what our buy put
  in). Found on the validation day: mayhem coins' price-setting reserves are sometimes
  reset far above their real SOL. It lowered "every coin" on Nov 17 from -0.174 to -0.190
  SOL (at a 0% Padre fee); ordinary coins did not change.
- **A buy that would take every token left on the curve** stops there and is flagged. On
  mayhem coins the reserves are often reset so low that 1 SOL would do this (7% of mayhem
  signals, half of the graduation model's mayhem picks).

### Step 2: checks on Nov 12-17, before the test days were simulated

| Check | Result |
|---|---|
| Tests | 382 pass. New: pool math against four real pool trades; a curve buy and a round trip worked out by hand (1 SOL on a new curve: 987,654,320 lamports into the curve, 34,199,203,120,618 tokens, fees 9,382,717 + 2,962,963); take-profit, stop-loss, time stop; slippage; failed orders; the move to the pool; the payout cap; the cuts; the once-only guard; the report's numbers against the ledger by hand. |
| No lookahead (tests) | A trade after the buy's second changes nothing in the buy; a trade after the last sell changes nothing in the row; a trade in the sell's own second does change it. Cutting a market's data at any second gives the same reserves and fees there as the whole market. |
| Real fills, curve | On a 3-hour slice of Nov 16: pricing each real order with the simulator's own inputs (reserves after the previous trade, fees of the last paying trade before it) reproduces the chain's numbers for 81,954 of 81,954 set-budget buys and 99,310 of 99,310 sells, mayhem coins included. 436 of those buys were a coin's first trade, priced with fees from other coins' first trades. |
| Real fills, pool | Nov 17: sells 100.000% exact (2,314,137), buys of exact tokens 99.9997%; 99.999% of pool trades start where the previous one ended. For buys with a budget the tokens for the SOL that reached the pool are exact in 100%; in 3% the buyer's budget was 0.5-1.6% larger than what the pool and its three fees took (most likely a trading app's own fee; my reading, the data does not say). The fees the simulator would use in a pool equal the fees the next trade paid in 97.8%. |
| Same result when rebuilt | The validation day built twice: 8 ledger files byte-identical, all 8,100 report cells identical. Also a test. |
| Largest win looked at | +76 SOL on 1 SOL in 5 minutes: an ordinary coin that graduated and kept rising in its pool. Real, not an error; such coins move the averages. |
| Frozen | `frozen.json`: hashes of both settings files, the cuts, the code and the R5 model files. `agent backtest test` refuses when any changed, and refuses a second run (tested). |

### Test days Nov 18-19, simulated once (3:10 AM PST, Nov 21)

65,865 coins (30,486 ordinary, 35,379 mayhem), 263,460 coin and decision-time rows,
2,371,140 simulated trades. Numbers are profit or loss in SOL per signal on a 1 SOL buy
(-0.100 = 10% of the stake lost), all four decision times together, 5 s delay; in
brackets, 95% intervals from resampling whole launch hours. The reports were built again
from the same trades at 3:20 AM PST with the 1% Padre fee in the headline (see above).

**Headline: our buy leaves no trace (strict), 5% of orders fail, Padre fee 1%.**

| Signal (rows bought) | `double_or_half` | `quick_50_25` | `hold_5_min` |
|---|---|---|---|
| Every coin (263,460) | -0.199 (-0.204 to -0.194) | -0.190 (-0.195 to -0.185) | -0.222 (-0.228 to -0.216) |
| Rules: pass (8,297) | -0.108 (-0.112 to -0.104) | -0.106 (-0.110 to -0.103) | -0.106 (-0.109 to -0.103) |
| Rules: pass or caution (107,496) | -0.176 (-0.182 to -0.169) | -0.162 (-0.168 to -0.157) | -0.208 (-0.217 to -0.199) |
| LightGBM stage 1: not blocked (47,825) | -0.131 (-0.137 to -0.123) | -0.121 (-0.126 to -0.115) | -0.124 (-0.128 to -0.120) |
| Logistic 2x, about 5 per hour (771) | -0.122 (-0.217 to -0.005) | -0.094 (-0.156 to -0.012) | -0.173 (-0.257 to -0.071) |
| Logistic 2x, about 20 per hour (3,142) | -0.083 (-0.151 to +0.003) | -0.048 (-0.101 to +0.021) | -0.104 (-0.164 to -0.034) |
| LightGBM 2x, about 5 per hour (758) | -0.140 (-0.238 to -0.045) | -0.111 (-0.170 to -0.047) | -0.126 (-0.237 to -0.017) |
| LightGBM 2x, about 20 per hour (2,842) | -0.131 (-0.184 to -0.067) | -0.080 (-0.114 to -0.037) | -0.146 (-0.202 to -0.080) |
| LightGBM graduation, about 5 per hour (807) | -0.145 (-0.231 to -0.049) | -0.123 (-0.191 to -0.044) | -0.155 (-0.260 to -0.036) |
| LightGBM graduation, about 20 per hour (3,011) | -0.106 (-0.182 to -0.013) | -0.097 (-0.154 to -0.026) | -0.142 (-0.213 to -0.060) |

**The generous view: our SOL stays in the reserves** (same fee and failed orders).

| Signal | `double_or_half` | `quick_50_25` | `hold_5_min` |
|---|---|---|---|
| Every coin | -0.068 (-0.071 to -0.065) | -0.059 (-0.061 to -0.057) | -0.084 (-0.086 to -0.082) |
| Rules: pass | -0.053 (-0.057 to -0.049) | -0.051 (-0.055 to -0.048) | -0.051 (-0.054 to -0.048) |
| Rules: pass or caution | -0.079 (-0.083 to -0.075) | -0.066 (-0.069 to -0.063) | -0.101 (-0.106 to -0.096) |
| LightGBM stage 1: not blocked | -0.079 (-0.085 to -0.071) | -0.069 (-0.074 to -0.063) | -0.072 (-0.076 to -0.067) |
| Logistic 2x, about 5 per hour | -0.102 (-0.196 to +0.016) | -0.072 (-0.134 to +0.011) | -0.152 (-0.238 to -0.049) |
| Logistic 2x, about 20 per hour | -0.054 (-0.122 to +0.033) | -0.018 (-0.072 to +0.050) | -0.071 (-0.131 to -0.002) |
| LightGBM 2x, about 5 per hour | -0.120 (-0.220 to -0.025) | -0.090 (-0.151 to -0.026) | -0.106 (-0.218 to +0.005) |
| LightGBM 2x, about 20 per hour | -0.104 (-0.159 to -0.040) | -0.052 (-0.085 to -0.010) | -0.113 (-0.169 to -0.047) |
| LightGBM graduation, about 5 per hour | -0.096 (-0.184 to +0.000) | -0.074 (-0.140 to +0.008) | -0.103 (-0.207 to +0.014) |
| LightGBM graduation, about 20 per hour | -0.017 (-0.095 to +0.079) | -0.008 (-0.066 to +0.062) | -0.040 (-0.110 to +0.044) |

**Delay sensitivity** (`double_or_half`, headline):

| Signal | 2 s | 5 s | 10 s |
|---|---|---|---|
| Every coin | -0.197 (-0.201 to -0.191) | -0.199 (-0.204 to -0.194) | -0.203 (-0.208 to -0.197) |
| Rules: pass | -0.108 (-0.112 to -0.105) | -0.108 (-0.112 to -0.104) | -0.108 (-0.112 to -0.105) |
| LightGBM stage 1: not blocked | -0.132 (-0.138 to -0.125) | -0.131 (-0.137 to -0.123) | -0.130 (-0.137 to -0.122) |
| Logistic 2x, about 20 per hour | -0.088 (-0.152 to -0.016) | -0.083 (-0.151 to +0.003) | -0.097 (-0.173 to +0.007) |
| LightGBM 2x, about 5 per hour | -0.139 (-0.242 to -0.036) | -0.140 (-0.238 to -0.045) | -0.139 (-0.226 to -0.050) |
| LightGBM 2x, about 20 per hour | -0.142 (-0.190 to -0.089) | -0.131 (-0.184 to -0.067) | -0.132 (-0.194 to -0.054) |
| LightGBM graduation, about 20 per hour | -0.137 (-0.192 to -0.074) | -0.106 (-0.182 to -0.013) | -0.112 (-0.205 to +0.013) |

**Per day** (`double_or_half`, headline; signals in square brackets):

| Signal | Nov 18 | Nov 19 |
|---|---|---|
| Every coin | -0.199 (-0.206 to -0.192) [139,672] | -0.200 (-0.208 to -0.193) [123,788] |
| Rules: pass | -0.109 (-0.116 to -0.103) [4,549] | -0.107 (-0.112 to -0.101) [3,748] |
| LightGBM stage 1: not blocked | -0.134 (-0.140 to -0.128) [26,009] | -0.126 (-0.138 to -0.111) [21,816] |
| Logistic 2x, about 20 per hour | -0.077 (-0.159 to +0.016) [1,511] | -0.088 (-0.198 to +0.048) [1,631] |
| LightGBM 2x, about 5 per hour | -0.179 (-0.310 to -0.036) [373] | -0.102 (-0.228 to +0.034) [385] |
| LightGBM 2x, about 20 per hour | -0.170 (-0.226 to -0.113) [1,400] | -0.092 (-0.186 to +0.016) [1,442] |
| LightGBM graduation, about 5 per hour | -0.230 (-0.321 to -0.133) [383] | -0.068 (-0.191 to +0.067) [424] |
| LightGBM graduation, about 20 per hour | -0.135 (-0.198 to -0.068) [1,481] | -0.078 (-0.208 to +0.083) [1,530] |

**Ordinary vs. mayhem coins** (`double_or_half`; signals in square brackets):

| Signal | Ordinary, strict | Mayhem, strict | Ordinary, generous | Mayhem, generous |
|---|---|---|---|---|
| Every coin | -0.111 (-0.116 to -0.105) [121,944] | -0.276 (-0.280 to -0.271) [141,516] | -0.058 (-0.062 to -0.052) | -0.078 (-0.079 to -0.076) |
| Rules: pass or caution | -0.111 (-0.115 to -0.108) [78,374] | -0.348 (-0.355 to -0.342) [29,122] | -0.057 (-0.060 to -0.053) | -0.140 (-0.145 to -0.135) |
| LightGBM stage 1: not blocked | -0.129 (-0.136 to -0.122) [47,556] | -0.335 (-0.406 to -0.253) [269] | -0.079 (-0.085 to -0.071) | -0.076 (-0.111 to -0.028) |
| Logistic 2x, about 20 per hour | -0.069 (-0.140 to +0.024) [2,908] | -0.259 (-0.334 to -0.178) [234] | -0.045 (-0.117 to +0.048) | -0.165 (-0.233 to -0.088) |
| LightGBM 2x, about 5 per hour | -0.140 (-0.238 to -0.045) [758] | - [0] | -0.120 (-0.220 to -0.025) | - |
| LightGBM 2x, about 20 per hour | -0.117 (-0.176 to -0.046) [2,597] | -0.280 (-0.347 to -0.206) [245] | -0.095 (-0.156 to -0.025) | -0.193 (-0.251 to -0.127) |
| LightGBM graduation, about 20 per hour | -0.036 (-0.136 to +0.089) [2,261] | -0.316 (-0.341 to -0.289) [750] | -0.010 (-0.111 to +0.117) | -0.039 (-0.056 to -0.022) |

**Other assumptions** (`double_or_half`, all coins; one change from the headline at a time):

| Signal | Headline | 0% of orders fail | 10% of orders fail | Padre fee 0% |
|---|---|---|---|---|
| Every coin | -0.199 (-0.204 to -0.194) | -0.210 (-0.215 to -0.204) | -0.189 (-0.194 to -0.184) | -0.183 (-0.188 to -0.177) |
| Rules: pass | -0.108 (-0.112 to -0.104) | -0.114 (-0.118 to -0.110) | -0.103 (-0.107 to -0.099) | -0.090 (-0.094 to -0.086) |
| LightGBM stage 1: not blocked | -0.131 (-0.137 to -0.123) | -0.138 (-0.144 to -0.130) | -0.123 (-0.129 to -0.116) | -0.113 (-0.119 to -0.105) |
| Logistic 2x, about 20 per hour | -0.083 (-0.151 to +0.003) | -0.092 (-0.165 to -0.001) | -0.067 (-0.133 to +0.016) | -0.065 (-0.134 to +0.021) |
| LightGBM 2x, about 5 per hour | -0.140 (-0.238 to -0.045) | -0.141 (-0.242 to -0.045) | -0.134 (-0.225 to -0.046) | -0.122 (-0.221 to -0.026) |
| LightGBM 2x, about 20 per hour | -0.131 (-0.184 to -0.067) | -0.138 (-0.192 to -0.073) | -0.114 (-0.171 to -0.045) | -0.113 (-0.167 to -0.049) |
| LightGBM graduation, about 20 per hour | -0.106 (-0.182 to -0.013) | -0.119 (-0.197 to -0.024) | -0.088 (-0.166 to +0.007) | -0.090 (-0.167 to +0.003) |

**What happened to the buys** (`double_or_half`, headline, all four decision times):

| Signal | Bought | Of those in profit | Take-profit | Stop-loss | Time stop | Median |
|---|---|---|---|---|---|---|
| Every coin | 93% | 3.1% | 3.0% | 13% | 77% | -0.106 |
| Rules: pass | 95% | 3.0% | 0.9% | 0.2% | 94% | -0.105 |
| LightGBM stage 1: not blocked | 95% | 4.7% | 2.8% | 5.9% | 86% | -0.120 |
| Logistic 2x, about 20 per hour | 93% | 29% | 23% | 57% | 13% | -0.536 |
| LightGBM 2x, about 5 per hour | 95% | 36% | 31% | 55% | 8.6% | -0.541 |
| LightGBM 2x, about 20 per hour | 93% | 31% | 25% | 56% | 12% | -0.529 |
| LightGBM graduation, about 20 per hour | 91% | 21% | 19% | 65% | 7.3% | -0.537 |

**How to read it:**
- **Nothing is profitable.** Under the strict view every signal loses 5% to 22% of the
  stake per signal. Under the generous view the best cells (LightGBM graduation at about
  20 per hour: -0.017 and -0.008) are still below zero, with intervals of about +/-0.08
  that reach above it. The truth is between the two views.
- **The models find the movers but not a profit.** Their picks hit +100% before -50% in
  23-31% of buys (R5's precision said 27-38%), but the stop-loss is hit in about 55% and
  lands late (one delay after the trigger, in a falling market), so the typical model
  trade loses about half the stake.
- **Buying a coin nobody trades costs 10.5% (strict) or 4.5% (generous):** that is the
  floor for "every coin" and "rules: pass". Of it, 2.5% is pump.fun's fees, about 1.9% the
  1% Padre fee on the buy and on the sale, 0.1% transaction fees, and in the strict view
  another 6% is the footprint limit.
- **Delay barely matters** between 2 s and 10 s: the differences are inside the intervals.
- **The two days agree for the unfitted signals** and differ for the model signals (Nov 18
  worse), with wide intervals: a few hundred trades a day is little.
- **Mayhem coins are worse in every row,** and their strict and generous numbers are far
  apart (their reserves are small after resets, so 1 SOL is a large trade). The LightGBM
  2x signal at 5 per hour picked no mayhem coin on the test days.
- **Fewer failed orders is worse, not better** (0%: -0.210; 5%: -0.199; 10%: -0.189 for
  every coin): a failed buy avoids a trade that loses on average.
- **Padre's fee:** the 1% in the headline is the user's cautious test setting, not a
  measured cost. Without it every number is 0.016-0.018 better per signal ("Padre fee 0%"
  above), which changes no conclusion.

### Training days Nov 12-16: the unfitted signals, for stability

165,664 coins, 5,963,904 simulated trades. `double_or_half`, headline, all four decision times, 5 s delay. The models learned from these days, so only the signals that were never fitted are shown.

| Signal | Nov 12 | Nov 13 | Nov 14 | Nov 15 | Nov 16 | All five days | Generous view, all five days |
|---|---|---|---|---|---|---|---|
| Every coin | -0.195 (-0.201 to -0.189) | -0.202 (-0.207 to -0.197) | -0.209 (-0.214 to -0.204) | -0.202 (-0.209 to -0.197) | -0.198 (-0.204 to -0.192) | -0.201 (-0.204 to -0.199) | -0.072 (-0.073 to -0.071) |
| Rules: pass | -0.106 (-0.111 to -0.101) | -0.091 (-0.109 to -0.067) | -0.106 (-0.114 to -0.093) | -0.114 (-0.118 to -0.110) | -0.114 (-0.118 to -0.109) | -0.106 (-0.111 to -0.101) | -0.051 (-0.056 to -0.046) |
| Rules: pass or caution | -0.176 (-0.184 to -0.168) | -0.191 (-0.200 to -0.184) | -0.199 (-0.206 to -0.192) | -0.186 (-0.196 to -0.178) | -0.178 (-0.186 to -0.172) | -0.186 (-0.190 to -0.182) | -0.085 (-0.087 to -0.083) |

Ordinary / mayhem, every coin: -0.118 (-0.120 to -0.116) / -0.271 (-0.274 to -0.268). The same picture on every day, before and after the Nov 15 program update: no day on which buying everything, or what the rules pass, made money.

### Limits

- **Our own buy is not in the market.** The strict and generous views bracket it; a full
  replay of every later trade on a curve that holds our buy was not built.
- **Two test days, one market,** both after the Nov 15 program update. Means are moved by
  rare coins that rise 50-fold; the intervals only cover hour-to-hour variation.
- **Mayhem coins:** fills are exact, but what a 1 SOL buy would do to a curve whose
  reserves the mayhem program resets cannot be known; 6.6% of mayhem signals would have
  taken every token left.
- **Guesses in the settings:** failed orders, the quote age, the priority fee, and Padre's
  fee (1% is the user's cautious choice; the real fee has not been checked).
- **Pool fees** are taken from the pool's recent trades (right 97.8% of the time; the rest
  differ by a few hundredths of a percent). Token-account rent is not counted.
- **Exit plans were not tuned.** The three in `padre_presets.yaml` were guesses from before
  any data; a better plan may exist, and finding one on these days would be overfitting.
- **Thresholds by count, not by value:** cuts were set by picks per hour. A cut chosen by
  expected value on the validation day was not tried.

### Spec notes (added 2026-11-21 with the user's OK; spec change-log entry 28)

1. **Spec 10 and 8.10, mayhem reserves:** "curve state uses the recorded reserve resets
   (`mayhem_params`)" cannot be done as written (the backfill has no such table) and is
   not needed: the reserves after the last recorded trade are exact. Replace with that.
2. **Spec 10, our own buy:** add that results are reported under two views (no trace;
   our SOL stays), with the strict one as the headline.
3. **Spec 10, settings:** name the new `padre_presets.yaml` settings (failed orders, quote
   age, sell retries, Padre's fee at 1% as a test setting) and `config/backtest.yaml`.
4. **Spec 13, layout:** the backtest is `sim/backtest.py`, `sim/market.py`,
   `sim/pool_math.py` and `backtest/run.py`, `backtest/report.py`; there is no
   `paper_trader.py` on this track.
5. **Spec 7, note 5 and `labels/make_labels.py`:** the note that mayhem entry prices are
   "less exact" is out of date (labels unchanged; only the wording). Corrected in the
   spec. The module's own note is left as it is: that file's hash is recorded with the
   dataset the models were trained on, so it is better changed with the next rebuild.

## R5: models (research track, built and closed 2026-11-21)

**Done when:** "The 15.4 report, with the test days scored once; SHAP and leakage
checks." **Result: met in every part; closed 2026-11-21 with the user's OK.** Every number of
the report (all stages, models, decision times, ordinary / mayhem, each test day, with
intervals) is in [R5_RESULTS.md](R5_RESULTS.md); the tables here are the main ones.

Built on branch `r5-models`:
- `src/agent/models/evaluate.py`: the model split (whole days, labels that reach a later
  set left out) and the plan's metrics with intervals that resample whole launch hours.
- `src/agent/models/frame.py`: the rows and columns a model may read (never an id, a rules
  column or a label; a test checks), and three signals known at launch.
- `src/agent/features/backfill_only.py`: six backfill-only signals (priority fee, block
  position, compute units of the launch and of the buys so far), with a no-lookahead test.
- `src/agent/models/train.py`, `report.py`: baselines, logistic regression, LightGBM,
  the checks, and the results file. Commands: `agent train` and `agent score-test`.
- LightGBM 4.7.0 installed with the user's OK (one 1.36 MB file from PyPI's file host,
  checked against its SHA-256; in `pyproject.toml` and `requirements-research.txt`).
- Tests: 348 pass (22 new), lint clean.

### Step 1: leak review, before any model (reported to the user first)

**No lookahead leak in the labels, features or dataset builder.**

| Check | Result |
|---|---|
| Code read | Every signal reads events with `ts <= cutoff` only; the creator count uses launches strictly before the coin's own; labels are only ever joined as `label_...` columns. |
| Tests | All 326 passed before any change, including the builder's no-lookahead tests. |
| Real data | 300 random training coins (1,200 rows): signals rebuilt from raw events cut at the cutoff match the dataset in every value. |
| One signal at a time | No signal scores above 0.92 AUC on any target on the training days. |
| Same code | The dataset's recorded code hash differs from today's files only by line endings (it was built in the other working folder). |

Four things that are not lookahead but would bias results, with the user's decisions
(2026-11-21, before any model was trained; spec 15.4 and change-log entry 27):
1. **Labels reach across the split boundaries** (65 min ahead): 2,470 training coins had
   labels reading Nov 17, 2,216 validation coins reading Nov 18. **Left out.**
2. **`dev_prior_launches` grows with the calendar** (mean 32 on Nov 12, 120 on Nov 15).
   **Models read `dev_launches_24h` instead** (a 24-hour window; Nov 12 has 2-24 h of
   history).
3. **The bundled stand-in is already decided at T+60 s** (`sniper_hold_pct >= 20` matches
   it 98.7-99.7% of the time; 0.9% of ordinary coins are bad only because of it).
   **Stage 1 is reported both as `bad` and as dead or rug alone.**
4. **Part-days:** `DaySplit.role` puts the Nov 11 part-day in training and the Nov 20
   part-day in test (R4's counts of 214,092 and 104,124 coins include them). **Models use
   whole days only** (`DaySplit.model_role`).

### What was trained

- **Rows:** SOL-paired coins (ordinary and mayhem), no gap, every label known. Training
  Nov 12-16: 165,664 coins (662,656 rows); validation Nov 17: 37,999 coins; test
  Nov 18-19: 65,865 coins (263,460 rows). No coin is in two sets.
- **Features:** 36 (33 dataset signals, `dev_launches_24h`, `launches_last_hour`,
  `launch_hour_utc`), plus 6 backfill-only ones for the "+ backfill-only" models.
- **Models, one per stage with the token's age as a feature:** base rate; the rules'
  verdict; logistic regression; LightGBM (8 settings tried on the validation day, early
  stopping on it); both fitted models again with the backfill-only signals.
  Probabilities calibrated on the validation day (isotonic).
- **Chosen on Nov 17:** logistic C 0.01; LightGBM 63 leaves (15 for graduation), at least
  100 rows per leaf, no class weights (tried, as spec 9 asks; never better), 48-258 rounds.
- **Time:** 8.3 minutes to train and check everything; 9 minutes to score the test days.
  Training twice gave an identical validation report.

### Test days Nov 18-19, scored once (2:09 AM PST, Nov 21)

Numbers in brackets are 95% intervals from resampling whole launch hours.

**Stage 1, bad (rug, dead or bundled): bad coins caught when blocking at most 30% of fine
ones.** 93.3% of coins are bad, so PR-AUC is high for everything (base rate 0.933, rules
0.949-0.957, logistic 0.985-0.988, LightGBM 0.988-0.993); this metric says more.

| Model | T+30 s | T+60 s | T+120 s | T+300 s |
|---|---|---|---|---|
| Base rate | 0 | 0 | 0 | 0 |
| Rules | 0.457 (0.439 to 0.473) | 0.594 (0.564 to 0.613) | 0 (0 to 0.665) | 0 |
| Logistic regression | 0.825 (0.812 to 0.835) | 0.839 (0.829 to 0.850) | 0.859 (0.848 to 0.868) | 0.882 (0.872 to 0.891) |
| LightGBM | 0.843 (0.829 to 0.855) | 0.868 (0.856 to 0.877) | 0.882 (0.872 to 0.891) | 0.901 (0.892 to 0.909) |
| LightGBM + backfill-only | 0.844 (0.832 to 0.854) | 0.862 (0.852 to 0.872) | 0.876 (0.867 to 0.888) | 0.897 (0.888 to 0.908) |

- The rules give three levels only; where "avoid" alone already blocks more than 30% of
  fine coins they catch nothing within the limit (0).
- Ordinary coins only (87.2% bad): LightGBM 0.720, 0.762, 0.797, 0.839; logistic 0.682 to
  0.794; rules 0.30-0.39 at best. Mayhem coins (98.5% bad): LightGBM 0.679 to 0.906.
- Without the bundled stand-in (dead or rug): LightGBM 0.854, 0.870, 0.887, 0.907; the
  already-known part of the target changes little.
- The practical track's reference (catch 90% while blocking 30%) is reached only at
  T+300 s on all coins, and not on ordinary coins alone.

**Stage 2, graduates later in the hour (coins still on the curve; 0.5-1.5% do).**

| Model | Metric | T+30 s | T+60 s | T+120 s | T+300 s |
|---|---|---|---|---|---|
| Base rate | PR-AUC | 0.014 | 0.014 | 0.011 | 0.005 |
| Rules | PR-AUC | 0.014 | 0.013 | 0.010 | 0.005 |
| Logistic regression | PR-AUC | 0.150 (0.124 to 0.178) | 0.212 (0.183 to 0.245) | 0.275 (0.239 to 0.314) | 0.218 (0.179 to 0.269) |
| LightGBM | PR-AUC | 0.193 (0.156 to 0.230) | 0.246 (0.212 to 0.281) | 0.313 (0.272 to 0.358) | 0.263 (0.222 to 0.317) |
| LightGBM + backfill-only | PR-AUC | 0.193 (0.156 to 0.230) | 0.247 (0.215 to 0.279) | 0.323 (0.281 to 0.368) | 0.269 (0.226 to 0.325) |
| Rules | Top 5 per hour | 0.000 | 0.007 | 0.002 | 0.001 |
| Logistic regression | Top 5 per hour | 0.271 (0.200 to 0.342) | 0.383 (0.317 to 0.458) | 0.404 (0.338 to 0.479) | 0.304 (0.246 to 0.362) |
| LightGBM | Top 5 per hour | 0.383 (0.321 to 0.450) | 0.417 (0.350 to 0.483) | 0.479 (0.412 to 0.550) | 0.350 (0.287 to 0.417) |
| LightGBM | Top 20 per hour | 0.244 (0.206 to 0.281) | 0.278 (0.241 to 0.316) | 0.301 (0.261 to 0.341) | 0.207 (0.183 to 0.232) |
| Base rate | Brier | 0.0143 | 0.0133 | 0.0106 | 0.0048 |
| LightGBM | Brier | 0.0129 | 0.0115 | 0.0086 | 0.0041 |

**Stage 3, 2x before 0.5x after a 1 SOL buy 5 s later (6.3% at T+30 s to 1.0% at T+300 s).**

| Model | Metric | T+30 s | T+60 s | T+120 s | T+300 s |
|---|---|---|---|---|---|
| Base rate | PR-AUC | 0.063 | 0.036 | 0.019 | 0.010 |
| Rules | PR-AUC | 0.065 | 0.038 | 0.021 | 0.011 |
| Logistic regression | PR-AUC | 0.184 (0.176 to 0.194) | 0.188 (0.175 to 0.203) | 0.193 (0.173 to 0.218) | 0.186 (0.162 to 0.217) |
| LightGBM | PR-AUC | 0.208 (0.199 to 0.217) | 0.218 (0.203 to 0.235) | 0.221 (0.196 to 0.250) | 0.204 (0.182 to 0.228) |
| LightGBM + backfill-only | PR-AUC | 0.208 (0.200 to 0.218) | 0.218 (0.203 to 0.236) | 0.219 (0.195 to 0.248) | 0.198 (0.177 to 0.221) |
| Rules | Top 5 per hour | 0.008 | 0.065 | 0.046 | 0.042 |
| Logistic regression | Top 5 per hour | 0.212 (0.158 to 0.271) | 0.275 (0.221 to 0.333) | 0.350 (0.292 to 0.412) | 0.329 (0.275 to 0.383) |
| LightGBM | Top 5 per hour | 0.269 (0.215 to 0.323) | 0.366 (0.290 to 0.447) | 0.377 (0.308 to 0.448) | 0.321 (0.271 to 0.375) |
| LightGBM | Top 20 per hour | 0.269 (0.242 to 0.295) | 0.301 (0.274 to 0.327) | 0.261 (0.230 to 0.292) | 0.242 (0.219 to 0.267) |
| Base rate | Brier | 0.0592 | 0.0346 | 0.0187 | 0.0100 |
| LightGBM | Brier | 0.0534 | 0.0305 | 0.0162 | 0.0087 |

**Reported separately (15.4):**
- **Mayhem vs ordinary:** the models rank ordinary coins better. Stage 3 PR-AUC at
  T+60 s: ordinary 0.275 (base 0.024), mayhem 0.180 (base 0.046). Stage 2 at T+60 s:
  ordinary 0.412 (base 0.011), mayhem 0.130 (base 0.016). On mayhem coins the rules beat
  the base rate a little (stage 3: 0.125 vs 0.093 at T+30 s) and the models beat the rules.
- **Each test day:** the two days agree. LightGBM stage 3 PR-AUC at T+60 s: 0.217 on
  Nov 18, 0.218 on Nov 19; stage 2: 0.215 and 0.283; stage 1 caught at 30%: 0.867 and 0.868.
- **With and without backfill-only signals:** no difference beyond the intervals in any
  stage. A live tool loses nothing by not having them.
- **Calibration (LightGBM, test days):** predicted and observed rates agree in every
  group, e.g. stage 3 at T+60 s: 0.2% → 0.2%, 0.9% → 1.0%, 2.8% → 2.8%, 9.1% → 9.8%,
  19.6% → 19.7%; stage 2 at T+60 s: 0.5% → 0.6%, 1.7% → 1.8%, 9.7% → 10.0%.
- **Every result is reported,** including the losers: the rules do not beat the base rate
  in stages 2-3 on ordinary coins, and their top picks at T+30 s are worse than chance.

### SHAP and leakage checks (on the validation day, before the test days were scored)

| Check | Result |
|---|---|
| SHAP, top features | Stage 1: `is_mayhem` 20%, `net_sol` 12%, `market_cap_sol` 12%, `curve_progress_pct` 8%. Stage 2: `net_sol` 14%, `buys_last_30s` 13%, `is_mayhem` 8%. Stage 3: `buys_last_30s` 24%, `net_sol` 21%. All are activity before the cutoff; no single feature dominates and none is suspicious. |
| Shuffled labels (5 times per stage) | ROC-AUC 0.35-0.59 against 0.88-0.94 for the real models; PR-AUC at the base rate for stage 1 (0.894 vs 0.896), 0.017 vs 0.011 for stage 2 and 0.044 vs 0.034 for stage 3, against 0.28 and 0.22 for the real models. See the note below. |
| Walk-forward inside training | The same order on every day: LightGBM > logistic > rules ≈ base rate. Stage 3 PR-AUC 0.196-0.218, stage 2 0.237-0.328, stage 1 0.980-0.989. Validation (0.22, 0.28, 0.98) and test (0.20-0.22, 0.19-0.31, 0.99) are in the same range: nothing collapsed on unseen days. |
| Sets kept apart | 0 coins in two sets; test rows are not read by `agent train` at all. |
| Features | A test refuses any id, rules column, target or `label_` column as a feature. |
| Scored once | `agent score-test` refuses when a test report exists; `agent train` refuses to replace the scored models; scoring checks the model files against the manifest. |

Note on the shuffled labels (my reading): the shuffled models are not exactly at chance,
but they land on both sides of it (stage 2 below 0.5, stage 3 above), far from the real
models. A model fitted to noise still gives active coins more varied scores than quiet
ones, and active coins hold most of the positives, so its ranking is not independent of
the outcome. A leak would instead keep the shuffled score close to the real one.

### Limits

- **Two test days, one market,** both after the Nov 15 program update. The intervals
  only cover hour-to-hour variation inside these days.
- **Stage 1 is mostly easy:** 93% of coins are bad and 98.5% of mayhem coins. The share
  caught at 30% blocked is the number to read, on ordinary coins.
- **Precision is not profit.** A top-5 pick doubling 37% of the time says nothing yet
  about fees, price impact or what the other 63% lose: that is R6.
- **One model for ordinary and mayhem coins** (with `is_mayhem` as a feature). A separate
  mayhem model (spec 8.10) was not tried.
- **No calibration chart yet** (the curve is given as numbers); a figure belongs in R7.
- `dev_launches_24h` is short on Nov 12 (the backfill starts 1.7 h before it).

## R4: more history (research track; plan 2026-11-18, downloaded 2026-11-19 to 2026-11-21, closed 2026-11-21)

**Closed 2026-11-21 with the user's OK.** The user OK'd option B on 2026-11-19 (epochs
1085-1091). Spec note (added to spec 16 with change-log entry 26, user's OK): the Nov 15
program update is inside the data; layouts unchanged, behaviour or fees may differ; the
backtest uses fees read from the chain for after it.

**Done when:** "Complete coverage, gaps logged; download, disk and time documented."

| Check | Result |
|---|---|
| Coverage | **One unbroken span**, Nov 11 2:18 PM to Nov 20 4:52 AM PST: 206.6 h, epochs 1084-1091. Every epoch has 6,750 of 6,750 chunks, none failed. |
| Gaps logged | The gap logs of all 8 epochs are empty. Rows per hour over the 205 whole hours: no hour under 40% of the median. Fewest in an hour: 854 launches, 104,060 curve trades, 467,571 PumpSwap trades (medians 1,696, 186,814 and 682,693; the low hours are the daily rhythm). |
| Read problems | 0 unreadable transactions, 0 undecodable events, 0 unknown events, 0 cut-off logs in every epoch. The 8-byte tail on trade events is as in 1084 (Phase 1); nothing new after the Nov 15 update. |
| Duplicates | None: 367,730 launches are 367,730 different coins, and epoch 1091 (stopped and continued 3 times) has no repeated event. |
| Download | 2,755 GB for 1085-1091 (3,165 GB with the 1084 pilot), at 63-65 MB/s. |
| Disk | 35.0 GB kept for 1085-1091, 40.7 GB for all 8 epochs (about 4.9 GB per day of chain). The data folder on D: is 40 GB; about 1,040 GB stay free. |
| Time | 11.3 h of download for 1085-1091 by the reports, plus about 0.3 h of 1091 that its report misses (below); 21.1 CPU hours. Clock time Nov 19 4:00 AM to Nov 21 12:21 AM, mostly waiting for 1091 to be published. |
| Split | Whole UTC days Nov 12-19: training Nov 12-16, validation Nov 17, test Nov 18-19 (`config/evaluation.yaml`), as planned. Training has 30.7 h after the Nov 15 update. |
| Tests | 326 pass, lint clean. |
| Rebuild | `agent dataset` (labels rebuilt first): 367,730 coins, 1,470,920 rows (x 4 cutoffs), **1,464,896 to model on** (99.6%; 94.7% with one day). By split, rows to model on: training 214,092 coins (856,368 rows), validation 48,008 (192,032), test 104,124 (416,496). 37 min (labels 25 min), 2.7 GB of memory, a 178 MB file. Only counts were looked at, no outcomes of the held-out days. |

| Epoch | Archive GB | Kept GB | Download h | CPU h | Chain h |
|---|---|---|---|---|---|
| 1084 (pilot) | 409.9 | 5.74 | 1.75 | 4.62 | 25.81 |
| 1085 | 403.4 | 5.18 | 1.77 | 3.45 | 25.85 |
| 1086 | 366.4 | 4.81 | 1.53 | 2.49 | 25.82 |
| 1087 | 333.2 | 4.73 | 1.38 | 2.39 | 25.79 |
| 1088 | 387.9 | 4.98 | 1.59 | 2.68 | 25.77 |
| 1089 | 365.7 | 5.09 | 1.45 | 2.58 | 25.84 |
| 1090 | 463.6 | 5.28 | 1.97 | 3.14 | 25.85 |
| 1091 | 434.7 | 4.88 | 1.62 (about 1.9 in all) | 4.34 | 25.83 |
| **1085-1091** | **2,755** | **35.0** | **11.3** | **21.1** | **180.8** |

What happened:
- **Nov 19, 4:00 AM to 1:41 PM:** epochs 1085-1090, one after another (9.7 h).
- **Nov 19, 11:53 PM: the waiting command stopped.** While it checked every 10 minutes
  for 1091, the archive once answered "429 Too Many Requests" (a rate limit), and the
  command treated every answer other than "not found" as fatal. Nothing was lost. Fixed:
  while waiting, temporary errors (HTTP 408, 425, 429 and 5xx, and network errors) mean
  "try again in 10 minutes" too; any other error still stops it. With a test. Restarted
  12:56 AM; it waited until the computer was turned off at about 6 AM.
- **Epoch 1091** ended about 4:50 AM Nov 20 (checked on a public Solana node) and was
  published by 2:49 PM. It was read in four sittings: 2:51-3:08 PM, 6:01-7:03 PM,
  7:03-7:36 PM and 12:19-12:21 AM Nov 21. The first ended without a clean stop (the
  computer or the app was closed), so it wrote no run record and its 0.3 h is not in the
  report's time. The one file of a chunk left half done was moved to `_orphans`, as
  designed, and the chunk was read again.

Before the download:
- **One command for all epochs:** `agent backfill --epoch 1085-1091 --wait` (ranges and
  lists now work). One Ctrl+C stops every epoch left, with exit code 130; the same command
  continues. It asks once for several epochs. `--status` and the summary now also print
  the layout check (unknown events, events longer than the layout files). 4 new tests; 325
  pass, lint clean.
- **Trial read, epoch 1087 right after the update** (slots 469,811,025-469,811,664: 11
  chunks, 0.55 GB, 8,721 bonding-curve trades, 29,824 PumpSwap trades): 0 unreadable,
  0 undecodable, 0 unknown events. The extra bytes are as in epoch 1084: an 8-byte tail on
  every trade event (non-zero 0.5% of the time on the curve vs 0.6% in 1084; 0.1-0.2% vs
  0.1% on PumpSwap), none on any other event. The update did not change what we read.
- **Download** started 4:00 AM PST Nov 19 in a terminal tab, with the pilot's settings
  (8 workers, 100 MB/s limit).

The plan, as written before the download. Checked on 2026-11-18 at about 9 PM PST (size-only requests; nothing downloaded):

| Check | Result |
|---|---|
| Archive | Epochs up to 1089 are published (1089 ended about 1:15 AM Nov 18). 1090 ends about 3 AM Nov 19 and 1091 about 5 AM Nov 20; the archive publishes about 5 h after an epoch ends. Sizes 333-417 GB each. |
| Disk | D: has 985 GB free; about 40 GB will be kept (5.3 GB per day of chain in the pilot). Only epoch 1084 is on disk now. |
| Internet | Unlimited data (confirmed 2026-11-12, again by the user 2026-11-18). |
| Resume | A stopped run continues where it left off (`progress.jsonl`); failed chunks go to the gap log and are retried by the next run. |
| Program updates | **pump.fun and PumpSwap were both updated on Nov 15 at about 9:20 AM PST** (slot 469,811,025, epoch 1087; read from the chain). Our layout files are pump.fun's published copy of Oct 13, and pump.fun has published nothing newer. The parser counts events with more data than the layout files describe and keeps those extra bytes, so a short trial read shows whether anything changed. |

**Which days.** Spec 15.3 says the most recent archived epochs. Two ways to read that:
- **A. Start tonight:** 1085-1089 and, before 1084, 1083 and 1082. 2,612 GB, about 11.5 h,
  done about 9 AM Nov 19. Whole UTC days Nov 10-17: training Nov 10-14, validation Nov 15,
  test Nov 16-17. All training days are before the Nov 15 update and all test days after it.
- **B. Newest days (recommended):** 1085-1091, right after 1084. 1085-1089 tonight (1,857 GB,
  about 8 h); 1090 and 1091 start by themselves once published. Done about midday Nov 20.
  Whole UTC days Nov 12-19: training Nov 12-16, validation Nov 17, test Nov 18-19. Training
  includes about 1.3 days after the update, so the test does not judge models on a program
  version they never saw.

**Steps, after the user's OK:**
1. **Trial read** of about 10 chunks (0.6 GB) right after the update in epoch 1087. Check
   against epoch 1084: 0 undecodable events, 0 unknown events, and no new extra bytes (in
   1084 every trade event carries the 8 bytes more than the published layout that Phase 1
   found, stored in `extra_hex`; nothing else does). If anything changed,
   stop, find the new layout (on-chain or current docs, rule 5), update the layout files,
   run the tests, then continue. The trial's rows count toward the full run.
2. **Download** one epoch after another in a terminal tab, with the pilot's settings (8
   workers, 100 MB/s limit, about 63 MB/s). It can be stopped and continued any time.
   First, a small change so one command covers all the epochs (`agent backfill` takes one
   epoch now, and a stopped run exits as if it finished, so a loop of commands would go on
   to the next epoch after Ctrl+C). With one command, one Ctrl+C stops everything, and
   running it again continues; epochs not yet published are waited for. With a test.
3. **Coverage:** every epoch's status and gap log; the backfill should be one unbroken span.
4. **Rebuild** labels and the dataset (about 10 minutes), and run the tests.
5. **Record** download size, disk use and time per epoch here.

Not part of R4: looking at outcomes on the validation and test days. R4 only checks that
the data is complete (rows and gaps per hour).

## R3: exploration (research track, built 2026-11-14, closed 2026-11-14)

**Done when:** "Notebook runs top to bottom from the dataset; findings in
`docs/PROGRESS.md`." **Result: met; closed with the user's OK, 2026-11-14.**

Built on branch `r1-labels`: `notebooks/r3_exploration.ipynb` (saved without outputs; the
executed copy and an HTML page go to `<data folder>\notebooks\`), `config/evaluation.yaml`
(spec 15.4 written down before any model) and `src/agent/models/evaluate.py` (the day
split and intervals that resample whole launch hours).
- **Held-out days:** exploration only uses rows before the validation and test days of
  the evaluation plan. With one whole UTC day (Nov 12) nothing is held out yet; once R4
  adds days, the newest are left out of the notebook automatically.
- **Packages:** the user had pandas and Jupyter in WSL (another project), not in this
  project's Windows `.venv`. Installed with the user's OK: 93 packages, 137 MB, all from
  PyPI's file host (`files.pythonhosted.org`, no other pip source configured), pre-built
  wheels only, each checked against its SHA-256; nothing already installed changed.
  Listed in `pyproject.toml` (`research` extra) and, with hashes,
  `requirements-research.txt`.

| Check | Result |
|---|---|
| Notebook runs top to bottom | `jupyter nbconvert --execute` on the dataset: 13 code cells, no errors or warnings |
| Tests | 321 pass (4 new: the plan file, whole days, the split, hour-resampled intervals); lint clean, now including the notebook |
| Charts | Fixed colors per group (validated for color blindness as a set: worst pair 9.2, all checks pass), each chart with its numbers in a table |

**Findings** (43,022 coins launched Nov 11 2:18 PM to Nov 12 about 3 PM PST; 95%
intervals over launch hours; one day of one market, so none of this is general yet):
1. **Most graduations happen before any decision time.** 60% of ordinary coins that
   graduate within the hour (241 of 403) have already done so by T+30 s; 233 graduate in
   the second they launch, with a median of 6 wallets buying the whole curve in the launch
   block. "Will it graduate within the hour?" is already answered for them. Among coins
   still on the curve, the share that graduates later is small: ordinary 1.06% (0.89 to
   1.24) at T+30 s, falling to 0.42% at T+300 s; mayhem 1.54% to 0.64%; other quote 0.74%
   to 0.46%. Mayhem coins almost never graduate at launch (1% by T+30 s). Target changed
   (spec note below).
2. **First-hour rates:** ordinary coins: dead 80.8% (77.1 to 83.8), rugged 3.5%,
   bundled (stand-in) 4.7%, bad 83.6% (80.1 to 86.5), graduated 2.6%. Mayhem: dead 95.1%,
   rugged 48.1%, graduated 1.6%. Other quote: dead 88.2%, graduated 1.0%.
3. **Coins die fast:** median time of the last trade is 5 min for ordinary coins, 75 s
   for mayhem, 60 s for other quote; 50% of ordinary coins trade after 5 min, 19% after
   30 min.
4. **"Not dead" is partly bots:** 17% of ordinary coins with a curve trade after 30 min
   got such trades only from wallets that traded 20+ different coins late (63 wallets;
   the busiest, 271 coins). A later choice: a "dead" that ignores those wallets.
5. **One unusual hour:** of ordinary coins launched Nov 11 at 11 PM, only 46% were bad
   (43% dead) against 64-92% in other hours; 217 of 394 still traded after 30 min, spread
   over many wallets (the busiest touched 26). Not explained yet. Hours like this are why
   intervals resample whole hours.
6. **Creators repeat:** 17,705 creators; the 439 with 11+ coins made 47% of all coins (the
   busiest, 459). Coins whose creator launched before are more often bad (82% with no
   earlier launch, 91% with 5+) and less often graduate later (1.3% vs 0.0-0.8%) or
   double (4.0% vs 1.6-1.8% from T+60 s); the same holds for coins with 12+ h of
   backfill before them.
7. **The curve's position is the strongest single signal** (curve progress, market cap,
   net SOL and price change carry the same information). Graduating later at T+60 s: AUC
   0.98, AP 0.49 (base rate 1.0%); among coins with 10+ trades, AUC 0.96, AP 0.52 (base
   2.5%). Doubling at T+60 s: AUC 0.92, AP 0.30 (base 3.4%); among coins with 10+ trades
   0.86 and 0.31 (base 6.5%). Bad: AUC 0.72 (lower values go with bad), AP 0.93 (base
   83.6%). Signals above 0.95 were checked: they reflect curve position before the cutoff
   (R2's no-lookahead tests), not a leak.
8. **The rules are a weak baseline.** As a filter, "avoid" catches 25-40% of bad ordinary
   coins while blocking 20-31% of fine ones; a flagged coin is bad 86-87% of the time
   against 83.6% for any coin. "Avoid or caution" blocks 98% of fine coins from T+60 s.
   As a picker, coins that pass at T+60 and T+120 s doubled 10.7% (5.8 to 16.0) and 10.6%
   of the time against 3.4% and 2.7% for all (only 112 and 132 coins), but did not
   graduate later more often. Whether that doubling lift holds is for the held-out days.

**Spec notes** (added to spec 7 and 15.4 with change-log entry 25, user's OK, 2026-11-14):
- **Graduation target (7, 15.4):** at each decision time, stage 2 predicts "graduates
  later in the hour" for coins still on the bonding curve; coins already complete at the
  decision time are counted and reported, not predicted.
- **Evaluation plan (15.4):** written to `config/evaluation.yaml` on 2026-11-14 before any
  model. "The most recent quarter of the days" rounds up; exploration and training use
  only rows before the validation days.

## R2: dataset (research track, built 2026-11-14)

**Done when:** "No-lookahead test passes on the builder; one row per coin and cutoff;
rebuilding gives identical output." **Result: met; closed 2026-11-14 with the user's OK.**

Built on branch `r1-labels` (after R1): `src/agent/dataset/build.py` and `agent dataset`,
which writes `<data folder>\dataset\dataset.parquet` and `manifest.json`.
- **Rows:** every backfilled coin at T+30, 60, 120 and 300 s, in launch order.
- **Columns (81):** 10 ids (mint, creator, quote coin, launch time, UTC date, launch hour,
  cutoff, cutoff time, `history_s`, `has_gap`); 35 signals (the live engine's snapshot);
  2 rules columns (verdict and reasons from `config/rules.yaml`); 34 label columns
  (`labels_complete` and 33 named `label_...`: the coin's labels and the entry labels for
  the same decision time).
- **Signals from the backfill only:** `load_states` (the shared replay) gained a `roots`
  choice. With the backfill only, the time the backfill does not cover counts as gaps,
  worked out from its progress files the same way as for the labels
  (`ingest/backfill_coverage.py`, moved out of the labels module). The live tools still
  read both sources, as before.
- **Labels kept current:** they are rebuilt first if missing, made with other settings,
  or made from other backfill files (the label manifest now keeps a fingerprint of the
  backfill's files).
- **Manifest:** code version (git commit, whether there are uncommitted changes, and a
  hash of the source files that decide the contents), rules hash, label settings hash,
  epochs, backfill stretches, the column groups, and the file's SHA-256.
- **Where the code lives:** spec 13 lists `features/build_snapshot.py`, but nothing under
  `features` may import the labels (R1's test), so the builder is its own package; output
  in `<data>\dataset\` rather than `data/features/`. Now a note in spec 13.

| Check | Result |
|---|---|
| Tests | 317 pass (9 new), lint clean |
| No-lookahead on the builder (test) | Events added after T+30/60/120/300 s (buys and sells by the creator, snipers, new wallets and the mayhem agent; the curve completing; new launches by the same creators) change no signal or rules column at or before that cutoff, and do change later ones. Deliberately leaking code (reading 10 s past the cutoff, or seeing a later curve completion) fails the test at T+30, 60 and 120 s; at T+300 s the builder does not load later events at all. |
| No-lookahead on the real data | 400 random coins, 1,600 rows: signals rebuilt from events cut at the cutoff in SQL, before the feature code sees them, are identical in all 1,600 |
| One row per coin and cutoff | 181,756 rows = 45,439 coins x 4; every coin has exactly 4; the same coins as the labels |
| Rebuilding gives identical output | Built twice on epoch 1084: the same SHA-256 (`38d35f2b9ba7...`); also a test |
| Labels on the right coin | 0 rows where the signals count more trades than the hour's label, the entry is not at cutoff + 5 s, the curve completed by the cutoff but the coin did not graduate in the hour, the dev bought more by the cutoff than in the hour, a SOL coin lacks entry labels, or another coin has them |
| Live recording not read | Test: a buy that is only in `raw/` does not show up |
| Run on epoch 1084 | About 1 minute when the labels are current (5.5 min when they are rebuilt first); 22 MB. 172,088 rows to model on (no gap, every label known: 43,022 coins x 4) |

**The rules' verdicts** (rows to model on; avoid / caution / pass):

| Coins | T+30 s | T+60 s | T+120 s | T+300 s |
|---|---|---|---|---|
| Ordinary | 24 / 41 / 35% | 28 / 72 / 0.7% | 33 / 66 / 0.9% | 39 / 61 / 0.8% |
| Mayhem | 54 / 46 / 0% | 75 / 25 / 0% | 86 / 14 / 0% | 91 / 9 / 0% |
| Priced in another coin | 10 / 90 / 0% | 13 / 87 / 0% | 14 / 86 / 0% | 17 / 83 / 0% |

- Only ordinary coins can pass: mayhem coins and coins priced in another coin always get
  a caution reason (by design). From T+60 s almost no coin passes, because "fewer than 10
  buyers" starts at 60 s and fires for 32,001 of 43,022 coins. At later cutoffs the rules
  are close to "avoid everything"; R3 scores them as the first baseline. How each verdict
  turned out is left to R3.
- `dev_prior_launches` only counts launches inside the backfill: coins have a median of
  15.9 h of backfill before them (0 to 25.8 h; `history_s`). Even so, "dev launched 3+
  coins before" fires for 20,843 coins at T+60 s. More days (R4) make this signal fairer.
- SOL amounts are empty for coins priced in another coin (30% of rows).

**Spec note:** added to spec 13 (the dataset builder is `src/agent/dataset/build.py` and
writes to `data/dataset/`), with change-log entry 24 (user's OK, 2026-11-14).

## R1: labels (research track, built 2026-11-13)

**Done when:** "A test for each label on built examples; labels are computed apart from the
features and never feed them; label rates per day, mayhem vs. ordinary, look plausible."
**Result: met; closed 2026-11-14 with the user's OK.** With one day on disk there is one set of daily
rates; comparing days comes with R4.

Built on branch `r1-labels`: `config/labels.yaml` (every threshold),
`src/agent/labels/make_labels.py`, and `agent labels`, which writes to
`<data folder>\labels\`: `coins.parquet` (one row per coin), `entries.parquet` (one row per
coin and decision time) and `manifest.json` (settings hash, backfilled stretches, counts).
- **Source:** the backfill only, with trades in exact order (slot, position in the block,
  event). Each coin's curve trades, its PumpSwap pool's trades and its post-complete buys,
  up to 65 minutes after launch (the last entry at T+305 s plus one hour).
- **Edges of the data:** the stretches the backfill covers come from its progress files;
  a coin whose 65 minutes are not fully covered keeps a row with empty labels.

**Choices beyond the spec's wording** (in `labels.yaml` or the module's notes; now a note in
spec 7, "How the labels are computed"):
1. **Prices are execution prices:** quote paid or received per token, before fees. They
   mean the same on the curve and in the pool. Almost every graduated coin's pool is a
   "boost" pool with a virtual quote reserve (99% of their trades), so reserve-based prices
   would depend on that formula. For SOL-paired coins the first pool price is a median
   1.01x the last curve price (0.62-1.16x for 90% of coins).
2. **Price labels (rug, hit_2x) for SOL-paired coins only** (spec 5 models them first).
   28% of launches and 200 of the 961 graduations are priced in another coin; across
   graduation their units do not line up (32 coins jump more than 3x).
3. **The mayhem agent's own trade prices are left out of price labels** (setting), and
   hit_2x is also given with them. On mayhem coins a person trading right after the agent
   pays 0.70-1.25x the previous price (10th-90th percentile); on ordinary coins
   consecutive prices differ by 0.98-1.02x. The agent's trades still count as activity.
4. **Trades under 0.001 SOL are not prices** (rounding noise).
5. **The entry price includes the 1 SOL buy's price impact but not fees;** fees belong to
   the backtest (R6).
6. **`is_rug` uses the creator's wallet only;** `is_bundled_proxy` as in spec 7.

| Check | Result |
|---|---|
| Tests | 308 pass (15 new: each label on built coins, coverage stretches, an end-to-end build from Parquet, the summary), lint clean |
| Labels kept apart from features | Test: nothing under `features`, `daemon`, `models` or `tui` imports `agent.labels`, and `agent.labels` imports neither `features` nor `daemon`. The end-to-end test also shows labels never read the live recording. |
| Run on epoch 1084 | 45,439 coins launched Nov 11 2:18 PM to Nov 12 4:06 PM PST; 43,022 with every label known (the rest launched in the last 65 minutes); 3.5 minutes |
| Double counting | 274 post-complete buys; 110 of their transactions also hold PumpSwap trades, but in other pools (multi-hop swaps), so nothing is counted twice |
| Same data, same files | Rows are saved in launch order; building twice on epoch 1084 gave byte-identical `coins.parquet` and `entries.parquet` (also a test) |

**First hour, share of coins with every label known:**

| Coins | Count | Dead | Rugged | Bundled (stand-in) | Bad | Graduated |
|---|---|---|---|---|---|---|
| Ordinary (SOL) | 15,466 | 81% | 3.5% | 4.7% | 84% | 2.6% |
| Mayhem (SOL) | 14,826 | 95% | 48.1% | 0.0% | 98% | 1.6% |
| Priced in another coin | 12,730 | 88% | - | 0.2% | - | 1.0% |

"Dead" is the same to the percent without the mayhem agent's trades.

**Reached 2x before 0.5x within an hour, after a 1 SOL buy 5 s after the decision time:**

| Coins | T+30 s | T+60 s | T+120 s | T+300 s |
|---|---|---|---|---|
| Ordinary | 3.9% | 3.4% | 2.7% | 1.9% |
| Mayhem | 9.5% | 5.0% | 2.5% | 0.8% |
| Mayhem, with the agent's prices | 11.4% | 6.1% | 3.1% | 1.0% |

**Why these look plausible, and what is odd:**
- **Mayhem coins are a different market.** The median one trades for 75 seconds; in its
  first hour it has 40 agent trades and 15 by people (medians). Creators sell at least
  half their buy in 95% of them, and people's prices fall a median 83% from the peak. So
  "rugged" at 48% mostly measures that collapse, not a classic creator rug. Reported
  separately, as planned; the write-up should not compare mayhem and ordinary rug rates.
- **Ordinary coins mostly fade rather than rug:** 41% launch without a creator buy; of the
  creators who buy, 77% sell at least half within the hour, but only 5% of coins ever
  fall 80% from a peak (most never rise).
- **Bundled stand-in:** 68% of ordinary coins have at least one first-second buyer besides
  the creator (median 2), and 4.7% have them holding 20% or more at T+60 s. Only 5.6% of
  mayhem coins have any first-second buyer besides the agent.
- **Holdings come from trades only:** one mayhem coin shows its early buyers at 126% of
  supply (a volume-bot pattern: wallets buy, pass tokens on off the curve, others sell).
  Left as measured and named as a limit; it is 1 coin of 45,439.
- **hit_2x falls at later decision times** (fewer coins still moving); mayhem coins' higher
  early rate reflects their price swings.

**Spec note:** choices 1-6 above added to spec 7 as "How the labels are computed", with
change-log entry 23 (user's OK, 2026-11-14). No threshold changed.

## Step B: engine always on (fast path, built 2026-11-12)

**Done when:** "24 h continuous run with recorder uptime ≥ 95% (the 15.1 operations
threshold)." **Not yet met:** it needs the 24-hour run after deployment. Baseline, the
current single connection: 88.6% of the time since 04:03 recorded (1 h 47 m off in 249
gaps, as of 19:50).

Built in a separate copy of the code (`.worktrees/step-b`, branch `step-b`), so the
running recorder and tonight's backfill, which load code from the main folder, are not
touched. It goes live at the restart after the Phase 1 audit.
- **Several live connections at once** (`connection:` per source in settings.yaml). Two
  connections, both on Solana's public server, starting on its two different addresses.
  A transaction both deliver is recorded once (the first copy); the repeat-memory now
  holds 200,000 signatures (over half an hour of successful transactions).
- **Gap log for several connections** (`ingest/gaps.py`): each connection's unbroken run
  of events is a segment; a gap is logged only for on-chain time no connection covered.
  This includes the case where one connection is up but running late and the other one
  drops and reconnects: if the late one drops before reaching the moment the other one
  came back, the stretch in between is logged as a gap even though a connection was
  always up. With one connection the rule is unchanged.
- **Reconnects:** the longest wait between attempts is now 8 s (was 30 s).
- **Continuous recording:** `recorder.goal_hours: 0` (no burst bar); `agentd status`
  shows an `uptime:` line for the last 24 hours against `recorder.uptime_target` (95%),
  one `feed N:` line per connection (how late, how often first, drops), how far on chain
  everything has been recorded, and a `verdicts:` line.

| Check | Result |
|---|---|
| Tests | 268 pass (25 new), ruff clean. One older test (chain snapshots on schedule) waited a fixed 2.5 s and failed when the PC was busy, with the old code too; it now waits for the second snapshot and checks the spacing |
| Gap log, several connections | Tests: one connection dropping while the other runs is no gap; both dropping is a gap from the last event either delivered; a late connection covers the other's reconnect; a hole is logged when it drops first; repeats count as coverage; pause and lost rows break every connection |
| Engine with two fake feeds | Each transaction recorded once, both connections in the status, no gap |
| Engine speed | About 140 µs per transaction including the repeat: ~1% of one core at ~90 successful transactions a second |
| 24 h uptime ≥ 95% | After deployment |

Findings (from the engine log and two short live tests, 2026-11-12):
1. **Why the feed drops.** The public server closes the connection every 1-5 minutes
   (close code it reports as invalid, logged as "1002 protocol error"). After some drops
   it refuses new connections with HTTP 413 for 40-60 s, on both of its addresses at
   once (53 times today), so it limits by our IP address. Connections already open keep
   streaming during a refusal: that is what the second connection covers.
2. **The feed often runs late.** Events arrive 0.3-77 s after their on-chain time
   (5-minute log samples: median about 6 s, 20-77 s at times), and the server cuts the connection
   when it is far behind; the events it had not sent yet are lost, which is why the
   median gap was 22 s although a reconnect takes about 1.5 s. Not our engine: it used 2%
   of one core. Two connections were late together (minute by minute 1-13 s, within a
   few seconds of each other), so a second connection helps little with lateness; it
   helps with drops. Late data delays verdicts: a T+30 s verdict comes when the data up
   to T+30 s has arrived (`wait_s` in each verdict, median in `agentd status`).
3. **No free alternative feed found.** PublicNode accepted the subscription but sent
   nothing in 45 s; dRPC's free plan refuses `logsSubscribe`; Helius bills the stream
   (settings.yaml). So both connections use Solana's public server.
4. **Data use doubles** to about 130 GB a day with two connections.
5. **An 8-minute test with two extra connections (19:28-19:36)** ran fine (one drop, the
   other connection covered it: 1,727 transactions reached only one connection), but the
   running recorder was refused reconnects twice during it, at 19:29 (57 s gap) and
   19:36 (68 s gap). Those refusals usually come every 1-2 hours, so the test's extra
   load most likely caused them. Both are in the gap log; the Phase 1 audit counts them
   as gaps. No more live tests until the 24-hour run is over.

Deployment, after the Phase 1 audit: `agentd stop`, bring branch `step-b` into the main
folder (after tonight's backfill has finished), `agentd start`, then `agent verify-curve`
and, after a few hours, `agent check-live`.

## Step D: the screen (fast path, built 2026-11-12)

**Done when:** "Copy-to-verdict median < 2 s; mint first in every alert." **Met in tests
and by timing on recorded tokens; the real measurement needs the engine running the new
code** (after the Phase 1 audit). Textual 8.2.8 installed by the user on 2026-11-12.

Built in the step-b copy, as spec 3 and 12 describe:
- **Engine side** (`daemon/local_api.py`): a WebSocket on 127.0.0.1 only
  (`local_api.port`, 8765) that refuses web pages (a browser always sends an Origin
  header). It answers "analyze this mint" from memory, pushes every token that passes,
  and serves the status. A lookup uses the same settled point a verdict waits for (2 s
  behind how far the recording has covered the chain), so it never sees a partial second.
  It only reads the engine's memory; if the port is taken, the engine records without it.
- **Screen** (`agent screen`, `tui/`): Textual app with the alert feed (mint first), a
  paste box, the analysis panel (verdict and reasons, main signals, earlier verdicts,
  data age, warnings for gaps, mayhem and non-SOL coins) and a status bar (feeds, data
  lag, last gap, median copy-to-verdict time). Commands: a mint, `mute`/`unmute`,
  `clipboard` (F2), `start`, `help`, `quit`. It reconnects by itself when the engine
  restarts and holds no data. Every copy-to-verdict time goes to `run/screen_latency.jsonl`.
- **Clipboard watcher** (`tui/clipboard.py`): Windows API through ctypes, no extra
  package. It checks Windows' clipboard change counter every 0.1 s, which does not lock
  the clipboard, and reads the text only after a change, so it cannot get in the way of
  copying in the browser. Only text that is exactly one address counts; anything else is
  dropped unread past 64 characters, and never stored, logged or sent.
- Names and symbols (chosen by whoever launched the coin) are cleaned before display:
  no control characters, so a name cannot send terminal escape codes.

| Check | Result |
|---|---|
| Tests | 290 pass (19 new), ruff clean. The app test opens the screen, gets the latest alert, analyzes a copied mint and a typed one, and refuses text that is not an address without showing it. Found on the way: a copied mint was put in the paste box, so the next typed mint was added to it; the box now stays empty |
| Mint first in every alert | Pass (tests: alert feed lines, alerts log, analysis panel) |
| Engine lookup time | 0.07 ms median, 1.8 ms at the 99th percentile, 7.5 ms worst, over 1,760 recorded tokens at 15 minutes old (up to 2,703 trades) |
| Copy-to-verdict median < 2 s | Expected about 0.1 s (the clipboard check interval) plus a local round trip; the app test checks each under 2 s. Real measurement after deployment |
| Local API | Tests: lookups, status, bad requests, alert feed (latest then new), web pages refused (403), listens on 127.0.0.1, follows engine restarts; a real engine run answers a lookup for a token it recorded |

Changed after the user tried the screen (2026-11-12, against a stand-in that follows the
recorder's files, since the running engine is still the old code):
- Alert lines wrap (they were cut off in a narrow window) and can be clicked, or picked
  with the arrow keys and Enter, to analyze that coin.
- A coin's bonding-curve address finds the coin too (one copied from Padre was the curve,
  not the mint); the panel shows the mint first and says what was copied.
- Coins copied from GeckoTerminal were hours old and already graduated: outside what the
  screen covers (first 15 minutes on the curve), which fits the user's plan to trade
  fresh launches.

Not in this step (spec 12, later): RPC lookup of coins the engine did not see launch,
`watch`, `filter`, `ask` and `stats`, enrichment (RugCheck, website), scores from models.

## Step C: live signals + rules verdict (fast path, built 2026-11-12)

**Done when:** "No-lookahead test passes; a token's snapshot is identical live and
replayed; every new token gets a verdict with reasons at T+30/60/120/300s." **Met in
tests; the live checks need the engine running the new code** (after the Phase 1 audit).

Built (first part, before step B):
- `features/token_state.py`: one token's state from its events and its signals at a
  cutoff: trade flow, holders, dev, snipers/bundles, curve, mayhem share (spec 8.1-8.4,
  8.10). Reads only events with `ts <= cutoff`; arrival order and repeats do not matter.
- `features/replay.py`: the same code over recorded (and later backfilled) Parquet, with
  gap flags, plus `outcomes` (dead at 30 min, graduated in 1 h) for evaluation only.
- `models/rules.py` + `config/rules.yaml`: the hand-written rules baseline (spec 9):
  avoid / caution / pass with plain-word reasons. Every threshold is a starting guess.
- `agent signals --hours N --at S`: replays recorded tokens through both and shows the
  verdicts and what happened to the tokens afterwards.

Built with step B (branch `step-b`):
- `daemon/live_state.py`: the engine keeps every new token's state in memory for 15
  minutes and judges it at T+30/60/120/300 s once the recording has covered the chain 2 s
  past the cutoff, so a verdict uses every event up to the cutoff and none after it.
  Verdicts touched by a recording gap say so. Trades that arrive before their token's
  create (possible with two connections) wait up to 3 minutes for it. Rules are read
  again when `config/rules.yaml` changes; a file that does not parse keeps the old rules.
- Verdicts are saved to `live/verdicts/` (every signal included, plus how late the
  verdict came and which rules version made it); tokens that pass go to
  `run/alerts.log`. `agentd alerts [--follow]` shows them, mint first;
  `agentd start --foreground --show verdicts|alerts` prints them.
- `agent check-live --hours N`: replays the recorded data for the tokens judged live and
  checks that every snapshot is identical.

| Check | Result |
|---|---|
| Tests | 268 pass with step B (47 new since Phase 3), ruff clean |
| No-lookahead test (spec 6) | Pass: 300 later trades and a curve completion change no earlier snapshot |
| Same signals live and replayed | Pass in tests: real transactions through the recorder; the live verdicts' snapshots equal a replay of the Parquet files it wrote. Found and fixed on the way: trades arriving before their create were lost live |
| Same signals on the real feed | After deployment: `agent check-live` |
| Every new token judged at T+30/60/120/300 s | After deployment (tests: each cutoff once, only after it is covered) |
| First real run (8 h, 08:41-16:41 local) | 15,313 tokens; 9,322 judged at T+60 s, 5,991 left out because a recording gap touched their first minute; 15 s for 3 h of tokens |

First look at the starting rules (tokens whose first hour was recorded, gaps under 10%):

| Verdict at T+60 s | Tokens | Dead at 30 min | Graduated in 1 h |
|---|---|---|---|
| avoid | 756 | 93% | 4.1% |
| caution | 1,208 | 88% | 0.7% |
| pass | 7 | 57% | 0.0% |

Findings:
1. **The starting rules do not sort tokens well.** "avoid" tokens graduated more often
   than "caution" ones, and only 0.2% of tokens pass. "Dev already sold" alone fires on
   41% of all tokens. Thresholds should be set on one period and checked on another
   (never tuned on the hours they are judged on); the backfill gives enough days for that.
2. **Recording gaps hide 39% of tokens' first minute** at today's drop rate (194 gaps in
   11 h), which is why step B (engine always on, second connection) comes first.
3. About 90% of new tokens are dead within 30 minutes and about 2% graduate within an
   hour (this sample); any filter is judged against those base rates.

Left for step C: apply mayhem reserve resets; market context (launches and graduations
per hour, spec 8.8); choose the rule thresholds on one period and check them on another.
Until then the rules pass about 0.2% of tokens (about 3 an hour) and the passes are not
known to be better than chance.

## Phase 3: backfill pilot (2026-11-12)

**Done when:** "Measured download size, disk use, and parse time per epoch; parity test
passes against the recorder for an overlapping period." **Not yet met:** both need the
pilot epoch (1084), which the archive has not published yet.

Plan approved by the user on 2026-11-12, with two spec edits (SPEC.md change log 12-13):
our own archive reader instead of Jetstreamer, and a precise parity definition.

Built:
- `src/agent/ingest/car.py`: reads Old Faithful's CAR files (DAG-CBOR nodes, split
  values, zstd + protobuf transaction metadata) and the per-slot index. Every node is
  checked against the hash in its CID. Transactions in all three formats in use are
  read, and every field is walked so the sizes must add up exactly: legacy, v0 (lookup
  tables) and v1 (SIMD-0385, signatures at the end, active on mainnet since October).
- `src/agent/ingest/oldfaithful_extract.py`: keeps successful transactions that mention
  pump.fun or PumpSwap (the live feed's own rule) and runs their logs through the shared
  parser, `source = backfill`. Adds backfill-only `bf_` columns: position in the block,
  fee, signature count, compute units, whether it mentions pump.fun (directly or only
  through a lookup table), and where it is in the archive file.
- `src/agent/ingest/oldfaithful_backfill.py`: 64-slot chunks, one HTTP range request
  each, 6 worker processes, 80 MB/s limit (settings.yaml `backfill:`). Resumable
  (`progress.jsonl`), retries, failed chunks in `backfill/gaps/`, files from an
  interrupted write set aside (never deleted). Downloads are added to the ledger that
  `agentd status` reports. Rows go to `raw_backfill/`.
- `src/agent/backfill_parity.py`: the parity test as defined in spec 4.1.
- Commands: `agent backfill --epoch N [--wait] [--status]`, `agent parity --epoch N`.
- Tests: `test_car.py`, `test_backfill_parity.py` (the spec's required test),
  `test_backfill.py`; fixture: one real slot (468072129, epoch 1083) and the RPC's copy
  of its 128 pump.fun/PumpSwap transactions (`tools/capture_car_fixture.py`).
  221 tests pass; ruff clean.

Checks against real data (small reads only, about 0.3 GB in total):

| Check | Result |
|---|---|
| 40 slots of epoch 1083 (28.6 MB) | 12,797 transactions, all read with exact sizes: 4,319 legacy, 4,451 v0 (2,041 with lookup tables), 4,027 v1 |
| Fixture slot vs. the RPC | all 128 transactions identical in signature, slot, block time, accounts, lookup-table accounts, fee, compute units, logs and error |
| Same transactions through both paths | every shared column identical (archive bytes vs. live log lines) |
| Trial run, 6 chunks (0.22 GB), real worker processes | 101,092 transactions; 22,704 kept; 21,320 failed ones dropped; 0 unreadable; 0 decode errors; 38 MB/s decoding per core |

Findings:
1. **v1 transactions are about 31% of all transactions.** Readers older than
   SIMD-0385 fail on them. The live path is not affected (it reads logs only).
2. **44% of bonding-curve trades reach pump.fun only through a lookup table**
   (trial sample). The live feed must deliver them for the recorder's miss rate to be
   ~0.6%; the parity test measures this directly (`backfill_only_via_lookup`).
3. **Disk is the limit for 28 days, not bandwidth.** The trial kept about 4 GB per day
   of chain, mostly PumpSwap trades (~240 bytes per row). 28 days would be about
   110-115 GB; D: has 86 GB free. Options for Phase 4: keep PumpSwap rows only for
   tokens that launched in the window, drop or shrink columns, or a bigger drive. The
   pilot measures this on a full epoch.
4. **Epoch size:** epoch 1083 is 416.7 GB (6,750 chunks, no gaps between slots). The
   archive has no filter, so the whole epoch is downloaded.
5. **Not stored yet:** Jito tips and the priority fee itself (spec 5 lists `priority_fee`
   and `jito_tip` on trades). The raw fee and signature count are stored, and the
   archive position of each kept transaction, so tips can later be read from about 1% of
   the epoch instead of all of it. Proposed for Phase 4.
6. Epoch 1084 (overlaps the recording from 12:03 UTC Nov 12 to its end, ~00:06 UTC
   Nov 13) was not in the archive at 16:23 UTC. Expected about 6 hours after its end.

### Pilot epoch 1084 (run 2026-11-12, times PST)

Published about 8:20 PM; downloaded 8:43-10:52 PM (1.75 h, 65 MB/s on average).

| Check | Result |
|---|---|
| Download size | 409.9 GB, all 6,750 chunks, 0 failed (`backfill/gaps/` empty) |
| Disk use | 5.74 GB kept: 5.34 GB per day of chain, so 28 days is about 150 GB (D: has about 275 GB free) |
| Parse time | 4.62 CPU hours, 25 MB/s per core; 4.3 CPU hours per day of chain |
| Read | 168,176,917 transactions; 39,647,122 kept; 42,560,380 failed ones dropped; 0 unreadable, 0 undecodable events, 0 cut-off logs |
| Rows | tokens 45,439; trades 4,388,491; amm_trades 20,700,870; amm_pools 3,690; curve_completes 961; migrations 961; post_complete_buys 274 |
| Network per day of chain | 381 GB (settings said 310; updated) |

Download speed: the speed limit is split evenly across the workers and each worker stops
downloading while it decodes, so 6 workers at 80 MB/s gave about 52 MB/s; restarted with
8 workers at 100 MB/s, about 60-65 MB/s. Bursts reached 107 MB/s, so the line is not the
limit. Worth fixing before the 28-day backfill (a shared limit instead of a split one).

**Parity** (`agent parity --epoch 1084`; overlap 4:03:52 AM to 4:06:57 PM Nov 12):

| Table | In both | Differ | Only recorder | Only backfill: in gaps | outside gaps (share) |
|---|---|---|---|---|---|
| tokens | 22,457 | 0 | 4 | 3,524 | 158 (0.70%) |
| trades | 2,142,090 | 29 | 28,451 | 354,093 | 14,029 (0.65%) |
| amm_trades | 245,976 | 0 | 147 | 41,373 | 1,738 (0.70%) |
| curve_completes, migrations, post_complete_buys, amm_pools | 1,577 | 0 | 0 | 196 | 8 |

**Result as defined: NOT PASSED.** 29 trades differ, and the 28,468 transactions only the
recorder has could not be checked against the chain (the RPC refused, HTTP 413). What
was found:
1. **The 29 differing trades are forks, not parser errors.** Same transaction, but the
   recorder has it 2-3 slots earlier than the archive. The chain (`getTransaction`,
   finalized) confirms the archive's slot for all 3 checked: the live feed delivered the
   copy from a block that was later dropped, and the transaction landed again 2-3 slots
   later (in some, against a different curve state, so the amounts differ too).
2. **The 28,451 recorder-only trades (1.3% of what the recorder kept) never landed.**
   They are in 6,809 slots that all have a block in the archive, and the backfill kept
   other trades from 6,797 of those blocks. 30 of 30 sampled are not on chain, while 3 of
   3 control transactions (in both) are. So the live feed delivers about 1.3% of trades
   that never land, even at commitment `confirmed`; the archive reader is not missing them.
3. The recorder has 10 repeated rows (same transaction and event twice).
4. **The recorder's miss rate, measured against the archive outside logged gaps: 0.70% of
   tokens, 0.65% of trades** (Phase 1 asks for under 1% of tokens).
5. **The chain check was too heavy for the public RPC.** With the full history it takes
   about 1.3 s per signature, so 256 per request (the method's maximum) ran out of time
   and was refused. Fixed in the step-b copy: 8 per request, and every "missing" is asked
   once more (the node once reported a real transaction missing). Checking all 28,468
   would take about 10 hours.

**Chain check on a sample** (the user chose a random sample of 1,000 over checking all
28,468, which would take about 10 hours on the public RPC; run 11:22-11:48 PM,
`audits/parity-e1084-chain-sample-20261112T234821.json`, seed 20261112):

| Check | Result |
|---|---|
| 1,000 random recorder-only transactions | **1,000 not on chain** (each "missing" asked twice) |
| What that allows for all 28,468 | at most 0.30% could be on chain (95% confidence) |
| 10 control transactions in both | found before and after the run (the node's history answered properly) |
| 29 trades whose slot differs | the chain agrees with the archive's slot for **all 29** |

So everything both copies captured is identical except 29 trades the live feed reported
from blocks that were dropped (the chain confirms the archive's copy), and the rows only
the recorder has are transactions that never landed. **Parity test: PASS**, with the fork
re-lands counted as matching and the sampled chain check as the test, both OK'd by the
user on 2026-11-13 (spec 4.1 note and change log 17). The sample can be repeated with
`tools/parity_chain_sample.py`.

**Phase 3 done when: met.** Download size, disk use and parse time measured; parity
passes. Closed with the user's OK on 2026-11-13.

## Phase 2: curve math + tests (2026-11-12)

**Done when:** "Simulated prices match on-chain trade prices within tolerance." **Result: met.**

`agent verify-curve --hours 24` on the recording so far (12:03-14:03 UTC):

| Trade kind | Trades | Exact | Within tolerance |
|---|---|---|---|
| sell | 104,371 | 100.000% | 100% |
| buy, set amount of SOL | 95,377 | 99.999% | 100% |
| buy, exact tokens | 24,447 | 100.000% | 100% |
| buy, multi-hop route | 5,481 | 99.453% | 100% |
| **all** | **229,676** | **99.987%** | **100%** |

Tolerance: quote and fees within 1 lamport, tokens within one part in a million. The
largest difference seen was 1 lamport. Threshold for PASS: 99.9% exact and 99.99% in
tolerance (set in `sim/verify.py`). Tests: 189 passed, lint clean.

Also checked:
- **Fee schedule from chain** (Global + FeeConfig accounts, decoded with the IDL): matches
  the rates charged on 100% of direct trades (151,393 SOL-paired, 26,193 paired with
  another coin, 1,184 USDC-paired).
- **Continuity:** 99.47% of trades on normal coins start exactly where a recorded event
  left the curve. The 0.53% that do not are worth a look in Phase 3 (likely the logged
  gaps and cut-off logs).

What was built:
- `sim/curve_math.py`: buy (exact tokens or a set budget) and sell, fees, price impact,
  market cap, curve progress. Formulas from pump-sdk 4.1.0 (`bondingCurve.ts`, `fees.ts`),
  confirmed on recorded trades.
- `sim/chain_config.py`: reads `Global` and `FeeConfig` from chain into dated snapshots
  (`data/config_snapshots/`); fee schedule by quote type and market-cap tier. Account
  addresses are derived from the IDL. `agent snapshot-config` saves one now; the engine
  saves one at start-up and every 6 hours.
- `sim/verify.py` and `agent verify-curve`: the check above.
- `borsh_idl.py` can now decode accounts; the parser records one more event
  (`mayhem_params`, below).

Findings that matter later:
- **Mayhem mode coins.** About a fifth of new coins are "mayhem" coins, on which pump.fun's
  own agent trades. The agent's trades (63,378 so far, about 22% of all trades) are
  fee-free and priced by the mayhem program, not by the curve formula; it also resets
  these coins' reserves between trades. Ordinary traders on mayhem coins follow the
  normal formula exactly. The agent's wallet is derived from the IDL (`sol_vault_authority`
  of `set_mayhem_virtual_params`), nothing hardcoded.
- Because the resets were not recorded, only 36% of trades on mayhem coins start from a
  recorded state. The parser now records the resets (`mayhem_params`); this takes effect
  when the engine is next restarted (after Phase 1's audit), and the continuity number
  should then be re-checked.
- **Two fee details the docs do not state**, found in the data: a coin without a creator
  pays no creator fee even though its events show a rate; and a set-budget buy computes
  fees before trimming the net by up to 1 lamport.
- **Multi-hop routes** (a buy through one coin into another) charge each fee once for the
  whole route, so the per-coin schedule does not apply to them. Padre's normal buys are
  direct, so this does not affect backtests.
- **Not simulated:** the part of a v3 buy that goes past the end of the curve into the
  future PumpSwap pool. Such a buy is capped at the curve's remaining tokens and flagged.

Proposed spec changes, decided by the user on 2026-11-12 (spec 8.10, 10, change log 14):
1. Section 7 and 8: treat mayhem coins as their own category (a feature, or a separate
   model), since an automated pump.fun trader moves their prices. **Accepted.**
2. Section 10: backtests skip the mayhem agent's trades when measuring market activity,
   and use the recorded resets for the curve state. **Declined:** the agent's trades stay
   in, flagged per trade; the recorded resets are used for curve state.

## Phase 1: parser + recorder (2026-11-12)

**Done when:** "24h run with < 1% missed tokens (spot-check against an explorer); gaps logged."
**Result: met. Phase 1 closed with the user's OK on 2026-11-13.** The 24-hour audit found
0.54% of coins missed outside logged gaps, and every gap was logged. It checked against
the chain itself (every launch), which covers the spec's explorer spot-check.

**The 24-hour audit** (`agent audit --hours 24 --trades-sample 20`, run 2026-11-13 and
finished 7:03 PM PST). Checked against the chain itself (every coin launch the chain
lists), not an explorer. Window: the last 24 recorded hours, Nov 12 10:11:08 AM to Nov 13
10:11:08 AM PST. The user stopped the engine at 10:11 AM, Nov 13. Details:
`D:\memecoin-agent-data\audits\audit-20261114T030312.json`.

| Check | Result |
|---|---|
| Coin launches on chain | 46,430 |
| Recorded | 41,554, plus 5 that the live feed delivered but never landed on chain |
| Missed outside logged gaps | **251 (0.54%; target under 1%)** |
| Missed inside logged gaps | 4,625 |
| Gaps in the window | 271, 1 h 56 m in total (8.0% of the 24 hours); every one logged |
| Trades of 20 random coins | 1,046 on chain, 2 missed (0.19%); 28 recorded that never landed (the live-feed effect Phase 3 measured at 1.3% of all trades) |
| Could not be checked | 0, after a recheck. The audit printed "NOT PASSED" only because the public server returned nothing for 2 transactions; asked again, both were coin launches inside logged gaps, so the counts above include them. |

What it means: outside its gaps the recorder misses almost nothing, but the free public
feed dropped out often enough that 10.5% of the day's coins were not recorded (89.5%
covered). On the research track this does not affect the models, since training uses the
complete backfill (spec 4.1). It is a finding for the write-up about the live feed.

**Earlier result:** built and tested; a six-minute live run passed the audit.

| Check | Result |
|---|---|
| Tests | 141 passed (`.venv\Scripts\pytest`), lint clean |
| Parser on real transactions | 46 mainnet transactions captured on 2026-11-12 (every pump.fun and PumpSwap instruction seen in 7 minutes, plus a bonding-curve completion and a migration): all decode, 0 errors, 0 events missing from the IDL |
| Six-minute live run | 165 tokens and 12,865 trades written; 0 reconnects; 0 decode errors; events arrive a median 1.1 s behind the chain (95% within 2.5 s) |
| Gap log | A deliberate 15 s pause was logged as one gap with exact start and end slots |
| Audit of that run (4.4 min window) | 130 creates on chain: 119 recorded, 10 missed inside the logged pause, 1 in the first second of the recording (an edge the audit now skips). No real misses. Trades of 8 random tokens: 470 on chain, 0 missed |
| Reading while writing | DuckDB read the Parquet files while the engine was writing them: fine on Windows |

**24-hour run:** started 2026-11-12 04:03 local time (12:03 UTC) with the user's OK, on a clean
data folder (the six-minute test recording was deleted with the user's OK). Two minutes in:
55 tokens and 4,456 trades written, 3 simulated transactions dropped, none on disk, no gaps.

Restarts during the run (all in the gap log):
- 04:29:46 the user stopped it and restarted it at 04:30:40 from their own PowerShell, to move it
  out of Claude's shell (54 s gap).
- 04:31:02 that run ended when the user closed the window, before its first write: its
  ~20 s of rows were lost. The user started it again at 04:32:24 (157 s gap, which covers
  the lost rows). Checked at 04:39: running, and not tied to any window, job or app.

Fix (2026-11-12): `agentd start` now asks Windows' process service (WMI) to start the engine,
with pythonw.exe and no window. Cause, confirmed by simulation: a terminal that ends
everything started from it on close, and refuses to let processes leave, also ended the
detached engine. A program started the new way survives it (`tests/test_launch.py`). Two
related bugs fixed: a crash before a run's first write was labeled `recorder_off` instead
of `crash` (coverage was already correct), and two writers in one process within the same
second could give two data files the same name. Tests: 146 passed.

- 04:45:27 the 04:32 run (started with the old method) ended without a clean shutdown; cause
  unknown. Restarted at 04:46:45 with the new method (78 s crash gap).
- 05:48:42 the public Solana endpoint dropped the connection and refused new ones (HTTP 413)
  for about a minute; the recorder kept retrying, switched between the two public
  addresses, reconnected by itself and logged a 64 s disconnect gap.

- 06:05 and 06:20: two more public-endpoint drops (93 s and 9 s), handled the same way.
- 06:10: the engine was stopped and started again with `agentd stop` / `agentd start`
  (23 s gap). Since then it runs the Phase 2 code: chain snapshot saved at start-up,
  mayhem reserve resets recorded.

**Interim audit at 06:22 (2.1 h window, not the Phase 1 result):** PASS. 3,422 creates on
chain; 22 missed outside logged gaps (0.64%), 261 inside gaps. The 22 are spread evenly
in time, not near gaps: the public feed silently drops about 0.6% of transactions.
Trades of 20 sample tokens: 0.69% missed. Also 5 recorded transactions (2 creates, 3
trades) are unknown to the chain: the feed delivered transactions that never landed.
Ideas for later: a second simultaneous connection to cut silent drops; the backfill
(Phase 3-4) is the clean source for training either way.

`agentd status` now shows how long the engine has run, how much time is recorded (minus
gaps), and a bar toward the 24-hour burst (`recorder.goal_hours`). Tests: 153 passed.

To finish Phase 1, when the status bar reaches 100% (about 04:07-04:10 on 2026-11-13) run
`.venv\Scripts\agent audit --hours 24 --trades-sample 20` and record the result here.

What was built:
- `ingest/idl/` the three official IDL files, pinned to commit `639b87f` (2026-10-13), with `SOURCE.txt`.
- `ingest/borsh_idl.py` decoder driven by the IDL (no layout in code); `ingest/solana.py` base58 and PDA derivation.
- `ingest/parsers.py` the one parser for live and backfill; `ingest/recorder.py` Parquet writer (temp file then rename); `ingest/gaps.py` gap log and tracker; `ingest/pipeline.py` ties them together.
- `ingest/rpc.py` and `audit.py`: `agent audit` compares the recording with the chain.
- `agentd pause-recording` / `resume-recording`; `agentd status` shows rows written, lag, gaps, disk.
- `tools/capture_fixtures.py` re-captures real test transactions when pump.fun changes its programs.

Findings that matter later:
- **Simulated transactions leak into the public feed.** They carry an all-zero signature and never happened on chain. The recorder now drops them. The six-minute test recording still holds one such token row and one trade row (written before the fix).
- **The live program is newer than the official IDL.** Trade events carry 8 more bytes than the IDL (2026-10-13) describes; usually zero, sometimes not. Known fields decode; the extra bytes are stored in `extra_hex`, so they can be decoded once pump.fun publishes the layout.
- **PumpSwap is not recorded live.** Its stream measured 319 messages/s, about 83 GB/day, four times the bonding-curve stream. PumpSwap rows are written only when a transaction also touches the bonding-curve program (migrations, multi-hop swaps).
- **Not everything is SOL-paired.** In a one-minute sample, 11% of trades and 19% of new tokens were paired with another coin (mostly the PUMP token), and 22% of new tokens were mayhem-mode. For those trades `sol_amount` is 0 and `quote_amount` holds the amount.
- Only 56% of new mints in that sample end in `pump` (spec 12.3 treats that as usual).
- Event time (`ts`) can be one second earlier than the node's block time. `ts` is used everywhere because the backfill will see the same value.
- **Storage:** 179 bytes per trade row and 682 per token row on disk, about 0.65 GB per day. My earlier estimate (0.15-0.35 GB) was too low.
- **Network:** the stream measured 22-54 GB per day across runs (it grows with market activity).
- Logs cut short by the node: 7 transactions in six minutes (about 0.05%); listed in `truncated_txs` for repair from the backfill.
- Not yet seen on real data: `PostCompleteBuyEvent` (rare), and transactions from before the 2026-09-12 and 2026-10-07 layout changes. Older layouts are tested by cutting a real event short; real old transactions arrive with the Phase 3 backfill.

Differed from the spec. Applied to `docs/SPEC.md` on 2026-11-12 with the user's approval (change log items 8-11), together with a fourth edit: record in bursts (24-hour runs) until Phase 12, continuously from then on.
1. Section 5: each table mirrors one on-chain event, with pump.fun's own column names (`user` stored as `wallet`). Bonding-curve trades are in `trades`, PumpSwap trades in `amm_trades`, instead of one `trades` table with a `venue` column.
2. Sections 2 and 15: live recording covers the bonding curve only, for the bandwidth reason above. PumpSwap history would come from the backfill; live PumpSwap for freshly graduated tokens could use one small subscription per new pool.
3. Section 12.3: a mint ending in `pump` is a weak hint, not a rule.

## Phase 0: setup (2026-11-12)

**Done when:** "Daemon connects and prints live create/trade events." **Result: met.**

| Check | Result |
|---|---|
| Unit and offline end-to-end tests | 37 passed (`.venv\Scripts\pytest`), lint clean |
| `agentd start --foreground` on the live feed, ~32 s | Connected to `solana-public` in under 1 s; printed 12 CREATE and 857 TRADE lines |
| `agentd status` while running | Showed pid, source, rates, data received, reconnects |
| `agentd start` (background), status from a separate command, `agentd stop` | Engine kept running between commands; second `start` said "already running"; stop took about 1 s; no process left behind |
| Secrets | No key in the log or status file; `.env` is git-ignored |

What was built:
- Repo, `.gitignore`, `.env.example`, `pyproject.toml`, `config/settings.yaml`, README.
- `src/agent/config.py` (settings + secrets), `logging_setup.py` (scrubs keys from every log line).
- `src/agent/ingest/ws_client.py` (live log stream: reconnect, quiet-feed detection, failover between sources) and `ingest/logs.py` (works out which program wrote each log line, so another program cannot fake a pump.fun event).
- `src/agent/daemon/main.py` (engine loop, heartbeat, stop flag), `daemon/control.py` (status/pid files), `cli/agentd.py` (`start`, `stop`, `status`).

Things to know:
- Phase 0 recognizes create and trade events by their 8-byte event tag only. It does not decode fields (mint, amounts) and writes nothing to disk. Both are Phase 1.
- **Data use:** the live stream measured 22-33 GB/day in three short runs (it varies with how many failed transactions are in the stream; my first measurement was 18.6 GB/day). The public endpoint does not compress, so that is real network use: about 20-35 GB for a 24 h test, 0.6-1 TB per month if left on nonstop.
- The feed delivered 5 of 869 events twice in one run (~0.6%). The Phase 1 recorder must drop repeats by signature.
- `wss://api.mainnet.solana.com` (the hostname in Solana's current docs) works and is the primary source; the older `-beta` hostname is the fallback.
- Not built in Phase 0, by design: `pause-recording` / `resume-recording` (Phase 1, with the gap log), `reload-models`, auto-start, the `agent` command.
- The project rules are in the README rather than a separate file.

## Section 18 verification (2026-11-12)

How each item was checked: **doc** = read the provider's current docs, **chain** = read from Solana mainnet via the public RPC, **measured** = measured on this PC, **not verified** = could not confirm.

| # | Item | Result | How |
|---|---|---|---|
| 1 | Helius free tier vs `logsSubscribe` on pump.fun | 1M credits/month, 10 RPC req/s, 5 WebSocket connections, standard WS methods only. WebSocket data is billed at 2 credits per 0.1 MB. The pump.fun log stream is ~133 msg/s, ~226 KB/s, ~18.6 GB/day (80% failed transactions) = ~381k credits/day = ~11M credits/month. **Does not fit** (free credits last under 3 days; the $49 plan's 10M would not cover it either). | doc + measured (30 s sample) |
| 2 | pump.fun instruction/event layouts | Official IDL (`pump-fun/pump-public-docs`, updated 2026-10-13) read. Create: `create`, `create_v2`. Trades: legacy `buy` / `sell` / `buy_exact_sol_in`; `*_v2` (27 accounts, non-SOL quote coins); `*_v3` (17 accounts, since 2026-10-07). Events: `CreateEvent`, `TradeEvent` (35 fields, includes per-trade fees, `quote_mint`, `ix_name`), `CompleteEvent`, `PostCompleteBuyEvent` (new), `CompletePumpAmmMigrationEvent`. Events appear both as `Program data:` log lines and as inner self-CPI instructions (seen on live transactions). Coins can now be paired with SOL, USDC, or another pump coin. | doc + chain |
| 3 | RugCheck endpoints | Keyless GET: `/v1/tokens/{mint}/report`, `/report/summary`, `/insiders/graph`, `/insiders/networks`, `/metadata`, `/votes`, `/v1/stats/*`. Bulk and write endpoints need a JWT. **No published rate limits**: cap ourselves, back off on HTTP 429, cache by mint. | doc (Swagger) |
| 4 | Bitquery sample | Repo exists with a Solana folder that includes pump.fun samples (trades, pools, per-token OHLCV). Sample only; full export is a paid product. Use for schema reference only. | doc |
| 5 | Old Faithful + Jetstreamer | Free hosting is live and nearly current (epoch 1083 file dated today; chain is in epoch 1084). CAR size: 293-388 GB for epochs 1075-1083, 555-672 GB for epochs 1040-1060. An epoch is now ~1.07 days (slots ~215 ms). 2-3 months = ~56-84 epochs = roughly 20-40 TB to stream. Download speed here: ~32 MB/s (~270 Mbit/s) in a short single-connection test, so ~3 h per recent epoch. Jetstreamer moved from Anza to Vaultic (`vaultic-win/jetstreamer`, v0.10.0, Rust 1.98.1+); no server-side program filter (stream the whole epoch, filter locally); CI covers Linux only, **Windows build not verified**. | doc + measured |
| 6 | MemeTrans / Kaggle | MemeTrans was renamed **MELT** (`git-disl/MELT`), license CC BY-NC 4.0, 41,470 tokens that migrated to Raydium: graduated tokens only and pre-PumpSwap mechanics, so usable only for the bundle-logic cross-check. Kaggle "Solana Skill Sprint" graduation competition exists (predict graduation from the first 100 blocks); **license, dates and rules not verified** (needs a logged-in Kaggle account). | doc / not verified |
| 7 | BigQuery | Public Solana dataset has user reports of stalling (2025-03-31) and multi-day delays (2025-11). **Current freshness and pump.fun decoded table names not verified** (needs a Google Cloud account). Not on the critical path. | not verified |
| 8 | Disk / machine | C: 30.6 GB free, D: 80.5 GB free. Ryzen 5 5600X, 32 GB RAM, desktop, sleep on AC already set to Never. | measured |
| 9 | Fee schedule + curve parameters | Read from the Global account `4wTV1Y...xnjf` and FeeConfig `8Wf5Ti...VwTt`: initial virtual token reserves 1,073,000,000,000,000; initial virtual SOL 30,000,000,000 lamports; initial real token reserves 793,100,000,000,000; total supply 1,000,000,000,000,000; pool migration fee 15,000,001 lamports; FeeConfig has one tier: protocol 95 bps + creator 30 bps; creator fee configurable up to 300 bps; buyback 5,000 bps of the protocol fee; max quote depth 2. Decoded by hand from IDL offsets: **preliminary**, to be re-checked against real trades in Phase 2. | chain |
| 10 | Clipboard on Windows | `pyperclip` 1.11.0 supports Windows natively. Plan: poll `GetClipboardSequenceNumber` (does not open the clipboard) and read only on change. **Not tested yet** (Phase 12). | doc |
| 11 | Windows specifics | Windows Terminal installed; long paths already enabled; Python 3.12.6 64-bit at `D:\COMP SCI\Python` (path contains a space, so use a venv). Parquet: write to a temp name, close, then rename; never rewrite a file a reader may have open. DuckDB: query Parquet read-only, no shared `.duckdb` file. Textual 8.2.8 rendering and Task Scheduler **not tested yet**. | measured / doc |
| 12 | Padre presets | Needed from the user before Phase 11: slippage, priority fee, buy size, take-profit, stop-loss, time stop. | open |

Other things found along the way:
- Both program IDs in the spec are correct and executable on mainnet. Fees now live in a separate fee program (`pfeeUxB6jkeY1Hxd7CsFCAjcbHA9rWtchMGdZ6VojVZ`).
- Solana now has version-1 transactions; RPC calls must allow them (`maxSupportedTransactionVersion`).
- About 37,000 pump.fun tokens are created per day (extrapolated from a 30 s sample).
- Public Solana RPC limits: 100 requests / 10 s per IP, 40 connections, 100 MB / 30 s. The pump.fun log stream uses ~7 MB / 30 s.
- PumpPortal: new-token and migration streams are free, trade streams are paid (0.01 SOL per 10,000 events).
- SQD Portal can filter history by program on the server, but its free tier is for evaluation only and is slated to be capped at ~50 MB-1 GB per month once billing starts.

## Open decisions

Decided by the user on 2026-11-12:
- Live feed: public Solana WebSocket as primary; Helius for lookups and gap repair.
- Data folder: `D:\memecoin-agent-data`.
- Spec moved to `docs/SPEC.md` with the seven recommended edits applied.
- After Phase 1: four more spec edits applied (SPEC.md change log items 8-11): event tables, bonding-curve-only live recording, `pump` suffix as a weak hint, recording in bursts until Phase 12.
- Internet plan: **unlimited data, confirmed** from the user's Xfinity usage page on 2026-11-12 ("Your plan comes with unlimited data"; 381 GB used so far in November). The recorder (22-54 GB per day) can run nonstop. The backfill is limited by time and by being far above normal home use, not by a monthly cap.

Still open:
1. ~~The audit of the 24-hour run (Phase 1 "done when").~~ Done 2026-11-13: met (0.54%
   missed outside gaps); the engine was stopped at 10:11 AM PST. Phase 1 closed with the
   user's OK.
   (Research track: the engine is not restarted, so the planned `agent verify-curve`
   re-check after a restart is not needed.)
1b. ~~A decision on the two proposed spec changes under Phase 2 (mayhem coins).~~ Decided 2026-11-12: own category, agent trades kept (spec 8.10).
2. Later option: live PumpSwap for freshly graduated tokens via one small subscription per new pool.
3. Padre presets (needed before Phase 11).
4. Backfill window (decide at the Phase 3 pilot). Connection: Xfinity, ~900 Mbps download, unlimited data.
5. Phase 3: the user OK'd the ~417 GB download of epoch 1084 on 2026-11-12 and runs it in their own window (`--wait`, starts once published). Then the pilot measurements and `agent parity --epoch 1084`.
6. Phase 4: disk plan for 28 days (about 110-115 GB at the trial's rate; D: has 194 GB free since the user cleared 114 GB on 2026-11-12, so it fits without trimming, with ~80 GB to spare), and the priority-fee / Jito-tip columns.
7. Phase order (user, 2026-11-12): keep section 15's order where it is functionally better, but get the agent live as soon as possible for trading feedback. **Approved by the user 2026-11-12 (spec 15.0, change log 15):** after Phases 1 and 3 close, run the full backfill (Phase 4) in the background, and meanwhile build live features, a rules-based verdict, the screen and the paper ledger (parts of Phases 8, 12, 13), so rules-based alerts go live and are paper-tracked; models (Phases 5-7, 9-11) replace the rules only if they beat them.
