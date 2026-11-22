"""What the screen shows, as plain text lines (the app adds colours). Kept apart from the
app so it can be tested without a terminal.

The screen is minimal and in plain English (spec 12.1, note of 2026-11-22): a coin is
shown by its name, with a few facts in sentences. Its address is in the details panel,
ready for Padre's search (spec 1). `analysis_lines(..., full=True)` gives every number,
address first, as the screen showed them before. Names and symbols come from whoever
launched the coin, so they are cleaned: control characters are dropped and the length is
capped.

Model scores (spec 15.3, step L1) are analysis, not advice: every probability is shown
beside the outcome's usual rate, with how late it came, and the panel always says when
the models were trained and that the backtest found no signal that makes money.
"""

from __future__ import annotations

from agent import clock

LEVEL_STYLE = {"pass": "bold green", "caution": "yellow", "avoid": "red", "error": "magenta"}
# The rules' verdict in plain words.
LEVEL_WORDS = {"pass": "looks OK", "caution": "be careful", "avoid": "avoid"}
MAX_LABEL = 20
MAX_NAME = 28
LATE_S = 10  # a verdict or score that waited this long for its data is called late
# The models' stages in plain words (a stage not named here is shown by its name).
STAGE_WORDS = {"bad": "bad", "grad_later": "graduates later", "hit_2x": "2x before half"}
PLAIN_STAGE_WORDS = {"bad": "goes bad", "grad_later": "graduates later",
                     "hit_2x": "doubles before halving"}  # fmt: skip


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


def coin_name(item: dict) -> str:
    """The coin as its launcher named it: "Name ($SYMBOL)", or what there is of it."""
    name, symbol = clean(item.get("name"), MAX_NAME), clean(item.get("symbol"), 12)
    if name and symbol:
        return f"{name} (${symbol})"
    return name or (f"${symbol}" if symbol else "(no name)")


def how_long(seconds: float | None) -> str:
    """A length of time in words: "45 sec", "2 min", "3 h"."""
    if seconds is None:
        return "?"
    seconds = max(int(seconds), 0)
    if seconds < 60:
        return f"{seconds} sec"
    if seconds < 3600:
        return f"{seconds // 60} min"
    return f"{seconds // 3600} h"


def _people(n: int, one: str = "buyer", many: str = "buyers") -> str:
    return f"{n:,} {one if n == 1 else many}"


def alert_text(alert: dict, now: float | None = None) -> str:
    """One coin in the list at the top: its name, how old it is at `now`, what the rules
    said and when, and two facts. No address: the details panel has it."""
    s = alert.get("signals") or {}
    parts = [coin_name(alert)]
    created = alert.get("created_ts")
    if now is not None and created is not None:
        parts.append(f"{how_long(now - created)} old")
    level = alert.get("level", "")
    at = how_long(alert.get("offset_s"))
    parts.append(f"looked OK at {at}" if level == "pass"
                 else f"{LEVEL_WORDS.get(level, level)} at {at}")  # fmt: skip
    if s.get("unique_buyers") is not None:
        parts.append(_people(int(s["unique_buyers"])))
    if s.get("market_cap_sol") is not None:
        parts.append(f"worth {s['market_cap_sol']:.0f} SOL")
    if alert.get("has_gap"):
        parts.append("some data missing")
    return " · ".join(parts)


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


def plain_model_lines(a: dict) -> list[str]:
    """The models' newest scores in a few plain lines, always with their limits. Empty
    for an engine that does not know about model scores."""
    models = a.get("models")
    if models is None:
        return []
    if not models.get("on"):
        return [f"No model guess: model scores are off ({models.get('why')})"]
    history = a.get("verdicts") or []
    if not history:
        return ["No model guess yet (the first comes when the coin is 30 sec old)"]
    newest = history[-1]
    at = how_long(newest.get("offset_s"))
    scores = newest.get("scores")
    if not scores:
        return [f"No model guess at {at} (the models were not on then)"]
    if "p" not in scores:
        return [f"No model guess at {at}: {scores.get('why')}"]
    usual = newest.get("usual") or {}
    missing = scores.get("missing") or {}
    parts = []
    for stage in models.get("stages") or []:
        name = stage["name"]
        words = PLAIN_STAGE_WORDS.get(name, name)
        if name in scores["p"]:
            parts.append(f"{words} {_pct(scores['p'][name])} (usually {_pct(usual.get(name))})")
        else:
            parts.append(f"{words}: no guess ({missing.get(name, 'no score')})")
    late = newest.get("wait_s")
    lines = [
        f"The model's guess at {at} (analysis, not advice"
        + (f"; it came {late:.0f} sec late" if late is not None and late >= LATE_S else "") + "):",
        "  " + " · ".join(parts),
        f"  Trained on {_days(models.get('trained_days'))}. In the backtest, no signal made "
        "money after costs.",
    ]  # fmt: skip
    context = scores.get("context") or {}
    covered = context.get("day_covered")
    if covered is not None and covered < 1:
        lines.append(
            f"! The creator's coin count ({context.get('dev_launches_24h')}) may be too low: "
            f"only {covered:.0%} of the day before the launch was recorded")
    return lines


def _plain_lines(a: dict, copied: bool) -> list[str]:
    s = a["snapshot"]
    reasons = "; ".join(a["reasons"]) if a["reasons"] else "no warning sign so far"
    buyers = s["unique_buyers"]
    bought = "1 person bought it" if buyers == 1 else f"{buyers:,} people bought it"
    if not s.get("dev_buy_pct"):
        creator = "The creator has not bought any."
    else:
        dev_sold = s.get("dev_sold_pct")
        sold = ("has not sold" if not dev_sold
                else f"has sold {dev_sold:.0f}% of what they bought")  # fmt: skip
        creator = f"The creator holds {s['dev_hold_pct']:.0f}% and {sold}."
    prior = s.get("dev_prior_launches")
    if prior:
        creator += f" They launched {prior:,} coin{'' if prior == 1 else 's'} before (that we saw)."
    worth = s.get("market_cap_sol")
    money = "Not priced in SOL" if worth is None else f"Worth about {worth:.0f} SOL"
    if s.get("completed"):
        curve = f"{money}. It has graduated (left the bonding curve)."
    else:
        curve = f"{money}, {s['curve_progress_pct']:.0f}% of the way to graduating."
    lines = [
        f"{coin_name(a)} · {how_long(s['token_age_s'])} old · data from "
        f"{how_long(a['data_age_s'])} ago",
        f"{LEVEL_WORDS.get(a['level'], a['level']).upper()}: {reasons}",
        f"  {bought}. The 10 biggest holders own {s['top10_hold_pct']:.0f}%.",
        f"  {creator}",
        f"  {curve}",
    ]
    lines += plain_model_lines(a)
    if a.get("copied") not in (None, a["mint"]):
        lines.append("! You copied the coin's trading address; this is the coin it belongs to")
    if a.get("has_gap"):
        lines.append("! Some of this coin's data is missing (the recording was interrupted)")
    if s.get("is_mayhem"):
        lines.append("! Mayhem coin: pump.fun's own bot trades it")
    lines.append(("Address (copied, paste it in Padre): " if copied else "Address: ") + a["mint"])
    return lines


def analysis_lines(a: dict, *, full: bool = False, copied: bool = False) -> list[str]:
    """The details panel for one coin. Line 2 is always the verdict (or why there is
    none), so the app can colour it. The default is a few plain sentences; `full` gives
    every number, address first. `copied`: the screen put the address on the clipboard."""
    if a.get("op") == "error":
        return [a.get("mint") or "", f"could not analyze: {a.get('error')}"]
    if not a.get("known"):
        return [a["mint"], f"not tracked: {a.get('why')}"]
    return _full_lines(a) if full else _plain_lines(a, copied)


def _full_lines(a: dict) -> list[str]:
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
    """The status bar in plain words: is the engine on, how fresh is the data, are model
    scores on; then only what is wrong, and how fast the screen answers."""
    if not connected:
        return f"Engine off: type start (or run `agentd start`) · clipboard {clipboard}"
    if not status:
        return f"Engine on, waiting for its status · clipboard {clipboard}"
    parts = ["Engine on"]
    feeds = status.get("connections") or []
    up = sum(1 for c in feeds if c.get("connected"))
    if (feeds and up == 0) or (not feeds and not status.get("connected")):
        parts.append("FEED DOWN")
    elif up < len(feeds):
        parts.append(f"{up} of {len(feeds)} feeds up")
    lag = status.get("covered_lag_s")
    if lag is not None:
        parts.append(f"data {lag:.0f} sec behind")
    live = status.get("live") or {}
    if live.get("enabled"):
        wait = live.get("median_wait_s")
        if wait is not None and wait >= LATE_S:
            parts.append(f"checks run {wait:.0f} sec late")
    elif live:
        parts.append("rule checks OFF")
    models = live.get("models")
    if models is not None:
        parts.append("model scores on" if models.get("on") else "model scores OFF")
    if not status.get("recording", True):
        parts.append("NOT RECORDING")
    gap = status.get("last_gap")
    if gap:
        parts.append(f"last interruption {clock.clock(gap['at'])} ({gap['seconds']} sec)")
    if median_s is not None:
        parts.append(f"answers in {median_s:.2f} sec")
    parts.append(f"clipboard {clipboard}")
    return " · ".join(parts)
