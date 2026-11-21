"""Labels (spec 7; research track step R1): what happened to each coin after launch.

Labels look into the future. They are for training and evaluating models, never an input
to features, rules or anything live: this package imports nothing from `agent.features`
or `agent.daemon`, and those import nothing from it (tests/test_labels.py checks).

Source: the backfill only (`raw_backfill/`; spec 4.1, research track), which has every
transaction and its position in the block (`bf_tx_index`), so trades are in exact order.
A coin is labeled only if the backfill covers its whole label window (`complete`);
coins too close to the edge of the data keep a row with empty labels.

Prices are execution prices: quote paid (buy) or received (sell) per token, before fees,
on the bonding curve and, after graduation, in the coin's PumpSwap pool. They mean the
same thing in both places, whatever the pool's reserves look like (PumpSwap pools for
graduated coins add a virtual quote reserve). Trades under `prices.min_trade_sol` are
left out of prices (rounding noise), and so, by default, are the mayhem agent's trades
(`prices.mayhem_agent`): the agent trades at prices its own program sets. Its trades
still count as activity and still move the price everyone else pays. Price labels are
made for SOL-paired coins only (spec 5): coins priced in another coin get the other
labels, and empty price labels.

Limits, to be named in the write-up:
- `is_bundled_proxy` stands in for the bundle cluster, which needs the early buyers'
  funding transfers; the backfill does not keep them (spec 7).
- `is_rug` uses the creator's own wallet; linked wallets need the same funding data.
- Holdings come from curve and pool trades only; transfers between wallets are not in
  the data.
- The simulated entry is not added to the market: the prices after it are the real ones.
- The entry is priced on the curve's reserves after the last trade before it. Mayhem
  coins' curves are also reset by the mayhem program between trades, so their entry
  prices are less exact; results are reported separately for them (spec 8.10).
"""

from __future__ import annotations

import hashlib
import json
import time
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

import duckdb
import pyarrow as pa
import yaml
from pydantic import BaseModel, ConfigDict

from agent.ingest.backfill_coverage import backfill_fingerprint, backfill_spans, covered
from agent.ingest.recorder import write_parquet_atomic
from agent.ingest.solana import b58encode
from agent.sim.curve_math import (
    NO_CREATOR,
    Curve,
    FeeRates,
    buy_with_budget,
    mayhem_agent_address,
)

LAMPORTS_PER_SOL = 1_000_000_000
NATIVE_SOL = b58encode(bytes(32))  # quote_mint of SOL-paired coins: the all-zero address
BACKFILL = "raw_backfill"
CHUNK_S = 3 * 3600  # coins are labeled in batches by launch time, to bound memory
OUT_FOLDER = "labels"


# --- settings (config/labels.yaml) -----------------------------------------------------


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class DeadSettings(_Strict):
    quiet_after_s: int = 1800


class RugSettings(_Strict):
    creator_sold_share: float = 0.5
    drawdown: float = 0.8


class BundleSettings(_Strict):
    first_s: int = 1
    at_s: int = 60
    hold_pct: float = 20.0


class GraduationSettings(_Strict):
    within_s: int = 3600


class DoubleSettings(_Strict):
    offsets_s: list[int] = [30, 60, 120, 300]
    entry_delay_s: int = 5
    buy_sol: float = 1.0
    up: float = 2.0
    down: float = 0.5
    within_s: int = 3600


class PriceSettings(_Strict):
    min_trade_sol: float = 0.001
    mayhem_agent: Literal["exclude", "include"] = "exclude"


class LabelSettings(_Strict):
    window_s: int = 3600
    dead: DeadSettings = DeadSettings()
    rug: RugSettings = RugSettings()
    bundle: BundleSettings = BundleSettings()
    graduation: GraduationSettings = GraduationSettings()
    double: DoubleSettings = DoubleSettings()
    prices: PriceSettings = PriceSettings()

    @property
    def horizon_s(self) -> int:
        """How long after launch the data must reach for every label to be known."""
        d = self.double
        return max(self.window_s, self.graduation.within_s,
                   max(d.offsets_s) + d.entry_delay_s + d.within_s)  # fmt: skip


def load_label_settings(path: Path) -> tuple[LabelSettings, str]:
    """The settings and a short hash of the file, kept with the labels it made."""
    raw = path.read_bytes()
    return LabelSettings.model_validate(yaml.safe_load(raw) or {}), _sha(raw)


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()[:12]


# --- one coin --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Trade:
    """A trade of the coin, on its bonding curve or in its PumpSwap pool."""

    order: tuple[int, int, int]  # slot, position in the block, event index
    ts: int
    wallet: str
    is_buy: bool
    quote: int  # quote base units before fees: into the curve/pool (buy) or out (sell)
    tokens: int  # raw token units
    on_curve: bool = True
    after: Curve | None = None  # the curve right after this trade (curve trades)
    rates: FeeRates | None = None  # the fees this trade was charged (curve trades)

    @property
    def price(self) -> float:
        return self.quote / self.tokens


@dataclass
class Coin:
    mint: str
    creator: str
    created_ts: int
    supply: int
    quote_mint: str
    is_mayhem: bool
    start: Curve  # the curve at launch
    completed_ts: int | None = None  # when its bonding curve completed (graduated)
    trades: list[Trade] = field(default_factory=list)

    @property
    def is_sol_paired(self) -> bool:
        return self.quote_mint == NATIVE_SOL


COIN_LABELS = (
    "trades_1h", "agent_trades_1h", "last_trade_after_s", "is_dead",
    "is_dead_without_agent", "graduated_1h", "graduated_after_s", "creator_bought_pct",
    "creator_sold_share", "max_drawdown", "peak_multiple", "is_rug", "early_buyers",
    "early_buyer_hold_pct", "is_bundled_proxy", "bad",
)  # fmt: skip


def label_coin(coin: Coin, s: LabelSettings, agent: str, *, complete: bool
               ) -> tuple[dict, list[dict]]:  # fmt: skip
    """The coin's labels and one entry row per decision time (SOL-paired coins)."""
    row = {
        "mint": coin.mint, "created_ts": coin.created_ts, "creator": coin.creator,
        "is_mayhem": coin.is_mayhem, "is_sol_paired": coin.is_sol_paired,
        "quote_mint": coin.quote_mint, "complete": complete,
    }  # fmt: skip
    if not complete:
        return row | dict.fromkeys(COIN_LABELS), []
    t0 = coin.created_ts
    trades = sorted((t for t in coin.trades if t0 <= t.ts <= t0 + s.horizon_s),
                    key=lambda t: t.order)  # fmt: skip
    hour = [t for t in trades if t.ts <= t0 + s.window_s]
    sol = coin.is_sol_paired

    # Dead: no trades after T+30 min (spec 7). Also without the mayhem agent's trades.
    late = [t for t in hour if t.ts > t0 + s.dead.quiet_after_s]
    row["trades_1h"] = len(hour)
    row["agent_trades_1h"] = sum(t.wallet == agent for t in hour)
    row["last_trade_after_s"] = hour[-1].ts - t0 if hour else None
    row["is_dead"] = not late
    row["is_dead_without_agent"] = not any(t.wallet != agent for t in late)

    # Graduated within the hour.
    done = coin.completed_ts
    row["graduated_1h"] = done is not None and done <= t0 + s.graduation.within_s
    row["graduated_after_s"] = done - t0 if row["graduated_1h"] else None

    # Rug: the creator sold at least half of what they bought and the price fell 80%.
    bought = sum(t.tokens for t in hour if t.wallet == coin.creator and t.is_buy)
    sold = sum(t.tokens for t in hour if t.wallet == coin.creator and not t.is_buy)
    row["creator_bought_pct"] = bought * 100 / coin.supply
    row["creator_sold_share"] = sold / bought if bought else None
    path = _prices(hour, s, agent, exclude_agent=s.prices.mayhem_agent == "exclude")
    if sol:
        drawdown, peak_multiple = _drawdown(path)
        row["max_drawdown"], row["peak_multiple"] = drawdown, peak_multiple
        row["is_rug"] = (row["creator_sold_share"] or 0) >= s.rug.creator_sold_share and (
            drawdown >= s.rug.drawdown
        )
    else:
        row["max_drawdown"] = row["peak_multiple"] = row["is_rug"] = None

    # Bundled (stand-in): early buyers' holdings at T+60 s.
    pct, early = _early_buyer_hold(coin, trades, s, agent)
    row["early_buyers"], row["early_buyer_hold_pct"] = early, pct
    row["is_bundled_proxy"] = pct >= s.bundle.hold_pct

    row["bad"] = _any_true(row["is_dead"], row["is_bundled_proxy"], row["is_rug"])
    if not sol:
        return row, []
    return row, [_entry(coin, trades, offset, s, agent) for offset in s.double.offsets_s]


def _prices(trades: list[Trade], s: LabelSettings, agent: str, *, exclude_agent: bool
            ) -> list[tuple[Trade, float]]:  # fmt: skip
    """(trade, execution price) for the trades that count as prices, in order."""
    least = s.prices.min_trade_sol * LAMPORTS_PER_SOL
    return [
        (t, t.price) for t in trades
        if t.tokens > 0 and t.quote >= least and not (exclude_agent and t.wallet == agent)
    ]  # fmt: skip


def _drawdown(path: list[tuple[Trade, float]]) -> tuple[float, float | None]:
    """The largest fall from a running peak (0.8 = an 80% fall), and the peak price as a
    multiple of the first price."""
    peak, worst = 0.0, 0.0
    for _, price in path:
        peak = max(peak, price)
        worst = max(worst, 1 - price / peak)
    return worst, (peak / path[0][1] if path else None)


def _early_buyer_hold(coin: Coin, trades: list[Trade], s: LabelSettings, agent: str
                      ) -> tuple[float, int]:  # fmt: skip
    """% of supply held at T+at_s by wallets whose first buy came within first_s of
    launch (not the creator or the mayhem agent), and how many such wallets."""
    at = coin.created_ts + s.bundle.at_s
    balances: dict[str, int] = defaultdict(int)
    first_buy: dict[str, int] = {}
    for t in trades:
        if t.ts > at:
            break
        balances[t.wallet] += t.tokens if t.is_buy else -t.tokens
        if t.is_buy:
            first_buy.setdefault(t.wallet, t.ts)
    early = {
        w for w, ts in first_buy.items()
        if ts <= coin.created_ts + s.bundle.first_s and w not in (coin.creator, agent)
    }  # fmt: skip
    return sum(max(balances[w], 0) for w in early) * 100 / coin.supply, len(early)


def _any_true(*values: bool | None) -> bool | None:
    if any(v is True for v in values):
        return True
    return None if any(v is None for v in values) else False


def _entry(coin: Coin, trades: list[Trade], offset: int, s: LabelSettings, agent: str
           ) -> dict:  # fmt: skip
    """A simulated buy of `buy_sol` at decision time + delay (spec 7, stage 3), and
    whether a later price reached `up` times its price before falling to `down` times."""
    d = s.double
    entry_ts = coin.created_ts + offset + d.entry_delay_s
    exclude = s.prices.mayhem_agent == "exclude"
    row = {"mint": coin.mint, "offset_s": offset, "entry_ts": entry_ts, "venue": None,
           "entry_price": None, "entry_impact_pct": None, "entry_capped": None}  # fmt: skip
    if coin.completed_ts is not None and coin.completed_ts <= entry_ts:
        # Graduated before the entry: priced at the last trade before it, without a
        # simulated price impact (the pools' reserves include a virtual part).
        before = [t for t in trades if t.ts <= entry_ts]
        priced = _prices(before, s, agent, exclude_agent=exclude)
        if priced:
            row["venue"], row["entry_price"] = "pool", priced[-1][1]
    else:
        curve_before = [t for t in trades if t.on_curve and t.ts <= entry_ts]
        curve = curve_before[-1].after if curve_before else coin.start
        budget = round(d.buy_sol * LAMPORTS_PER_SOL)
        fill = buy_with_budget(curve, budget, _rates(trades, entry_ts, agent))
        if fill.tokens > 0:
            row["venue"], row["entry_price"] = "curve", fill.curve_quote / fill.tokens
            row["entry_impact_pct"] = fill.price_impact * 100
            row["entry_capped"] = fill.capped
    after = [t for t in trades if entry_ts < t.ts <= entry_ts + d.within_s]
    for suffix, exclude_agent in (("", exclude), ("_with_agent_prices", False)):
        path = _prices(after, s, agent, exclude_agent=exclude_agent)
        race = _race(path, row["entry_price"], entry_ts, d)
        row |= {f"{key}{suffix}": value for key, value in race.items()}
    return row


def _rates(trades: list[Trade], entry_ts: int, agent: str) -> FeeRates:
    """The fees a person paid on the curve: the last such trade before the entry, else
    the first after it (fee rates are protocol settings; this is a label, not a signal)."""
    paid = [t for t in trades if t.on_curve and t.rates is not None and t.wallet != agent]
    before = [t for t in paid if t.ts <= entry_ts]
    if before:
        return before[-1].rates
    return paid[0].rates if paid else FeeRates(0, 0)


def _race(path: list[tuple[Trade, float]], entry: float | None, entry_ts: int,
          d: DoubleSettings) -> dict:  # fmt: skip
    """Which came first after the entry: a price at `up` x entry or one at `down` x."""
    out = {"first_hit": None, "hit_2x": None, "seconds_to_hit": None,
           "max_multiple": None, "min_multiple": None, "path_trades": len(path)}  # fmt: skip
    if entry is None:
        return out
    out["hit_2x"] = False
    for t, price in path:
        if price >= d.up * entry or price <= d.down * entry:
            out["first_hit"] = "up" if price >= d.up * entry else "down"
            out["hit_2x"] = out["first_hit"] == "up"
            out["seconds_to_hit"] = t.ts - entry_ts
            break
    if path:
        out["max_multiple"] = max(p for _, p in path) / entry
        out["min_multiple"] = min(p for _, p in path) / entry
    return out


# --- reading the backfill --------------------------------------------------------------


def _view(db: duckdb.DuckDBPyConnection, root: Path, table: str) -> bool:
    if not any((root / table).glob("*/*.parquet")):
        return False
    pattern = f"{(root / table).as_posix()}/*/*.parquet"
    db.execute(f"CREATE OR REPLACE TEMP VIEW {table} AS "
               f"SELECT * FROM read_parquet('{pattern}', union_by_name=true)")  # fmt: skip
    return True


def load_coins(data_dir: Path, start_ts: int, end_ts: int, horizon_s: int) -> list[Coin]:
    """Coins launched in [start_ts, end_ts) and their trades up to `horizon_s` after
    launch, on the curve and in the PumpSwap pool they migrated to, from the backfill."""
    root = data_dir / BACKFILL
    db = duckdb.connect()
    tmp = data_dir / "duckdb_tmp"  # spills stay on the data drive
    tmp.mkdir(parents=True, exist_ok=True)
    db.execute(f"SET temp_directory = '{tmp.as_posix()}'")
    try:
        if not _view(db, root, "tokens"):
            return []
        db.execute(
            "CREATE TEMP TABLE wanted AS SELECT mint, creator, ts, token_total_supply, "
            "quote_mint, is_mayhem_mode, virtual_quote_reserves, virtual_token_reserves, "
            "real_token_reserves FROM tokens WHERE ts >= ? AND ts < ? "
            "QUALIFY row_number() OVER (PARTITION BY mint ORDER BY ts, signature) = 1",
            [start_ts, end_ts],
        )
        coins = {
            r[0]: Coin(mint=r[0], creator=r[1], created_ts=r[2], supply=r[3], quote_mint=r[4],
                       is_mayhem=bool(r[5]), start=Curve(r[6], r[7], 0, r[8]))
            for r in db.execute("SELECT * FROM wanted").fetchall()
        }  # fmt: skip
        if not coins:
            return []
        if _view(db, root, "trades"):
            rows = db.execute(
                "SELECT t.mint, t.slot, coalesce(t.bf_tx_index, 0), t.event_index, t.ts, "
                "t.wallet, t.is_buy, t.quote_amount, t.token_amount, "
                "t.virtual_quote_reserves, t.virtual_token_reserves, t.real_quote_reserves, "
                "t.real_token_reserves, t.fee_basis_points, t.creator_fee_basis_points, "
                "t.creator FROM trades t JOIN wanted w USING (mint) "
                "WHERE t.ts >= w.ts AND t.ts <= w.ts + ?", [horizon_s],
            ).fetchall()  # fmt: skip
            for r in rows:
                creator_bps = 0 if r[15] == NO_CREATOR else (r[14] or 0)
                coins[r[0]].trades.append(Trade(
                    order=(r[1], r[2], r[3]), ts=r[4], wallet=r[5], is_buy=bool(r[6]),
                    quote=r[7], tokens=r[8], after=Curve(r[9], r[10], r[11] or 0, r[12]),
                    rates=FeeRates(r[13] or 0, creator_bps),
                ))  # fmt: skip
        if _view(db, root, "curve_completes"):
            for mint, ts in db.execute(
                "SELECT mint, min(c.ts) FROM curve_completes c JOIN wanted USING (mint) "
                "GROUP BY mint"
            ).fetchall():  # fmt: skip
                coins[mint].completed_ts = ts
        if _view(db, root, "migrations") and _view(db, root, "amm_trades"):
            db.execute(
                "CREATE TEMP TABLE pools AS SELECT m.mint, arg_min(m.pool, m.ts) AS pool, "
                "any_value(w.ts) AS created FROM migrations m JOIN wanted w USING (mint) "
                "GROUP BY m.mint"
            )
            for r in db.execute(
                "SELECT p.mint, a.slot, coalesce(a.bf_tx_index, 0), a.event_index, a.ts, "
                "a.wallet, a.is_buy, "
                "CASE WHEN a.is_buy THEN a.quote_amount_in ELSE a.quote_amount_out END, "
                "CASE WHEN a.is_buy THEN a.base_amount_out ELSE a.base_amount_in END "
                "FROM amm_trades a JOIN pools p USING (pool) "
                "WHERE a.ts >= p.created AND a.ts <= p.created + ?", [horizon_s],
            ).fetchall():  # fmt: skip
                coins[r[0]].trades.append(_pool_trade(r))
        if _view(db, root, "post_complete_buys"):
            # v3 buys that complete the curve and spend the rest of their budget in the
            # new pool: the pool part is a buy at the pool's price.
            for r in db.execute(
                "SELECT b.mint, b.slot, coalesce(b.bf_tx_index, 0), b.event_index, b.ts, "
                "b.wallet, true, b.quote_in, b.base_out FROM post_complete_buys b "
                "JOIN wanted w USING (mint) WHERE b.ts >= w.ts AND b.ts <= w.ts + ?",
                [horizon_s],
            ).fetchall():  # fmt: skip
                coins[r[0]].trades.append(_pool_trade(r))
        return list(coins.values())
    finally:
        db.close()


def _pool_trade(r: tuple) -> Trade:
    return Trade(order=(r[1], r[2], r[3]), ts=r[4], wallet=r[5], is_buy=bool(r[6]),
                 quote=r[7] or 0, tokens=r[8] or 0, on_curve=False)  # fmt: skip


# --- building ----------------------------------------------------------------------------


@dataclass
class LabelRun:
    coins: list[dict]
    entries: list[dict]
    spans: list[tuple[int, int]]
    settings_sha: str
    seconds: float


def build_labels(data_dir: Path, settings: LabelSettings, settings_sha: str, *,
                 progress=lambda _msg: None) -> LabelRun:  # fmt: skip
    """Label every backfilled coin and write `labels/coins.parquet`,
    `labels/entries.parquet` and `labels/manifest.json` in the data folder."""
    started = time.time()
    spans = backfill_spans(data_dir)
    if not spans:
        raise FileNotFoundError("No backfilled data. Run `agent backfill --epoch <E>` first.")
    agent = mayhem_agent_address()
    coin_rows: list[dict] = []
    entry_rows: list[dict] = []
    first, last = spans[0][0], spans[-1][1]
    for start in range(first, last + 1, CHUNK_S):
        coins = load_coins(data_dir, start, start + CHUNK_S, settings.horizon_s)
        for coin in coins:
            end = coin.created_ts + settings.horizon_s
            row, entries = label_coin(coin, settings, agent,
                                      complete=covered(spans, coin.created_ts, end))  # fmt: skip
            coin_rows.append(row)
            entry_rows.extend(entries)
        progress(f"labeled coins launched up to {min(start + CHUNK_S, last)}: "
                 f"{len(coin_rows):,} coins")  # fmt: skip
    # A fixed order, so building again from the same data gives the same files.
    coin_rows.sort(key=lambda r: (r["created_ts"], r["mint"]))
    entry_rows.sort(key=lambda r: (r["entry_ts"] - r["offset_s"], r["mint"], r["offset_s"]))
    run = LabelRun(coin_rows, entry_rows, spans, settings_sha, time.time() - started)
    write_labels(data_dir, run, settings)
    return run


GROUPS = ("ordinary", "mayhem", "other quote")  # SOL-paired, SOL-paired mayhem, the rest
RATE_COLUMNS = ("is_dead", "is_dead_without_agent", "is_rug", "is_bundled_proxy", "bad",
                "graduated_1h")  # fmt: skip


def group_of(coin: dict) -> str:
    if not coin["is_sol_paired"]:
        return "other quote"
    return "mayhem" if coin["is_mayhem"] else "ordinary"


def summarize(run: LabelRun) -> dict:
    """Label rates per group of coins (complete coins only) and hit_2x rates per group
    and decision time (entries that have an entry price)."""
    groups: dict[str, dict] = {}
    complete = [c for c in run.coins if c["complete"]]
    kinds = {c["mint"]: group_of(c) for c in complete}
    for name in GROUPS:
        coins = [c for c in complete if kinds[c["mint"]] == name]
        rates = {}
        for column in RATE_COLUMNS:
            # "bad" is only a rate where all its parts are known: where is_rug is unknown
            # (coins not paired with SOL), the known values are only the True ones.
            known = [c[column] for c in coins if c[column] is not None
                     and (column != "bad" or c["is_rug"] is not None)]  # fmt: skip
            rates[column] = sum(known) / len(known) if known else None
        groups[name] = {"coins": len(coins), "rates": rates, "hit_2x": {}}
    for name in ("ordinary", "mayhem"):
        for offset in sorted({e["offset_s"] for e in run.entries}):
            entries = [
                e for e in run.entries
                if e["offset_s"] == offset and kinds.get(e["mint"]) == name
                and e["entry_price"] is not None
            ]  # fmt: skip
            if entries:
                groups[name]["hit_2x"][offset] = {
                    "entries": len(entries),
                    "rate": sum(e["hit_2x"] for e in entries) / len(entries),
                    "rate_with_agent_prices": (
                        sum(e["hit_2x_with_agent_prices"] for e in entries) / len(entries)
                    ),
                }
    return {"coins": len(run.coins), "complete": len(complete), "groups": groups}


def write_labels(data_dir: Path, run: LabelRun, settings: LabelSettings) -> Path:
    folder = data_dir / OUT_FOLDER
    folder.mkdir(parents=True, exist_ok=True)
    write_parquet_atomic(pa.Table.from_pylist(run.coins), folder / "coins.parquet")
    write_parquet_atomic(pa.Table.from_pylist(run.entries), folder / "entries.parquet")
    manifest = {
        "made_at": time.time(), "seconds": round(run.seconds, 1),
        "settings_sha": run.settings_sha, "settings": settings.model_dump(),
        "backfill_spans": run.spans, "backfill_files": backfill_fingerprint(data_dir),
        "coins": len(run.coins), "entries": len(run.entries),
        "complete_coins": sum(1 for c in run.coins if c["complete"]),
    }  # fmt: skip
    (folder / "manifest.json").write_text(json.dumps(manifest, indent=1), encoding="utf-8")
    return folder
