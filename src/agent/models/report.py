"""The R5 results as text (spec 15.4): tables of every stage, model, decision time and
slice from the saved reports, with their intervals. `agent train` and `agent score-test`
write `docs/R5_RESULTS.md` with it."""

from __future__ import annotations

import json
from pathlib import Path

MODEL_NAMES = {
    "base_rate": "Base rate", "rules": "Rules", "logistic": "Logistic regression",
    "lightgbm": "LightGBM", "logistic_bf": "Logistic + backfill-only",
    "lightgbm_bf": "LightGBM + backfill-only",
}  # fmt: skip
METRIC_NAMES = {
    "pr_auc": "PR-AUC",
    "recall_at_max_fine_blocked": "Bad coins caught when blocking at most 30% of fine ones",
    "precision_top_5_per_hour": "Precision of the top 5 per hour",
    "precision_top_20_per_hour": "Precision of the top 20 per hour",
    "brier_score": "Brier score (lower is better)",
}
SLICE_NAMES = {"all": "all SOL-paired coins", "ordinary": "ordinary coins",
               "mayhem": "mayhem coins"}  # fmt: skip


def estimate(e: dict | None, digits: int = 3) -> str:
    if not e or e["value"] is None:
        return "-"
    if e["low"] is None:
        return f"{e['value']:.{digits}f}"
    return f"{e['value']:.{digits}f} ({e['low']:.{digits}f} to {e['high']:.{digits}f})"


def metric_table(cells: list[dict], metric: str, slice_name: str) -> list[str]:
    """Rows = models, columns = decision times, for one metric and slice."""
    rows = [c for c in cells if c["slice"] == slice_name and metric in c]
    offsets = sorted({c["offset_s"] for c in rows})
    if not rows:
        return []
    digits = 4 if metric == "brier_score" else 3
    lines = ["| Model | " + " | ".join(f"T+{o} s" for o in offsets) + " |",
             "|---|" + "---|" * len(offsets)]  # fmt: skip
    for model, label in MODEL_NAMES.items():
        by_offset = {c["offset_s"]: c for c in rows if c["model"] == model}
        if by_offset:
            lines.append(f"| {label} | " + " | ".join(
                estimate(by_offset[o][metric], digits) if o in by_offset else "-"
                for o in offsets) + " |")  # fmt: skip
    return lines


def counts_line(cells: list[dict], slice_name: str) -> str:
    seen = {}
    for c in cells:
        if c["slice"] == slice_name:
            seen[c["offset_s"]] = (c["n"], c["rate"])
    return "; ".join(f"T+{o} s: {n:,} coins, {rate:.1%} positive"
                     for o, (n, rate) in sorted(seen.items()))  # fmt: skip


def calibration_table(cells: list[dict], model: str) -> list[str]:
    lines = []
    for c in cells:
        if c["model"] == model and c["slice"] == "all" and c.get("calibration_curve"):
            pairs = ", ".join(f"{b['predicted']:.3f} → {b['observed']:.3f} ({b['n']:,})"
                              for b in c["calibration_curve"])  # fmt: skip
            lines.append(f"- T+{c['offset_s']} s: {pairs}")
    return lines


def render_report(report: dict, heading: str) -> list[str]:
    lines = [f"## {heading}", ""]
    for stage in report["stages"].values():
        cells = stage["cells"]
        lines += [f"### {stage['title']}", ""]
        slices = list(dict.fromkeys(c["slice"] for c in cells))
        for s in slices:
            lines += [f"**{SLICE_NAMES.get(s, s)}** ({counts_line(cells, s)})", ""]
            for metric, name in METRIC_NAMES.items():
                table = metric_table(cells, metric, s)
                if table:
                    lines += [f"{name}:", "", *table, ""]
        if stage["kind"] == "pick":
            lines += ["Calibration (predicted → observed, coins per group), all coins:", ""]
            for model in ("lightgbm", "logistic"):
                table = calibration_table(cells, model)
                if table:
                    lines += [f"{MODEL_NAMES[model]}:", *table, ""]
    return lines


def render_checks(checks: dict, manifest: dict) -> list[str]:
    lines = ["## Settings chosen on the validation day, and checks", ""]
    lines += ["| Stage | Logistic C | LightGBM leaves | Min rows per leaf | Class weight "
              "| Rounds |", "|---|---|---|---|---|---|"]  # fmt: skip
    for name, c in manifest["chosen"].items():
        g = c["lightgbm"]
        lines.append(f"| {name} | {c['logistic_C']} | {g['num_leaves']} | "
                     f"{g['min_data_in_leaf']} | {g['class_weight']} | {c['lightgbm_rounds']} |")
    lines += ["", "**SHAP: the features LightGBM leans on** (share of the mean absolute SHAP "
              "value on validation rows; top 8):", ""]  # fmt: skip
    for name, c in checks.items():
        top = ", ".join(f"{f['feature']} {f['share']:.0%}" for f in c["shap"][:8])
        lines.append(f"- {name}: {top}")
    lines += ["", "**With the backfill-only signals** (top 8):", ""]
    for name, c in checks.items():
        top = ", ".join(f"{f['feature']} {f['share']:.0%}" for f in c["shap_bf"][:8])
        lines.append(f"- {name}: {top}")
    lines += ["", "**Shuffled labels** (LightGBM trained on training labels in a random "
              "order, five times; mean and lowest to highest. A model that knows nothing "
              "has a ROC-AUC of 0.5 and a PR-AUC near the base rate):", "",
              "| Stage | Base rate | PR-AUC shuffled | PR-AUC real | ROC-AUC shuffled "
              "| ROC-AUC real |", "|---|---|---|---|---|---|"]  # fmt: skip
    for name, c in checks.items():
        s = c["shuffled_labels"]
        lines.append(
            f"| {name} | {s['base_rate']:.4f} | {s['shuffled_validation_pr_auc']:.4f} "
            f"({s['shuffled_lowest']:.4f} to {s['shuffled_highest']:.4f}) | "
            f"{s['real_pr_auc']:.4f} | {s['shuffled_roc_auc']:.3f} "
            f"({s['shuffled_roc_lowest']:.3f} to {s['shuffled_roc_highest']:.3f}) | "
            f"{s['real_roc_auc']:.3f} |")
    lines += ["", "**Walk-forward inside the training days** (chosen settings refitted on "
              "the days before, scored on the day; PR-AUC averaged over the four decision "
              "times):", "",
              "| Stage | Day scored | Days trained on | Base rate | Rules | Logistic | LightGBM |",
              "|---|---|---|---|---|---|---|"]  # fmt: skip
    for name, c in checks.items():
        for f in c["walk_forward"]:
            lines.append(f"| {name} | {f['scored_day']} | {f['trained_on_days']} | "
                         f"{f['base_rate']:.3f} | {f['rules']:.3f} | {f['logistic']:.3f} | "
                         f"{f['lightgbm']:.3f} |")  # fmt: skip
    return lines + [""]


def write_results(root: Path, folder: Path) -> Path:
    """`docs/R5_RESULTS.md` from the saved manifest, checks and reports in `folder`."""
    manifest = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
    days = manifest["days"]
    lines = [
        "# R5 results: baselines and models (spec 15.3, 15.4)", "",
        "Written by `agent train` and `agent score-test` from the saved reports. Numbers in "
        "brackets are 95% intervals from resampling whole launch hours.", "",
        f"- Training days (UTC): {days['train'][0]} to {days['train'][-1]}; validation: "
        f"{', '.join(days['validation'])}; test: {', '.join(days['test'])}.",
        f"- Rows: training {manifest['rows']['train']:,} ({manifest['coins']['train']:,} "
        f"coins), validation {manifest['rows']['validation']:,} "
        f"({manifest['coins']['validation']:,} coins). SOL-paired coins, one row per coin "
        "and decision time.",
        f"- Coins launched in the last {manifest['drop_label_overlap_s']} s before the "
        "validation or the test days are left out (their labels would read into them).",
        f"- {len(manifest['features'])} features; {len(manifest['features_backfill_only'])} "
        "more that only the backfill has (`+ backfill-only` rows).",
        "- Ranking metrics use each model's raw score; the Brier score and calibration use "
        "probabilities calibrated on the validation day.", "",
    ]  # fmt: skip
    test = folder / "test_report.json"
    if test.exists():
        report = json.loads(test.read_text(encoding="utf-8"))
        lines += render_report(report, f"Test days, scored once ({report['coins']:,} coins)")
    else:
        lines += ["## Test days", "", "Not scored yet.", ""]
    checks = json.loads((folder / "checks.json").read_text(encoding="utf-8"))
    lines += render_checks(checks, manifest)
    validation = json.loads((folder / "validation_report.json").read_text(encoding="utf-8"))
    lines += render_report(validation, "Validation day (used for tuning and calibration, "
                           "so these flatter the models)")  # fmt: skip
    path = root / "docs" / "R5_RESULTS.md"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path
