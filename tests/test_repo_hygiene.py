"""Hard rule: secrets, wallet lists and data never reach git."""

import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
MUST_BE_IGNORED = [
    ".env",
    "config/trusted_wallets.yaml",
    "config/trusted_wallet_notes.csv",
    "data/raw/trades.parquet",
]


def _git(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=REPO, capture_output=True, text=True)


requires_git = pytest.mark.skipif(
    shutil.which("git") is None or not (REPO / ".git").exists(), reason="not a git checkout"
)


@requires_git
@pytest.mark.parametrize("path", MUST_BE_IGNORED)
def test_private_paths_are_git_ignored(path):
    assert _git("check-ignore", "-q", path).returncode == 0, f"{path} is not git-ignored"


@requires_git
def test_env_file_is_not_tracked():
    assert _git("ls-files", "--error-unmatch", ".env").returncode != 0


def test_env_example_contains_no_values():
    for line in (REPO / ".env.example").read_text(encoding="utf-8").splitlines():
        if line.strip() and not line.lstrip().startswith("#"):
            name, _, value = line.partition("=")
            assert value.strip() == "", f"{name} has a value in .env.example"
