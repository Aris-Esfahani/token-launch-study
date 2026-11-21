"""Settings (config/settings.yaml) and secrets (.env).

Secrets are held as SecretStr and only unwrapped at the moment a connection is opened.
Anything that may contain a secret (an endpoint URL) is kept out of repr() and logs.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml
from pydantic import BaseModel, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

_PLACEHOLDER = re.compile(r"\$\{([A-Z0-9_]+)\}")


class Secrets(BaseSettings):
    """Values read from .env (or the process environment). Empty values count as unset."""

    model_config = SettingsConfigDict(extra="ignore", env_ignore_empty=True)

    helius_api_key: SecretStr | None = None
    data_dir: Path | None = None


class Source(BaseModel):
    name: str
    ws_url: str  # may contain ${ENV_VAR}; never log this
    enabled: bool = True
    # Connections run at the same time; sources with the same number are tried in turn
    # for that one connection.
    connection: int = 1


class Reconnect(BaseModel):
    initial_s: float = 1.0
    max_s: float = 30.0


class Live(BaseModel):
    commitment: str = "confirmed"
    subscribe: list[str] = ["pump"]
    sources: list[Source]
    reconnect: Reconnect = Reconnect()
    stale_after_s: float = 20.0


class Recorder(BaseModel):
    flush_interval_s: float = 60.0  # how often buffered rows are written to disk
    min_free_gb: float = 5.0  # stop recording (and log a gap) below this much free space
    goal_hours: float = 24.0  # length of one recording burst, shown as a bar; 0 = continuous
    session_break_hours: float = 1.0  # a gap at least this long starts a new burst
    uptime_target: float = 0.95  # spec 15.1: recorder uptime over the paper-trading period


class Chain(BaseModel):
    # How often the engine saves pump.fun's curve parameters and fee schedule from chain.
    # 0 turns it off (the default, so tests never reach the network).
    snapshot_every_hours: float = 0.0
    # Quote mints pump.fun's fee program treats as SOL and as stablecoins. Both lists are
    # fixed inside its program; settings.yaml holds them with their source.
    sol_like_quote_mints: list[str] = []
    stable_quote_mints: list[str] = []


class Dataset(BaseModel):
    # Days of pump.fun history wanted for a first model. Spec 4.1: start with 2-4 weeks
    # of backfill, extend toward 2-3 months if the pilot shows it is affordable.
    target_days: float = 28.0
    # Download per day of history from the backfill archive. Measured from the archive's
    # file sizes on 2026-11-12 (290-390 GB per ~1.07-day epoch); the Phase 3 pilot
    # replaces it with a real measurement.
    backfill_gb_per_day: float = 310.0


class BackfillSettings(BaseModel):
    base_url: str = "https://files.old-faithful.net"
    workers: int = 6
    max_mb_s: float = 80.0  # all workers together; 0 = no limit
    chunk_slots: int = 64
    commit_chunks: int = 64


class RpcSettings(BaseModel):
    url: str = "https://api.mainnet.solana.com"  # may contain ${ENV_VAR}; never log this
    max_rps: float = 3.0


class LocalApi(BaseModel):
    # The engine's connection for the screen (spec 3). It listens on 127.0.0.1 only;
    # 0 turns it off.
    port: int = 8765


class LiveModels(BaseModel):
    # Scores from the saved models beside each live verdict (spec 15.3, step L1).
    # Analysis only. The engine runs without them when they cannot be loaded.
    enabled: bool = True
    # A coin is scored only if at least this share of the hour before its launch was
    # recorded: the models read the number of launches in that hour.
    min_hour_covered: float = 0.95


class Settings(BaseModel):
    programs: dict[str, str]
    live: Live
    recorder: Recorder = Recorder()
    dataset: Dataset = Dataset()
    chain: Chain = Chain()
    backfill: BackfillSettings = BackfillSettings()
    rpc: RpcSettings = RpcSettings()
    local_api: LocalApi = LocalApi()
    live_models: LiveModels = LiveModels()


@dataclass(frozen=True)
class Endpoint:
    """A live source ready to connect to. The URL may hold an API key, so it is hidden."""

    name: str
    url: str = field(repr=False)
    connection: int = 1


@dataclass(frozen=True)
class AppConfig:
    root: Path
    settings: Settings
    secrets: Secrets
    data_dir: Path

    @property
    def run_dir(self) -> Path:
        return self.data_dir / "run"

    def secret_values(self) -> list[str]:
        """Raw secret strings, used only to scrub them out of log output."""
        key = self.secrets.helius_api_key
        return [key.get_secret_value()] if key else []

    def _fill(self, text: str) -> str | None:
        """Replace ${ENV_VAR} with its secret value. None if a needed value is not set."""
        values = {"HELIUS_API_KEY": self.secrets.helius_api_key}
        if any(not values.get(name) for name in _PLACEHOLDER.findall(text)):
            return None
        return _PLACEHOLDER.sub(lambda m: values[m.group(1)].get_secret_value(), text)

    def endpoints(self) -> tuple[list[Endpoint], list[str]]:
        """Enabled sources with ${ENV_VAR} filled in, plus the names of sources skipped
        because a value they need is not set."""
        ready: list[Endpoint] = []
        skipped: list[str] = []
        for src in self.settings.live.sources:
            if not src.enabled:
                continue
            url = self._fill(src.ws_url)
            if url is None:
                skipped.append(src.name)
            else:
                ready.append(Endpoint(src.name, url, src.connection))
        return ready, skipped

    def connections(self) -> tuple[dict[int, list[Endpoint]], list[str]]:
        """Enabled sources grouped by connection number (each group is one connection that
        runs all the time), plus the names of sources skipped for a missing value."""
        ready, skipped = self.endpoints()
        groups: dict[int, list[Endpoint]] = {}
        for endpoint in ready:
            groups.setdefault(endpoint.connection, []).append(endpoint)
        return dict(sorted(groups.items())), skipped

    def rpc_url(self) -> str:
        """The HTTP RPC address for lookups. May contain an API key: never log it."""
        url = self._fill(self.settings.rpc.url)
        if url is None:
            raise ValueError("rpc.url in settings.yaml needs a value that is not set in .env")
        return url

    def subscribed_programs(self) -> dict[str, str]:
        """{program address: short name} for the programs followed live."""
        programs = self.settings.programs
        unknown = [k for k in self.settings.live.subscribe if k not in programs]
        if unknown:
            raise ValueError(f"live.subscribe names unknown programs: {unknown}")
        return {programs[k]: k for k in self.settings.live.subscribe}


def find_project_root(start: Path | None = None) -> Path:
    """The folder holding config/settings.yaml: AGENT_HOME, else the current folder or a
    parent of it, else the folder this package was installed from (editable install)."""
    home = os.environ.get("AGENT_HOME")
    if home:
        return Path(home)
    for base in (start or Path.cwd(), Path(__file__).resolve()):
        for folder in (base, *base.parents):
            if (folder / "config" / "settings.yaml").is_file():
                return folder
    raise FileNotFoundError(
        "config/settings.yaml not found. Run from the project folder or set AGENT_HOME."
    )


def load_config(root: Path | None = None) -> AppConfig:
    root = (root or find_project_root()).resolve()
    with open(root / "config" / "settings.yaml", encoding="utf-8") as fh:
        settings = Settings.model_validate(yaml.safe_load(fh))
    secrets = Secrets(_env_file=root / ".env", _env_file_encoding="utf-8")
    data_dir = secrets.data_dir or (root / "data")
    if not data_dir.is_absolute():
        data_dir = root / data_dir
    return AppConfig(root=root, settings=settings, secrets=secrets, data_dir=data_dir)
