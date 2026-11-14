"""What the screen shows, as plain text lines (the app adds colours). Kept apart from the
app so it can be tested without a terminal.

Every line about a coin starts with its mint, so it can be pasted into Padre's search
(spec 1). Names and symbols come from whoever launched the coin, so they are cleaned:
control characters are dropped and the length is capped.
"""

from __future__ import annotations

from agent import clock

LEVEL_STYLE = {"pass": "bold green", "caution": "yellow", "avoid": "red", "error": "magenta"}
MAX_LABEL = 20


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
    if not status.get("recording", True):
        parts.append("NOT RECORDING")
    gap = status.get("last_gap")
    if gap:
        parts.append(f"last gap {clock.clock(gap['at'])} ({gap['seconds']}s)")
    parts.append(f"copy-to-verdict {_num(median_s, '.2f', 's')}")
    parts.append(f"clipboard {clipboard}")
    return " · ".join(parts)
