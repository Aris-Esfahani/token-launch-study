"""Curve math against real mainnet trades (tests/fixtures/tx) and hand-checked numbers.

The expected values below are the chain's own: each fixture trade's event reports the
amounts and fees pump.fun computed, and the curve math must reproduce them exactly.
"""

import pytest

from agent.ingest.parsers import Parser, RawTx
from agent.sim.curve_math import (
    NO_CREATOR,
    Curve,
    FeeRates,
    buy_with_budget,
    ceil_fee,
    mayhem_agent_address,
    quote_for_tokens,
    rates_of_trade,
    replay,
    sell_tokens,
    split_budget,
    tokens_for_quote,
)
from helpers import load_fixture

PARSER = Parser()
RATES = FeeRates(95, 30)


def trades_of(name: str) -> list[dict]:
    tx = load_fixture(name)
    out = PARSER.parse(
        RawTx(tx["signature"], tx["slot"], tx["logMessages"], "fixture", block_time=tx["blockTime"])
    )
    return out.rows["trades"]


def exact(fill, trade) -> bool:
    return (fill.curve_quote, fill.tokens, fill.protocol_fee, fill.creator_fee) == (
        trade["quote_amount"], trade["token_amount"], trade["fee"], trade["creator_fee"]
    )  # fmt: skip


@pytest.mark.parametrize(
    "name",
    ["pump-BuyV2", "pump-BuyV3", "pump-BuyExactSolIn", "pump-BuyExactQuoteInV3", "pump-Sell",
     "pump-SellV2", "pump-SellV3", "pump-event-CompleteEvent"],
)  # fmt: skip
def test_replaying_real_trades_gives_the_chains_numbers(name):
    for trade in trades_of(name):
        assert trade["wallet"] != mayhem_agent_address()
        assert exact(replay(trade), trade), trade["signature"]


def test_a_buy_of_point_three_sol_costs_exactly_point_three_sol():
    # The recorded trader asked for exactly 5,423,787,361,875 tokens, which cost a net
    # 296,296,296 lamports plus fees: 0.3 SOL in total. Spending a 0.3 SOL budget instead
    # puts the same amount into the curve; the budget rule gives a few token units less.
    (trade,) = trades_of("pump-BuyV3")
    before = Curve.before_trade(trade)
    fill = buy_with_budget(before, 300_000_000, RATES)
    assert (fill.curve_quote, fill.protocol_fee, fill.creator_fee) == (
        296_296_296, 2_814_815, 888_889
    )  # fmt: skip
    paid_by_trader = trade["quote_amount"] + trade["fee"] + trade["creator_fee"]
    assert fill.user_quote == 300_000_000 == paid_by_trader
    assert fill.tokens == tokens_for_quote(before, 296_296_296)
    assert 0 < trade["token_amount"] - fill.tokens < trade["token_amount"] * 1e-8


def test_budget_split_keeps_fees_from_before_the_trim():
    # From a recorded trade: 98,119,158 + its fees is one lamport over the 99,345,648
    # budget, so the net drops by one and the fees stay as computed on the first net.
    assert split_budget(99_345_648, RATES) == (98_119_157, 932_133, 294_358)
    net, protocol, creator = split_budget(1_000_000_000, RATES)
    assert net + protocol + creator <= 1_000_000_000


def test_fee_rounding_is_always_up():
    assert ceil_fee(68_736_316, 95) == 652_996  # 652,995.002 rounds up
    assert ceil_fee(10_000, 95) == 95
    assert ceil_fee(1, 95) == 1
    assert ceil_fee(123_456, 0) == 0


def test_sell_returns_gross_minus_fees():
    (trade,) = trades_of("pump-Sell")
    fill = sell_tokens(Curve.before_trade(trade), trade["token_amount"], rates_of_trade(trade))
    assert fill.curve_quote == trade["quote_amount"]
    assert fill.user_quote == trade["quote_amount"] - trade["fee"] - trade["creator_fee"]
    assert fill.price_impact < 0


def test_a_coin_without_a_creator_pays_no_creator_fee():
    (trade,) = trades_of("pump-Sell")
    trade = {**trade, "creator": NO_CREATOR}
    assert rates_of_trade(trade) == FeeRates(trade["fee_basis_points"], 0)


def test_buying_the_rest_of_the_curve_completes_it():
    trade = trades_of("pump-event-CompleteEvent")[-1]  # the buy that emptied the curve
    before = Curve.before_trade(trade)
    fill = replay(trade)
    assert fill.capped and fill.tokens == before.real_token and fill.after.real_token == 0
    big = buy_with_budget(before, 10 * trade["quote_amount"], rates_of_trade(trade))
    assert big.capped and big.tokens == before.real_token
    assert big.curve_quote == quote_for_tokens(before, before.real_token)  # rest not spent


def test_round_trip_loses_the_fees_and_rounding():
    curve = Curve(30_000_000_000, 1_073_000_000_000_000, 0, 793_100_000_000_000)
    buy = buy_with_budget(curve, 1_000_000_000, RATES)
    sell = sell_tokens(buy.after, buy.tokens, RATES)
    assert sell.user_quote < buy.user_quote
    assert sell.after.virtual_quote == pytest.approx(curve.virtual_quote, abs=2)
    assert buy.price_impact > 0 and buy.average_price > curve.price()


def test_formulas_agree_with_each_other():
    curve = Curve(42_000_000_000, 760_000_000_000_000, 12_000_000_000, 480_000_000_000_000)
    for tokens in (1_000_000, 5_000_000_000, 100_000_000_000_000):
        net = quote_for_tokens(curve, tokens)
        assert tokens_for_quote(curve, net) <= tokens  # buying for that price never gives more
        assert tokens_for_quote(curve, net + 2) >= tokens


def test_market_cap_and_progress():
    curve = Curve(30_000_000_000, 1_073_000_000_000_000, 0, 793_100_000_000_000)
    assert curve.market_cap(1_000_000_000_000_000) == 27_958_993_476  # about 28 SOL
    assert curve.progress(793_100_000_000_000) == 0.0
    half = Curve(1, 1, 0, 396_550_000_000_000)
    assert half.progress(793_100_000_000_000) == pytest.approx(0.5)


def test_the_mayhem_agent_comes_from_the_idl_and_its_trades_follow_other_rules():
    agent = mayhem_agent_address()
    assert agent == "BwWK17cbHxwWBKZkUYvzxLcNQ1YVyaFezduWbtm2de6s"  # matches the chain
    (trade,) = trades_of("pump-Buy")  # a fee-free buy by the agent on a mayhem coin
    assert trade["wallet"] == agent and trade["fee"] == 0
    assert not exact(replay(trade), trade)
