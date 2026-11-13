"""Hard rule: API keys are never printed, logged, or committed."""

import logging
import sys

import pytest

from agent.config import load_config
from agent.logging_setup import MASK, RedactingFormatter, redact, setup_logging
from helpers import make_project

KEY = "sk-test-0123456789abcdef"

HELIUS_SOURCE = """\
- name: helius
  ws_url: wss://example.invalid/?api-key=${HELIUS_API_KEY}
"""


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    monkeypatch.delenv("HELIUS_API_KEY", raising=False)
    monkeypatch.delenv("DATA_DIR", raising=False)
    monkeypatch.delenv("AGENT_HOME", raising=False)


@pytest.fixture
def reset_logging():
    yield
    root = logging.getLogger()
    for handler in root.handlers[:]:
        handler.close()
        root.removeHandler(handler)


def test_redact_known_secret_and_unknown_key_parameter():
    assert KEY not in redact(f"connect failed for {KEY}", [KEY])
    text = redact("GET wss://host/?api-key=abc123&x=1 and api_key=zzz", [])
    assert "abc123" not in text and "zzz" not in text
    assert f"api-key={MASK}&x=1" in text


def test_formatter_masks_message_arguments_and_tracebacks():
    formatter = RedactingFormatter("%(message)s", [KEY])
    logger = logging.getLogger("test.secrets")
    try:
        raise ConnectionError(f"could not open wss://host/?api-key={KEY}")
    except ConnectionError:
        record = logger.makeRecord(
            "test", logging.ERROR, __file__, 1, "failed with %s", (KEY,), sys.exc_info()
        )
        out = formatter.format(record)
    assert KEY not in out
    assert "ConnectionError" in out  # the traceback is still there, only the key is gone


def test_log_file_never_contains_the_key(tmp_path, reset_logging):
    log_file = tmp_path / "agentd.log"
    setup_logging(log_file, console=False, secrets=[KEY])
    logging.getLogger("x").error("url=wss://host/?api-key=%s raw=%s", KEY, KEY)
    for handler in logging.getLogger().handlers:
        handler.flush()
    content = log_file.read_text(encoding="utf-8")
    assert KEY not in content and MASK in content


def test_config_objects_do_not_show_the_key(tmp_path):
    make_project(tmp_path, env=f"HELIUS_API_KEY={KEY}\n", extra_sources=HELIUS_SOURCE)
    cfg = load_config(tmp_path)
    endpoints, skipped = cfg.endpoints()
    assert skipped == []
    helius = next(e for e in endpoints if e.name == "helius")
    assert helius.url.endswith(f"api-key={KEY}")  # the real URL is built for connecting
    for shown in (repr(cfg), str(cfg), repr(endpoints), repr(cfg.secrets), str(cfg.secrets)):
        assert KEY not in shown
    assert cfg.secret_values() == [KEY]


def test_source_needing_a_missing_key_is_skipped(tmp_path):
    make_project(tmp_path, env="HELIUS_API_KEY=\n", extra_sources=HELIUS_SOURCE)
    endpoints, skipped = load_config(tmp_path).endpoints()
    assert [e.name for e in endpoints] == ["test"]
    assert skipped == ["helius"]
