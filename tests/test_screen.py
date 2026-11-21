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

    def copy(self, text: str | None) -> None:
        self.number += 1
        self.value = text

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


def test_the_analysis_panel_starts_with_the_mint_then_the_verdict():
    lines = view.analysis_lines(analysis())
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
    ))  # fmt: skip
    assert flagged[1] == "AVOID: dev sold 80% of their buy"
    assert "sold 80% of their buy" in flagged[4]
    assert "no verdict yet" in flagged[5]
    assert [ln for ln in flagged if ln.startswith("!")] == [
        "! data incomplete: a recording gap touched this coin",
        "! mayhem coin: pump.fun's own bot trades it",
    ]


def test_a_copied_bonding_curve_address_is_pointed_out():
    lines = view.analysis_lines(analysis(copied=MINT_B))  # MINT_B stands in for the curve
    assert lines[0].startswith(MINT_A)  # the real mint, ready for Padre's search
    assert "! you copied the coin's bonding-curve address; its mint is the first line" in lines
    assert not any("bonding-curve" in ln for ln in view.analysis_lines(analysis(copied=MINT_A)))


def test_unknown_mints_and_errors_say_why():
    unknown = {"op": "analysis", "mint": MINT_A, "known": False, "why": "not in the live data"}
    assert view.analysis_lines(unknown) == [MINT_A, "not tracked: not in the live data"]
    error = {"op": "error", "error": "not a Solana address"}
    assert view.analysis_lines(error)[1] == "could not analyze: not a Solana address"


def test_alert_lines_start_with_the_time_and_mint():
    alert = {
        "mint": MINT_A, "symbol": "STM", "offset_s": 60, "level": "pass", "has_gap": False,
        "decided_at": time.time(), "wait_s": 3.0,
        "signals": {"market_cap_sol": 32.4, "curve_progress_pct": 12.0, "unique_buyers": 25,
                    "top10_hold_pct": 22.0, "dev_hold_pct": 3.0},
    }  # fmt: skip
    at, rest = view.alert_text(alert).split("  ", 1)
    assert at.strip().endswith(("AM", "PM"))
    assert rest == (
        f"{MINT_A}  $STM  T+60s  PASS  mc 32 SOL · curve 12% · 25 buyers · top10 22% · dev 3%"
    )
    late = view.alert_text({**alert, "wait_s": 17.0, "has_gap": True, "signals": {}})
    assert "PASS, 17s late  mc - · curve -" in late and late.endswith("recording gap)")


def test_launcher_chosen_names_are_cleaned():
    assert view.clean("Stonk\x1b[31mRed\x07") == "Stonk[31mRed"  # no escape or bell
    assert view.clean("x" * 40) == "x" * 19 + "…"
    assert view.clean(None) == ""
    assert view.alert_text({
        "mint": MINT_A, "symbol": "\x1b]8;;evil\x07", "offset_s": 30, "level": "pass",
        "decided_at": time.time(), "signals": {},
    }).count("\x1b") == 0  # fmt: skip


def test_the_status_bar():
    offline = view.status_text(None, connected=False, median_s=None, clipboard="on (F2)")
    assert offline.startswith("engine offline")
    status = {
        "connections": [{"connected": True}, {"connected": False}], "covered_lag_s": 4.2,
        "creates_per_hr": 1820, "live": {"enabled": True, "median_wait_s": 6.0},
        "recording": True, "last_gap": {"at": time.time(), "seconds": 63},
    }  # fmt: skip
    text = view.status_text(status, connected=True, median_s=0.123, clipboard="on (F2)")
    assert text.startswith("feeds 1/2 up · data 4s behind · 1,820 tokens/hr · verdicts 6s late")
    assert "(63s)" in text and "copy-to-verdict 0.12s" in text and text.endswith("on (F2)")


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
        api.publish({"mint": MINT_B, "symbol": "OLD", "offset_s": 30, "level": "pass",
                     "decided_at": time.time(), "signals": {}})  # fmt: skip
        clip = FakeClipboard()
        app = AgentScreen(port, tmp_path, clipboard=clip)
        try:
            async with app.run_test(size=(140, 40)) as pilot:
                await until(lambda: app.client.connected)
                alerts = app.query_one("#alerts")
                await until(lambda: alerts.option_count == 1)
                clip.copy(MINT_A)  # copied in Padre
                panel = app.query_one("#analysis")
                await until(lambda: "PASS" in str(panel.render()))
                copied = str(panel.render())
                await pilot.click("#alerts", offset=(4, 1))  # the alert line (MINT_B)
                await until(lambda: "AVOID" in str(panel.render()))
                clicked = str(panel.render())
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
        return copied, clicked, typed, refused, status, list(app.latencies)

    copied, clicked, typed, refused, status, latencies = asyncio.run(scenario())
    assert copied.startswith(MINT_A) and "PASS: no rule fired" in copied
    assert clicked.startswith(MINT_B) and "AVOID: top 10 wallets hold 40%" in clicked
    assert typed.startswith(MINT_B) and "AVOID" in typed
    assert "not a Solana address" in refused and "not a mint" not in refused
    assert "feeds 1/1 up" in status and "copy-to-verdict" in status
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
    lines = view.analysis_lines(scored_analysis())
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
    lines = view.analysis_lines(a)
    assert lines[8] == f"  T+60s, 31s late: no score: {why}"
    assert not any("creator history" in ln for ln in lines)  # nothing scored last: no note

    partial = scored_analysis()
    partial["verdicts"][1]["scores"]["context"] = {
        **partial["verdicts"][1]["scores"]["context"], "day_covered": 0.31}
    assert view.analysis_lines(partial)[-1] == (
        "! creator history: 31% of the 24 h before launch recorded, so the creator's launch "
        "count (3) is a lower bound"
    )
    made_before = scored_analysis()
    made_before["verdicts"][0]["scores"] = None
    assert view.analysis_lines(made_before)[7] == (
        "  T+30s, 4s late: no score (the models were not on when this verdict was made)")


def test_the_panel_says_when_model_scores_are_off_or_not_made_yet():
    off = analysis(models={"on": False, "why": 'lightgbm is not installed (pip install -e '
                                               '".[research]")'})  # fmt: skip
    assert view.analysis_lines(off)[-1] == (
        'model scores off: lightgbm is not installed (pip install -e ".[research]")')
    early = scored_analysis(verdicts=[])
    assert view.analysis_lines(early)[-1] == "no model score yet (the first is at T+30s)"
    # An engine from before model scores sends nothing about them: nothing is added.
    assert not any("model" in ln for ln in view.analysis_lines(analysis()))


def test_the_status_bar_says_whether_model_scores_are_on():
    status = {"connections": [], "connected": True,
              "live": {"enabled": True, "median_wait_s": 6.0,
                       "models": {"on": True, "version": "r5"}}}  # fmt: skip
    assert " · models r5 · " in view.status_text(status, connected=True, median_s=None,
                                                  clipboard="off")  # fmt: skip
    status["live"]["models"] = {"on": False, "why": "no bundle"}
    assert " · model scores OFF · " in view.status_text(status, connected=True, median_s=None,
                                                         clipboard="off")  # fmt: skip
