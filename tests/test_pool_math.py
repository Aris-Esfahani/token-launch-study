"""PumpSwap pool math against real mainnet pool trades and a built pool.

The expected values are the chain's own: four trades of one graduated coin's pool on
2026-11-16 (pool HTii2QRM9dXkmpsJ3brvmoph4V2eFuJNM28w2Nv89qxR), as their events report
them. The effective quote reserve is the pool's quote plus its virtual quote reserve.
"""

from __future__ import annotations

import pyarrow as pa
import pyarrow.parquet as pq

from agent.sim.pool_math import (
    Pool,
    PoolRates,
    pool_buy_tokens,
    pool_buy_with_budget,
    pool_sell_tokens,
    verify_pool_trades,
)

POOL = "HTii2QRM9dXkmpsJ3brvmoph4V2eFuJNM28w2Nv89qxR"


def test_a_real_buy_with_a_budget_gives_the_chains_numbers():
    # 2tbhNAto...: buy_exact_quote_in_v2 of 2 SOL, the pool's first trade.
    pool = Pool(67_405_853_768 + 17_584_505_288, 206_900_000_000_000)
    fill = pool_buy_with_budget(pool, 2_000_000_000, PoolRates(2, 93, 30))
    assert fill.pool_quote == 1_975_308_641  # user_quote_amount_in: the net
    assert (fill.lp_fee, fill.protocol_fee, fill.creator_fee) == (395_062, 18_370_371, 5_925_926)
    assert fill.tokens == 4_699_456_330_797
    assert fill.user_quote == 2_000_000_000  # the whole budget, fees included
    # The next trade's event starts from these reserves (69,396,668,583 + 17,569,394,176).
    assert fill.after == Pool(69_396_668_583 + 17_569_394_176, 202_200_543_669_203)


def test_a_real_buy_of_exact_tokens_gives_the_chains_numbers():
    # 4MV3YFdE...: buy of 946,813.833906 tokens.
    pool = Pool(87_249_486_181 + 17_400_213_175, 168_092_196_871_127)
    fill = pool_buy_tokens(pool, 946_813_833_906, PoolRates(20, 5, 95))
    assert fill.pool_quote == 592_800_000
    assert (fill.lp_fee, fill.protocol_fee, fill.creator_fee) == (1_185_600, 296_400, 5_631_600)
    assert fill.user_quote == 599_913_600
    assert fill.after.quote == pool.quote + 593_985_600  # the net and the liquidity fee stay


def test_real_sells_give_the_chains_numbers():
    # 48tajjrf...: sell of 946,813.833906 tokens.
    pool = Pool(89_645_812_454 + 17_395_427_996, 164_340_039_664_381)
    fill = pool_sell_tokens(pool, 946_813_833_906, PoolRates(20, 5, 95))
    assert fill.pool_quote == 613_165_083
    assert (fill.lp_fee, fill.protocol_fee, fill.creator_fee) == (1_226_331, 306_583, 5_825_069)
    assert fill.user_quote == 605_807_100
    assert fill.after == Pool(pool.quote - 611_938_752, pool.token + 946_813_833_906)
    # 4LdxNTNZ...: a smaller sell later in the same pool.
    pool = Pool(91_146_567_268 + 17_384_791_174, 162_090_486_857_765)
    fill = pool_sell_tokens(pool, 75_559_266_494, PoolRates(20, 5, 95))
    assert (fill.pool_quote, fill.user_quote) == (50_568_846, 49_962_018)


def test_a_budget_too_small_to_buy_a_token_buys_nothing():
    fill = pool_buy_with_budget(Pool(10**11, 10**14), 1, PoolRates(20, 5, 95))
    assert fill.tokens == 0


def _event(fill, slot: int, *, budget: int | None = None, virtual: int = 0, **changes) -> dict:
    """A pool trade's event as the backfill stores it."""
    buy = fill.side == "buy"
    row = {
        "pool": POOL, "slot": slot, "bf_tx_index": 0, "event_index": 0, "ts": 1_000 + slot,
        "is_buy": buy, "ix_name": ("buy_exact_quote_in" if budget else "buy") if buy else None,
        "pool_base_token_reserves": fill.before.token,
        "pool_quote_token_reserves": fill.before.quote - virtual,
        "virtual_quote_reserves": str(virtual),
        "base_amount_out": fill.tokens if buy else None,
        "base_amount_in": None if buy else fill.tokens,
        "quote_amount_in": (budget or fill.pool_quote) if buy else None,
        "quote_amount_in_with_lp_fee": fill.pool_quote + fill.lp_fee if buy else None,
        "user_quote_amount_in": (fill.pool_quote if budget else fill.user_quote) if buy else None,
        "quote_amount_out": None if buy else fill.pool_quote,
        "quote_amount_out_without_lp_fee": None if buy else fill.pool_quote - fill.lp_fee,
        "user_quote_amount_out": None if buy else fill.user_quote,
        "lp_fee_basis_points": 20, "lp_fee": fill.lp_fee,
        "protocol_fee_basis_points": 5, "protocol_fee": fill.protocol_fee,
        "coin_creator_fee_basis_points": 95, "coin_creator_fee": fill.creator_fee,
        "cashback_fee_basis_points": 0, "cashback": 0,
    }  # fmt: skip
    return row | changes


def test_the_check_counts_what_the_formulas_reproduce(tmp_path):
    rates = PoolRates(20, 5, 95)
    pool = Pool(85 * 10**9, 2 * 10**14)
    fills = [pool_buy_with_budget(pool, 10**9, rates)]
    fills.append(pool_buy_tokens(fills[-1].after, 10**12, rates))
    fills.append(pool_sell_tokens(fills[-1].after, 5 * 10**11, rates))
    rows = [_event(fills[0], 1, budget=10**9, virtual=17 * 10**9),
            _event(fills[1], 2, virtual=17 * 10**9), _event(fills[2], 3, virtual=16 * 10**9)]
    # A sell whose event reports one lamport too many, from reserves nobody left behind.
    odd = pool_sell_tokens(Pool(90 * 10**9, 2 * 10**14), 10**11, rates)
    rows.append(_event(odd, 4, quote_amount_out=odd.pool_quote + 1))
    # A trade in a pool that is not a graduated pump.fun coin's: not checked.
    rows.append(_event(fills[2], 5, pool="SomeOtherPool"))
    root = tmp_path / "raw_backfill"
    for table, data in (("amm_trades", rows), ("migrations", [{"pool": POOL, "ts": 1_000}])):
        (root / table / "date=2026-11-16").mkdir(parents=True)
        pq.write_table(pa.Table.from_pylist(data), root / table / "date=2026-11-16" / "a.parquet")
    got = verify_pool_trades(tmp_path, 0, 10_000)
    assert got["buy with a budget"] == {"trades": 1, "amounts_exact": 1, "fees_exact": 1,
                                        "with_previous": 0, "continues": 0}  # fmt: skip
    assert got["buy, exact tokens"] == {"trades": 1, "amounts_exact": 1, "fees_exact": 1,
                                        "with_previous": 1, "continues": 1}  # fmt: skip
    # (One lamport more does not change the odd sell's fees, which round up.)
    assert got["sell"] == {"trades": 2, "amounts_exact": 1, "fees_exact": 2,
                           "with_previous": 2, "continues": 1}  # fmt: skip
    assert verify_pool_trades(tmp_path, 0, 1_002)["buy with a budget"]["trades"] == 1
    assert "sell" not in verify_pool_trades(tmp_path, 0, 1_002)
