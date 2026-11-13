"""The hand-written rules filter: the baseline every model must beat (spec 9).

The rules live in `config/rules.yaml`. Each reads one signal from a snapshot
(`features/token_state.py`) and fires when the signal crosses its threshold. A token's
verdict is "avoid" if any avoid rule fires, else "caution" if any caution rule fires, else
"pass". Its reasons say, in plain words, which rules fired.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

LEVELS = ("avoid", "caution")  # checked in this order
PASS = "pass"
_TESTS = ("at_least", "below", "is")


class RulesError(ValueError):
    pass


@dataclass(frozen=True)
class Rule:
    level: str
    feature: str
    reason: str
    test: str  # at_least | below | is
    value: float | bool
    min_age_s: int = 0

    def fires(self, snapshot: dict) -> bool:
        if self.feature not in snapshot:
            raise RulesError(f"rules.yaml: unknown signal {self.feature!r}")
        value = snapshot[self.feature]
        if value is None or snapshot["token_age_s"] < self.min_age_s:
            return False
        if self.test == "is":
            return bool(value) is self.value
        if self.test == "at_least":
            return value >= self.value
        return value < self.value

    def explain(self, snapshot: dict) -> str:
        return self.reason.format(value=snapshot[self.feature])

    @property
    def summary(self) -> str:
        """The reason with its number left out, e.g. "top 10 wallets hold ...%"."""
        return re.sub(r"\{value[^}]*\}", "...", self.reason)


@dataclass
class Verdict:
    level: str
    reasons: list[str] = field(default_factory=list)
    fired: list[Rule] = field(default_factory=list)  # the rules behind `reasons`, same order


def parse_rules(config: dict) -> list[Rule]:
    rules = []
    for level in LEVELS:
        for i, item in enumerate(config.get(level) or []):
            where = f"rules.yaml {level}[{i}]"
            tests = [t for t in _TESTS if t in item]
            if len(tests) != 1:
                raise RulesError(f"{where}: needs exactly one of {', '.join(_TESTS)}")
            if not item.get("feature") or not item.get("reason"):
                raise RulesError(f"{where}: needs a feature and a reason")
            test = tests[0]
            value = item[test]
            if test == "is" and not isinstance(value, bool):
                raise RulesError(f"{where}: 'is' takes true or false")
            if test != "is" and (isinstance(value, bool) or not isinstance(value, int | float)):
                raise RulesError(f"{where}: '{test}' takes a number")
            rules.append(Rule(level, item["feature"], item["reason"], test, value,
                              int(item.get("min_age_s", 0))))  # fmt: skip
    unknown = set(config) - set(LEVELS)
    if unknown:
        raise RulesError(f"rules.yaml: unknown section(s) {sorted(unknown)}")
    return rules


def load_rules(path: Path) -> list[Rule]:
    with open(path, encoding="utf-8") as fh:
        return parse_rules(yaml.safe_load(fh) or {})


def judge(snapshot: dict, rules: list[Rule]) -> Verdict:
    """The verdict at a snapshot. An "avoid" token also lists its caution reasons, after
    the avoid ones."""
    fired = [r for r in rules if r.fires(snapshot)]
    for level in LEVELS:
        if any(r.level == level for r in fired):
            ordered = [r for lv in LEVELS[LEVELS.index(level):] for r in fired if r.level == lv]
            return Verdict(level, [r.explain(snapshot) for r in ordered], ordered)
    return Verdict(PASS)
