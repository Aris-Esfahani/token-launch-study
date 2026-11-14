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
| R1. Labels | Not started | Starts on epoch 1084, already on disk (no download). `is_bundled_proxy` stands in for the bundle label (spec 7). |
| R2. Dataset | Not started | One row per coin and cutoff, features plus labels. |
| R3. Exploration | Not started | Needs Jupyter and plotting packages (free; the user installs them). |
| R4. More history | Not started | 7 more days: about 2.7 TB to download, about 12 h, about 37 GB kept. Asked for first. |
| R5. Models | Not started | Evaluation plan in spec 15.4, written to `config/evaluation.yaml` before any test-day scores. |
| R6. Backtest (analysis) | Not started | |
| R7. Write-up | Not started | |

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
