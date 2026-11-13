from pathlib import Path

import pytest

from agent.config import find_project_root, load_config
from helpers import PUMP, make_project

REPO = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    monkeypatch.delenv("HELIUS_API_KEY", raising=False)
    monkeypatch.delenv("DATA_DIR", raising=False)
    monkeypatch.delenv("AGENT_HOME", raising=False)


def test_real_settings_file_loads():
    cfg = load_config(REPO)
    assert cfg.subscribed_programs() == {PUMP: "pump"}
    names = [s.name for s in cfg.settings.live.sources]
    assert names[0] == "solana-public"
    # The metered Helius stream must stay off unless someone turns it on deliberately.
    assert not next(s for s in cfg.settings.live.sources if s.name == "helius").enabled


def test_data_dir_defaults_to_project_data_folder(tmp_path):
    make_project(tmp_path)
    cfg = load_config(tmp_path)
    assert cfg.data_dir == tmp_path.resolve() / "data"
    assert cfg.run_dir == cfg.data_dir / "run"


def test_data_dir_from_env_file_keeps_windows_backslashes(tmp_path):
    make_project(tmp_path, env="DATA_DIR=D:\\memecoin-agent-data\n")
    assert str(load_config(tmp_path).data_dir).replace("/", "\\").endswith(
        "D:\\memecoin-agent-data"
    )


def test_unknown_program_in_subscribe_is_an_error(tmp_path):
    make_project(tmp_path)
    path = tmp_path / "config" / "settings.yaml"
    path.write_text(path.read_text().replace("subscribe: [pump]", "subscribe: [nope]"))
    with pytest.raises(ValueError, match="unknown programs"):
        load_config(tmp_path).subscribed_programs()


def test_project_root_found_from_a_subfolder_and_from_agent_home(tmp_path, monkeypatch):
    make_project(tmp_path)
    sub = tmp_path / "a" / "b"
    sub.mkdir(parents=True)
    assert find_project_root(sub) == tmp_path
    monkeypatch.setenv("AGENT_HOME", str(tmp_path))
    assert find_project_root(Path.home()) == tmp_path
