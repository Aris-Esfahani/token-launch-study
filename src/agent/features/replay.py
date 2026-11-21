"""Recorded (or backfilled) tokens replayed through the code the engine uses live.

`load_states` reads tokens created in a time window and their events from Parquet into
`TokenState`s; `snapshots` takes their signals at the spec's cutoffs. `outcomes` is the
only function here that looks past a cutoff: it gives what happened to each token later,
for checking rules and models, never as an input to them.

By default the live recording (`raw/`) and the backfill (`raw_backfill/`) are both read,
the recorder's copy of an event winning, and gaps come from the recorder's gap log. With
`roots=BACKFILL_ONLY` (the research dataset; spec 4.1, research track) only the backfill
is read, and the time it does not cover counts as gaps.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import duckdb

from agent.features.token_state import SNAPSHOT_OFFSETS_S, CreatorIndex, TokenState
from agent.ingest.backfill_coverage import backfill_gaps, backfill_spans
from agent.ingest.gaps import overlap_seconds, read_gaps

RECORDED, BACKFILLED = "raw", "raw_backfill"
ROOTS = (RECORDED, BACKFILLED)
BACKFILL_ONLY = (BACKFILLED,)
TRADE_COLUMNS = (
    "mint, slot, ts, signature, event_index, wallet, is_buy, quote_amount, token_amount, "
    "virtual_quote_reserves, virtual_token_reserves, real_quote_reserves, real_token_reserves"
)
DEAD_AFTER_S = 30 * 60  # spec 7: is_dead = no trades after T+30 min
GRADUATE_WITHIN_S = 60 * 60  # spec 7: graduates_1h
OUTCOME_WINDOW_S = 60 * 60  # both outcomes are known one hour after creation
OUTCOME_MAX_GAP_SHARE = 0.10


@dataclass
class Replay:
    states: list[TokenState]
    creators: CreatorIndex
    data_end_ts: int | None  # the newest event recorded: no cutoff may pass it
    gaps: list[dict]

    def has_gap(self, state: TokenState, until_ts: int) -> bool:
        """True if recording was off at any time between the token's creation and
        `until_ts`: its events may be incomplete (spec 3.2)."""
        return self.gap_seconds(state.created_ts, until_ts) > 0

    def gap_seconds(self, start_ts: float, end_ts: float) -> float:
        """How much of [start_ts, end_ts] the recording missed."""
        return overlap_seconds(self.gaps, start_ts, end_ts)


def _tables(data_dir: Path, table: str, roots: tuple[str, ...]) -> list[str]:
    return [
        f"{(data_dir / root / table).as_posix()}/*/*.parquet"
        for root in roots
        if any((data_dir / root / table).glob("*/*.parquet"))
    ]


def _scan(db: duckdb.DuckDBPyConnection, data_dir: Path, table: str, name: str,
          roots: tuple[str, ...]) -> bool:  # fmt: skip
    """A view over a table's files in `roots`, one row per event."""
    patterns = _tables(data_dir, table, roots)
    if not patterns:
        return False
    if len(roots) == 1:  # one source: `TokenState` already counts a repeated event once
        db.execute(f"CREATE OR REPLACE TEMP VIEW {name} AS SELECT * FROM "
                   f"read_parquet({patterns!r}, union_by_name=true)")  # fmt: skip
        return True
    db.execute(
        f"CREATE OR REPLACE TEMP VIEW {name} AS SELECT * EXCLUDE (_n) FROM ("
        f"SELECT *, row_number() OVER (PARTITION BY signature, event_index ORDER BY source DESC) "
        f"AS _n FROM read_parquet({patterns!r}, union_by_name=true)) WHERE _n = 1"
    )  # the recorder's copy wins over the backfill's ('recorder' > 'backfill')
    return True


def load_states(data_dir: Path, start_ts: int, end_ts: int, *,
                max_offset_s: int = max(SNAPSHOT_OFFSETS_S), after_s: int = 0,
                roots: tuple[str, ...] = ROOTS) -> Replay:  # fmt: skip
    """Tokens created in [start_ts, end_ts) with their trades up to `max_offset_s` after
    creation (plus `after_s` more, for `outcomes`), read from `roots`."""
    db = duckdb.connect()
    db.execute(f"SET temp_directory = '{(data_dir / 'duckdb_tmp').as_posix()}'")
    if RECORDED in roots:
        gaps = read_gaps(data_dir / "gaps")
    else:
        gaps = backfill_gaps(backfill_spans(data_dir))
    if not _scan(db, data_dir, "tokens", "tokens", roots):
        return Replay([], CreatorIndex(), None, gaps)
    creators = CreatorIndex()
    for creator, ts, mint in db.execute(
        "SELECT creator, ts, mint FROM tokens WHERE ts < ? ORDER BY ts", [end_ts]
    ).fetchall():
        creators.add(creator, ts, mint)
    created = _dicts(db.execute(
        "SELECT * FROM tokens WHERE ts >= ? AND ts < ? "
        "QUALIFY row_number() OVER (PARTITION BY mint ORDER BY ts, signature) = 1 ORDER BY ts",
        [start_ts, end_ts],
    ))  # fmt: skip
    states = {row["mint"]: TokenState(row) for row in created}
    data_end = None
    if states and _scan(db, data_dir, "trades", "trades", roots):
        db.execute("CREATE TEMP TABLE wanted AS SELECT mint, ts AS created FROM tokens "
                   "WHERE ts >= ? AND ts < ?", [start_ts, end_ts])  # fmt: skip
        horizon = max_offset_s + after_s
        rows = db.execute(
            f"SELECT {TRADE_COLUMNS} FROM trades JOIN wanted USING (mint) "
            "WHERE trades.ts >= wanted.created AND trades.ts <= wanted.created + ?",
            [horizon],
        )  # fmt: skip
        for row in _dicts(rows):
            states[row["mint"]].add_trade(row)
        data_end = db.execute("SELECT max(ts) FROM trades").fetchall()[0][0]
        if _scan(db, data_dir, "curve_completes", "completes", roots):
            for row in db.execute(
                "SELECT mint, ts FROM completes JOIN wanted USING (mint) "
                "WHERE completes.ts <= wanted.created + ?", [horizon]
            ).fetchall():  # fmt: skip
                states[row[0]].add_complete({"ts": row[1]})
    return Replay(list(states.values()), creators, data_end, gaps)


def _dicts(cursor: duckdb.DuckDBPyConnection) -> list[dict]:
    names = [d[0] for d in cursor.description]
    return [dict(zip(names, row, strict=True)) for row in cursor.fetchall()]


def snapshots(replay: Replay, offsets: tuple[int, ...] = SNAPSHOT_OFFSETS_S) -> list[dict]:
    """One row of signals per token and cutoff, for cutoffs the recording has reached.
    Each row says whether a recording gap touched the token by then (`has_gap`)."""
    rows = []
    for state in replay.states:
        for offset in offsets:
            cutoff = state.created_ts + offset
            if replay.data_end_ts is None or cutoff > replay.data_end_ts:
                continue
            row = state.snapshot(cutoff, creators=replay.creators)
            row["offset_s"] = offset
            row["has_gap"] = replay.has_gap(state, cutoff)
            rows.append(row)
    return rows


def outcomes(replay: Replay) -> dict[str, dict]:
    """What happened to each token in its first hour (spec 7): `is_dead` (no trade after
    T+30 min) and `graduated_1h`. Only tokens whose hour has been recorded, with recording
    gaps covering at most `OUTCOME_MAX_GAP_SHARE` of it, get an entry: short gaps can hide
    a trade or two, not a whole hour of trading. These look into the future: for
    evaluating rules and models, never as signals."""
    out = {}
    for state in replay.states:
        end = state.created_ts + OUTCOME_WINDOW_S
        if replay.data_end_ts is None or end > replay.data_end_ts:
            continue
        if replay.gap_seconds(state.created_ts, end) > OUTCOME_MAX_GAP_SHARE * OUTCOME_WINDOW_S:
            continue
        trades = state.trades_until(end)
        out[state.mint] = {
            "is_dead": not any(t.ts > state.created_ts + DEAD_AFTER_S for t in trades),
            "graduated_1h": state.completed_by(state.created_ts + GRADUATE_WITHIN_S),
        }
    return out
