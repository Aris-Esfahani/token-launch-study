"""The backfill parity test (spec 4.1, mandatory): for a period both the live recorder and
the backfill cover, they must produce identical rows.

The two can never hold exactly the same set of transactions: the public live feed drops
some transactions without telling us (about 0.6% on 2026-11-12) and delivers a few that
never landed on chain. So the test is, per table, for the overlap window:

1. Every row both captured (same signature and event_index) is identical in every column
   the two share. Only `recv_ts` (when the recorder saw it) and `source` may differ.
2. Every row only the recorder has belongs to a transaction the chain has no record of
   (or the backfill is missing something: a failure).
3. Rows only the backfill has are what the recorder missed. They are counted, split into
   those inside the recorder's logged gaps (expected) and outside them (silent drops by
   the live feed), but they do not fail the test.

The live feed follows pump.fun only, so PumpSwap rows are compared only for transactions
that mention pump.fun (`bf_mentions_pump`); the backfill holds all PumpSwap trades.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path

import duckdb

from agent.ingest.gaps import read_gaps
from agent.ingest.oldfaithful_backfill import ROWS_FOLDER, read_progress

COMPARED_TABLES = (
    "tokens", "trades", "curve_completes", "post_complete_buys", "migrations",
    "mayhem_params", "amm_trades", "amm_pools",
)  # fmt: skip
PUMPSWAP_TABLES = ("amm_trades", "amm_pools")
NOT_COMPARED = {"recv_ts", "source", "date"}
EDGE_SLOTS = 10  # the first and last slots of the recording are only partly covered
EXAMPLES = 10

# check(signatures) -> {signature: "missing" | "failed" | "ok"}
ChainCheck = Callable[[list[str]], Awaitable[dict[str, str]]]


@dataclass
class TableParity:
    both: int = 0
    mismatched: int = 0
    mismatched_columns: dict[str, int] = field(default_factory=dict)
    mismatch_examples: list[dict] = field(default_factory=list)
    backfill_only: int = 0
    backfill_only_in_gaps: int = 0
    backfill_only_via_lookup: int = 0  # outside gaps, pump.fun reached via a lookup table
    recorder_only: int = 0
    recorder_only_signatures: list[str] = field(default_factory=list)
    duplicate_keys: dict[str, int] = field(default_factory=dict)

    @property
    def backfill_only_outside_gaps(self) -> int:
        return self.backfill_only - self.backfill_only_in_gaps

    @property
    def silent_miss_share(self) -> float:
        """Share of rows outside the gaps that the recorder missed."""
        seen = self.both + self.backfill_only_outside_gaps
        return self.backfill_only_outside_gaps / seen if seen else 0.0


@dataclass
class ParityResult:
    epoch: int
    windows: list[tuple[int, int]] = field(default_factory=list)  # slot ranges compared
    start_ts: int | None = None
    end_ts: int | None = None
    tables: dict[str, TableParity] = field(default_factory=dict)
    chain_status: dict[str, str] = field(default_factory=dict)  # recorder-only signatures
    unchecked: list[str] = field(default_factory=list)
    controls: list[str] = field(default_factory=list)  # in both: known to be on chain
    note: str = ""

    @property
    def compared_rows(self) -> int:
        return sum(t.both for t in self.tables.values())

    @property
    def mismatched_rows(self) -> int:
        return sum(t.mismatched for t in self.tables.values())

    @property
    def recorder_only_on_chain(self) -> list[str]:
        return sorted(s for s, status in self.chain_status.items() if status != "missing")

    @property
    def passed(self) -> bool:
        return (
            self.compared_rows > 0
            and self.mismatched_rows == 0
            and not self.recorder_only_on_chain
            and not self.unchecked
            and not any(t.duplicate_keys for t in self.tables.values())
        )

    def to_json(self) -> dict:
        return {
            "epoch": self.epoch,
            "passed": self.passed,
            "windows": self.windows,
            "start_ts": self.start_ts,
            "end_ts": self.end_ts,
            "compared_rows": self.compared_rows,
            "mismatched_rows": self.mismatched_rows,
            "tables": {
                name: {**t.__dict__, "backfill_only_outside_gaps": t.backfill_only_outside_gaps,
                       "silent_miss_share": t.silent_miss_share}
                for name, t in self.tables.items()
            },  # fmt: skip
            "recorder_only_on_chain": self.recorder_only_on_chain,
            "chain_status": self.chain_status,
            "unchecked": self.unchecked,
            "note": self.note,
        }


def done_windows(data_dir: Path, epoch: int, chunk_slots: int,
                 first_slot: int) -> list[tuple[int, int]]:
    """Slot ranges the backfill finished for the epoch, merged."""
    done = sorted(read_progress(data_dir / "backfill" / f"epoch-{epoch}" / "progress.jsonl").done)
    windows: list[tuple[int, int]] = []
    for cid in done:
        lo = first_slot + cid * chunk_slots
        hi = lo + chunk_slots - 1
        if windows and lo == windows[-1][1] + 1:
            windows[-1] = (windows[-1][0], hi)
        else:
            windows.append((lo, hi))
    return windows


def _glob(root: Path, table: str) -> str | None:
    folder = root / table
    if not any(folder.glob("date=*/*.parquet")):
        return None
    return f"{folder.as_posix()}/date=*/*.parquet"


def _columns(db: duckdb.DuckDBPyConnection, pattern: str) -> list[str]:
    rows = db.execute(f"DESCRIBE SELECT * FROM read_parquet('{pattern}', union_by_name=true)")
    return [r[0] for r in rows.fetchall()]


def _quote(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def compare(data_dir: Path, epoch: int, *, chunk_slots: int, first_slot: int) -> ParityResult:
    """Steps 1 and 3, and the list for step 2 (the chain check is separate: `run_parity`)."""
    result = ParityResult(epoch)
    raw, backfill = data_dir / "raw", data_dir / ROWS_FOLDER
    windows = done_windows(data_dir, epoch, chunk_slots, first_slot)
    db = duckdb.connect()
    span = None
    for table in ("tokens", "trades"):
        pattern = _glob(raw, table)
        if pattern:
            lo, hi = db.execute(
                f"SELECT min(slot), max(slot) FROM read_parquet('{pattern}', union_by_name=true)"
            ).fetchone()
            if lo is not None:
                span = (lo, hi) if span is None else (min(span[0], lo), max(span[1], hi))
    if span is None or not windows:
        result.note = (
            "no recorded data" if span is None else "the backfill has finished no chunks yet"
        )
        return result
    rec_lo, rec_hi = span[0] + EDGE_SLOTS, span[1] - EDGE_SLOTS
    result.windows = [(max(a, rec_lo), min(b, rec_hi)) for a, b in windows
                      if b >= rec_lo and a <= rec_hi]  # fmt: skip
    if not result.windows:
        result.note = "the recording and the finished backfill chunks do not overlap"
        return result

    db.execute("CREATE TEMP TABLE windows (lo BIGINT, hi BIGINT)")
    db.executemany("INSERT INTO windows VALUES (?, ?)", result.windows)
    db.execute("CREATE TEMP TABLE gaps (start_slot BIGINT, end_slot BIGINT, "
               "start_ts DOUBLE, end_ts DOUBLE)")  # fmt: skip
    gaps = [(g["start_slot"], g["end_slot"], g["start_ts"], g["end_ts"])
            for g in read_gaps(data_dir / "gaps")]  # fmt: skip
    if gaps:
        db.executemany("INSERT INTO gaps VALUES (?, ?, ?, ?)", gaps)
    in_window = "EXISTS (SELECT 1 FROM windows w WHERE x.slot BETWEEN w.lo AND w.hi)"
    in_gap = (
        "EXISTS (SELECT 1 FROM gaps g WHERE "
        "(g.start_slot IS NOT NULL AND g.end_slot IS NOT NULL "
        " AND b.slot BETWEEN g.start_slot AND g.end_slot) OR "
        "((g.start_slot IS NULL OR g.end_slot IS NULL) AND b.ts BETWEEN g.start_ts AND g.end_ts))"
    )

    for table in COMPARED_TABLES:
        rec_pattern, bf_pattern = _glob(raw, table), _glob(backfill, table)
        if bf_pattern is None:
            continue
        stats = TableParity()
        result.tables[table] = stats
        bf_cols = _columns(db, bf_pattern)
        rec_cols = _columns(db, rec_pattern) if rec_pattern else []
        shared = [c for c in rec_cols if c in bf_cols and c not in NOT_COMPARED]
        extra = " AND x.bf_mentions_pump" if table in PUMPSWAP_TABLES else ""
        db.execute(
            f"CREATE OR REPLACE TEMP VIEW b AS SELECT * FROM "
            f"read_parquet('{bf_pattern}', union_by_name=true) x WHERE {in_window}{extra}"
        )
        if rec_pattern:
            db.execute(
                f"CREATE OR REPLACE TEMP VIEW r AS SELECT * FROM "
                f"read_parquet('{rec_pattern}', union_by_name=true) x WHERE {in_window}"
            )
        else:  # the recorder never wrote this table: every backfill row is backfill-only
            db.execute("CREATE OR REPLACE TEMP VIEW r AS SELECT * FROM b WHERE false")
            shared = ["signature", "event_index"]
        for side in ("r", "b"):
            dupes = db.execute(
                f"SELECT count(*) FROM (SELECT signature, event_index FROM {side} "
                "GROUP BY ALL HAVING count(*) > 1)"
            ).fetchone()[0]
            if dupes:
                stats.duplicate_keys["recorder" if side == "r" else "backfill"] = dupes
        compared = [c for c in shared if c not in ("signature", "event_index")]
        differs = [f"r.{_quote(c)} IS DISTINCT FROM b.{_quote(c)}" for c in compared]
        any_diff = " OR ".join(differs) or "false"
        db.execute(
            "CREATE OR REPLACE TEMP TABLE j AS SELECT "
            "r.signature AS r_sig, b.signature AS b_sig, "
            "coalesce(r.signature, b.signature) AS signature, "
            "coalesce(r.event_index, b.event_index) AS event_index, "
            f"({any_diff}) AS differs, "
            + "".join(f"({d}) AS d{i}, " for i, d in enumerate(differs))
            + f"b.slot IS NOT NULL AND {in_gap} AS in_gap, "
            "coalesce(b.bf_pump_via_lookup, false) AS via_lookup "
            "FROM r FULL OUTER JOIN b ON r.signature = b.signature "
            "AND r.event_index = b.event_index"
        )
        both, rec_only, bf_only, bf_gap, via, mismatched = db.execute(
            "SELECT count(*) FILTER (r_sig IS NOT NULL AND b_sig IS NOT NULL), "
            "count(*) FILTER (b_sig IS NULL), count(*) FILTER (r_sig IS NULL), "
            "count(*) FILTER (r_sig IS NULL AND in_gap), "
            "count(*) FILTER (r_sig IS NULL AND NOT in_gap AND via_lookup), "
            "count(*) FILTER (r_sig IS NOT NULL AND b_sig IS NOT NULL AND differs) FROM j"
        ).fetchone()
        stats.both, stats.recorder_only, stats.backfill_only = both, rec_only, bf_only
        stats.backfill_only_in_gaps, stats.backfill_only_via_lookup = bf_gap, via
        stats.mismatched = mismatched
        if mismatched:
            sums = db.execute(
                "SELECT " + ", ".join(f"count(*) FILTER (d{i})" for i in range(len(differs)))
                + " FROM j WHERE r_sig IS NOT NULL AND b_sig IS NOT NULL"
            ).fetchone()
            stats.mismatched_columns = {c: n for c, n in zip(compared, sums, strict=True) if n}
            keys = db.execute(
                "SELECT signature, event_index FROM j WHERE r_sig IS NOT NULL "
                f"AND b_sig IS NOT NULL AND differs LIMIT {EXAMPLES}"
            ).fetchall()
            for signature, event_index in keys:
                cols = ", ".join(f"r.{_quote(c)} AS r_{i}, b.{_quote(c)} AS b_{i}"
                                 for i, c in enumerate(stats.mismatched_columns))  # fmt: skip
                values = db.execute(
                    f"SELECT {cols} FROM r JOIN b USING (signature, event_index) "
                    "WHERE signature = ? AND event_index = ?", [signature, event_index]
                ).fetchone()
                diff = {c: [values[2 * i], values[2 * i + 1]]
                        for i, c in enumerate(stats.mismatched_columns)}  # fmt: skip
                stats.mismatch_examples.append(
                    {"signature": signature, "event_index": event_index, "recorder_vs_backfill":
                     {k: [str(v) for v in pair] for k, pair in diff.items()}}
                )  # fmt: skip
        if not result.controls and both:
            result.controls = [row[0] for row in db.execute(
                "SELECT DISTINCT signature FROM j WHERE r_sig IS NOT NULL AND b_sig IS NOT NULL "
                "ORDER BY signature LIMIT 5"
            ).fetchall()]  # fmt: skip
        stats.recorder_only_signatures = [
            row[0] for row in db.execute(
                "SELECT DISTINCT signature FROM j WHERE b_sig IS NULL ORDER BY 1"
            ).fetchall()
        ]  # fmt: skip
    pattern = _glob(backfill, "trades") or _glob(backfill, "amm_trades")
    if pattern:
        result.start_ts, result.end_ts = db.execute(
            f"SELECT min(ts), max(ts) FROM read_parquet('{pattern}', union_by_name=true) x "
            f"WHERE {in_window}"
        ).fetchone()
    db.close()
    return result


async def run_parity(data_dir: Path, epoch: int, *, chunk_slots: int, first_slot: int,
                     check: Callable[[ParityResult], ChainCheck] | None) -> ParityResult:
    """The whole test: compare, then ask the chain about every recorder-only transaction.
    `check` makes the chain check from the comparison (which supplies control signatures).
    Without it, those transactions are reported as unchecked (the test cannot pass)."""
    result = compare(data_dir, epoch, chunk_slots=chunk_slots, first_slot=first_slot)
    signatures = sorted({s for t in result.tables.values() for s in t.recorder_only_signatures})
    if not signatures:
        return result
    if check is None:
        result.unchecked = signatures
        return result
    status = await check(result)(signatures)
    result.chain_status = {s: status[s] for s in signatures if s in status}
    result.unchecked = [s for s in signatures if s not in status]
    return result


def rpc_check(rpc, controls: list[str] | None = None) -> ChainCheck:
    """A ChainCheck using getSignatureStatuses (with the full history). `controls` are
    signatures known to be on chain: if the node reports any of them missing, its history
    is not reliable and nothing is reported as checked."""

    async def check(signatures: list[str]) -> dict[str, str]:
        out: dict[str, str] = {}
        wanted = list(signatures) + list(controls or [])
        for i in range(0, len(wanted), 256):
            batch = wanted[i : i + 256]
            reply = await rpc.call("getSignatureStatuses",
                                   [batch, {"searchTransactionHistory": True}])  # fmt: skip
            for signature, status in zip(batch, (reply or {}).get("value", []), strict=False):
                if status is None:
                    out[signature] = "missing"
                elif status.get("err") is not None:
                    out[signature] = "failed"
                else:
                    out[signature] = "ok"
        if any(out.get(c) != "ok" for c in controls or []):
            return {}
        return {s: out[s] for s in signatures if s in out}

    return check
