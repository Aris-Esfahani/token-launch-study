"""The screen (spec 12): the list of new coins that look OK, a paste box with a clipboard
watcher, the details panel and a status bar, all in plain English (spec 12.1, note of
2026-11-22). Run it with `agent screen`, beside Padre.

A coin has one line in the list, shown by its name; clicking it shows its details and
copies its address for Padre's search. F3 switches the details between a few sentences
and every number.

It holds no data: alerts and analyses come from the engine (`agentd`) over its local
API, so the screen can be closed and opened again without losing anything. Each
analysis is timed from the moment the mint was copied (or Enter was pressed) to the
moment its verdict is on screen; the median is in the status bar (spec 12.5) and every
time is appended to run/screen_latency.jsonl.
"""

from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
import sys
import time
from collections import deque
from pathlib import Path
from statistics import median

from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.widgets import Footer, Input, OptionList, Static
from textual.widgets.option_list import Option

from agent.ingest.solana import is_address
from agent.tui import view
from agent.tui.client import EngineClient, EngineOffline
from agent.tui.clipboard import POLL_S, ClipboardSource, ClipboardWatcher, find_mint

STATUS_EVERY_S = 2.0
AGES_EVERY_S = 5.0  # how often the coins' ages in the list are brought up to date
MAX_ALERTS = 500  # coins kept in the list
HELP = [
    "How to use this screen",
    "Click a coin in the list (or pick it with the arrow keys and press Enter) to see its "
    "details. Its address is copied, ready to paste into Padre.",
    "Or copy a coin's address in Padre: its details show up here by themselves.",
    "Or paste an address in the box and press Enter.",
    "F3 (or type more): switch between the short details and every number.",
    "F2 (or type clipboard): stop or start watching what you copy.",
    "mute <address> / unmute <address>: hide or show a coin in the list.",
    "start: start the engine if it is off.   quit: close the screen (the engine keeps running).",
]


class AgentScreen(App):
    TITLE = "memecoin agent"
    CSS = """
    #alerts { height: 1fr; border: round $accent; }
    #input { margin: 0 0; }
    #analysis { height: auto; min-height: 9; max-height: 60%; overflow-y: auto;
                border: round $secondary; padding: 0 1; }
    #status { height: 1; padding: 0 1; background: $panel; }
    """
    BINDINGS = [
        Binding("f2", "toggle_clipboard", "Clipboard on/off"),
        Binding("f3", "toggle_detail", "More/less detail"),
        Binding("ctrl+l", "clear_alerts", "Clear list"),
        Binding("ctrl+q", "quit", "Quit"),
    ]

    def __init__(
        self, port: int, run_dir: Path, *, clipboard: ClipboardSource | None = None
    ) -> None:
        super().__init__()
        self.client = EngineClient(port, on_alert=self._alert, on_connection=self._connection)
        self.watcher = ClipboardWatcher(clipboard)
        self.run_dir = run_dir
        self.latencies: deque[float] = deque(maxlen=200)
        self.muted: set[str] = set()
        self._alert_mints: dict[str, str] = {}  # list line id -> mint
        self._coins: dict[str, dict] = {}  # mint -> its line: id, newest alert, old, text
        self._alert_count = 0
        self.full = False  # the details panel: a few sentences, or every number (F3)
        self._shown: tuple[dict, bool] | None = None  # the coin in the panel, was it copied
        self._status: dict | None = None
        self._stop = asyncio.Event()

    # --- layout ----------------------------------------------------------------------

    def compose(self) -> ComposeResult:
        # Each coin is a line you can click (long lines wrap, so nothing is cut off).
        yield OptionList(id="alerts")
        yield Input(
            placeholder="paste a coin's address and press Enter, or just copy it in Padre"
            " · type help",
            id="input",
        )
        yield Static(id="analysis")
        yield Static(id="status")
        yield Footer()

    def on_mount(self) -> None:
        alerts = self.query_one("#alerts", OptionList)
        alerts.border_title = "NEW COINS that look OK · click one for details"
        self.query_one("#analysis", Static).border_title = "DETAILS"
        self._show(HELP)
        self.run_worker(self.client.run(self._stop), group="engine")
        self.set_interval(POLL_S, self._poll_clipboard)
        self.set_interval(STATUS_EVERY_S, self._refresh_status)
        self.set_interval(AGES_EVERY_S, self._refresh_ages)
        self._draw_status()

    def on_unmount(self) -> None:
        self._stop.set()

    # --- engine events ---------------------------------------------------------------

    def _alert(self, alert: dict, old: bool) -> None:
        if alert.get("mint") in self.muted:
            return
        alerts = self.query_one("#alerts", OptionList)
        text = view.alert_text(alert, time.time())
        coin = self._coins.get(alert["mint"])
        if coin is not None:  # a later check of a coin already in the list: same line
            coin.update(alert=alert, old=old, text=text)
            alerts.replace_option_prompt(coin["id"], self._line(coin))
            return
        follow = alerts.option_count == 0 or alerts.is_vertical_scroll_end
        self._alert_count += 1
        line_id = str(self._alert_count)
        coin = {"id": line_id, "alert": alert, "old": old, "text": text}
        self._coins[alert["mint"]] = coin
        self._alert_mints[line_id] = alert["mint"]
        alerts.add_option(Option(self._line(coin), id=line_id))
        if alerts.option_count > MAX_ALERTS:
            gone = self._alert_mints.pop(alerts.get_option_at_index(0).id or "", None)
            self._coins.pop(gone or "", None)
            alerts.remove_option_at_index(0)
        if follow:  # keep showing the newest, unless you scrolled up to read older ones
            alerts.scroll_end(animate=False)

    @staticmethod
    def _line(coin: dict) -> Text:
        style = "dim" if coin["old"] else view.LEVEL_STYLE.get(coin["alert"].get("level", ""), "")
        return Text(coin["text"], style=style)

    def _refresh_ages(self) -> None:
        """Coins get older while they sit in the list: rewrite the lines whose age changed."""
        alerts = self.query_one("#alerts", OptionList)
        now = time.time()
        for coin in self._coins.values():
            text = view.alert_text(coin["alert"], now)
            if text != coin["text"]:
                coin["text"] = text
                alerts.replace_option_prompt(coin["id"], self._line(coin))

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        mint = self._alert_mints.get(event.option_id or "")
        if mint is not None:
            # Ready to paste into Padre's search (the watcher skips its own copy).
            copied = self.watcher.copy(mint)
            self._start_analysis(mint, "alert", None, copied=copied)

    def _connection(self, up: bool) -> None:
        if not up:
            self._status = None
        self._draw_status()

    # --- analysis --------------------------------------------------------------------

    def _poll_clipboard(self) -> None:
        mint = self.watcher.poll()
        if mint is not None:
            self._start_analysis(mint, "clipboard", self.watcher.changed_at)

    def _start_analysis(self, mint: str, source: str, started: float | None, *,
                        copied: bool = False) -> None:  # fmt: skip
        started = time.perf_counter() if started is None else started
        self.run_worker(self._analyze(mint, source, started, copied), group="analyze",
                        exclusive=True)  # fmt: skip

    async def _analyze(self, mint: str, source: str, started: float, copied: bool) -> None:
        self._shown = None
        self._show([mint, "looking it up..."])
        try:
            result = await self.client.analyze(mint)
        except EngineOffline as exc:
            self._show([mint, f"the engine is off ({exc}): type start to start it"])
            return
        self._shown = (result, copied)
        self._show_coin()
        seconds = time.perf_counter() - started
        self.latencies.append(seconds)
        self._log_latency(source, seconds, bool(result.get("known")))
        self._draw_status()

    def _log_latency(self, source: str, seconds: float, known: bool) -> None:
        record = {"at": time.time(), "source": source, "seconds": round(seconds, 4),
                  "known": known}  # fmt: skip
        try:
            with open(self.run_dir / "screen_latency.jsonl", "a", encoding="utf-8") as f:
                f.write(json.dumps(record) + "\n")
        except OSError:
            pass  # timing is a nice-to-have; never let it break the screen

    def _show_coin(self) -> None:
        if self._shown is None:
            return
        result, copied = self._shown
        lines = view.analysis_lines(result, full=self.full, copied=copied)
        if result.get("known"):
            lines.append("F3: the short version" if self.full else "F3: every number")
        self._show(lines, level=result.get("level"))

    def _show(self, lines: list[str], *, level: str | None = None) -> None:
        text = Text()
        for i, line in enumerate(lines):
            if i:
                text.append("\n")
            if i == 0:
                style = "bold"
            elif i == 1 and level:
                style = view.LEVEL_STYLE.get(level, "")
            elif line.startswith("!"):
                style = "yellow"
            elif line.startswith("F3: "):
                style = "dim"
            else:
                style = ""
            text.append(line, style=style)
        self.query_one("#analysis", Static).update(text)

    # --- commands --------------------------------------------------------------------

    def on_input_submitted(self, event: Input.Submitted) -> None:
        started = time.perf_counter()
        text = event.value.strip()
        event.input.value = ""
        if not text:
            return
        words = text.split()
        command = words[0].lower()
        argument = words[1] if len(words) == 2 else None
        if command in ("quit", "exit"):
            self.exit()
        elif command == "help":
            self._show(HELP)
        elif command == "start":
            self.run_worker(self._start_engine(), group="start", exclusive=True)
        elif command == "clipboard":
            self.action_toggle_clipboard()
        elif command in ("more", "less", "details"):
            self.action_toggle_detail()
        elif command in ("mute", "unmute") and is_address(argument):
            (self.muted.add if command == "mute" else self.muted.discard)(argument)
            self._show([argument, "hidden from the list" if command == "mute"
                        else "shown in the list again"])  # fmt: skip
        else:
            mint = find_mint(text)
            if mint is None:
                # Not echoed: whatever was pasted by mistake stays off the screen.
                self._show(["", "that is not a Solana address or a command (type help)"])
            else:
                self._start_analysis(mint, "typed", started)

    async def _start_engine(self) -> None:
        if self.client.connected:
            self._show(["", "the engine is already running"])
            return
        command = _agentd_command()
        if command is None:
            self._show(["", "could not find agentd; run `agentd start` in a terminal"])
            return
        self._show(["", "starting the engine..."])
        done = await asyncio.to_thread(
            subprocess.run, [*command, "start"], capture_output=True, text=True, timeout=60
        )
        output = (done.stdout or done.stderr or "").strip().splitlines()
        self._show(["", output[-1] if output else f"agentd start exited with {done.returncode}"])

    def action_toggle_clipboard(self) -> None:
        self.watcher.toggle()
        self._draw_status()

    def action_toggle_detail(self) -> None:
        self.full = not self.full
        self._show_coin()

    def action_clear_alerts(self) -> None:
        self.query_one("#alerts", OptionList).clear_options()
        self._alert_mints.clear()
        self._coins.clear()

    # --- status bar ------------------------------------------------------------------

    async def _refresh_status(self) -> None:
        if self.client.connected:
            try:
                self._status = await self.client.status()
            except EngineOffline:
                self._status = None
        self._draw_status()

    def _draw_status(self) -> None:
        if not self.watcher.available:
            clipboard = "watching not available here"
        else:
            clipboard = "watching on (F2)" if self.watcher.enabled else "watching off (F2)"
        text = view.status_text(
            self._status,
            connected=self.client.connected,
            median_s=median(self.latencies) if self.latencies else None,
            clipboard=clipboard,
        )
        self.query_one("#status", Static).update(Text(text))


def _agentd_command() -> list[str] | None:
    """How to run `agentd` from here: the script beside this Python, or on the PATH."""
    script = Path(sys.executable).with_name("agentd.exe" if sys.platform == "win32" else "agentd")
    if script.exists():
        return [str(script)]
    found = shutil.which("agentd")
    return [found] if found else None


def run_screen(port: int, run_dir: Path) -> None:
    from agent.tui.clipboard import system_clipboard

    AgentScreen(port, run_dir, clipboard=system_clipboard()).run()
