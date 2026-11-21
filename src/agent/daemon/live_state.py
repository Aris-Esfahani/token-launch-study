"""Live signals and verdicts for new tokens (spec 12.5; fast path step C).

Every row the recorder keeps also goes here. Each new token gets a `TokenState` (the same
code `agent signals` replays recorded data with) and is judged by the rules at T+30, 60,
120 and 300 s (spec 6). A verdict at cutoff T is made only once the recording has covered
the chain past T, so it uses every event up to T and none after it: the same snapshot a
replay of the recorded data gives later. If a gap touched the token's life by then, the
verdict says so (`has_gap`): its data may be incomplete.

Tokens created before the engine started, or whose create event fell in a gap, are not
tracked (there is no create event to start from). With two connections a token's create
can arrive after some of its trades (the faster connection missed it, the slower one
delivers it later), so trades of unknown tokens are held for a few minutes in case their
create turns up.

With the saved models loaded (spec 15.3, step L1) each verdict also carries their scores:
the calibrated probability of each stage, made from the verdict's own snapshot plus the
coin's launch context (`agent.scoring`). Analysis only; a verdict without a score says
why. Scores are made at the decision times only, the ages the models were trained at.
"""

from __future__ import annotations

import hashlib
import heapq
import json
import logging
import math
import time
from collections import Counter, deque
from dataclasses import dataclass, field
from pathlib import Path

from agent.clock import clock
from agent.features.token_state import SNAPSHOT_OFFSETS_S, CreatorIndex, TokenState
from agent.ingest.borsh_idl import Field
from agent.ingest.gaps import overlap_seconds
from agent.models.rules import PASS, Rule, RulesError, judge, load_rules
from agent.scoring.launch_context import LaunchHistory
from agent.scoring.live import LiveScorer

log = logging.getLogger(__name__)

# A cutoff counts as covered once the recording is this far past it. Event times are
# whole seconds, so events stamped with the cutoff second can still arrive until the
# chain is a second past it; one more second allows for block times that are not
# strictly increasing.
SETTLE_S = 2
KEEP_S = 15 * 60  # spec 12.5: rolling state for tokens under 15 minutes old
HOLD_S = 180  # trades of a token not seen yet wait this long (on chain) for its create
TABLES = ("tokens", "trades", "curve_completes")  # the recorded tables the signals read
# Signals sent with each alert, so the screen can show them without asking again.
ALERT_SIGNALS = (
    "market_cap_sol", "curve_progress_pct", "unique_buyers", "holders", "top10_hold_pct",
    "dev_hold_pct", "sniper_hold_pct", "net_sol_last_30s",
)  # fmt: skip

VERDICT_TABLE = "verdicts"
VERDICT_COLUMNS = [
    Field("ts", "i64"),  # the cutoff, so files are grouped by the day it falls in
    Field("mint", "text"),
    Field("symbol", "text"),
    Field("name", "text"),
    Field("created_ts", "i64"),
    Field("offset_s", "i64"),
    Field("level", "text"),
    Field("reasons", "json"),
    Field("has_gap", "bool"),
    Field("decided_at", "f64"),  # wall-clock time the verdict was made
    Field("wait_s", "f64"),  # decided_at minus the cutoff: how late the verdict came
    Field("rules_sha", "text"),  # which version of config/rules.yaml judged it
    Field("snapshot", "json"),  # every signal the verdict saw
    # The models' scores (agent.scoring.live.LiveScorer.score), or why there are none;
    # empty when the engine ran without the models.
    Field("scores", "json"),
]


def rules_fingerprint(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()[:12]


@dataclass
class _Tracked:
    state: TokenState
    symbol: str | None
    name: str | None
    history: list[dict] = field(default_factory=list)  # this token's verdicts so far
    curve: str | None = None  # its bonding-curve account


@dataclass
class LiveStats:
    tokens_tracked: int = 0
    tokens_seen: int = 0
    verdicts: Counter = field(default_factory=Counter)  # by level
    verdicts_with_gap: int = 0
    # (decided_at, wait_s) of recent verdicts, for the median delay in `agentd status`
    recent_waits: deque = field(default_factory=lambda: deque(maxlen=500))
    last_alert: dict | None = None
    scored: int = 0  # verdicts with model scores
    not_scored: int = 0  # verdicts the models were on for but could not score
    last_not_scored: str | None = None  # the latest reason


class LiveSignals:
    def __init__(
        self,
        rules: list[Rule],
        rules_sha: str,
        creators: CreatorIndex,
        gaps: list[dict],
        *,
        offsets: tuple[int, ...] = SNAPSHOT_OFFSETS_S,
        keep_s: int = KEEP_S,
        settle_s: int = SETTLE_S,
        agent: str | None = None,
        scorer: LiveScorer | None = None,
        history: LaunchHistory | None = None,
        models_off: str | None = None,
    ) -> None:
        self.rules = rules
        self.rules_sha = rules_sha
        self.creators = creators
        # The saved models and the launch history their context signals are counted
        # from; without either there are no scores and `models_off` says why.
        self.scorer = scorer if history is not None else None
        self.history = history
        self.models_off = None if self.scorer is not None else (
            models_off or "the engine was started without the saved models")
        # True while no event was ever recorded before this run: the history then starts
        # at this run's first recorded trade.
        self._first_recording = history is not None and history.recorded_from is None
        self._gaps = gaps  # the gap log's rows; grows as the engine logs new gaps
        self._offsets = offsets
        self._keep_s = max(keep_s, max(offsets))
        self._settle_s = settle_s
        self._agent = agent
        self._tokens: dict[str, _Tracked] = {}
        # Bonding-curve account -> mint: Padre shows both, and either may get copied.
        self._curves: dict[str, str] = {}
        # Rows of tokens not tracked (yet), by mint, and the order they arrived in.
        self._held: dict[str, deque[tuple[str, dict]]] = {}
        self._held_order: deque[tuple[int, str]] = deque()
        self._due: list[tuple[int, int, str, int]] = []  # (cutoff, order, mint, offset)
        self._order = 0
        self.stats = LiveStats()

    def add(self, table: str, row: dict) -> None:
        """One recorded row, in stored form (see Pipeline.stored)."""
        if table == "tokens":
            mint = row["mint"]
            if mint in self._tokens:
                return
            state = TokenState(row, agent=self._agent)
            curve = row.get("bonding_curve")
            tracked = self._tokens[mint] = _Tracked(
                state, row.get("symbol"), row.get("name"), curve=curve
            )
            if curve:
                self._curves[curve] = mint
            self.creators.add(row["creator"], row["ts"], mint)
            if self.history is not None and self.history.launches is not self.creators:
                self.history.launches.add(row["creator"], row["ts"], mint)
            for offset in self._offsets:
                self._order += 1
                heapq.heappush(self._due, (state.created_ts + offset, self._order, mint, offset))
            self.stats.tokens_seen += 1
            for held_table, held_row in self._held.pop(mint, ()):
                self._apply(tracked, held_table, held_row)
        elif table in ("trades", "curve_completes"):
            if self._first_recording and table == "trades":
                first = self.history.recorded_from
                if first is None or row["ts"] < first:
                    self.history.recorded_from = float(row["ts"])
            tracked = self._tokens.get(row["mint"])
            if tracked is not None:
                self._apply(tracked, table, row)
            else:
                self._held.setdefault(row["mint"], deque()).append((table, row))
                self._held_order.append((row["ts"], row["mint"]))
        self.stats.tokens_tracked = len(self._tokens)

    @staticmethod
    def _apply(tracked: _Tracked, table: str, row: dict) -> None:
        if table == "trades":
            # A replay reads a token's trades from its creation on; do the same.
            if row["ts"] >= tracked.state.created_ts:
                tracked.state.add_trade(row)
        else:
            tracked.state.add_complete(row)

    def due(self, covered_ts: float | None, now: float | None = None) -> list[dict]:
        """Verdicts for every cutoff the recording has now covered (`covered_ts` is how
        far on chain it has), oldest first. Also forgets tokens past `keep_s`."""
        if covered_ts is None:
            return []
        now = time.time() if now is None else now
        out: list[dict] = []
        while self._due and self._due[0][0] + self._settle_s <= covered_ts:
            cutoff, _, mint, offset = heapq.heappop(self._due)
            tracked = self._tokens.get(mint)
            if tracked is None:
                continue
            out.append(self._verdict(tracked, cutoff, offset, now))
        self._forget(covered_ts)
        return out

    def _verdict(self, tracked: _Tracked, cutoff: int, offset: int, now: float) -> dict:
        state = tracked.state
        snapshot = state.snapshot(cutoff, creators=self.creators)
        try:
            verdict = judge(snapshot, self.rules)
            level, reasons = verdict.level, verdict.reasons
        except RulesError as exc:  # a rule names a signal that does not exist
            level, reasons = "error", [str(exc)]
        has_gap = overlap_seconds(self._gaps, state.created_ts, cutoff) > 0
        row = {
            "ts": cutoff,
            "mint": state.mint,
            "symbol": tracked.symbol,
            "name": tracked.name,
            "created_ts": state.created_ts,
            "offset_s": offset,
            "level": level,
            "reasons": reasons,
            "has_gap": has_gap,
            "decided_at": now,
            "wait_s": round(now - cutoff, 2),
            "rules_sha": self.rules_sha,
            "snapshot": snapshot,
            "scores": self._scores(state, snapshot, has_gap),
        }
        tracked.history.append({"offset_s": offset, "level": level, "wait_s": row["wait_s"],
                                "scores": row["scores"]})  # fmt: skip
        self.stats.verdicts[level] += 1
        self.stats.verdicts_with_gap += has_gap
        self.stats.recent_waits.append((now, row["wait_s"]))
        if level == PASS and not has_gap:
            self.stats.last_alert = {"at": now, "mint": state.mint, "offset_s": offset}
        return row

    def _scores(self, state: TokenState, snapshot: dict, has_gap: bool) -> dict | None:
        """The models' scores for a verdict's snapshot, or why there are none. Whatever
        goes wrong here must never cost a verdict."""
        if self.scorer is None:
            return None
        try:
            context = self.history.context(state.creator, state.created_ts)
            scores = self.scorer.score(snapshot, context, has_gap)
        except Exception as exc:  # noqa: BLE001 - scoring is extra; verdicts go on
            log.exception("model scoring failed for %s", state.mint)
            scores = {"model": self.scorer.name, "why": f"scoring failed ({type(exc).__name__})"}
        if "p" in scores:
            self.stats.scored += 1
        else:
            self.stats.not_scored += 1
            self.stats.last_not_scored = scores.get("why")
        return scores

    def models(self) -> dict:
        """Whether model scores are on, and what the screen should say about them."""
        if self.scorer is None:
            return {"on": False, "why": self.models_off}
        return {"on": True, **self.scorer.info()}

    def _forget(self, covered_ts: float) -> None:
        old = [m for m, t in self._tokens.items() if t.state.created_ts + self._keep_s < covered_ts]
        for mint in old:
            self._curves.pop(self._tokens.pop(mint).curve or "", None)
        self.stats.tokens_tracked = len(self._tokens)
        # Held rows leave in the order they arrived (each mint's queue is in that order too).
        while self._held_order and self._held_order[0][0] + HOLD_S < covered_ts:
            _, mint = self._held_order.popleft()
            rows = self._held.get(mint)
            if rows:
                rows.popleft()
                if not rows:
                    del self._held[mint]

    @property
    def held_rows(self) -> int:
        return len(self._held_order)

    def median_wait_s(self, within_s: float = 600, now: float | None = None) -> float | None:
        now = time.time() if now is None else now
        waits = sorted(w for at, w in self.stats.recent_waits if now - at <= within_s)
        return waits[len(waits) // 2] if waits else None

    def analyze(self, mint: str, covered_ts: float | None, now: float | None = None) -> dict:
        """What the engine knows about `mint` right now (spec 12.2, the fast reply), from
        memory only: its signals up to the last second the recording has fully covered,
        and the rules' verdict on them. Not counted as a verdict and not saved."""
        now = time.time() if now is None else now
        copied = mint
        mint = self._curves.get(mint, mint)  # a bonding-curve address finds its coin
        tracked = self._tokens.get(mint)
        if tracked is None:
            if mint in self._held:
                why = ("it is trading, but it launched more than 15 minutes ago (only coins "
                       "in their first 15 minutes are followed live), before the engine "
                       "started, or during a gap")  # fmt: skip
            else:
                why = ("not in the live data: launched more than 15 minutes ago or before "
                       "the engine started, missed in a gap, or not a pump.fun coin")  # fmt: skip
            return {"mint": mint, "known": False, "why": why}
        state = tracked.state
        # The same settled point a verdict waits for: every event up to here has arrived.
        if covered_ts is None:
            cutoff = state.created_ts
        else:
            cutoff = max(math.floor(covered_ts) - self._settle_s, state.created_ts)
        snapshot = state.snapshot(cutoff, creators=self.creators)
        try:
            verdict = judge(snapshot, self.rules)
            level, reasons = verdict.level, verdict.reasons
        except RulesError as exc:
            level, reasons = "error", [str(exc)]
        return {
            "mint": mint,
            "known": True,
            "copied": copied,  # what was asked about: the mint, or the coin's bonding curve
            "symbol": tracked.symbol,
            "name": tracked.name,
            "created_ts": state.created_ts,
            "cutoff_ts": cutoff,
            "data_age_s": round(now - cutoff, 1),
            "level": level,
            "reasons": reasons,
            "has_gap": overlap_seconds(self._gaps, state.created_ts, cutoff) > 0,
            "verdicts": [self._with_usual(v, state.is_mayhem) for v in tracked.history],
            "models": self.models(),
            "rules_sha": self.rules_sha,
            "snapshot": snapshot,
        }


    def _with_usual(self, verdict: dict, is_mayhem: bool) -> dict:
        """A past verdict for the screen: with each stage's usual rate beside its score."""
        if self.scorer is None or not verdict.get("scores"):
            return dict(verdict)
        return {**verdict, "usual": self.scorer.usual(verdict["offset_s"], is_mayhem)}


class RulesFile:
    """config/rules.yaml, read again when it changes, so rules can be edited while the
    engine runs. A file that does not parse is reported and the previous rules are kept."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._mtime: float | None = None
        self.rules: list[Rule] = []
        self.sha = ""
        self.error: str | None = None

    def load(self) -> bool:
        """Read the file if it changed. Returns True if new rules were loaded."""
        try:
            mtime = self.path.stat().st_mtime
        except OSError as exc:
            self.error = f"cannot read {self.path.name}: {exc}"
            return False
        if mtime == self._mtime:
            return False
        self._mtime = mtime
        try:
            rules = load_rules(self.path)
        except (OSError, RulesError) as exc:
            self.error = f"{self.path.name} not loaded, keeping the previous rules: {exc}"
            return False
        self.rules, self.sha, self.error = rules, rules_fingerprint(self.path), None
        return True


def alert_summary(verdict: dict) -> dict:
    """A verdict without its full snapshot: what the screen's alert feed shows."""
    keep = ("ts", "mint", "symbol", "name", "created_ts", "offset_s", "level", "reasons",
            "has_gap", "decided_at", "wait_s")  # fmt: skip
    summary = {k: verdict[k] for k in keep}
    summary["signals"] = {k: verdict["snapshot"].get(k) for k in ALERT_SIGNALS}
    return summary


def alert_line(verdict: dict) -> str:
    """One line for the alerts log and the foreground view, mint first (to paste into
    Padre)."""
    at = f"{clock(verdict['decided_at'], seconds=True):>11s}"  # aligned: "10:05:27 PM"
    symbol = f"${verdict['symbol']}" if verdict.get("symbol") else "$?"
    late = f", {verdict['wait_s']:.0f}s late" if verdict["wait_s"] >= 10 else ""
    gap = "  (data incomplete: recording gap)" if verdict["has_gap"] else ""
    reasons = "; ".join(verdict["reasons"]) if verdict["reasons"] else "no rule fired"
    return (
        f"{at}  {verdict['mint']}  {symbol}  T+{verdict['offset_s']}s  "
        f"{verdict['level'].upper()}{late}  {reasons}{gap}"
    )


def to_json(value: object) -> str:
    return json.dumps(value, separators=(",", ":"))
