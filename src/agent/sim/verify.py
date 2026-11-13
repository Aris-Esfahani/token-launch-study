"""Phase 2 check: does our curve math reproduce the trades pump.fun actually made?

For every recorded trade in a window, the curve is worked back to just before the trade,
the trade is re-run with `curve_math`, and the result is compared with what the chain
reported: quote amount, tokens, protocol fee and creator fee.

Also reported:
- fee schedule: the rates `chain_config.Schedule` predicts from a chain snapshot, against
  the rates each trade was charged (tokens created while recording, direct trades);
- continuity: whether each trade starts from the curve a recorded event left behind.

Trades by pump.fun's mayhem agent are counted but not compared: its program sets their
prices, so they follow different rules (see curve_math.mayhem_agent_address).
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import asdict, dataclass, field
from pathlib import Path

import duckdb

from agent.sim.chain_config import QuoteKinds, Schedule, snapshot_at
from agent.sim.curve_math import NO_CREATOR, Curve, mayhem_agent_address, replay

# Done when (spec section 15, Phase 2): "simulated prices match on-chain trade prices
# within tolerance". Tolerance: the quote amount and each fee within 1 base unit, and
# the token amount within one part in a million.
MIN_EXACT_SHARE = 0.999
MIN_WITHIN_TOLERANCE_SHARE = 0.9999
QUOTE_TOLERANCE = 1
TOKEN_TOLERANCE = 1e-6


@dataclass
class Bucket:
    trades: int = 0
    exact: int = 0
    within: int = 0
    worst_quote_diff: int = 0
    worst_token_diff: float = 0.0
    examples: list[str] = field(default_factory=list)  # signatures outside tolerance

    @property
    def exact_share(self) -> float:
        return self.exact / self.trades if self.trades else 1.0

    @property
    def within_share(self) -> float:
        return self.within / self.trades if self.trades else 1.0


@dataclass
class VerifyResult:
    start: float
    end: float
    buckets: dict[str, Bucket] = field(default_factory=dict)
    agent_trades: int = 0
    schedule: dict[str, list[int]] = field(default_factory=dict)  # kind -> [checked, matched]
    multi_hop_fee_trades: int = 0
    continuity: dict[str, list[int]] = field(default_factory=dict)  # coin kind -> [checked, ok]

    @property
    def total(self) -> Bucket:
        out = Bucket()
        for b in self.buckets.values():
            out.trades += b.trades
            out.exact += b.exact
            out.within += b.within
            out.worst_quote_diff = max(out.worst_quote_diff, b.worst_quote_diff)
            out.worst_token_diff = max(out.worst_token_diff, b.worst_token_diff)
        return out

    @property
    def passed(self) -> bool:
        total = self.total
        return (
            total.trades > 0
            and total.exact_share >= MIN_EXACT_SHARE
            and total.within_share >= MIN_WITHIN_TOLERANCE_SHARE
        )

    def to_json(self) -> dict:
        data = asdict(self)
        data["total"] = asdict(self.total)
        data["passed"] = self.passed
        return data


def kind_of(trade: dict) -> str:
    if not trade["is_buy"]:
        return "sell"
    if trade["ix_name"] == "buy":
        return "buy, exact tokens"
    if trade["ix_name"] == "multi_hop_swap":
        return "buy, multi-hop route"
    return "buy, set amount of SOL"


def _rows(raw: Path, table: str, where: str = "", params: list | None = None) -> list[dict]:
    if not any((raw / table).glob("date=*/*.parquet")):
        return []
    source = f"read_parquet('{(raw / table).as_posix()}/*/*.parquet', union_by_name=true)"
    with duckdb.connect() as db:
        cursor = db.execute(f"SELECT * FROM {source} {where}", params or [])
        names = [d[0] for d in cursor.description]
        return [dict(zip(names, row, strict=True)) for row in cursor.fetchall()]


def verify(
    data_dir: Path,
    start: float,
    end: float,
    kinds: QuoteKinds,
    snapshots: list[dict],
) -> VerifyResult:
    raw = data_dir / "raw"
    result = VerifyResult(start=start, end=end)
    agent = mayhem_agent_address()
    trades = _rows(raw, "trades", "WHERE ts BETWEEN ? AND ?", [start, end])
    tokens = {t["mint"]: t for t in _rows(raw, "tokens")}

    for trade in trades:
        if trade["wallet"] == agent:
            result.agent_trades += 1
            continue
        bucket = result.buckets.setdefault(kind_of(trade), Bucket())
        bucket.trades += 1
        fill = replay(trade)
        quote_diff = max(
            abs(fill.curve_quote - trade["quote_amount"]),
            abs(fill.protocol_fee - trade["fee"]),
            abs(fill.creator_fee - trade["creator_fee"]),
        )
        token_diff = abs(fill.tokens - trade["token_amount"]) / max(trade["token_amount"], 1)
        bucket.worst_quote_diff = max(bucket.worst_quote_diff, quote_diff)
        bucket.worst_token_diff = max(bucket.worst_token_diff, token_diff)
        if quote_diff == 0 and fill.tokens == trade["token_amount"]:
            bucket.exact += 1
        if quote_diff <= QUOTE_TOLERANCE and token_diff <= TOKEN_TOLERANCE:
            bucket.within += 1
        elif len(bucket.examples) < 5:
            bucket.examples.append(trade["signature"])

    _check_schedule(result, trades, tokens, kinds, snapshots, agent)
    _check_continuity(result, trades, tokens, _rows(raw, "mayhem_params"))
    return result


def _check_schedule(result, trades, tokens, kinds, snapshots, agent) -> None:
    if not snapshots:
        return
    for trade in trades:
        token = tokens.get(trade["mint"])
        if trade["wallet"] == agent or token is None:
            continue
        if trade["ix_name"] == "multi_hop_swap":
            # A route through several coins charges each fee once for the whole route.
            result.multi_hop_fee_trades += 1
            continue
        schedule = Schedule.from_snapshot(snapshot_at(snapshots, trade["ts"]), kinds)
        quote_mint = token["quote_mint"]
        rates = schedule.rates(
            Curve.before_trade(trade),
            quote_mint=quote_mint,
            coin_creator_fee_bps=token["creator_fee_bps"] or 0,
            creator=trade["creator"],
            mayhem=bool(token["is_mayhem_mode"]),
            mint_supply=token["token_total_supply"],
        )
        # A coin without a creator pays no creator fee, whatever rate its events show.
        no_creator = trade["creator"] == NO_CREATOR
        creator_bps = 0 if no_creator else trade["creator_fee_basis_points"]
        charged = (trade["fee_basis_points"], creator_bps)
        if quote_mint in kinds.sol_like:
            kind = "SOL-paired"
        elif quote_mint in kinds.stable:
            kind = "stablecoin-paired"
        else:
            kind = "paired with another coin"
        counts = result.schedule.setdefault(kind, [0, 0])
        counts[0] += 1
        counts[1] += (rates.protocol_bps, rates.creator_bps) == charged


def _check_continuity(result, trades, tokens, mayhem_params) -> None:
    """For tokens created while recording, each trade should start from a curve state a
    recorded event left behind: the creation, an earlier trade, or a mayhem reset."""
    states: dict[str, set[tuple[int, int]]] = defaultdict(set)
    for mint, token in tokens.items():
        states[mint].add((token["virtual_quote_reserves"], token["virtual_token_reserves"]))
    for trade in trades:
        states[trade["mint"]].add(
            (trade["virtual_quote_reserves"], trade["virtual_token_reserves"])
        )
    for reset in mayhem_params:
        states[reset["mint"]].add(
            (reset["new_virtual_sol_reserves"], reset["new_virtual_token_reserves"])
        )
    for trade in trades:
        token = tokens.get(trade["mint"])
        if token is None:
            continue
        kind = "mayhem coins" if token["is_mayhem_mode"] else "other coins"
        before = Curve.before_trade(trade)
        counts = result.continuity.setdefault(kind, [0, 0])
        counts[0] += 1
        counts[1] += (before.virtual_quote, before.virtual_token) in states[trade["mint"]]
