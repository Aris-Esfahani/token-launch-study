"""A token's state, built from its on-chain events, and its signals at a cutoff (spec 6, 8).

One piece of code for both paths: the engine adds events as they arrive (live), and
`replay.py` adds stored events (training data, checks). A snapshot takes a cutoff and
reads only events with `ts <= cutoff`, so events after the cutoff can already be in the
state and change nothing (the no-lookahead rule, spec 6). A snapshot does not depend on
the order events were added in, and an event added twice counts once.

Limits of these chain-only signals (enrichment comes later, spec 8.5-8.7):
- Holdings are worked out from bonding-curve trades only. Wallet-to-wallet transfers are
  not in the recorded tables, so a wallet that moves its tokens elsewhere still looks like
  a holder.
- Within one slot the recorded tables do not keep the transactions' order. The curve
  state after the last trade is found by chaining the trades' reserves (each trade starts
  where the previous one ended); if that is ambiguous, a fixed order is used.
- Mayhem coins (spec 8.10): the mayhem program also resets their reserves between trades;
  those resets are not applied here yet, so their price is the one after the last trade.
"""

from __future__ import annotations

from bisect import bisect_left, insort
from collections import defaultdict
from dataclasses import dataclass
from statistics import median

from agent.ingest.solana import b58encode
from agent.sim.curve_math import Curve, mayhem_agent_address

NATIVE_SOL = b58encode(bytes(32))  # quote_mint of SOL-paired coins: the all-zero address
LAMPORTS_PER_SOL = 1_000_000_000
SNAPSHOT_OFFSETS_S = (30, 60, 120, 300)  # spec 6
EARLY_SLOTS = 3  # spec 8.2: buys in the first 1-3 slots after the create slot
RECENT_S = 30  # spec 8.4: "last 30s"


@dataclass(frozen=True, slots=True)
class Trade:
    slot: int
    ts: int
    signature: str
    event_index: int
    wallet: str
    is_buy: bool
    quote: int  # quote base units (lamports for SOL-paired coins)
    tokens: int  # raw token units
    after: Curve
    is_creator: bool
    is_agent: bool  # pump.fun's mayhem agent

    @property
    def key(self) -> tuple[str, int]:
        return self.signature, self.event_index

    @property
    def order(self) -> tuple[int, str, int]:
        return self.slot, self.signature, self.event_index

    def before(self) -> Curve:
        if self.is_buy:
            return self.after.moved(-self.quote, self.tokens)
        return self.after.moved(self.quote, -self.tokens)


class CreatorIndex:
    """When each creator launched coins, to count their earlier launches at a cutoff.
    Only launches the recorder or the backfill saw are counted."""

    def __init__(self) -> None:
        self._launches: dict[str, list[int]] = defaultdict(list)
        self._seen: set[str] = set()

    def add(self, creator: str, ts: int, mint: str) -> None:
        if mint in self._seen:
            return
        self._seen.add(mint)
        insort(self._launches[creator], ts)

    def launches_before(self, creator: str, ts: int) -> int:
        return bisect_left(self._launches.get(creator, []), ts)


class TokenState:
    def __init__(self, create: dict, *, agent: str | None = None) -> None:
        self.mint: str = create["mint"]
        self.creator: str = create["creator"]
        self.created_ts: int = create["ts"]
        self.create_slot: int = create["slot"]
        self.supply: int = create["token_total_supply"]
        self.quote_mint: str = create["quote_mint"]
        self.is_mayhem = bool(create["is_mayhem_mode"])
        self.start = Curve(
            create["virtual_quote_reserves"], create["virtual_token_reserves"],
            0, create["real_token_reserves"],
        )  # fmt: skip
        self._agent = agent if agent is not None else mayhem_agent_address()
        self._trades: dict[tuple[str, int], Trade] = {}
        self._completed: set[int] = set()

    @property
    def is_sol_paired(self) -> bool:
        return self.quote_mint == NATIVE_SOL

    def add_trade(self, row: dict) -> None:
        if row["mint"] != self.mint:
            raise ValueError(f"trade of {row['mint']} added to {self.mint}")
        trade = Trade(
            slot=row["slot"], ts=row["ts"], signature=row["signature"],
            event_index=row["event_index"], wallet=row["wallet"], is_buy=bool(row["is_buy"]),
            quote=row["quote_amount"], tokens=row["token_amount"],
            after=Curve.after_trade(row), is_creator=row["wallet"] == self.creator,
            is_agent=row["wallet"] == self._agent,
        )  # fmt: skip
        self._trades.setdefault(trade.key, trade)

    def add_complete(self, row: dict) -> None:
        self._completed.add(row["ts"])

    def completed_by(self, ts: int) -> bool:
        """The bonding curve was complete (the coin graduated) at `ts`."""
        return any(t <= ts for t in self._completed)

    def trades_until(self, cutoff: int) -> list[Trade]:
        return sorted((t for t in self._trades.values() if t.ts <= cutoff), key=lambda t: t.order)

    def snapshot(self, cutoff: int, *, creators: CreatorIndex | None = None) -> dict:
        """Signals at `cutoff` (epoch seconds), from events with ts <= cutoff only."""
        trades = self.trades_until(cutoff)
        age_s = cutoff - self.created_ts
        minutes = max(age_s, 1) / 60
        pct = 100 / self.supply
        sol = self.is_sol_paired

        def in_sol(quote: int | float | None) -> float | None:
            return None if quote is None or not sol else quote / LAMPORTS_PER_SOL

        buys = [t for t in trades if t.is_buy]
        sells = [t for t in trades if not t.is_buy]
        balances: dict[str, int] = defaultdict(int)
        first_buy_slot: dict[str, int] = {}
        for t in trades:  # in slot order, so the first buy seen is the earliest
            balances[t.wallet] += t.tokens if t.is_buy else -t.tokens
            if t.is_buy:
                first_buy_slot.setdefault(t.wallet, t.slot)
        held = {w: b for w, b in balances.items() if b > 0}
        largest = sorted(held.values(), reverse=True)

        dev_bought = sum(t.tokens for t in buys if t.is_creator)
        dev_sold = sum(t.tokens for t in sells if t.is_creator)
        snipers = {
            w for w, slot in first_buy_slot.items()
            if slot <= self.create_slot + EARLY_SLOTS and w not in (self.creator, self._agent)
        }  # fmt: skip
        launch_slot_buyers = {
            t.wallet for t in buys
            if t.slot == self.create_slot and not t.is_creator and not t.is_agent
        }  # fmt: skip
        recent = [t for t in trades if t.ts > cutoff - RECENT_S]
        buy_quote = sum(t.quote for t in buys)
        agent_buy_quote = sum(t.quote for t in buys if t.is_agent)
        buyers = {t.wallet for t in buys}
        curve = self._curve_after(trades)
        progress = curve.progress(self.start.real_token) * 100

        return {
            "mint": self.mint,
            "cutoff_ts": cutoff,
            "token_age_s": age_s,
            "is_mayhem": self.is_mayhem,
            "is_sol_paired": sol,
            "completed": self.completed_by(cutoff),
            # Trade flow (8.4)
            "trades": len(trades),
            "buys": len(buys),
            "sells": len(sells),
            "unique_buyers": len(buyers),
            "unique_buyers_per_min": len(buyers) / minutes,
            "buy_sol": in_sol(buy_quote),
            "sell_sol": in_sol(sum(t.quote for t in sells)),
            "net_sol": in_sol(buy_quote - sum(t.quote for t in sells)),
            "buys_last_30s": sum(t.is_buy for t in recent),
            "sells_last_30s": sum(not t.is_buy for t in recent),
            "net_sol_last_30s": in_sol(sum(t.quote if t.is_buy else -t.quote for t in recent)),
            "median_buy_sol": in_sol(median(t.quote for t in buys)) if buys else None,
            "max_buy_sol": in_sol(max(t.quote for t in buys)) if buys else None,
            "largest_sell_pct": max((t.tokens for t in sells), default=0) * pct,
            "curve_progress_pct": progress,
            "progress_per_min": progress / minutes,
            "price_change_pct": (curve.price() / self.start.price() - 1) * 100,
            "market_cap_sol": in_sol(curve.market_cap(self.supply)),
            # Holders (8.3), from curve trades only
            "holders": len(held),
            "top1_hold_pct": (largest[0] if largest else 0) * pct,
            "top10_hold_pct": sum(largest[:10]) * pct,
            # Dev (8.1)
            "dev_buy_pct": dev_bought * pct,
            "dev_buy_sol": in_sol(sum(t.quote for t in buys if t.is_creator)),
            "dev_hold_pct": max(balances.get(self.creator, 0), 0) * pct,
            "dev_sold_pct": dev_sold * 100 / dev_bought if dev_bought else None,
            "dev_prior_launches": (
                creators.launches_before(self.creator, self.created_ts) if creators else None
            ),
            # Snipers and bundles (8.2)
            "launch_slot_buyers": len(launch_slot_buyers),
            "snipers": len(snipers),
            "sniper_hold_pct": sum(max(balances[w], 0) for w in snipers) * pct,
            # Mayhem coins (8.10): the agent's trades stay in everything above
            "mayhem_agent_buy_share": agent_buy_quote / buy_quote if buy_quote else 0.0,
            "mayhem_agent_hold_pct": max(balances.get(self._agent, 0), 0) * pct,
        }

    def _curve_after(self, trades: list[Trade]) -> Curve:
        """The curve after the last trade at the cutoff."""
        if not trades:
            return self.start
        last_slot = trades[-1].slot
        in_slot = [t for t in trades if t.slot == last_slot]
        if len(in_slot) == 1:
            return in_slot[0].after
        # Each trade starts where the previous one ended: the last one is the trade whose
        # end is no other trade's start.
        starts = [_reserves(t.before()) for t in in_slot]
        ends = [t for t in in_slot if starts.count(_reserves(t.after)) == 0]
        if len(ends) == 1:
            return ends[0].after
        return in_slot[-1].after  # ambiguous (e.g. a mayhem reset): fixed order


def _reserves(curve: Curve) -> tuple[int, int]:
    return curve.virtual_quote, curve.virtual_token
