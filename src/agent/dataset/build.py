"""The research dataset (spec 15.3, step R2): one row per coin and cutoff (T+30/60/120/300 s)
with the coin's signals at the cutoff, the hand-written rules' verdict on them, and the
coin's labels.

- **Signals** come from the code the engine uses live (`agent.features`), replayed on the
  backfill only (spec 4.1, research track). They read events up to the cutoff only
  (spec 6); `tests/test_dataset.py` checks that on this builder.
- **Rules:** `rules_verdict` and `rules_reasons` are `config/rules.yaml` applied to the
  signals, as the engine does live: the first baseline (spec 15.4).
- **Labels** come from `agent labels` (`<data>/labels/`), rebuilt first if they are
  missing or were made with other settings or other backfilled data. Every label column
  starts with `label_`: labels look into the future and are never inputs.
- **Which rows to use:** `has_gap` is true when the backfill does not cover all the time
  from launch to the cutoff (the signals may miss events); `labels_complete` is false
  when the coin's labels are unknown. Rows to model on have neither.

Limits, to be named in the write-up:
- `dev_prior_launches` counts launches inside the backfill only, so it is too low for coins
  launched early in the data; `history_s` says how much backfill came before each coin.
- Signals use bonding-curve trades only, like the live engine: a coin that graduated
  before the cutoff (`completed`) has its pool trades in the labels but not the signals.
- Backfill-only fields (priority fee, position in the block; spec 4.1) are not features
  yet.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import duckdb
import pyarrow as pa

import agent
from agent.clock import stamp
from agent.features.replay import BACKFILL_ONLY, load_states
from agent.features.token_state import SNAPSHOT_OFFSETS_S
from agent.ingest.backfill_coverage import (
    backfill_epochs,
    backfill_fingerprint,
    backfill_spans,
)
from agent.ingest.recorder import write_parquet_atomic
from agent.labels.make_labels import COIN_LABELS, build_labels, load_label_settings
from agent.labels.make_labels import OUT_FOLDER as LABELS_FOLDER
from agent.models.rules import judge, load_rules

OUT_FOLDER = "dataset"
FILE_NAME = "dataset.parquet"
CHUNK_S = 3 * 3600  # coins are replayed in batches by launch time, to bound memory
LABEL_PREFIX = "label_"
ID_COLUMNS = (
    "mint", "creator", "quote_mint", "created_ts", "date", "hour_ts", "offset_s",
    "cutoff_ts", "history_s", "has_gap",
)  # fmt: skip
RULE_COLUMNS = ("rules_verdict", "rules_reasons")
# The code that decides what the dataset holds; its hash goes in the manifest.
SOURCES = ("dataset", "features", "labels", "models/rules.py", "sim/curve_math.py",
           "ingest/backfill_coverage.py")  # fmt: skip


@dataclass
class DatasetRun:
    path: Path
    manifest: dict
    labels_rebuilt: bool


def build_dataset(root: Path, data_dir: Path, *, progress=lambda _msg: None) -> DatasetRun:
    """Build `<data>/dataset/dataset.parquet` and its `manifest.json`. `root` is the
    project folder (for `config/` and the code version)."""
    started = time.time()
    spans = backfill_spans(data_dir)
    if not spans:
        raise FileNotFoundError("No backfilled data. Run `agent backfill --epoch <E>` first.")
    labels_rebuilt = _fresh_labels(root, data_dir, spans, progress)
    rules_path = root / "config" / "rules.yaml"
    rules = load_rules(rules_path)
    offsets = SNAPSHOT_OFFSETS_S
    first, last = spans[0][0], spans[-1][1]
    chunks: list[pa.Table] = []
    coins = 0
    for start in range(first, last + 1, CHUNK_S):
        replay = load_states(data_dir, start, start + CHUNK_S, max_offset_s=max(offsets),
                             roots=BACKFILL_ONLY)  # fmt: skip
        rows = []
        for state in sorted(replay.states, key=lambda s: (s.created_ts, s.mint)):
            for offset in offsets:
                cutoff = state.created_ts + offset
                signals = state.snapshot(cutoff, creators=replay.creators)
                verdict = judge(signals, rules)
                rows.append({
                    "mint": state.mint, "creator": state.creator,
                    "quote_mint": state.quote_mint, "created_ts": state.created_ts,
                    "date": _utc_date(state.created_ts),
                    "hour_ts": state.created_ts - state.created_ts % 3600,
                    "offset_s": offset, "cutoff_ts": cutoff,
                    "history_s": state.created_ts - first,
                    "has_gap": replay.has_gap(state, cutoff),
                    **{k: v for k, v in signals.items() if k not in ("mint", "cutoff_ts")},
                    "rules_verdict": verdict.level,
                    "rules_reasons": "; ".join(verdict.reasons),
                })  # fmt: skip
        coins += len(replay.states)
        if rows:
            chunks.append(pa.Table.from_pylist(rows))
        progress(f"signals for coins launched up to {stamp(min(start + CHUNK_S, last))}: "
                 f"{coins:,} coins")  # fmt: skip
    if not chunks:
        raise FileNotFoundError("The backfill holds no coins.")
    features = pa.concat_tables(chunks, promote_options="default")
    table = _join_labels(data_dir, features)
    folder = data_dir / OUT_FOLDER
    path = folder / FILE_NAME
    write_parquet_atomic(table, path)
    manifest = _manifest(root, data_dir, table, features, spans, rules_path,
                         time.time() - started, path)  # fmt: skip
    (folder / "manifest.json").write_text(json.dumps(manifest, indent=1), encoding="utf-8")
    return DatasetRun(path, manifest, labels_rebuilt)


def _fresh_labels(root: Path, data_dir: Path, spans: list, progress) -> bool:
    """Rebuild the labels if they are missing, or were made with other settings or from
    other backfilled data. True if they were rebuilt."""
    settings, sha = load_label_settings(root / "config" / "labels.yaml")
    folder = data_dir / LABELS_FOLDER
    try:
        made = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        made = {}
    current = (made.get("settings_sha") == sha
               and made.get("backfill_spans") == [list(s) for s in spans]
               and made.get("backfill_files") == backfill_fingerprint(data_dir)
               and (folder / "coins.parquet").exists()
               and (folder / "entries.parquet").exists())  # fmt: skip
    if current:
        return False
    progress("The labels are missing or out of date: building them first.")
    build_labels(data_dir, settings, sha, progress=progress)
    return True


def _join_labels(data_dir: Path, features: pa.Table) -> pa.Table:
    """Each row's coin labels and its entry labels (same decision time), all named
    `label_...`, in a fixed order: launch time, mint, cutoff."""
    folder = (data_dir / LABELS_FOLDER).as_posix()
    with duckdb.connect() as db:
        db.execute(f"SET temp_directory = '{(data_dir / 'duckdb_tmp').as_posix()}'")
        db.register("features", features)
        db.execute(f"CREATE TEMP VIEW coins AS SELECT * FROM '{folder}/coins.parquet'")
        db.execute(f"CREATE TEMP VIEW entries AS SELECT * FROM '{folder}/entries.parquet'")
        names = [d[0] for d in db.sql("SELECT * FROM entries LIMIT 0").description]
        # With no SOL-paired coin there are no entries, and the file has no columns.
        entry_columns = [c for c in names if c not in ("mint", "offset_s")]
        only = db.execute(
            "SELECT (SELECT count(*) FROM (SELECT DISTINCT mint FROM features) f "
            "  WHERE mint NOT IN (SELECT mint FROM coins)), "
            "(SELECT count(*) FROM coins WHERE mint NOT IN (SELECT mint FROM features))"
        ).fetchall()[0]
        if only != (0, 0):
            raise ValueError(f"Labels and signals disagree on which coins exist: {only[0]:,} "
                             f"only in the signals, {only[1]:,} only in the labels.")  # fmt: skip
        labels = ", ".join(
            [f"c.{c} AS {LABEL_PREFIX}{c}" for c in COIN_LABELS]
            + [f"e.{c} AS {LABEL_PREFIX}{c}" for c in entry_columns]
        )
        join = ("LEFT JOIN entries e ON e.mint = f.mint AND e.offset_s = f.offset_s "
                if "mint" in names else "")  # fmt: skip
        table = db.sql(
            f"SELECT f.*, c.complete AS labels_complete, {labels} FROM features f "
            f"JOIN coins c USING (mint) {join}"
            "ORDER BY f.created_ts, f.mint, f.offset_s"
        ).to_arrow_table()
    return table.combine_chunks()  # the same layout every time, so the same file bytes


def _manifest(root: Path, data_dir: Path, table: pa.Table, features: pa.Table,
              spans: list, rules_path: Path, seconds: float, path: Path) -> dict:  # fmt: skip
    names = table.column_names
    labels_made = json.loads(
        (data_dir / LABELS_FOLDER / "manifest.json").read_text(encoding="utf-8"))
    usable = sum(not gap and complete for gap, complete in zip(
        table.column("has_gap").to_pylist(), table.column("labels_complete").to_pylist(),
        strict=True))  # fmt: skip
    feature_columns = [c for c in features.column_names
                       if c not in ID_COLUMNS and c not in RULE_COLUMNS]  # fmt: skip
    return {
        "made_at": time.time(),
        "seconds": round(seconds, 1),
        "file": path.name,
        "file_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "rows": table.num_rows,
        "coins": len(set(table.column("mint").to_pylist())),
        "usable_rows": usable,
        "offsets_s": list(SNAPSHOT_OFFSETS_S),
        "source": "raw_backfill",
        "epochs": backfill_epochs(data_dir),
        "backfill_spans": spans,
        "backfill_files": backfill_fingerprint(data_dir),
        "code": _code_version(root),
        "rules_sha": hashlib.sha256(rules_path.read_bytes()).hexdigest()[:12],
        "labels_settings_sha": labels_made.get("settings_sha"),
        "labels_made_at": labels_made.get("made_at"),
        "columns": {
            "ids": list(ID_COLUMNS),
            "features": feature_columns,
            "rules": list(RULE_COLUMNS),
            "labels": ["labels_complete", *(c for c in names if c.startswith(LABEL_PREFIX))],
        },
    }


def _code_version(root: Path) -> dict:
    """The git commit, whether the code differs from it, and a hash of the files that
    decide the dataset's contents (meaningful even before a commit)."""
    package = Path(agent.__file__).parent
    files = []
    for source in SOURCES:
        target = package / source
        files += sorted(target.rglob("*.py")) if target.is_dir() else [target]
    digest = hashlib.sha256()
    for file in files:
        digest.update(file.relative_to(package).as_posix().encode())
        digest.update(file.read_bytes())
    status = _git(root, "status", "--porcelain", "--", "src", "config")
    return {
        "git_commit": _git(root, "rev-parse", "HEAD"),
        "uncommitted_changes": None if status is None else bool(status),
        "source_sha": digest.hexdigest()[:12],
    }


def _git(root: Path, *args: str) -> str | None:
    try:
        done = subprocess.run(["git", "-C", str(root), *args], capture_output=True,
                              text=True, timeout=20, check=True)  # fmt: skip
    except (OSError, subprocess.SubprocessError):
        return None
    return done.stdout.strip()


def _utc_date(ts: int) -> str:
    return datetime.fromtimestamp(ts, UTC).strftime("%Y-%m-%d")
