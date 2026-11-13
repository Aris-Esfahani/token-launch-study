"""Log attribution: an event only counts as pump.fun's if pump.fun wrote it."""

from agent.ingest.logs import event_discriminator, event_payloads, instruction_names, walk_logs
from helpers import OTHER, PUMP, TOKEN, data_line, program_block

TRADE = event_discriminator("TradeEvent") + b"\x01" * 40
CREATE = event_discriminator("CreateEvent") + b"\x02" * 40


def test_discriminators_match_the_official_idl():
    # Values copied from pump-fun/pump-public-docs idl/pump.json on 2026-11-12. If these
    # fail, the "sha256('event:<Name>')[:8]" rule no longer describes pump.fun's events.
    assert list(event_discriminator("TradeEvent")) == [189, 219, 127, 211, 78, 230, 97, 238]
    assert list(event_discriminator("CreateEvent")) == [27, 114, 169, 77, 222, 235, 99, 118]
    assert list(event_discriminator("CompleteEvent")) == [95, 114, 97, 156, 212, 46, 152, 8]


def test_direct_call_with_nested_program():
    logs = [
        f"Program {PUMP} invoke [1]",
        "Program log: Instruction: Buy",
        *program_block(TOKEN, ["TransferChecked"], [], depth=2),
        data_line(TRADE),
        f"Program {PUMP} consumed 40000 of 200000 compute units",
        f"Program {PUMP} success",
    ]
    lines, truncated = walk_logs(logs)
    assert not truncated
    assert instruction_names(lines, PUMP) == ["Buy"]
    assert instruction_names(lines, TOKEN) == ["TransferChecked"]
    assert event_payloads(lines, PUMP) == [TRADE]


def test_call_through_a_router():
    logs = [
        f"Program {OTHER} invoke [1]",
        "Program log: Instruction: Route",
        *program_block(PUMP, ["SellV3"], [TRADE], depth=2),
        f"Program {OTHER} success",
    ]
    lines, _ = walk_logs(logs)
    assert instruction_names(lines, PUMP) == ["SellV3"]
    assert event_payloads(lines, PUMP) == [TRADE]


def test_create_and_buy_in_one_transaction():
    logs = program_block(PUMP, ["CreateV2"], [CREATE]) + program_block(
        PUMP, ["BuyExactSolIn"], [TRADE]
    )
    lines, _ = walk_logs(logs)
    assert instruction_names(lines, PUMP) == ["CreateV2", "BuyExactSolIn"]
    assert event_payloads(lines, PUMP) == [CREATE, TRADE]


def test_another_program_cannot_fake_a_pump_event():
    logs = program_block(OTHER, ["Buy"], [TRADE])
    lines, _ = walk_logs(logs)
    assert instruction_names(lines, PUMP) == []
    assert event_payloads(lines, PUMP) == []


def test_a_program_cannot_fake_the_call_stack_from_inside_a_log_line():
    logs = [
        f"Program {OTHER} invoke [1]",
        f"Program log: Program {PUMP} invoke [2]",  # printed text, not a real call
        "Program log: Instruction: Buy",
        data_line(TRADE),
        f"Program {OTHER} success",
    ]
    lines, _ = walk_logs(logs)
    assert event_payloads(lines, PUMP) == []
    assert instruction_names(lines, PUMP) == []


def test_failed_inner_program_leaves_the_stack():
    logs = [
        f"Program {OTHER} invoke [1]",
        f"Program {PUMP} invoke [2]",
        f"Program {PUMP} failed: custom program error: 0x1772",
        data_line(TRADE),  # written by OTHER after pump.fun exited
        f"Program {OTHER} success",
    ]
    lines, _ = walk_logs(logs)
    assert event_payloads(lines, PUMP) == []
    assert event_payloads(lines, OTHER) == [TRADE]


def test_truncated_logs_are_flagged():
    logs = [f"Program {PUMP} invoke [1]", "Program log: Instruction: Buy", "Log truncated"]
    lines, truncated = walk_logs(logs)
    assert truncated
    assert instruction_names(lines, PUMP) == ["Buy"]


def test_unreadable_event_data_is_skipped():
    logs = [f"Program {PUMP} invoke [1]", "Program data: not*base64*", f"Program {PUMP} success"]
    lines, _ = walk_logs(logs)
    assert event_payloads(lines, PUMP) == []
