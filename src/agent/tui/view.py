"""What the screen shows, as plain text lines (the app adds colours). Kept apart from the
app so it can be tested without a terminal.

Every line about a coin starts with its mint, so it can be pasted into Padre's search
(spec 1). Names and symbols come from whoever launched the coin, so they are cleaned:
control characters are dropped and the length is capped.

Model scores (spec 15.3, step L1) are analysis, not advice: every probability is shown
beside the outcome's usual rate, with how late it came, and the panel always says when
the models were trained and that the backtest found no signal that makes money.
"""

from __future__ import annotations

from agent import clock

LEVEL_STYLE = {"pass": "bold green", "caution": "yellow", "avoid": "red", "error": "magenta"}
MAX_LABEL = 20
# The models' stages in plain words (a stage not named here is shown by its name).
STAGE_WORDS = {"bad": "bad", "grad_later": "graduates later", "hit_2x": "2x before half"}


def clean(text: object, limit: int = MAX_LABEL) -> str:
    """A launcher-chosen label made safe to print: printable characters only, capped."""
    if not isinstance(text, str):
        return ""
    printable = "".join(ch for ch in text if ch.isprintable()).strip()
    return printable if len(printable) <= limit else printable[: limit - 1] + "…"


def age(seconds: float | None) -> str:
    if seconds is None:
        return "?"
    seconds = max(int(seconds), 0)
    if seconds < 120:
        return f"{seconds}s"
    minutes, secs = divmod(seconds, 60)
    return f"{minutes}m {secs:02d}s"


def _num(value: float | None, fmt: str, unit: str = "") -> str:
    return "-" if value is None else f"{value:{fmt}}{unit}"


def _symbol(item: dict) -> str:
    symbol = clean(item.get("symbol"), 12)
    return f"${symbol}" if symbol else "$?"


def alert_text(alert: dict) -> str:
    """One line of the alert feed: time, mint, symbol, when, verdict, key signals."""
    s = alert.get("signals") or {}
    parts = [
        f"mc {_num(s.get('market_cap_sol'), '.0f', ' SOL')}",
        f"curve {_num(s.get('curve_progress_pct'), '.0f', '%')}",
        f"{_num(s.get('unique_buyers'), '.0f')} buyers",
        f"top10 {_num(s.get('top10_hold_pct'), '.0f', '%')}",
        f"dev {_num(s.get('dev_hold_pct'), '.0f', '%')}",
    ]
    late = f", {alert['wait_s']:.0f}s late" if alert.get("wait_s", 0) >= 10 else ""
    gap = "  (data incomplete: recording gap)" if alert.get("has_gap") else ""
    at = clock.clock(alert["decided_at"], seconds=True)
    return (
        f"{at:>11s}  {alert['mint']}  {_symbol(alert)}  T+{alert['offset_s']}s  "
        f"{alert['level'].upper()}{late}  {' · '.join(parts)}{gap}"
    )


def _pct(share: float | None) -> str:
    """A probability or rate as a percentage: one decimal below 10%."""
    if share is None:
        return "?"
    return f"{share * 100:.1f}%" if share < 0.0995 else f"{share * 100:.0f}%"


_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


def _days(days: list[str] | None) -> str:
    """A run of days ("2026-11-12", ...) as "Nov 12-16, 2026" (days are UTC days)."""
    try:
        (y1, m1, d1), (y2, m2, d2) = ([int(x) for x in day.split("-")] for day in
                                      (days[0], days[-1]))  # fmt: skip
        first, last = f"{_MONTHS[m1 - 1]} {d1}", f"{_MONTHS[m2 - 1]} {d2}"
    except (TypeError, ValueError, IndexError):
        return "?"
    if (y1, m1, d1) == (y2, m2, d2):
        return f"{first}, {y1}"
    if (y1, m1) == (y2, m2):
        return f"{first}-{d2}, {y1}"
    return f"{first} to {last}, {y2}" if y1 == y2 else f"{first}, {y1} to {last}, {y2}"


def score_text(verdict: dict, stages: list[dict]) -> str:
    """One decision time's model scores: each stage's probability beside its usual rate,
    or why there is none."""
    late = verdict.get("wait_s")
    head = f"T+{verdict['offset_s']}s" + (f", {late:.0f}s late" if late is not None else "")
    scores = verdict.get("scores")
    if not scores:
        return f"{head}: no score (the models were not on when this verdict was made)"
    if "p" not in scores:
        return f"{head}: no score: {scores.get('why')}"
    usual = verdict.get("usual") or {}
    missing = scores.get("missing") or {}
    parts = []
    for stage in stages:
        name = stage["name"]
        words = STAGE_WORDS.get(name, name)
        if name in scores["p"]:
            parts.append(f"{words} {_pct(scores['p'][name])} (usual {_pct(usual.get(name))})")
        else:
            parts.append(f"{words} - ({missing.get(name, 'no score')})")
    return f"{head}: " + " · ".join(parts)


def model_lines(a: dict) -> list[str]:
    """The model-score part of the analysis panel. Empty for an engine that does not
    know about model scores."""
    models = a.get("models")
    if models is None:
        return []
    if not models.get("on"):
        return [f"model scores off: {models.get('why')}"]
    history = a.get("verdicts") or []
    if not history:
        return ["no model score yet (the first is at T+30s)"]
    test_days = _days(models.get("test_days"))
    lines = ["model scores, as analysis and not advice (usual = how often it happened on the "
             f"test days {test_days}, same time, same kind of coin):"]  # fmt: skip
    lines += ["  " + score_text(v, models.get("stages") or []) for v in history]
    lines.append(f"! models trained on {_days(models.get('trained_days'))}; the market drifts, "
                 "and the backtest found no signal that makes money after costs")  # fmt: skip
    context = ((history[-1].get("scores") or {}).get("context")) or {}
    covered = context.get("day_covered")
    if covered is not None and covered < 1 and "p" in (history[-1].get("scores") or {}):
        lines.append(
            f"! creator history: {covered:.0%} of the 24 h before launch recorded, so the "
            f"creator's launch count ({context.get('dev_launches_24h')}) is a lower bound")
    return lines


def analysis_lines(a: dict) -> list[str]:
    """The analysis panel for one mint. Line 2 is always the verdict (or why there is
    none), so the app can colour it."""
    if a.get("op") == "error":
        return [a.get("mint") or "", f"could not analyze: {a.get('error')}"]
    if not a.get("known"):
        return [a["mint"], f"not tracked: {a.get('why')}"]
    s = a["snapshot"]
    name = clean(a.get("name"))
    head = [
        f"{a['mint']}  {_symbol(a)}" + (f" ({name})" if name else ""),
        f"age {age(s['token_age_s'])}",
        f"curve {s['curve_progress_pct']:.0f}%",
        f"mc {_num(s.get('market_cap_sol'), '.1f', ' SOL')}",
        f"data as of {age(a['data_age_s'])} ago",
    ]
    reasons = "; ".join(a["reasons"]) if a["reasons"] else "no rule fired"
    via_curve = a.get("copied") not in (None, a["mint"])
    dev_sold = s.get("dev_sold_pct")
    sold = "has not sold" if not dev_sold else f"sold {dev_sold:.0f}% of their buy"
    prior = s.get("dev_prior_launches")
    lines = [
        " · ".join(head),
        f"{a['level'].upper()}: {reasons}",
        (f"buyers {s['unique_buyers']} ({s['unique_buyers_per_min']:.0f}/min) · holders "
         f"{s['holders']} · {s['buys']} buys / {s['sells']} sells · net "
         f"{_num(s.get('net_sol'), '+.2f', ' SOL')} · last 30s "
         f"{_num(s.get('net_sol_last_30s'), '+.2f', ' SOL')}"),
        (f"top10 {s['top10_hold_pct']:.0f}% · top1 {s['top1_hold_pct']:.0f}% · snipers "
         f"{s['snipers']} hold {s['sniper_hold_pct']:.0f}% · launch-slot buyers "
         f"{s['launch_slot_buyers']} · biggest sell {s['largest_sell_pct']:.1f}%"),
        (f"dev holds {s['dev_hold_pct']:.1f}% (bought {s['dev_buy_pct']:.1f}%, {sold}) · "
         + ("earlier launches unknown" if prior is None else f"{prior} earlier launches seen")),
    ]  # fmt: skip
    history = a.get("verdicts") or []
    lines.append(
        "verdicts so far: " + " · ".join(f"T+{v['offset_s']}s {v['level']}" for v in history)
        if history
        else "no verdict yet (the first is at T+30s)"
    )
    lines += model_lines(a)
    if via_curve:
        lines.append("! you copied the coin's bonding-curve address; its mint is the first line")
    if a.get("has_gap"):
        lines.append("! data incomplete: a recording gap touched this coin")
    if s.get("is_mayhem"):
        lines.append("! mayhem coin: pump.fun's own bot trades it")
    if not s.get("is_sol_paired"):
        lines.append("! not paired with SOL: SOL amounts are not shown")
    if s.get("completed"):
        lines.append("! the bonding curve is complete (graduated)")
    return lines


def status_text(
    status: dict | None,
    *,
    connected: bool,
    median_s: float | None,
    clipboard: str,
) -> str:
    """The status bar: engine, feed, rates, verdict delay, copy-to-verdict time."""
    if not connected:
        return f"engine offline: type `start` (or run `agentd start`) · clipboard {clipboard}"
    if not status:
        return f"engine connected, waiting for its status · clipboard {clipboard}"
    feeds = status.get("connections") or []
    up = sum(1 for c in feeds if c.get("connected"))
    lag = status.get("covered_lag_s")
    parts = [
        f"feeds {up}/{len(feeds)} up" if feeds else
        ("feed connected" if status.get("connected") else "feed DOWN"),
        f"data {_num(lag, '.0f', 's')} behind",
        f"{status.get('creates_per_hr', '-'):,} tokens/hr"
        if isinstance(status.get("creates_per_hr"), int) else "tokens/hr -",
    ]  # fmt: skip
    live = status.get("live") or {}
    if live.get("enabled"):
        wait = live.get("median_wait_s")
        parts.append(f"verdicts {_num(wait, '.0f', 's')} late")
    elif live:
        parts.append("verdicts OFF")
    models = live.get("models")
    if models is not None:
        parts.append(f"models {models.get('version', 'on')}" if models.get("on")
                     else "model scores OFF")  # fmt: skip
    if not status.get("recording", True):
        parts.append("NOT RECORDING")
    gap = status.get("last_gap")
    if gap:
        parts.append(f"last gap {clock.clock(gap['at'])} ({gap['seconds']}s)")
    parts.append(f"copy-to-verdict {_num(median_s, '.2f', 's')}")
    parts.append(f"clipboard {clipboard}")
    return " · ".join(parts)
