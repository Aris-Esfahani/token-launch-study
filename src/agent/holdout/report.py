"""A held-out period beside the first test days: did the R5 and R6 results hold?

`judge_r5` and `judge_r6` apply the checks fixed in `config/holdout.yaml` before the
period was scored, cell by cell, and `verdict` turns them into "held", "held in direction
but not in size" or "did not hold". `write_results` writes `docs/<NAME>_RESULTS.md` from
the saved reports: the verdicts, every check, the two periods side by side, and every
number of the new period.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from agent.backtest.report import Cells, money, money_and_count, render_set
from agent.backtest.run import ALL_TIMES
from agent.backtest.run import OUT_FOLDER as BACKTEST_FOLDER
from agent.holdout.run import (
    BACKTEST_REPORT,
    BUILD_FILE,
    CHECK_FILE,
    SCORE_REPORT,
    HoldoutPlan,
    Holds,
    NotAllowed,
    get_period,
    view_dir,
)
from agent.models.report import METRIC_NAMES, MODEL_NAMES, estimate, render_report
from agent.models.train import OUT_FOLDER as MODELS_FOLDER

HELD, DIRECTION, NOT_HELD = "held", "held in direction but not in size", "did not hold"
RECALL = "recall_at_max_fine_blocked"
# The metric "beats the baselines" is judged on, and the ones "same size" compares.
HEAD_METRIC = {"filter": RECALL, "pick": "pr_auc"}
SIZE_METRICS = {
    "filter": ("pr_auc", RECALL),
    "pick": ("pr_auc", "precision_top_5_per_hour", "precision_top_20_per_hour"),
}
SHORT = {"pr_auc": "PR-AUC", RECALL: "caught at 30% blocked",
         "precision_top_5_per_hour": "top 5 per hour",
         "precision_top_20_per_hour": "top 20 per hour", "brier_score": "Brier score"}  # fmt: skip


def days_label(days: list[str]) -> str:
    """'Nov 18-19' for consecutive days of one month, else the days listed."""
    dates = [datetime.strptime(d, "%Y-%m-%d") for d in days]
    if len(dates) > 1 and len({(d.year, d.month) for d in dates}) == 1:
        return f"{dates[0]:%b} {dates[0].day}-{dates[-1].day}"
    return ", ".join(f"{d:%b} {d.day}" for d in dates)


def overlap(a: dict | None, b: dict | None) -> bool | None:
    """Do two estimates' intervals overlap? None when either is missing."""
    if not a or not b or None in (a.get("low"), a.get("high"), b.get("low"), b.get("high")):
        return None
    return a["low"] <= b["high"] and b["low"] <= a["high"]


def _not_above_zero(e: dict | None) -> bool | None:
    """False when an estimate's whole interval lies above zero; None when it is missing."""
    if not e or e.get("low") is None:
        return None
    return not e["low"] > 0


def _item(group: str, where: str, what: str, ok: bool | None, new: str, old: str = "") -> dict:
    return {"check": group, "where": where, "what": what, "ok": ok, "new": new, "old": old}


def verdict(items: list[dict], main: str) -> str:
    """`main` is the check that decides the direction of the result."""
    if any(i["ok"] is False for i in items if i["check"] == main):
        return NOT_HELD
    return DIRECTION if any(i["ok"] is False for i in items) else HELD


# --- R5 ----------------------------------------------------------------------------------


class Scores:
    def __init__(self, report: dict) -> None:
        self.stages = report["stages"]
        self.by = {(name, c["offset_s"], c["slice"], c["model"]): c
                   for name, stage in self.stages.items() for c in stage["cells"]}  # fmt: skip

    def cell(self, stage: str, offset: int, model: str, slice_name: str = "all") -> dict:
        return self.by.get((stage, offset, slice_name, model), {})

    def offsets(self, stage: str) -> list[int]:
        return sorted({c["offset_s"] for c in self.stages[stage]["cells"]})

    def slices(self, stage: str) -> list[str]:
        return list(dict.fromkeys(c["slice"] for c in self.stages[stage]["cells"]))


def mean_rates(cell: dict) -> tuple[float, float] | None:
    """(mean predicted rate, rate that happened) from a cell's calibration curve."""
    curve = cell.get("calibration_curve")
    if not curve:
        return None
    n = sum(g["n"] for g in curve)
    return (sum(g["n"] * g["predicted"] for g in curve) / n,
            sum(g["n"] * g["observed"] for g in curve) / n)  # fmt: skip


def _ratio(cell: dict) -> float | None:
    rates = mean_rates(cell)
    return rates[0] / rates[1] if rates and rates[1] else None


def judge_r5(new: dict, old: dict, holds: Holds) -> list[dict]:
    """Every R5 check of the plan, one item per cell."""
    n, o = Scores(new), Scores(old)
    items = []
    for stage in holds.stages:
        kind, title = n.stages[stage]["kind"], n.stages[stage]["title"].split(":")[0]
        head = HEAD_METRIC[kind]
        for offset in n.offsets(stage):
            where = f"{title}, T+{offset} s"
            model = n.cell(stage, offset, holds.model).get(head)
            for base in dict.fromkeys([*holds.beats, *holds.interval_above]):
                other = n.cell(stage, offset, base).get(head)
                ok, low = None, base in holds.interval_above
                if model and other and model["value"] is not None and other["value"] is not None:
                    ok = model["value"] > other["value"] if base in holds.beats else True
                    if low:
                        ok = ok and model["low"] is not None and model["low"] > other["value"]
                items.append(_item(
                    "beats", where,
                    f"{SHORT[head]}: above {MODEL_NAMES[base]}"
                    + (", low end too" if low else ""), ok,
                    f"{estimate(model)} against {estimate(other and {**other, 'low': None})}"))
            for metric in SIZE_METRICS[kind]:
                a = n.cell(stage, offset, holds.model).get(metric)
                b = o.cell(stage, offset, holds.model).get(metric)
                items.append(_item("size", where, f"{SHORT[metric]}: intervals overlap",
                                   overlap(a, b), estimate(a), estimate(b)))  # fmt: skip
            if kind != "pick":
                continue
            mine = n.cell(stage, offset, holds.model)
            a, b = mine.get("brier_score"), n.cell(stage, offset, "base_rate").get("brier_score")
            ok = (a["value"] < b["value"]
                  if a and b and None not in (a["value"], b["value"]) else None)  # fmt: skip
            items.append(_item("calibration", where, "Brier score below the base rate's", ok,
                               f"{estimate(a, 4)} against {estimate(b and {**b, 'low': None}, 4)}"))
            ratio, before = _ratio(mine), _ratio(o.cell(stage, offset, holds.model))
            low, high = holds.calibration_ratio
            items.append(_item(
                "calibration", where,
                f"mean predicted rate / rate that happened within {low:g} to {high:g}",
                None if ratio is None else low <= ratio <= high,
                "-" if ratio is None else f"{ratio:.2f}",
                "-" if before is None else f"{before:.2f}"))
    return items


# --- R6 ----------------------------------------------------------------------------------


def _generous(report: dict) -> str | None:
    head = report["variants"][0]
    return next((v["name"] for v in report["variants"]
                 if v["footprint"] != head["footprint"]), None)  # fmt: skip


def judge_r6(new: dict, old: dict) -> list[dict]:
    """Every R6 check of the plan: one item per signal and exit plan (and per delay or day
    where the check has them)."""
    n, o = Cells(new), Cells(old)
    presets = new["presets"]
    delay = presets["entry_delay_s"]
    delays = sorted({delay, *presets["entry_delay_tests_s"]})
    generous = _generous(new)
    items = []
    for signal, info in n.signals.items():
        for plan in (p["name"] for p in presets["exit_plans"]):
            where = f"{info['label']}, `{plan}`"
            key = {"when": ALL_TIMES, "delay": delay, "plan": plan}
            strict = n.get(signal, **key).get("mean_pnl_sol")
            kind = n.get(signal, **key, variant=generous).get("mean_pnl_sol")
            items.append(_item("no profit", where, "strict view: mean below zero",
                               None if not strict or strict["value"] is None
                               else strict["value"] < 0, money(strict),
                               money(o.get(signal, **key).get("mean_pnl_sol"))))  # fmt: skip
            items.append(_item(
                "no profit", where, "generous view: interval not entirely above zero",
                _not_above_zero(kind), money(kind),
                money(o.get(signal, **key, variant=generous).get("mean_pnl_sol"))))
            for name, variant in (("strict", "headline"), ("generous", generous)):
                a = n.get(signal, **key, variant=variant).get("mean_pnl_sol")
                b = o.get(signal, **key, variant=variant).get("mean_pnl_sol")
                items.append(_item("size", where, f"{name} view: intervals overlap",
                                   overlap(a, b), money(a), money(b)))  # fmt: skip
            # The other delays: the same size as on the first test days at that delay.
            for d in (d for d in delays if d != delay):
                a = n.get(signal, ALL_TIMES, d, plan).get("mean_pnl_sol")
                b = o.get(signal, ALL_TIMES, d, plan).get("mean_pnl_sol")
                items.append(_item("pattern", where, f"{d} s delay: intervals overlap",
                                   overlap(a, b), money(a), money(b)))  # fmt: skip
            # Mayhem coins: not clearly better than ordinary ones (the mayhem interval
            # entirely above the ordinary one).
            ordinary = n.get(signal, **key, slice_name="ordinary").get("mean_pnl_sol")
            mayhem = n.get(signal, **key, slice_name="mayhem").get("mean_pnl_sol")
            known = (ordinary and mayhem and mayhem.get("low") is not None
                     and ordinary.get("high") is not None)  # fmt: skip
            items.append(_item(
                "pattern", where, "mayhem coins not clearly better than ordinary ones",
                not mayhem["low"] > ordinary["high"] if known else None,
                f"mayhem {money(mayhem)}, ordinary {money(ordinary)}"))
            for day in new["dates"] if len(new["dates"]) > 1 else []:
                e = n.get(signal, **key, slice_name=day).get("mean_pnl_sol")
                items.append(_item("pattern", where, f"{day}: interval not entirely above zero",
                                   _not_above_zero(e), money(e)))  # fmt: skip
    return items


# --- the text ----------------------------------------------------------------------------

CHECK_TITLES = {
    "beats": "Beats the baselines", "size": "Same size", "calibration": "Calibration",
    "no profit": "Still no profit", "pattern": "Same pattern",
}  # fmt: skip


def _mark(ok: bool | None) -> str:
    return "not checked (no data)" if ok is None else "yes" if ok else "**NO**"


def summary_lines(items: list[dict], old_label: str) -> list[str]:
    """One line per check: how many cells passed, and the ones that did not."""
    lines = []
    for group in dict.fromkeys(i["check"] for i in items):
        rows = [i for i in items if i["check"] == group]
        judged = [i for i in rows if i["ok"] is not None]
        passed = sum(1 for i in judged if i["ok"])
        line = f"- **{CHECK_TITLES[group]}:** {passed} of {len(judged)} cells pass"
        if len(judged) < len(rows):
            line += f" ({len(rows) - len(judged)} could not be checked: no data)"
        lines.append(line + ".")
        for i in rows:
            if i["ok"] is False:
                was = f"; {old_label} was {i['old']}" if i["old"] else ""
                lines.append(f"  - Fails: {i['where']}, {i['what']}: {i['new']}{was}.")
    return lines


def checks_table(items: list[dict], old_label: str) -> list[str]:
    lines = [f"| Check | Where | What | New days | {old_label} | Passes |",
             "|---|---|---|---|---|---|"]  # fmt: skip
    for i in items:
        lines.append(f"| {CHECK_TITLES[i['check']]} | {i['where']} | {i['what']} | {i['new']} "
                     f"| {i['old'] or '-'} | {_mark(i['ok'])} |")  # fmt: skip
    return lines + [""]


def r5_tables(new: dict, old: dict, holds: Holds, labels: tuple[str, str]) -> list[str]:
    """Each stage of the plan: every model on the two periods, one row per model and
    period; then the plan's model by kind of coin and by day."""
    n, o = Scores(new), Scores(old)
    new_label, old_label = labels
    lines = []
    for stage in n.stages:
        kind = n.stages[stage]["kind"]
        offsets = n.offsets(stage)
        columns = " | ".join(f"T+{t} s" for t in offsets)
        rule = "---|" * len(offsets)
        lines += [f"### {n.stages[stage]['title']}", ""]
        if stage not in holds.stages:
            lines += ["Shown for completeness; the checks are on the three main stages.", ""]
        lines += ["Coins and the share with the outcome: "
                  + "; ".join(
                      f"T+{t} s: {old_label} {o.cell(stage, t, holds.model).get('n', 0):,} "
                      f"({o.cell(stage, t, holds.model).get('rate', 0):.1%}), {new_label} "
                      f"{n.cell(stage, t, holds.model).get('n', 0):,} "
                      f"({n.cell(stage, t, holds.model).get('rate', 0):.1%})"
                      for t in offsets) + ".", ""]  # fmt: skip
        metrics = [m for m in METRIC_NAMES if m in n.cell(stage, offsets[0], holds.model)]
        for metric in metrics:
            digits = 4 if metric == "brier_score" else 3
            lines += [f"{METRIC_NAMES[metric]}:", "", f"| Model | Days | {columns} |",
                      f"|---|---|{rule}"]  # fmt: skip
            for model, name in MODEL_NAMES.items():
                for scores, label in ((o, old_label), (n, new_label)):
                    if scores.cell(stage, offsets[0], model):
                        lines.append(f"| {name} | {label} | " + " | ".join(
                            estimate(scores.cell(stage, t, model).get(metric), digits)
                            for t in offsets) + " |")  # fmt: skip
            lines.append("")
        head = HEAD_METRIC[kind]
        lines += [f"{MODEL_NAMES[holds.model]} by kind of coin and by day "
                  f"({SHORT[head]}):", "", f"| Coins | Days | {columns} |",
                  f"|---|---|{rule}"]  # fmt: skip
        for scores, label in ((o, old_label), (n, new_label)):
            for s in scores.slices(stage):
                name = {"all": "All", "ordinary": "Ordinary", "mayhem": "Mayhem"}.get(s, s)
                lines.append(f"| {name} | {label} | " + " | ".join(
                    estimate(scores.cell(stage, t, holds.model, s).get(head))
                    for t in offsets) + " |")  # fmt: skip
        lines.append("")
        if kind == "pick":
            lines += [f"Calibration of {MODEL_NAMES[holds.model]}: mean predicted rate → "
                      "rate that happened (their ratio):", "", f"| Days | {columns} |",
                      f"|---|{rule}"]  # fmt: skip
            for scores, label in ((o, old_label), (n, new_label)):
                parts = []
                for t in offsets:
                    rates = mean_rates(scores.cell(stage, t, holds.model))
                    parts.append("-" if not rates or not rates[1] else
                                 f"{rates[0]:.4f} → {rates[1]:.4f} ({rates[0] / rates[1]:.2f})")
                lines.append(f"| {label} | " + " | ".join(parts) + " |")
            lines.append("")
    return lines


def _buys(c: dict) -> str:
    if not c.get("n"):
        return "-"
    e = c["exits"]
    won = "-" if c["won"] is None else f"{c['won']:.0%}"
    return (f"{c['bought']:.0%} bought, {won} of those in profit; take-profit "
            f"{e['take_profit']:.0%}, stop-loss {e['stop_loss']:.0%}, time "
            f"{e['time_stop']:.0%}; median {c['median_pnl_sol']:+.3f}")  # fmt: skip


def r6_tables(new: dict, old: dict, labels: tuple[str, str]) -> list[str]:
    n, o = Cells(new), Cells(old)
    new_label, old_label = labels
    presets = new["presets"]
    delay = presets["entry_delay_s"]
    delays = sorted({delay, *presets["entry_delay_tests_s"]})
    generous = _generous(new)
    lines = []
    for plan in (p["name"] for p in presets["exit_plans"]):
        key = {"when": ALL_TIMES, "delay": delay, "plan": plan}
        lines += [f"### Exit plan `{plan}`", "",
                  f"**Both views of our own buy** (all four decision times, {delay} s delay). "
                  "Mean profit or loss per signal in SOL; signals in square brackets:", "",
                  f"| Signal | Strict, {old_label} | Strict, {new_label} | Generous, {old_label} "
                  f"| Generous, {new_label} |", "|---|---|---|---|---|"]  # fmt: skip
        for signal, info in n.signals.items():
            lines.append(
                f"| {info['label']} | {money_and_count(o.get(signal, **key))} | "
                f"{money_and_count(n.get(signal, **key))} | "
                f"{money(o.get(signal, **key, variant=generous).get('mean_pnl_sol'))} | "
                f"{money(n.get(signal, **key, variant=generous).get('mean_pnl_sol'))} |")
        lines += ["", "**Delay sensitivity** (strict view, all four decision times):", "",
                  "| Signal | Days | " + " | ".join(f"{d} s" for d in delays) + " |",
                  "|---|---|" + "---|" * len(delays)]  # fmt: skip
        for signal, info in n.signals.items():
            for cells, label in ((o, old_label), (n, new_label)):
                lines.append(f"| {info['label']} | {label} | " + " | ".join(
                    money(cells.get(signal, ALL_TIMES, d, plan).get("mean_pnl_sol"))
                    for d in delays) + " |")  # fmt: skip
        days = [(d, o) for d in old["dates"]] + [(d, n) for d in new["dates"]]
        lines += ["", f"**Per day** (strict view, {delay} s delay; signals in square "
                  "brackets):", "",
                  "| Signal | " + " | ".join(d for d, _ in days) + " |",
                  "|---|" + "---|" * len(days)]  # fmt: skip
        for signal, info in n.signals.items():
            lines.append(f"| {info['label']} | " + " | ".join(
                money_and_count(cells.get(signal, **key, slice_name=d)) for d, cells in days)
                + " |")  # fmt: skip
        lines += ["", f"**Ordinary vs. mayhem coins** (strict view, {delay} s delay; signals "
                  "in square brackets):", "",
                  f"| Signal | Ordinary, {old_label} | Ordinary, {new_label} | Mayhem, "
                  f"{old_label} | Mayhem, {new_label} |", "|---|---|---|---|---|"]  # fmt: skip
        for signal, info in n.signals.items():
            lines.append(f"| {info['label']} | " + " | ".join(
                money_and_count(cells.get(signal, **key, slice_name=s))
                for s in ("ordinary", "mayhem") for cells in (o, n)) + " |")  # fmt: skip
        lines += ["", "**What happened to the buys** (strict view):", "",
                  f"| Signal | {old_label} | {new_label} |", "|---|---|---|"]  # fmt: skip
        for signal, info in n.signals.items():
            lines.append(f"| {info['label']} | {_buys(o.get(signal, **key))} | "
                         f"{_buys(n.get(signal, **key))} |")  # fmt: skip
        lines.append("")
    return lines


def _read(path: Path) -> dict | None:
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None


def write_results(root: Path, data_dir: Path, holdout: HoldoutPlan, holdout_sha: str,
                  name: str) -> tuple[Path, dict]:  # fmt: skip
    """`docs/<NAME>_RESULTS.md` from the saved reports, and the two verdicts. Refused when
    `config/holdout.yaml` is not the file the period was scored and simulated under."""
    period = get_period(holdout, name)
    view = view_dir(data_dir, name)
    scored, simulated = _read(view / SCORE_REPORT), _read(view / BACKTEST_REPORT)
    if scored is None and simulated is None:
        raise NotAllowed(f"Period {name} has no report yet: run `agent holdout score` and "
                         "`agent holdout backtest` first.")  # fmt: skip
    for report in (scored, simulated):
        if report and report["state"]["holdout_sha"] != holdout_sha:
            raise NotAllowed("config/holdout.yaml changed since the period was scored or "
                             "simulated: what counts as \"held\" is fixed beforehand.")  # fmt: skip
    first_scores = _read(data_dir / MODELS_FOLDER / period.models / "test_report.json")
    first_trades = _read(data_dir / BACKTEST_FOLDER / period.backtest / "test_report.json")
    manifest = _read(data_dir / MODELS_FOLDER / period.models / "manifest.json")
    built, checked = _read(view / BUILD_FILE), _read(view / CHECK_FILE)
    old_label, new_label = days_label(manifest["days"]["test"]), days_label(period.days)
    labels = (new_label, old_label)
    holds = holdout.holds
    verdicts, body = {}, []
    lines = [
        f"# {name.upper()} results: the frozen R5 models and R6 backtest on new days "
        "(spec 15.4)", "",
        f"Written by `agent holdout report` from the saved reports. {period.title}: "
        f"{', '.join(period.days)} (whole UTC days), beside the first test days, "
        f"{', '.join(manifest['days']['test'])}. Nothing was trained, tuned or chosen on the "
        "new days: the models, the cuts and every setting are the ones the first test days "
        "were run with, and the new days were scored once and simulated once. What counts "
        "as \"held\" was written to `config/holdout.yaml` before any new day was "
        "downloaded. Numbers in brackets are 95% intervals from resampling whole launch "
        "hours.", "",
        "## Short answer", "",
    ]  # fmt: skip
    if scored:
        items = judge_r5(scored, first_scores, holds)
        verdicts["r5"] = verdict(items, "beats")
        lines += [f"**R5, the models: {verdicts['r5']}.** {MODEL_NAMES[holds.model]}, all "
                  f"coins, each decision time, {new_label} against {old_label}:", "",
                  *summary_lines(items, old_label), ""]  # fmt: skip
        body += [f"## R5: {new_label} beside {old_label}", "",
                 f"{scored['coins']:,} coins and {scored['rows']:,} coin and decision-time "
                 f"rows on the new days ({first_scores['coins']:,} and "
                 f"{first_scores['rows']:,} on {old_label}).", "",
                 *r5_tables(scored, first_scores, holds, labels),
                 "### Every R5 check", "", *checks_table(items, old_label)]  # fmt: skip
    else:
        lines += ["**R5, the models:** not scored yet.", ""]
    if simulated:
        items = judge_r6(simulated, first_trades)
        verdicts["r6"] = verdict(items, "no profit")
        presets = simulated["presets"]
        lines += [f"**R6, the backtest: {verdicts['r6']}.** Every signal and exit plan, "
                  f"all four decision times together, {presets['entry_delay_s']} s delay, "
                  f"{new_label} against {old_label}:", "",
                  *summary_lines(items, old_label), ""]  # fmt: skip
        groups = simulated["coins_by_group"]
        body += [f"## R6: {new_label} beside {old_label}", "",
                 f"{simulated['coins']:,} coins ({groups.get('ordinary', 0):,} ordinary, "
                 f"{groups.get('mayhem', 0):,} mayhem) and {simulated['ledger']['rows']:,} "
                 f"simulated trades on the new days ({first_trades['coins']:,} coins and "
                 f"{first_trades['ledger']['rows']:,} trades on {old_label}). Profit or loss "
                 f"in SOL per signal on a {presets['buy_sol']:g} SOL buy, after all costs. "
                 "Strict: our buy leaves no trace in the market (the headline). Generous: "
                 "our SOL stays in the reserves. The truth is between the two.", "",
                 *r6_tables(simulated, first_trades, labels),
                 "### Every R6 check", "", *checks_table(items, old_label)]  # fmt: skip
    else:
        lines += ["**R6, the backtest:** not simulated yet.", ""]
    lines += ["A check that fails is listed above with its numbers; the tables below show "
              "every cell. One more period of a few days is still one market: \"held\" says "
              f"the results were not an accident of {old_label}, not that they hold for "
              "good.",
              "", "## What was run", ""]  # fmt: skip
    if built:
        same = built["same_as_first"]
        lines.append(
            f"- **Data:** epochs {built['epochs'][0]} to {built['epochs'][-1]}; the period's "
            f"dataset has {built['coins']:,} coins and {built['rows']:,} rows. The "
            f"{same['rows']:,} rows of coins the first dataset knew completely are "
            + ("the same in both, value for value." if built["same"] else
               f"**not** all the same ({same['only_in_first']} and {same['only_in_new']} "
               "rows differ)."))  # fmt: skip
    if checked:
        lines.append(
            f"- **Check before the new days were touched:** the validation day "
            f"({', '.join(checked['validation_days'])}) scored and simulated again. Largest "
            f"difference from the saved R5 validation report: "
            f"{checked['r5_validation_scores_largest_difference']:.1e}; the cuts worked out "
            f"again differ from the frozen ones by "
            f"{checked['r6_cuts_largest_difference']:.1e}; "
            f"{checked['r6_validation_ledger_files']} ledger files "
            + ("are byte for byte the saved ones" if checked["r6_validation_ledger_same_files"]
               else "are **not** the saved ones")
            + "; largest difference from the saved R6 validation report: "
            f"{checked['r6_validation_cells_largest_difference']:.1e}.")  # fmt: skip
        state = checked["state"]
        lines.append(
            f"- **Frozen (short hashes):** plan {state['plan_sha']}, models "
            f"{state['models_sha']}, cuts {state['cuts_sha']}, backtest settings "
            f"{state['settings_sha']}, trade settings {state['presets_sha']}, code "
            f"{state['source_sha']}, this period's settings {state['holdout_sha']}.")
    lines += ["", *body]
    if scored:
        lines += render_report(scored, f"Appendix: every R5 number of {new_label}")
    if simulated:
        lines += render_set(simulated, f"Appendix: every R6 number of {new_label}")
    path = root / "docs" / f"{name.upper()}_RESULTS.md"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path, verdicts
