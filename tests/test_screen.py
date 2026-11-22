"""The screen (spec 12): the clipboard watcher, the text it shows, and the app itself."""

import asyncio
import sys
import time

import pytest

from agent.tui import view
from agent.tui.clipboard import ClipboardWatcher, find_mint, system_clipboard
from test_engine import MINT_A, MINT_B

WALLET_KEY = "4" * 88  # the length of a wallet's base58 private key


class FakeClipboard:
    def __init__(self, text: str | None = None) -> None:
        self.number, self.value, self.reads = 1, text, 0

    def copy(self, text: str | None) -> bool:
        self.number += 1
        self.value = text
        return True

    def sequence(self) -> int:
        return self.number

    def text(self) -> str | None:
        self.reads += 1
        return self.value


def test_only_exactly_one_address_counts_as_a_mint():
    assert find_mint(MINT_A) == MINT_A
    assert find_mint(f"  {MINT_A}\r\n") == MINT_A
    assert find_mint(MINT_A[:-1] + "0") is None  # 0 is not base58
    assert find_mint(MINT_A[:20]) is None
    assert find_mint(f"{MINT_A} {MINT_B}") is None
    assert find_mint(f"buy {MINT_A}") is None
    assert find_mint(WALLET_KEY) is None
    assert find_mint("") is None and find_mint(None) is None


def test_the_watcher_reports_each_new_copy_once_and_reads_only_after_a_change():
    clip = FakeClipboard(MINT_B)  # already there when the screen opens: ignored
    watcher = ClipboardWatcher(clip)
    assert watcher.poll() is None and clip.reads == 0  # nothing changed, nothing read
    clip.copy(MINT_A)
    assert watcher.poll() == MINT_A and watcher.changed_at is not None
    assert watcher.poll() is None  # no change since
    clip.copy(MINT_A)
    assert watcher.poll() is None  # the same mint copied again
    clip.copy("some other text")
    assert watcher.poll() is None
    clip.copy(MINT_B)
    assert watcher.poll() == MINT_B


def test_the_watcher_can_be_switched_off_and_skips_what_was_copied_meanwhile():
    clip = FakeClipboard()
    watcher = ClipboardWatcher(clip)
    assert watcher.toggle() is False
    clip.copy(MINT_A)
    assert watcher.poll() is None
    assert watcher.toggle() is True
    assert watcher.poll() is None  # copied while it was off
    clip.copy(MINT_B)
    assert watcher.poll() == MINT_B


def test_without_a_clipboard_the_watcher_is_unavailable():
    watcher = ClipboardWatcher(None)
    assert not watcher.available and not watcher.enabled
    assert watcher.toggle() is False and watcher.poll() is None
    assert watcher.copy(MINT_A) is False


def test_the_watcher_copies_an_address_and_nothing_else_and_skips_its_own_copy():
    clip = FakeClipboard()
    watcher = ClipboardWatcher(clip)
    assert watcher.copy(MINT_A) is True and clip.value == MINT_A
    assert watcher.poll() is None  # its own copy is not a coin the user copied
    clip.copy(MINT_A)
    assert watcher.poll() is None  # the same coin copied again by hand: still the same
    clip.copy(MINT_B)
    assert watcher.poll() == MINT_B
    for not_an_address in ("hello", WALLET_KEY, f"{MINT_A} {MINT_B}", ""):
        assert watcher.copy(not_an_address) is False
    assert clip.value == MINT_B  # nothing but an address is ever written

    class Refuses(FakeClipboard):
        def copy(self, text):
            raise OSError("clipboard busy")

    busy = ClipboardWatcher(Refuses())
    assert busy.copy(MINT_A) is False and busy.poll() is None


@pytest.mark.skipif(sys.platform != "win32", reason="the clipboard watcher is Windows only")
def test_the_windows_clipboard_counter_can_be_read():
    clip = system_clipboard()
    assert clip is not None and isinstance(clip.sequence(), int)  # content is not read


def analysis(**changes) -> dict:
    snapshot = {
        "token_age_s": 74, "curve_progress_pct": 34.2, "market_cap_sol": 41.25,
        "unique_buyers": 25, "unique_buyers_per_min": 20.3, "holders": 22, "buys": 40,
        "sells": 8, "net_sol": 3.1, "net_sol_last_30s": -0.4, "top10_hold_pct": 22.0,
        "top1_hold_pct": 4.0, "snipers": 2, "sniper_hold_pct": 5.0, "launch_slot_buyers": 1,
        "largest_sell_pct": 0.8, "dev_hold_pct": 3.0, "dev_buy_pct": 4.0, "dev_sold_pct": None,
        "dev_prior_launches": 0, "is_mayhem": False, "is_sol_paired": True, "completed": False,
    }  # fmt: skip
    base = {
        "op": "analysis", "mint": MINT_A, "known": True, "symbol": "STM",
        "name": "StonkMarket", "data_age_s": 2.0, "level": "pass", "reasons": [],
        "has_gap": False, "verdicts": [{"offset_s": 30, "level": "caution"}],
        "snapshot": snapshot,
    }  # fmt: skip
    return {**base, **changes}


def test_the_full_panel_starts_with_the_mint_then_the_verdict():
    lines = view.analysis_lines(analysis(), full=True)
    assert lines[0] == (
        f"{MINT_A}  $STM (StonkMarket) · age 74s · curve 34% · mc 41.2 SOL · data as of 2s ago"
    )
    assert lines[1] == "PASS: no rule fired"
    assert "buyers 25 (20/min)" in lines[2] and "last 30s -0.40 SOL" in lines[2]
    assert "dev holds 3.0% (bought 4.0%, has not sold) · 0 earlier launches seen" == lines[4]
    assert lines[5] == "verdicts so far: T+30s caution"
    assert not any(line.startswith("!") for line in lines)

    flagged = view.analysis_lines(analysis(
        level="avoid", reasons=["dev sold 80% of their buy"], has_gap=True, verdicts=[],
        snapshot={**analysis()["snapshot"], "is_mayhem": True, "dev_sold_pct": 80.0},
    ), full=True)  # fmt: skip
    assert flagged[1] == "AVOID: dev sold 80% of their buy"
    assert "sold 80% of their buy" in flagged[4]
    assert "no verdict yet" in flagged[5]
    assert [ln for ln in flagged if ln.startswith("!")] == [
        "! data incomplete: a recording gap touched this coin",
        "! mayhem coin: pump.fun's own bot trades it",
    ]


def test_a_copied_bonding_curve_address_is_pointed_out():
    curve = analysis(copied=MINT_B)  # MINT_B stands in for the curve
    lines = view.analysis_lines(curve, full=True)
    assert lines[0].startswith(MINT_A)  # the real mint, ready for Padre's search
    assert "! you copied the coin's bonding-curve address; its mint is the first line" in lines
    same = analysis(copied=MINT_A)
    assert not any("bonding-curve" in ln for ln in view.analysis_lines(same, full=True))
    plain = view.analysis_lines(curve)
    assert "! You copied the coin's trading address; this is the coin it belongs to" in plain
    assert plain[-1] == f"Address: {MINT_A}"
    assert not any("trading address" in ln for ln in view.analysis_lines(same))


def test_the_plain_panel_says_it_in_sentences_and_ends_with_the_address():
    lines = view.analysis_lines(analysis())
    assert lines == [
        "StonkMarket ($STM) · 1 min old · data from 2 sec ago",
        "LOOKS OK: no warning sign so far",
        "  25 people bought it. The 10 biggest holders own 22%.",
        "  The creator holds 3% and has not sold.",
        "  Worth about 41 SOL, 34% of the way to graduating.",
        f"Address: {MINT_A}",
    ]
    clicked = view.analysis_lines(analysis(), copied=True)
    assert clicked[-1] == f"Address (copied, paste it in Padre): {MINT_A}"

    snapshot = {**analysis()["snapshot"], "unique_buyers": 1, "dev_sold_pct": 80.0,
                "dev_prior_launches": 12, "is_mayhem": True, "completed": True,
                "token_age_s": 7_300}  # fmt: skip
    flagged = view.analysis_lines(analysis(
        level="avoid", reasons=["dev sold 80% of their buy", "only 1 buyers so far"],
        has_gap=True, snapshot=snapshot, name=None))  # fmt: skip
    assert flagged[0] == "$STM · 2 h old · data from 2 sec ago"
    assert flagged[1] == "AVOID: dev sold 80% of their buy; only 1 buyers so far"
    assert flagged[2] == "  1 person bought it. The 10 biggest holders own 22%."
    assert flagged[3] == ("  The creator holds 3% and has sold 80% of what they bought. They "
                          "launched 12 coins before (that we saw).")  # fmt: skip
    assert flagged[4] == "  Worth about 41 SOL. It has graduated (left the bonding curve)."
    assert [ln for ln in flagged if ln.startswith("!")] == [
        "! Some of this coin's data is missing (the recording was interrupted)",
        "! Mayhem coin: pump.fun's own bot trades it",
    ]
    other = {**analysis()["snapshot"], "market_cap_sol": None, "is_sol_paired": False,
             "dev_buy_pct": 0.0, "dev_hold_pct": 0.0}  # fmt: skip
    lines = view.analysis_lines(analysis(level="caution", reasons=["x"], snapshot=other))
    assert lines[1] == "BE CAREFUL: x"
    assert lines[3] == "  The creator has not bought any."
    assert lines[4] == "  Not priced in SOL, 34% of the way to graduating."


def test_unknown_mints_and_errors_say_why():
    unknown = {"op": "analysis", "mint": MINT_A, "known": False, "why": "not in the live data"}
    assert view.analysis_lines(unknown) == [MINT_A, "not tracked: not in the live data"]
    error = {"op": "error", "error": "not a Solana address"}
    assert view.analysis_lines(error)[1] == "could not analyze: not a Solana address"


def test_a_coin_in_the_list_is_shown_by_its_name_in_plain_words():
    now = time.time()
    alert = {
        "mint": MINT_A, "symbol": "STM", "name": "StonkMarket", "created_ts": now - 150,
        "offset_s": 60, "level": "pass", "has_gap": False, "decided_at": now, "wait_s": 3.0,
        "signals": {"market_cap_sol": 32.4, "curve_progress_pct": 12.0, "unique_buyers": 25,
                    "top10_hold_pct": 22.0, "dev_hold_pct": 3.0},
    }  # fmt: skip
    assert view.alert_text(alert, now) == (
        "StonkMarket ($STM) · 2 min old · looked OK at 1 min · 25 buyers · worth 32 SOL")
    assert MINT_A not in view.alert_text(alert, now)  # the address is in the details
    assert view.alert_text(alert, now + 3 * 3600).startswith("StonkMarket ($STM) · 3 h old · ")
    early = {**alert, "offset_s": 30, "created_ts": now - 31, "has_gap": True,
             "signals": {"unique_buyers": 1}}  # fmt: skip
    assert view.alert_text(early, now) == (
        "StonkMarket ($STM) · 31 sec old · looked OK at 30 sec · 1 buyer · some data missing")
    # What an older engine or a test sends: no name, no launch time.
    bare = {"mint": MINT_A, "symbol": "STM", "offset_s": 300, "level": "caution", "signals": {}}
    assert view.alert_text(bare, now) == "$STM · be careful at 5 min"
    assert view.alert_text({**bare, "symbol": None}) == "(no name) · be careful at 5 min"
    assert view.how_long(None) == "?" and view.how_long(-4) == "0 sec"


def test_launcher_chosen_names_are_cleaned():
    assert view.clean("Stonk\x1b[31mRed\x07") == "Stonk[31mRed"  # no escape or bell
    assert view.clean("x" * 40) == "x" * 19 + "…"
    assert view.clean(None) == ""
    assert view.coin_name({"name": "A\x1b[2JB\n", "symbol": "\x07S"}) == "A[2JB ($S)"
    assert view.coin_name({"name": "n" * 60, "symbol": "s" * 30}) == (
        "n" * 27 + "… ($" + "s" * 11 + "…)")
    assert view.alert_text({
        "mint": MINT_A, "symbol": "\x1b]8;;evil\x07", "offset_s": 30, "level": "pass",
        "decided_at": time.time(), "signals": {},
    }).count("\x1b") == 0  # fmt: skip


def test_the_status_bar():
    offline = view.status_text(None, connected=False, median_s=None, clipboard="on (F2)")
    assert offline.startswith("Engine off: type start")
    status = {
        "connections": [{"connected": True}, {"connected": False}], "covered_lag_s": 4.2,
        "creates_per_hr": 1820, "live": {"enabled": True, "median_wait_s": 6.0},
        "recording": True, "last_gap": {"at": time.time(), "seconds": 63},
    }  # fmt: skip
    text = view.status_text(status, connected=True, median_s=0.123, clipboard="on (F2)")
    assert text.startswith("Engine on · 1 of 2 feeds up · data 4 sec behind · last interruption ")
    assert text.endswith("(63 sec) · answers in 0.12 sec · clipboard on (F2)")
    # All well: only what is on. Trouble is named, in capitals where it stops the data.
    fine = {"connections": [{"connected": True}] * 2, "covered_lag_s": 1.0, "recording": True,
            "live": {"enabled": True, "median_wait_s": 4.0}}  # fmt: skip
    assert view.status_text(fine, connected=True, median_s=None, clipboard="off (F2)") == (
        "Engine on · data 1 sec behind · clipboard off (F2)")
    bad = {"connections": [{"connected": False}], "recording": False,
           "live": {"enabled": True, "median_wait_s": 31.0}}  # fmt: skip
    assert view.status_text(bad, connected=True, median_s=None, clipboard="x") == (
        "Engine on · FEED DOWN · checks run 31 sec late · NOT RECORDING · clipboard x")
    off = view.status_text({"connected": True, "live": {"enabled": False, "rules_error": "x"}},
                           connected=True, median_s=None, clipboard="x")  # fmt: skip
    assert "rule checks OFF" in off
    waiting = view.status_text({}, connected=True, median_s=None, clipboard="x")
    assert waiting.startswith("Engine on, waiting")


# --- the app itself (needs the Textual package) -------------------------------------


def test_the_app_shows_alerts_and_analyzes_copied_and_typed_mints(tmp_path):
    pytest.importorskip("textual")
    from agent.daemon.local_api import LocalApi
    from agent.tui.app import AgentScreen
    from test_local_api import free_port, until

    async def scenario():
        avoid = analysis(mint=MINT_B, level="avoid", reasons=["top 10 wallets hold 40%"])
        answers = {MINT_A: analysis(), MINT_B: avoid}
        api = LocalApi(lambda m: answers[m], lambda: {"connections": [{"connected": True}],
                                                      "covered_lag_s": 1.0})  # fmt: skip
        port = free_port()
        await api.start(port)
        old = {"mint": MINT_B, "symbol": "OLD", "name": "Old Coin", "offset_s": 30,
               "level": "pass", "created_ts": time.time() - 45, "decided_at": time.time(),
               "signals": {"unique_buyers": 3}}  # fmt: skip
        api.publish(old)
        clip = FakeClipboard()
        app = AgentScreen(port, tmp_path, clipboard=clip)
        try:
            async with app.run_test(size=(140, 40)) as pilot:
                await until(lambda: app.client.connected)
                alerts = app.query_one("#alerts")
                await until(lambda: alerts.option_count == 1)
                first_line = str(alerts.get_option_at_index(0).prompt)
                # A later check of the same coin, and another coin: one line per coin.
                api.publish({**old, "offset_s": 60, "signals": {"unique_buyers": 9}})
                api.publish({**old, "mint": MINT_A, "name": "Stonk", "symbol": "STM"})
                await until(lambda: alerts.option_count == 2)
                await until(lambda: "9 buyers" in str(alerts.get_option_at_index(0).prompt))
                second_line = str(alerts.get_option_at_index(0).prompt)
                app._coins[MINT_B]["alert"]["created_ts"] -= 600  # as if 10 minutes passed
                app._refresh_ages()
                aged_line = str(alerts.get_option_at_index(0).prompt)
                clip.copy(MINT_A)  # copied in Padre
                panel = app.query_one("#analysis")
                await until(lambda: "LOOKS OK" in str(panel.render()))
                copied = str(panel.render())
                await pilot.click("#alerts", offset=(4, 1))  # the first coin (MINT_B)
                await until(lambda: "AVOID" in str(panel.render()))
                clicked = str(panel.render())
                on_clipboard = clip.value
                await pilot.pause(0.3)  # the watcher polls: its own copy starts nothing
                after_copy = (len(app.latencies), str(panel.render()))
                await pilot.press("f3")
                await pilot.pause()
                full = str(panel.render())
                await pilot.press("f3")
                await pilot.pause()
                short_again = str(panel.render())
                await pilot.click("#input")
                await pilot.press(*MINT_B, "enter")  # typed or pasted
                await pilot.pause()
                await until(lambda: len(app.latencies) == 3)
                typed = str(panel.render())
                await pilot.press(*"not a mint", "enter")
                await pilot.pause()
                refused = str(panel.render())
                status = str(app.query_one("#status").render())
        finally:
            await api.close()
        lines = (first_line, second_line, aged_line)
        panels = (copied, clicked, on_clipboard, after_copy, full, short_again)
        return lines, panels, typed, refused, status, list(app.latencies)

    lines, panels, typed, refused, status, latencies = asyncio.run(scenario())
    first_line, second_line, aged_line = lines
    copied, clicked, on_clipboard, after_copy, full, short_again = panels
    assert first_line.startswith("Old Coin ($OLD) · ") and "looked OK at 30 sec" in first_line
    assert MINT_B not in first_line and first_line.endswith("3 buyers")
    assert "looked OK at 1 min · 9 buyers" in second_line
    assert "Old Coin ($OLD) · 10 min old · " in aged_line
    # Copied in Padre: the details, with the address but nothing written to the clipboard.
    assert copied.startswith("StonkMarket ($STM) · ") and "LOOKS OK: no warning sign" in copied
    assert f"Address: {MINT_A}" in copied and "F3: every number" in copied
    # Clicked in the list: the same, and the address is on the clipboard for Padre.
    assert "AVOID: top 10 wallets hold 40%" in clicked
    assert f"Address (copied, paste it in Padre): {MINT_B}" in clicked
    assert on_clipboard == MINT_B and after_copy == (2, clicked)
    assert full.startswith(MINT_B) and "launch-slot buyers" in full
    assert "F3: the short version" in full and short_again == clicked
    assert "AVOID" in typed and f"Address: {MINT_B}" in typed
    assert "not a Solana address" in refused and "not a mint" not in refused
    assert status.startswith("Engine on · data 1 sec behind") and "answers in 0." in status
    assert len(latencies) == 3 and max(latencies) < 2.0  # spec: copy-to-verdict under 2 s
    assert len((tmp_path / "screen_latency.jsonl").read_text().splitlines()) == 3


# --- model scores in the analysis panel (spec 15.3 step L1) --------------------------------


def scored_analysis(**changes) -> dict:
    from scoring_helpers import INFO

    context = {"dev_launches_24h": 3, "launches_last_hour": 1800, "launch_hour_utc": 11,
               "hour_covered": 1.0, "day_covered": 1.0}  # fmt: skip
    usual = {"bad": 0.8717, "grad_later": 0.0108, "hit_2x": 0.0241}
    verdicts = [
        {"offset_s": 30, "level": "caution", "wait_s": 4.2, "usual": usual,
         "scores": {"model": "r5/lightgbm", "context": context,
                    "p": {"bad": 0.9712, "grad_later": 0.004, "hit_2x": 0.131}}},
        {"offset_s": 60, "level": "avoid", "wait_s": 31.0, "usual": usual,
         "scores": {"model": "r5/lightgbm", "context": context,
                    "p": {"bad": 0.5, "hit_2x": 0.02},
                    "missing": {"grad_later": "already graduated"}}},
    ]  # fmt: skip
    return analysis(**{"verdicts": verdicts, "models": {"on": True, **INFO}, **changes})


def test_model_scores_are_shown_beside_the_verdict_with_their_usual_rates_and_limits():
    lines = view.analysis_lines(scored_analysis(), full=True)
    assert lines[1] == "PASS: no rule fired"  # the rules' verdict keeps its place
    assert lines[5] == "verdicts so far: T+30s caution · T+60s avoid"
    assert lines[6] == (
        "model scores, as analysis and not advice (usual = how often it happened on the "
        "test days Nov 18-19, 2026, same time, same kind of coin):"
    )
    assert lines[7] == ("  T+30s, 4s late: bad 97% (usual 87%) · graduates later 0.4% "
                        "(usual 1.1%) · 2x before half 13% (usual 2.4%)")  # fmt: skip
    assert lines[8] == ("  T+60s, 31s late: bad 50% (usual 87%) · graduates later - "
                        "(already graduated) · 2x before half 2.0% (usual 2.4%)")  # fmt: skip
    assert lines[9] == ("! models trained on Nov 12-16, 2026; the market drifts, and "
                        "the backtest found no signal that makes money after costs")  # fmt: skip
    assert len(lines) == 10
    assert view._days(["2026-11-30", "2026-12-01", "2026-12-02"]) == "Nov 30 to Dec 2, 2026"
    assert view._days(["2026-11-17"]) == "Nov 17, 2026" and view._days(None) == "?"


def test_a_missing_score_says_why_and_a_short_creator_history_is_pointed_out():
    a = scored_analysis()
    why = "only 40% of the hour before its launch was recorded (95% needed to count the " \
          "launches in it)"
    a["verdicts"][1]["scores"] = {"model": "r5/lightgbm", "context": {}, "why": why}
    lines = view.analysis_lines(a, full=True)
    assert lines[8] == f"  T+60s, 31s late: no score: {why}"
    assert not any("creator history" in ln for ln in lines)  # nothing scored last: no note
    assert view.plain_model_lines(a) == [f"No model guess at 1 min: {why}"]

    partial = scored_analysis()
    partial["verdicts"][1]["scores"]["context"] = {
        **partial["verdicts"][1]["scores"]["context"], "day_covered": 0.31}
    assert view.analysis_lines(partial, full=True)[-1] == (
        "! creator history: 31% of the 24 h before launch recorded, so the creator's launch "
        "count (3) is a lower bound"
    )
    assert view.plain_model_lines(partial)[-1] == (
        "! The creator's coin count (3) may be too low: only 31% of the day before the "
        "launch was recorded")
    made_before = scored_analysis()
    made_before["verdicts"][0]["scores"] = None
    assert view.analysis_lines(made_before, full=True)[7] == (
        "  T+30s, 4s late: no score (the models were not on when this verdict was made)")
    assert view.plain_model_lines({**made_before, "verdicts": made_before["verdicts"][:1]}) == [
        "No model guess at 30 sec (the models were not on then)"]


def test_the_plain_panel_gives_the_newest_model_guess_with_its_usual_rates_and_limits():
    a = scored_analysis()
    lines = view.analysis_lines(a)
    assert lines[1] == "LOOKS OK: no warning sign so far"  # the rules' verdict keeps its place
    assert lines[5:8] == [
        "The model's guess at 1 min (analysis, not advice; it came 31 sec late):",
        "  goes bad 50% (usually 87%) · graduates later: no guess (already graduated) · "
        "doubles before halving 2.0% (usually 2.4%)",
        "  Trained on Nov 12-16, 2026. In the backtest, no signal made money after costs.",
    ]
    assert lines[8] == f"Address: {MINT_A}" and len(lines) == 9
    first = view.plain_model_lines({**a, "verdicts": a["verdicts"][:1]})
    assert first[:2] == [
        "The model's guess at 30 sec (analysis, not advice):",
        "  goes bad 97% (usually 87%) · graduates later 0.4% (usually 1.1%) · "
        "doubles before halving 13% (usually 2.4%)",
    ]


def test_the_panel_says_when_model_scores_are_off_or_not_made_yet():
    off = analysis(models={"on": False, "why": 'lightgbm is not installed (pip install -e '
                                               '".[research]")'})  # fmt: skip
    assert view.analysis_lines(off, full=True)[-1] == (
        'model scores off: lightgbm is not installed (pip install -e ".[research]")')
    assert view.plain_model_lines(off) == [
        'No model guess: model scores are off (lightgbm is not installed (pip install -e '
        '".[research]"))']
    early = scored_analysis(verdicts=[])
    assert view.analysis_lines(early, full=True)[-1] == (
        "no model score yet (the first is at T+30s)")
    assert view.plain_model_lines(early) == [
        "No model guess yet (the first comes when the coin is 30 sec old)"]
    # An engine from before model scores sends nothing about them: nothing is added.
    assert not any("model" in ln for ln in view.analysis_lines(analysis(), full=True))
    assert not any("model" in ln for ln in view.analysis_lines(analysis()))


def test_the_status_bar_says_whether_model_scores_are_on():
    status = {"connections": [], "connected": True,
              "live": {"enabled": True, "median_wait_s": 6.0,
                       "models": {"on": True, "version": "r5"}}}  # fmt: skip
    assert " · model scores on · " in view.status_text(status, connected=True, median_s=None,
                                                        clipboard="off")  # fmt: skip
    status["live"]["models"] = {"on": False, "why": "no bundle"}
    assert " · model scores OFF · " in view.status_text(status, connected=True, median_s=None,
                                                         clipboard="off")  # fmt: skip
