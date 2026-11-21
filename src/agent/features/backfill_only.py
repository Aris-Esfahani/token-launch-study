"""Backfill-only signals (spec 4.1): what only full transactions show, so the live log
stream cannot supply it: the fee paid for priority, the position in the block and the
compute used. Research track only. Every model is also reported without them (spec 15.4),
because a live tool could not compute them.

One row per coin and cutoff, from the coin's launch transaction and the bonding-curve
trades with `ts <= cutoff` (spec 6); the mayhem agent's own trades are left out of the
buy columns. Jito tips are not stored by the backfill, so they are not here.

Columns, all starting with `bf_`:
- `bf_create_priority_sol`, `bf_create_tx_index`: the launch transaction's priority fee
  (its fee above the base fee per signature) and its position in its block.
- `bf_buy_priority_median_sol`, `bf_buy_priority_max_sol`: over the buys so far.
- `bf_buy_tx_index_median`: how early in their blocks the buys landed.
- `bf_buy_compute_median`: compute units of the buying transactions.
"""

from __future__ import annotations

from pathlib import Path

import duckdb
import pyarrow as pa

from agent.features.token_state import SNAPSHOT_OFFSETS_S
from agent.sim.curve_math import mayhem_agent_address

BACKFILLED = "raw_backfill"
BASE_FEE_LAMPORTS = 5000  # Solana's base fee per signature (the chain's, not pump.fun's)
LAMPORTS_PER_SOL = 1_000_000_000
COLUMNS = (
    "bf_create_priority_sol", "bf_create_tx_index", "bf_buy_priority_median_sol",
    "bf_buy_priority_max_sol", "bf_buy_tx_index_median", "bf_buy_compute_median",
)  # fmt: skip


def backfill_only_signals(data_dir: Path, start_ts: int, end_ts: int,
                          offsets: tuple[int, ...] = SNAPSHOT_OFFSETS_S) -> pa.Table:  # fmt: skip
    """`mint`, `offset_s` and the `bf_` columns for coins launched in [start_ts, end_ts)."""
    root = data_dir / BACKFILLED
    tokens = f"{(root / 'tokens').as_posix()}/*/*.parquet"
    trades = f"{(root / 'trades').as_posix()}/*/*.parquet"
    priority = (f"greatest(bf_fee::BIGINT - {BASE_FEE_LAMPORTS} * bf_signatures, 0) "
                f"/ {LAMPORTS_PER_SOL}")  # fmt: skip
    with duckdb.connect() as db:
        tmp = data_dir / "duckdb_tmp"
        tmp.mkdir(parents=True, exist_ok=True)
        db.execute(f"SET temp_directory = '{tmp.as_posix()}'")
        db.execute(
            f"CREATE TEMP TABLE coins AS SELECT mint, ts AS created_ts, "
            f"{priority} AS bf_create_priority_sol, bf_tx_index AS bf_create_tx_index "
            f"FROM read_parquet('{tokens}', union_by_name=true) WHERE ts >= ? AND ts < ? "
            f"QUALIFY row_number() OVER (PARTITION BY mint ORDER BY ts, signature) = 1",
            [start_ts, end_ts],
        )
        db.execute("CREATE TEMP TABLE cutoffs AS SELECT unnest(?::BIGINT[]) AS offset_s",
                   [list(offsets)])  # fmt: skip
        db.execute("CREATE TEMP TABLE buys (mint VARCHAR, after_s BIGINT, priority_sol DOUBLE, "
                   "tx_index BIGINT, compute_units BIGINT)")  # fmt: skip
        if any((root / "trades").glob("*/*.parquet")):
            # Buys after the last cutoff are never read; each cutoff then takes the buys
            # at or before itself (spec 6).
            db.execute(
                f"INSERT INTO buys SELECT t.mint, t.ts - c.created_ts, {priority}, "
                f"t.bf_tx_index, t.bf_compute_units "
                f"FROM read_parquet('{trades}', union_by_name=true) t JOIN coins c USING (mint) "
                f"WHERE t.is_buy AND t.wallet <> ? AND t.ts >= c.created_ts "
                f"AND t.ts <= c.created_ts + ?",
                [mayhem_agent_address(), max(offsets)],
            )
        return db.sql(
            "SELECT c.mint, k.offset_s, c.bf_create_priority_sol, c.bf_create_tx_index, "
            "median(b.priority_sol) AS bf_buy_priority_median_sol, "
            "max(b.priority_sol) AS bf_buy_priority_max_sol, "
            "median(b.tx_index) AS bf_buy_tx_index_median, "
            "median(b.compute_units) AS bf_buy_compute_median "
            "FROM coins c CROSS JOIN cutoffs k "
            "LEFT JOIN buys b ON b.mint = c.mint AND b.after_s <= k.offset_s "
            "GROUP BY c.mint, c.created_ts, k.offset_s, c.bf_create_priority_sol, "
            "c.bf_create_tx_index ORDER BY c.created_ts, c.mint, k.offset_s"
        ).to_arrow_table()
