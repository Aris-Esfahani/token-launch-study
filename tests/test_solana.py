import pytest

from agent.ingest.borsh_idl import Idl
from agent.ingest.solana import b58decode, b58encode, find_program_address
from helpers import PUMP, load_fixture

FEE_PROGRAM = "pfeeUxB6jkeY1Hxd7CsFCAjcbHA9rWtchMGdZ6VojVZ"


def test_base58_round_trip():
    assert b58encode(bytes(32)) == "1" * 32
    raw = b58decode(PUMP)
    assert len(raw) == 32
    assert b58encode(raw) == PUMP
    assert b58decode("11" + b58encode(b"\x01\x02")) == b"\x00\x00\x01\x02"


def test_base58_rejects_characters_outside_the_alphabet():
    with pytest.raises(ValueError):
        b58decode("0OIl")


def test_program_derived_addresses_match_accounts_read_from_chain():
    # Both addresses were read from mainnet on 2026-11-12 (owner and layout checked).
    assert find_program_address([b"global"], PUMP)[0] == (
        "4wTV1YmiEkRvAtNtsSGPtUrqRYQMe5SKy2uB4Jjaxnjf"
    )
    assert find_program_address([b"fee_config", b58decode(PUMP)], FEE_PROGRAM)[0] == (
        "8Wf5TiAheLUqBrKXeYg2JtAFFMWtKdG2BSFgqUcPVwTt"
    )


def test_create_marker_account_comes_from_the_idl_and_appears_in_a_real_create():
    idl = Idl.load("pump")
    marker = idl.account_address("create_v2", "mint_authority")
    assert marker == idl.account_address("create", "mint_authority")

    def accounts(name: str) -> list[str]:
        tx = load_fixture(name)
        loaded = tx.get("loadedAddresses") or {}
        return tx["accountKeys"] + loaded.get("writable", []) + loaded.get("readonly", [])

    assert marker in accounts("pump-CreateV2")
    assert marker not in accounts("pump-Sell")  # only creates mention it: the audit relies on that
