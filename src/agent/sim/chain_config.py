"""pump.fun's curve parameters and fee schedule, read from chain and kept as dated
snapshots, never hardcoded (spec section 4).

    <data_dir>/config_snapshots/config-<UTC time>.json

A snapshot holds the decoded `Global` account (pump program: starting reserves, supply,
migration fee, fee switches) and `FeeConfig` account (pump fees program: fee tiers), the
slot they were read at, and their raw data, so a snapshot can be decoded again if the IDL
files change. Account addresses are derived from the IDL.

The fee rules mirror pump-fun/pump-sdk 4.1.0 `computeFeesBps` (src/fees.ts, read
2026-11-12). Recorded trades carry the rates actually charged, so a backtest uses those;
this schedule is for trades that never happened (a simulated entry).
"""

from __future__ import annotations

import base64
import json
import os
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from agent.ingest.borsh_idl import Idl
from agent.ingest.rpc import Rpc
from agent.sim.curve_math import NO_CREATOR, Curve, FeeRates

SNAPSHOT_FOLDER = "config_snapshots"


@dataclass(frozen=True)
class QuoteKinds:
    """Which quote mints pump.fun's fee program treats as SOL and as stablecoins. Both
    lists are fixed inside its program, so they come from settings.yaml (with sources)."""

    sol_like: frozenset[str]
    stable: frozenset[str]


def _jsonable(value: object) -> object:
    if isinstance(value, bytes):
        return value.hex()
    if isinstance(value, int) and not -(2**53) <= value <= 2**53:
        return str(value)  # JSON readers lose precision on very large integers
    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_jsonable(v) for v in value]
    return value


def account_addresses() -> dict[str, tuple[str, Idl]]:
    """{label: (address, IDL that decodes it)} for the two accounts a snapshot reads."""
    pump, fees = Idl.load("pump"), Idl.load("pump_fees")
    return {
        "global": (pump.account_address("buy", "global"), pump),
        "fee_config": (pump.account_address("buy", "fee_config"), fees),
    }


async def fetch_snapshot(rpc: Rpc) -> dict:
    snapshot: dict = {"fetched_at": time.time(), "accounts": {}}
    for label, (address, idl) in account_addresses().items():
        found = await rpc.account_info(address)
        if found is None:
            raise ValueError(f"account {label} ({address}) not found")
        data, slot, owner = found
        decoded = idl.decode_account(data)
        if decoded is None:
            raise ValueError(f"account {label} is not a type the IDL knows; update the IDL files")
        snapshot["accounts"][label] = {
            "address": address,
            "owner": owner,
            "slot": slot,
            "type": decoded.name,
            "values": _jsonable(decoded.values),
            "data_base64": base64.b64encode(data).decode(),
        }
    return snapshot


def save_snapshot(data_dir: Path, snapshot: dict) -> Path:
    folder = data_dir / SNAPSHOT_FOLDER
    folder.mkdir(parents=True, exist_ok=True)
    stamp = datetime.fromtimestamp(snapshot["fetched_at"], UTC).strftime("%Y%m%dT%H%M%S")
    path = folder / f"config-{stamp}.json"
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(snapshot, indent=1), encoding="utf-8")
    os.replace(tmp, path)
    return path


def load_snapshots(data_dir: Path) -> list[dict]:
    folder = data_dir / SNAPSHOT_FOLDER
    files = sorted(folder.glob("config-*.json")) if folder.is_dir() else []
    snapshots = [json.loads(f.read_text(encoding="utf-8")) for f in files]
    return sorted(snapshots, key=lambda s: s["fetched_at"])


def snapshot_at(snapshots: list[dict], ts: float) -> dict | None:
    """The newest snapshot taken at or before `ts`, else the oldest one (fees rarely
    change, and the trade's own recorded rates are the ground truth anyway)."""
    earlier = [s for s in snapshots if s["fetched_at"] <= ts]
    if earlier:
        return earlier[-1]
    return snapshots[0] if snapshots else None


def _int(value: object) -> int:
    return int(value)  # snapshot integers may be stored as text


def _fees(entry: dict) -> tuple[int, int]:
    return _int(entry["protocol_fee_bps"]), _int(entry["creator_fee_bps"])


def _tier(tiers: list[dict], market_cap: int) -> tuple[int, int]:
    """pump-fees-math `calculate_fee_tier`: the last tier whose threshold the market cap
    reaches, or the first tier below every threshold."""
    if not tiers:
        raise ValueError("empty fee tier list")
    chosen = tiers[0]
    for tier in tiers:
        if market_cap >= _int(tier["market_cap_lamports_threshold"]):
            chosen = tier
    return _fees(chosen["fees"])


@dataclass(frozen=True)
class Schedule:
    """Curve parameters and fee rules from one snapshot."""

    global_values: dict
    fee_config: dict | None
    kinds: QuoteKinds

    @classmethod
    def from_snapshot(cls, snapshot: dict, kinds: QuoteKinds) -> Schedule:
        accounts = snapshot["accounts"]
        fee_config = accounts.get("fee_config", {}).get("values")
        return cls(accounts["global"]["values"], fee_config, kinds)

    def get(self, field: str) -> int:
        return _int(self.global_values[field])

    def starting_curve(self) -> Curve:
        """A new SOL-paired curve, as `create` / `create_v2` sets it up."""
        return Curve(
            self.get("initial_virtual_sol_reserves"),
            self.get("initial_virtual_token_reserves"),
            0,
            self.get("initial_real_token_reserves"),
        )

    def rates(
        self,
        curve: Curve,
        *,
        quote_mint: str,
        coin_creator_fee_bps: int = 0,
        creator: str = "",
        mayhem: bool = False,
        mint_supply: int | None = None,
    ) -> FeeRates:
        """The fee rates a trade on this curve pays (pump-sdk `computeFeesBps`)."""
        if self.fee_config is None:
            protocol = self.get("fee_basis_points")
            creator_bps = self.get("creator_fee_basis_points")
        else:
            supply = mint_supply if (mayhem and mint_supply) else self.get("token_total_supply")
            market_cap = curve.market_cap(supply)
            if quote_mint in self.kinds.sol_like:
                protocol, creator_bps = _tier(self.fee_config["fee_tiers"], market_cap)
            elif quote_mint in self.kinds.stable:
                tiers = self.fee_config["stable_fee_tiers"] or self.fee_config["fee_tiers"]
                protocol, creator_bps = _tier(tiers, market_cap)
            else:
                exotic = self.fee_config["exotic_flat_fees"]
                is_set = any(_int(v) for v in exotic.values())
                protocol, creator_bps = _fees(exotic if is_set else self.fee_config["flat_fees"])
        if self.global_values.get("creator_fee_configurable") and coin_creator_fee_bps:
            creator_bps = coin_creator_fee_bps  # the coin's own rate replaces the schedule's
        if creator == NO_CREATOR:
            creator_bps = 0
        return FeeRates(protocol, creator_bps)
