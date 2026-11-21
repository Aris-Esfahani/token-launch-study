"""The R6 results as text: tables of after-cost profit and loss per signal from the saved
backtest reports, with their intervals. `agent backtest` writes `docs/R6_RESULTS.md` with
it."""

from __future__ import annotations

import json
from pathlib import Path

from agent.backtest.run import ALL_TIMES, FIRST_TIME, out_dir

SLICE_NAMES = {"all": "All coins", "ordinary": "Ordinary", "mayhem": "Mayhem"}
VARIANT_NAMES = {"headline": "Headline", "footprint_stays": "Our SOL stays in the reserves",
                 "footprint_none": "Our buy leaves no trace"}  # fmt: skip


def money(e: dict | None) -> str:
    """A mean in SOL with its interval, e.g. '-0.086 (-0.090 to -0.082)'."""
    if not e or e["value"] is None:
        return "-"
    if e["low"] is None:
        return f"{e['value']:+.3f}"
    return f"{e['value']:+.3f} ({e['low']:+.3f} to {e['high']:+.3f})"


def money_and_count(c: dict) -> str:
    """The mean with its interval and, in square brackets, how many signals it is over."""
    return f"{money(c.get('mean_pnl_sol'))} [{c.get('n', 0):,}]"


def time_name(when) -> str:
    return {ALL_TIMES: "All four times", FIRST_TIME: "First signal per coin"}.get(
        when, f"T+{when} s")  # fmt: skip


def variant_name(v: dict, head: dict) -> str:
    if v["name"] == "headline":
        return "Headline"
    if v["failed_rate"] != head["failed_rate"]:
        return f"{v['failed_rate']:.0%} of orders fail"
    if v["footprint"] != head["footprint"]:
        return VARIANT_NAMES[f"footprint_{v['footprint']}"]
    return f"Padre fee {v['platform_fee_pct']:g}%"


class Cells:
    def __init__(self, report: dict) -> None:
        self.report = report
        self.by_key = {(c["signal"], c["time"], c["delay_s"], c["plan"], c["slice"],
                        c["variant"]): c for c in report["cells"]}  # fmt: skip
        self.signals = report["signals"]
        self.times = list(dict.fromkeys(c["time"] for c in report["cells"]))
        self.head = report["variants"][0]

    def get(self, signal, when, delay, plan, slice_name="all", variant="headline") -> dict:
        return self.by_key.get((signal, when, delay, plan, slice_name, variant), {})

    def table(self, columns: list[tuple[str, dict]], *, value=None) -> list[str]:
        """Rows = signals; each column is (title, the cell key parts that differ)."""
        value = value or (lambda c: money(c.get("mean_pnl_sol")))
        lines = ["| Signal | " + " | ".join(title for title, _ in columns) + " |",
                 "|---|" + "---|" * len(columns)]  # fmt: skip
        for signal, info in self.signals.items():
            cells = [self.get(signal, **key) for _, key in columns]
            lines.append(f"| {info['label']} | " + " | ".join(value(c) for c in cells) + " |")
        return lines


def _pct(x: float | None) -> str:
    return "-" if x is None else f"{x:.0%}" if x >= 0.095 or x == 0 else f"{x:.1%}"


def what_happened(c: dict) -> str:
    if not c.get("n"):
        return "0 signals"
    e = c["exits"]
    return (f"{c['n']:,} signals; bought {_pct(c['bought'])}; of those in profit "
            f"{_pct(c['won'])}; exits: take-profit {_pct(e['take_profit'])}, stop-loss "
            f"{_pct(e['stop_loss'])}, time {_pct(e['time_stop'])}")  # fmt: skip


def plan_text(plan: dict) -> str:
    parts = []
    if plan.get("take_profit_pct") is not None:
        parts.append(f"take-profit +{plan['take_profit_pct']:g}%")
    if plan.get("stop_loss_pct") is not None:
        parts.append(f"stop-loss -{plan['stop_loss_pct']:g}%")
    parts.append(f"time stop {plan['time_stop_min']:g} min")
    return ", ".join(parts)


def render_set(report: dict, heading: str, *, note: str = "") -> list[str]:
    cells = Cells(report)
    presets = report["presets"]
    delay = presets["entry_delay_s"]
    delays = sorted({presets["entry_delay_s"], *presets["entry_delay_tests_s"]})
    dates = report["dates"]
    groups = report["coins_by_group"]
    lines = [f"## {heading}", ""]
    if note:
        lines += [note, ""]
    lines += [f"{report['coins']:,} coins ({groups.get('ordinary', 0):,} ordinary, "
              f"{groups.get('mayhem', 0):,} mayhem), {report['rows']:,} coin and decision-time "
              f"rows, {report['ledger']['rows']:,} simulated trades. Signals (rows bought): "
              + "; ".join(f"{s['label']} {s['rows']:,}" for s in report["signals"].values())
              + ".", ""]  # fmt: skip
    for plan in presets["exit_plans"]:
        name = plan["name"]
        lines += [f"### Exit plan `{name}` ({plan_text(plan)})", "",
                  f"**By decision time** ({delay} s delay, headline setting). Mean profit or "
                  "loss per signal in SOL:", ""]  # fmt: skip
        lines += cells.table([(time_name(t), {"when": t, "delay": delay, "plan": name})
                              for t in cells.times]) + [""]  # fmt: skip
        lines += ["What happened to those buys (all four decision times):", ""]
        for signal, info in cells.signals.items():
            lines.append(f"- {info['label']}: "
                         f"{what_happened(cells.get(signal, ALL_TIMES, delay, name))}")  # fmt: skip
        lines += ["", "**Delay sensitivity** (all four decision times):", ""]
        lines += cells.table([(f"{d} s", {"when": ALL_TIMES, "delay": d, "plan": name})
                              for d in delays]) + [""]  # fmt: skip
        if len(dates) > 1:
            lines += [f"**Per day** (all four decision times, {delay} s delay; signals in "
                      "square brackets):", ""]  # fmt: skip
            lines += cells.table([(d, {"when": ALL_TIMES, "delay": delay, "plan": name,
                                       "slice_name": d}) for d in dates],
                                 value=money_and_count) + [""]  # fmt: skip
        lines += [f"**Ordinary vs. mayhem coins** (all four decision times, {delay} s delay; "
                  "signals in square brackets), under the headline setting and with our SOL "
                  "staying in the reserves:", ""]  # fmt: skip
        key = {"when": ALL_TIMES, "delay": delay, "plan": name}
        stays = next((v["name"] for v in report["variants"]
                      if v["footprint"] != cells.head["footprint"]), None)  # fmt: skip
        columns = [(SLICE_NAMES[s], key | {"slice_name": s}) for s in ("ordinary", "mayhem")]
        if stays:
            columns += [(f"{SLICE_NAMES[s]}, our SOL stays", key | {"slice_name": s,
                                                                    "variant": stays})
                        for s in ("ordinary", "mayhem")]  # fmt: skip
        lines += cells.table(columns, value=money_and_count) + [""]
        capped = {info["label"]: cells.get(s, ALL_TIMES, delay, name, "mayhem").get("capped")
                  for s, info in cells.signals.items()}  # fmt: skip
        lines += ["Mayhem buys that would have taken every token left on the curve (share of "
                  "signals): " + "; ".join(f"{k} {_pct(v)}" for k, v in capped.items()) + ".",
                  ""]  # fmt: skip
        lines += [f"**Other assumptions** (all four decision times, {delay} s delay, all coins; "
                  "one change at a time):", ""]  # fmt: skip
        lines += cells.table([(variant_name(v, cells.head),
                               {"when": ALL_TIMES, "delay": delay, "plan": name,
                                "variant": v["name"]})
                              for v in report["variants"]]) + [""]  # fmt: skip
    return lines


def render_cuts(cuts: dict, frozen: dict) -> list[str]:
    lines = ["## The cuts, chosen on the validation day", "",
             f"The validation day has {frozen['validation_hours']:.1f} hours of launches "
             "(coins launched in its last 65 minutes are left out, as in R5). A pick signal "
             "buys at or above its cut (a raw model score); the stage 1 filter buys at or "
             "below its cut. In brackets: rows bought on the validation day.", "",
             "| Signal | T+30 s | T+60 s | T+120 s | T+300 s |",
             "|---|---|---|---|---|"]  # fmt: skip
    for name, by_offset in cuts.items():
        lines.append(f"| `{name}` | " + " | ".join(
            f"{v['cut']:.4f} ({v.get('picked', v.get('bought')):,})"
            for _, v in sorted(by_offset.items(), key=lambda kv: int(kv[0]))) + " |")  # fmt: skip
    return lines + [""]


def render_pool_check(report: dict) -> str:
    parts = []
    for kind, c in report["pool_check"].items():
        linked = c["continues"] / c["with_previous"] if c["with_previous"] else 1.0
        parts.append(f"{kind}: {c['trades']:,} trades, amounts exact "
                     f"{c['amounts_exact'] / c['trades']:.4%}, fees exact "
                     f"{c['fees_exact'] / c['trades']:.4%}, starts where the previous trade "
                     f"ended {linked:.4%}")  # fmt: skip
    return "; ".join(parts)


def write_results(root: Path, data_dir: Path) -> Path:
    """`docs/R6_RESULTS.md` from the reports saved in `<data>/backtest/r6/`."""
    folder = out_dir(data_dir)

    def read(name: str) -> dict | None:
        path = folder / name
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None

    validation, test, train = (read(f"{s}_report.json") for s in ("validation", "test", "train"))
    cuts, frozen = read("cuts.json"), read("frozen.json")
    any_report = test or validation or train
    presets, head = any_report["presets"], any_report["variants"][0]
    per_tx = presets["priority_fee_sol"] + any_report["base_fee_lamports"] / 1e9
    lines = [
        "# R6 results: backtest as analysis (spec 10, 15.3)", "",
        "Written by `agent backtest` from the saved reports. Analysis only: nothing here was "
        "traded. Every number is profit or loss in SOL per signal on a "
        f"{presets['buy_sol']:g} SOL buy (so -0.050 is a loss of 5% of the stake), after all "
        "costs; in brackets, 95% intervals from resampling whole launch hours.", "",
        "**How a trade is simulated** (`config/padre_presets.yaml`, `src/agent/sim/backtest.py`):",
        f"- Buy {presets['buy_sol']:g} SOL (pump.fun's fees included) {presets['entry_delay_s']} s "
        f"after the decision time (also {', '.join(str(d) for d in presets['entry_delay_tests_s'])}"
        " s), on the reserves after the last trade at or before that second: exact bonding-curve "
        "math, or PumpSwap pool math for a coin that already graduated.",
        "- Fees are read from the chain data, per trade: the rates the coin's last trade by a "
        "person paid (in a pool, the most common rates of its last trades).",
        f"- Each transaction that reaches the chain also pays {per_tx:.6f} SOL (priority fee "
        f"{presets['priority_fee_sol']:g} + Solana's base fee of "
        f"{any_report['base_fee_lamports']:,} lamports, read from the data), failed or not.",
        f"- An order fails if the price moved more than {presets['slippage_pct']:g}% against it "
        f"since the quote {presets['quote_age_s']} s earlier, and {head['failed_rate']:.0%} of "
        "orders fail by chance (also "
        + ", ".join(f"{r:.0%}" for r in presets["failed_tx_rate_tests"]
                    if r != head["failed_rate"])
        + "). A failed buy is not retried; a failed sell is retried one delay later.",
        "- The exit is triggered by the price after each later trade against the price paid; "
        "the sell lands one delay after the trigger (a time stop at its planned time).",
        "- **Headline setting: our own buy leaves no trace in the market** (strict: the sale "
        "is priced on reserves that never held our SOL, which on a new curve understates it "
        "by about 6% of the stake). \"Our SOL stays in the reserves\" is the generous view: "
        "exact when nobody else trades, too kind when others sell after us. The truth is "
        "between the two.",
        f"- Padre's own fee is counted as {head['platform_fee_pct']:g}% of the SOL bought and "
        "of the SOL sold. Padre's real fee has not been checked: this is a test setting the "
        "user chose, not a measured cost, and "
        + (", ".join(f"{p:g}%" for p in presets.get("platform_fee_tests_pct", [])) or "nothing")
        + " is shown as a what-if.",
        "- Not counted: the rent of a token account. A buy that would take every token left "
        "on the curve stops there, and what follows is priced on the market as recorded.", "",
    ]  # fmt: skip
    if cuts and frozen:
        lines += render_cuts(cuts, frozen)
    again = (test or {}).get("summarized_again")
    if again:
        was = again["was"]
        lines += [
            "**The test days were simulated once.** This report was built again from the same "
            f"saved trades (no trade was simulated again) after Padre's fee in the headline "
            f"was changed from {was['platform_fee_pct']:g}% to {head['platform_fee_pct']:g}%. "
            "The fee changes no simulated trade, only what is taken off each result; the "
            "earlier headline is the what-if column, and the earlier report is kept beside "
            f"this one (`{again['kept']}`).", ""]  # fmt: skip
    if test:
        lines += ["PumpSwap pool math against the recorded pool trades of the test days: "
                  + render_pool_check(test) + ".", "",
                  "(For a buy with a budget, \"amounts exact\" also asks that the buyer's whole "
                  "budget equals what the pool priced plus its three fees. Where it does not, "
                  "the budget was 0.5 to 1.6% larger: an extra charge outside the pool's fees, "
                  "most likely a trading app's own fee. The simulated buys pay no such charge "
                  "unless the Padre what-if is on. Trades routed through several pools are "
                  "charged once for the whole route and are not used for fees.)", ""]  # fmt: skip
        lines += render_set(test, f"Test days {', '.join(test['dates'])}, simulated once")
    else:
        lines += ["## Test days", "", "Not simulated yet.", ""]
    if validation:
        lines += render_set(
            validation, f"Validation day {', '.join(validation['dates'])}",
            note="The cuts were chosen on this day and the models were tuned and calibrated on "
                 "it, so the model signals are flattered here. The unfitted signals (every "
                 "coin, the rules) are not.")  # fmt: skip
    if train:
        lines += render_set(
            train, f"Training days {train['dates'][0]} to {train['dates'][-1]}",
            note="Only the signals that were never fitted: the models learned from these days. "
                 "For stability across days.")  # fmt: skip
    path = root / "docs" / "R6_RESULTS.md"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path
