"""pump.fun bonding-curve math: prices, simulated fills, fees and price impact.

All amounts are integers in base units, exactly as on chain: lamports (or the quote
coin's base units) and raw token units. Nothing here hardcodes a reserve, a fee rate or a
curve parameter; they come from recorded trades or from chain snapshots (chain_config.py).

The rules, as pump.fun's program applies them (pump-fun/pump-sdk 4.1.0, src/bondingCurve.ts
and src/fees.ts, read 2026-11-12) and as `agent verify-curve` checks against every
recorded trade:

- Price is a constant product of the curve's *virtual* reserves (quote, token).
- Buy an exact number of tokens:  net quote = floor(tokens * vq / (vt - tokens)) + 1
- Buy with an exact quote amount: tokens = floor((net - 1) * vt / (vq + net - 1)),
  where net is the quote left after fees.
- Sell:                           gross quote = floor(tokens * vq / (vt + tokens))
- Each fee is ceil(amount * bps / 10_000), on the net quote (buy) or gross quote (sell).
  A buyer pays net + fees; a seller receives gross - fees. A coin without a creator
  (creator = the all-zero address) pays no creator fee, whatever rate its events show.
- Buy with a budget (fees included): net = floor(budget * 10_000 / (10_000 + total bps)),
  fees are charged on that net, and if net + fees exceed the budget the net is reduced by
  the excess (the fees stay as computed). So the buyer pays exactly the budget.
- A buy that reaches the curve's remaining tokens takes all of them for their exact
  price and completes the curve.
- After a trade the reserves move by the net (buy) or gross (sell) quote and the tokens.

One trader is exempt: on mayhem-mode coins, pump.fun's own mayhem agent trades at prices
its program sets (see `mayhem_agent_address`). Everyone else, on every coin, follows the
rules above exactly.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache

from agent.ingest.borsh_idl import Idl
from agent.ingest.solana import b58encode, find_program_address

BPS = 10_000


def ceil_fee(amount: int, bps: int) -> int:
    """A fee as pump.fun charges it: amount * bps / 10_000, rounded up."""
    return (amount * bps + BPS - 1) // BPS


@dataclass(frozen=True, slots=True)
class FeeRates:
    """Fee rates in basis points (1 bps = 0.01%)."""

    protocol_bps: int
    creator_bps: int

    @property
    def total_bps(self) -> int:
        return self.protocol_bps + self.creator_bps

    def on(self, amount: int) -> tuple[int, int]:
        """(protocol fee, creator fee) charged on `amount`."""
        return ceil_fee(amount, self.protocol_bps), ceil_fee(amount, self.creator_bps)


@dataclass(frozen=True, slots=True)
class Curve:
    """A bonding curve's reserves. `virtual_*` set the price; `real_*` are what the
    curve actually holds (real_token is what is left to buy before it completes)."""

    virtual_quote: int
    virtual_token: int
    real_quote: int
    real_token: int

    @classmethod
    def after_trade(cls, trade: dict) -> Curve:
        """The curve right after a recorded trade (the reserves its event reports)."""
        return cls(
            trade["virtual_quote_reserves"],
            trade["virtual_token_reserves"],
            trade["real_quote_reserves"],
            trade["real_token_reserves"],
        )

    @classmethod
    def before_trade(cls, trade: dict) -> Curve:
        """The curve right before a recorded trade, worked back from its event."""
        after = cls.after_trade(trade)
        quote, tokens = trade["quote_amount"], trade["token_amount"]
        if trade["is_buy"]:
            return after.moved(-quote, tokens)
        return after.moved(quote, -tokens)

    def moved(self, quote_delta: int, token_delta: int) -> Curve:
        """The curve with `quote_delta` added to and `token_delta` added to its reserves."""
        return Curve(
            self.virtual_quote + quote_delta,
            self.virtual_token + token_delta,
            self.real_quote + quote_delta,
            self.real_token + token_delta,
        )

    def price(self) -> float:
        """Marginal price: quote base units per raw token unit."""
        return self.virtual_quote / self.virtual_token

    def market_cap(self, supply: int) -> int:
        """Market cap in quote base units, as pump.fun computes it for fee tiers."""
        return self.virtual_quote * supply // self.virtual_token

    def progress(self, initial_real_token: int) -> float:
        """Share of the curve's sellable tokens already bought (1.0 = complete)."""
        return 1 - self.real_token / initial_real_token


# --- the pure curve formulas -----------------------------------------------------------


def quote_for_tokens(curve: Curve, tokens: int) -> int:
    """Net quote a buy of exactly `tokens` puts into the curve."""
    return tokens * curve.virtual_quote // (curve.virtual_token - tokens) + 1


def tokens_for_quote(curve: Curve, net_quote: int) -> int:
    """Tokens a buy with `net_quote` (after fees) receives, before the supply cap."""
    if net_quote <= 1:
        return 0
    spend = net_quote - 1
    return spend * curve.virtual_token // (curve.virtual_quote + spend)


def quote_for_sale(curve: Curve, tokens: int) -> int:
    """Gross quote a sale of `tokens` takes out of the curve, before fees."""
    return tokens * curve.virtual_quote // (curve.virtual_token + tokens)


def split_budget(budget: int, rates: FeeRates) -> tuple[int, int, int]:
    """(net into the curve, protocol fee, creator fee) for a buy that spends `budget`
    in total (pump.fun's exact-quote-in rule: fees on the first net, then the net is
    trimmed so the total equals the budget)."""
    net = budget * BPS // (BPS + rates.total_bps)
    protocol, creator = rates.on(net)
    over = net + protocol + creator - budget
    return net - max(over, 0), protocol, creator


# --- simulated fills -------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Fill:
    side: str  # "buy" or "sell"
    tokens: int
    curve_quote: int  # net into the curve (buy) or gross out of it (sell)
    protocol_fee: int
    creator_fee: int
    before: Curve
    after: Curve
    capped: bool = False  # a buy hit the remaining supply and completes the curve

    @property
    def user_quote(self) -> int:
        """What the trader pays (buy) or receives (sell), fees included."""
        fees = self.protocol_fee + self.creator_fee
        return self.curve_quote + fees if self.side == "buy" else self.curve_quote - fees

    @property
    def average_price(self) -> float:
        """Quote base units per raw token unit, fees included."""
        return self.user_quote / self.tokens if self.tokens else float("nan")

    @property
    def price_impact(self) -> float:
        """How far this trade moved the marginal price (+0.05 = up 5%)."""
        return self.after.price() / self.before.price() - 1


def buy_tokens(curve: Curve, tokens: int, rates: FeeRates) -> Fill:
    """Buy exactly `tokens` (capped at what the curve has left)."""
    capped = tokens >= curve.real_token
    tokens = min(tokens, curve.real_token)
    net = quote_for_tokens(curve, tokens)
    protocol, creator = rates.on(net)
    return Fill("buy", tokens, net, protocol, creator, curve, curve.moved(net, -tokens), capped)


def buy_with_budget(curve: Curve, budget: int, rates: FeeRates) -> Fill:
    """Spend `budget` in total, fees included (what a Padre buy of N SOL does).

    A buy that reaches the remaining supply takes all of it and completes the curve
    (`capped`). pump.fun's v3 buys would spend the rest of the budget in the PumpSwap
    pool that the migration creates; that part is not simulated.
    """
    net, protocol, creator = split_budget(budget, rates)
    if net >= quote_for_tokens(curve, curve.real_token):
        return buy_tokens(curve, curve.real_token, rates)
    tokens = tokens_for_quote(curve, net)
    return Fill("buy", tokens, net, protocol, creator, curve, curve.moved(net, -tokens))


def sell_tokens(curve: Curve, tokens: int, rates: FeeRates) -> Fill:
    """Sell exactly `tokens`."""
    gross = quote_for_sale(curve, tokens)
    protocol, creator = rates.on(gross)
    return Fill("sell", tokens, gross, protocol, creator, curve, curve.moved(-gross, tokens))


NO_CREATOR = b58encode(bytes(32))  # the all-zero address: the coin has no creator


def rates_of_trade(trade: dict) -> FeeRates:
    """The fee rates a recorded trade was charged."""
    creator_bps = 0 if trade["creator"] == NO_CREATOR else trade["creator_fee_basis_points"]
    return FeeRates(trade["fee_basis_points"], creator_bps)


def replay(trade: dict) -> Fill:
    """Re-run a recorded trade from the curve as it was just before it, the way the trader
    asked for it: exact tokens (`buy`), exact quote in (other buys) or a sale."""
    before, rates = Curve.before_trade(trade), rates_of_trade(trade)
    if not trade["is_buy"]:
        return sell_tokens(before, trade["token_amount"], rates)
    if trade["ix_name"] == "buy":
        return buy_tokens(before, trade["token_amount"], rates)
    budget = trade["quote_amount"] + trade["fee"] + trade["creator_fee"]
    return buy_with_budget(before, budget, rates)


@lru_cache(maxsize=1)
def mayhem_agent_address() -> str:
    """The wallet pump.fun's mayhem agent trades from, derived from the official IDL: the
    `sol_vault_authority` of `set_mayhem_virtual_params` (seed "sol-vault" under the mayhem
    program). Its trades are priced by the mayhem program, not by the formulas above."""
    pump = Idl.load("pump")
    spec = next(
        a
        for a in pump.instructions["set_mayhem_virtual_params"]["accounts"]
        if a["name"] == "sol_vault_authority"
    )
    seeds = [bytes(seed["value"]) for seed in spec["pda"]["seeds"]]
    program = b58encode(bytes(spec["pda"]["program"]["value"]))
    return find_program_address(seeds, program)[0]
