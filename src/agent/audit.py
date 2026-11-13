"""Coverage audit: did the recorder miss anything?

Tokens: every pump.fun create mentions the program's fixed "mint authority" account, and
nothing else does. So asking the chain for all transactions that mention that account
gives the complete list of creates for a period, which is compared with what was recorded.
A create the recorder does not have is fetched and parsed to confirm it really is one,
then classed as inside a logged gap (expected) or outside (a real miss).

Trades (optional sample): for a few recorded tokens, the chain's list of transactions on
the token's bonding-curve account is compared with the recorded trades in the same way.
"""

from __future__ import annotations

import random
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path

import duckdb

from agent.ingest.gaps import read_gaps
from agent.ingest.parsers import Parser, RawTx
from agent.ingest.rpc import Rpc

EDGE_S = 60  # recorded rows are loaded this far beyond the window, to avoid edge effects
GAP_MARGIN_S = 2
MAX_PAGES_PER_TOKEN = 5


@dataclass
class AuditResult:
    start: float
    end: float
    onchain_creates: int = 0
    recorded_creates: int = 0
    missed_outside_gaps: list[str] = field(default_factory=list)
    missed_inside_gaps: list[str] = field(default_factory=list)
    recorded_not_onchain: list[str] = field(default_factory=list)
    not_creates: int = 0  # mentioned the marker account but held no create event
    unfetched: list[str] = field(default_factory=list)
    gaps: list[dict] = field(default_factory=list)
    trade_sample_tokens: int = 0
    onchain_trades: int = 0
    missed_trades: list[str] = field(default_factory=list)
    recorded_trades_not_onchain: list[str] = field(default_factory=list)
    rpc_requests: int = 0

    @property
    def missed_share(self) -> float:
        """Share of on-chain creates missed outside logged gaps: the Phase 1 measure."""
        return len(self.missed_outside_gaps) / self.onchain_creates if self.onchain_creates else 0.0

    @property
    def missed_trade_share(self) -> float:
        return len(self.missed_trades) / self.onchain_trades if self.onchain_trades else 0.0

    def to_json(self) -> dict:
        return {**asdict(self), "missed_share": self.missed_share,
                "missed_trade_share": self.missed_trade_share}  # fmt: skip


def _query(raw: Path, table: str, sql: str, params: list) -> list[tuple]:
    """Run SQL over one recorded table (`{t}` in the SQL). Empty if nothing is recorded."""
    if not any((raw / table).glob("date=*/*.parquet")):
        return []
    source = f"read_parquet('{(raw / table).as_posix()}/*/*.parquet', union_by_name=true)"
    with duckdb.connect() as db:
        return db.execute(sql.format(t=source), params).fetchall()


def recorded_span(raw: Path) -> tuple[float, float] | None:
    """On-chain time of the first and last recorded trade (the recorder's coverage)."""
    rows = _query(raw, "trades", "SELECT min(ts), max(ts) FROM {t}", [])
    return (float(rows[0][0]), float(rows[0][1])) if rows and rows[0][0] is not None else None


async def _signatures_in_window(
    rpc: Rpc, address: str, start: float, end: float, max_pages: int | None = None
) -> tuple[list[dict], bool]:
    """Successful transactions mentioning `address` with start <= blockTime <= end, and
    whether the list is complete (False if `max_pages` cut it short)."""
    found: list[dict] = []
    before = None
    pages = 0
    while max_pages is None or pages < max_pages:
        page = await rpc.signatures_for_address(address, before=before)
        pages += 1
        if not page:
            return found, True
        for item in page:
            when = item.get("blockTime")
            if item.get("err") is None and when is not None and start <= when <= end:
                found.append(item)
        oldest = page[-1].get("blockTime")
        if len(page) < 1000 or (oldest is not None and oldest < start):
            return found, True
        before = page[-1]["signature"]
    return found, False


async def _parse_from_chain(rpc: Rpc, parser: Parser, signature: str):
    tx = await rpc.transaction(signature)
    if tx is None:
        return None
    return parser.parse(
        RawTx(signature, tx["slot"], tx["meta"]["logMessages"] or [], "audit",
              block_time=tx.get("blockTime"))  # fmt: skip
    )


def _in_gap(when: float, gaps: list[dict]) -> bool:
    return any(g["start_ts"] - GAP_MARGIN_S <= when <= g["end_ts"] + GAP_MARGIN_S for g in gaps)


async def run_audit(
    data_dir: Path,
    rpc: Rpc,
    parser: Parser,
    marker_account: str,
    *,
    start: float,
    end: float,
    trades_sample: int = 0,
    seed: int = 0,
    progress: Callable[[str], None] = lambda _msg: None,
) -> AuditResult:
    raw = data_dir / "raw"
    result = AuditResult(start=start, end=end)
    result.gaps = [g for g in read_gaps(data_dir / "gaps") if g["end_ts"] >= start
                   and g["start_ts"] <= end]  # fmt: skip

    recorded = {
        sig: ts
        for sig, ts in _query(
            raw, "tokens", "SELECT signature, ts FROM {t} WHERE ts BETWEEN ? AND ?",
            [start - EDGE_S, end + EDGE_S],
        )
    }  # fmt: skip
    result.recorded_creates = sum(1 for ts in recorded.values() if start <= ts <= end)

    progress("listing creates on chain...")
    onchain, _ = await _signatures_in_window(rpc, marker_account, start, end)
    onchain_sigs = {item["signature"] for item in onchain}

    missing = [item for item in onchain if item["signature"] not in recorded]
    progress(f"{len(onchain)} on chain, {len(missing)} not in the recording; checking those...")
    confirmed = len(onchain) - len(missing)
    for item in missing:
        parsed = await _parse_from_chain(rpc, parser, item["signature"])
        if parsed is None:
            result.unfetched.append(item["signature"])
        elif not parsed.rows.get("tokens"):
            result.not_creates += 1
        else:
            confirmed += 1
            bucket = (
                result.missed_inside_gaps
                if _in_gap(item["blockTime"], result.gaps)
                else result.missed_outside_gaps
            )
            bucket.append(item["signature"])
    result.onchain_creates = confirmed
    result.recorded_not_onchain = [
        sig for sig, ts in recorded.items() if start <= ts <= end and sig not in onchain_sigs
    ]

    if trades_sample:
        await _audit_trades(raw, rpc, parser, result, trades_sample, seed, progress)
    result.rpc_requests = rpc.requests
    return result


async def _audit_trades(
    raw: Path, rpc: Rpc, parser: Parser, result: AuditResult, sample: int, seed: int,
    progress: Callable[[str], None],
) -> None:  # fmt: skip
    start, end = result.start, result.end
    tokens = _query(
        raw, "tokens",
        "SELECT mint, bonding_curve FROM {t} WHERE ts BETWEEN ? AND ? ORDER BY signature",
        [start, end],
    )  # fmt: skip
    chosen = random.Random(seed).sample(tokens, min(sample, len(tokens)))
    result.trade_sample_tokens = len(chosen)
    for number, (mint, curve) in enumerate(chosen, start=1):
        progress(f"trades for sample token {number}/{len(chosen)}...")
        recorded = {
            row[0]
            for table in ("trades", "tokens", "curve_completes", "post_complete_buys", "migrations")
            for row in _query(raw, table, "SELECT signature FROM {t} WHERE mint = ?", [mint])
        }
        onchain, complete = await _signatures_in_window(
            rpc, curve, start - EDGE_S, end + EDGE_S, MAX_PAGES_PER_TOKEN
        )
        if complete:
            # Recorded trades the chain does not have: simulated or dropped transactions.
            known = {item["signature"] for item in onchain}
            result.recorded_trades_not_onchain += [
                sig
                for sig, ts in _query(
                    raw, "trades", "SELECT signature, ts FROM {t} WHERE mint = ?", [mint]
                )
                if start <= ts <= end and sig not in known
            ]
        onchain = [item for item in onchain if start <= item["blockTime"] <= end]
        for item in onchain:
            if item["signature"] in recorded:
                result.onchain_trades += 1
                continue
            parsed = await _parse_from_chain(rpc, parser, item["signature"])
            trades = [] if parsed is None else parsed.rows.get("trades", [])
            if any(t.get("mint") == mint for t in trades):
                result.onchain_trades += 1
                if not _in_gap(item["blockTime"], result.gaps):
                    result.missed_trades.append(item["signature"])
