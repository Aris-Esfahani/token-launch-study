"""The screen (spec 12): live alerts, a paste box with a clipboard watcher, the analysis
panel and a status bar. Run it with `agent screen`, beside Padre.

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
MAX_ALERTS = 500  # alert lines kept in the feed
HELP = [
    "commands",
    "<mint>: analyze it (or just copy the mint in Padre: the clipboard watcher picks it up)",
    "click an alert (or pick it with the arrow keys and press Enter) to analyze that coin",
    "mute <mint> / unmute <mint>: hide or show that coin in the alert feed",
    "clipboard: turn the clipboard watcher on or off (also F2)",
    "start: start the engine if it is not running",
    "quit: close the screen (the engine keeps running)",
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
        Binding("ctrl+l", "clear_alerts", "Clear alerts"),
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
        self._alert_mints: dict[str, str] = {}  # alert line id -> mint
        self._alert_count = 0
        self._status: dict | None = None
        self._stop = asyncio.Event()

    # --- layout ----------------------------------------------------------------------

    def compose(self) -> ComposeResult:
        # Each alert is a line you can click (long lines wrap, so no signal is cut off).
        yield OptionList(id="alerts")
        yield Input(
            placeholder="paste a mint and press Enter, or just copy it in Padre · help",
            id="input",
        )
        yield Static(id="analysis")
        yield Static(id="status")
        yield Footer()

    def on_mount(self) -> None:
        alerts = self.query_one("#alerts", OptionList)
        alerts.border_title = "ALERTS (tokens that pass the rules; click one to analyze it)"
        self.query_one("#analysis", Static).border_title = "ANALYZE"
        self._show(["copy a mint in Padre, or paste one above", "", *HELP[1:]])
        self.run_worker(self.client.run(self._stop), group="engine")
        self.set_interval(POLL_S, self._poll_clipboard)
        self.set_interval(STATUS_EVERY_S, self._refresh_status)
        self._draw_status()

    def on_unmount(self) -> None:
        self._stop.set()

    # --- engine events ---------------------------------------------------------------

    def _alert(self, alert: dict, old: bool) -> None:
        if alert.get("mint") in self.muted:
            return
        style = view.LEVEL_STYLE.get(alert.get("level", ""), "")
        if old:
            style = "dim"
        alerts = self.query_one("#alerts", OptionList)
        follow = alerts.option_count == 0 or alerts.is_vertical_scroll_end
        self._alert_count += 1
        line_id = str(self._alert_count)
        self._alert_mints[line_id] = alert["mint"]
        alerts.add_option(Option(Text(view.alert_text(alert), style=style), id=line_id))
        if alerts.option_count > MAX_ALERTS:
            self._alert_mints.pop(alerts.get_option_at_index(0).id or "", None)
            alerts.remove_option_at_index(0)
        if follow:  # keep showing the newest, unless you scrolled up to read older ones
            alerts.scroll_end(animate=False)

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        mint = self._alert_mints.get(event.option_id or "")
        if mint is not None:
            self._start_analysis(mint, "alert", None)

    def _connection(self, up: bool) -> None:
        if not up:
            self._status = None
        self._draw_status()

    # --- analysis --------------------------------------------------------------------

    def _poll_clipboard(self) -> None:
        mint = self.watcher.poll()
        if mint is not None:
            self._start_analysis(mint, "clipboard", self.watcher.changed_at)

    def _start_analysis(self, mint: str, source: str, started: float | None) -> None:
        started = time.perf_counter() if started is None else started
        self.run_worker(self._analyze(mint, source, started), group="analyze", exclusive=True)

    async def _analyze(self, mint: str, source: str, started: float) -> None:
        self._show([mint, "analyzing..."])
        try:
            result = await self.client.analyze(mint)
        except EngineOffline as exc:
            self._show([mint, f"engine offline ({exc}): type `start` to start it"])
            return
        self._show(view.analysis_lines(result), level=result.get("level"))
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
        elif command in ("mute", "unmute") and is_address(argument):
            (self.muted.add if command == "mute" else self.muted.discard)(argument)
            self._show([argument, f"{command}d in the alert feed"])
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

    def action_clear_alerts(self) -> None:
        self.query_one("#alerts", OptionList).clear_options()
        self._alert_mints.clear()

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
            clipboard = "not available here"
        else:
            clipboard = "on (F2)" if self.watcher.enabled else "off (F2)"
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
