"""The hand-written rules filter (spec 9 baseline) and its config file."""

from __future__ import annotations

from pathlib import Path

import pytest

from agent.models.rules import PASS, RulesError, judge, load_rules, parse_rules
from test_token_state import T0, launch, state_with

RULES_FILE = Path(__file__).resolve().parents[1] / "config" / "rules.yaml"


def test_the_shipped_rules_load_and_name_real_signals():
    rules = load_rules(RULES_FILE)
    assert {r.level for r in rules} == {"avoid", "caution"}
    snapshot = state_with(launch().rows).snapshot(T0 + 60)
    for rule in rules:
        assert rule.feature in snapshot, rule.feature
        rule.fires(snapshot)  # no error
        if snapshot[rule.feature] is not None:
            rule.explain(snapshot)  # the reason's format fits the value


def snap(**values):
    return {"token_age_s": 60, "a": 1.0, "b": 5.0, "flag": False, "missing": None, **values}


RULES = parse_rules({
    "avoid": [{"feature": "a", "at_least": 10, "reason": "a is {value:.0f}"}],
    "caution": [
        {"feature": "b", "below": 3, "reason": "b only {value:.0f}"},
        {"feature": "flag", "is": True, "reason": "flagged"},
        {"feature": "missing", "at_least": 0, "reason": "never"},
        {"feature": "b", "at_least": 100, "min_age_s": 120, "reason": "late b"},
    ],
})  # fmt: skip


def test_levels_and_reasons():
    assert judge(snap(), RULES).level == PASS and judge(snap(), RULES).reasons == []
    v = judge(snap(b=1.0), RULES)
    assert v.level == "caution" and v.reasons == ["b only 1"]
    v = judge(snap(a=12.0, b=1.0, flag=True), RULES)
    assert v.level == "avoid" and v.reasons == ["a is 12", "b only 1", "flagged"]
    assert [r.feature for r in v.fired] == ["a", "b", "flag"]
    assert v.fired[0].summary == "a is ..."


def test_missing_values_and_young_tokens_do_not_fire():
    assert judge(snap(missing=None), RULES).level == PASS
    assert judge(snap(b=500.0), RULES).level == PASS  # too young for "late b"
    assert judge(snap(b=500.0, token_age_s=120), RULES).reasons == ["late b"]


def test_a_rule_on_an_unknown_signal_is_an_error():
    rules = parse_rules({"avoid": [{"feature": "nope", "at_least": 1, "reason": "x"}]})
    with pytest.raises(RulesError, match="unknown signal"):
        judge(snap(), rules)


@pytest.mark.parametrize("config", [
    {"avoid": [{"feature": "a", "reason": "x"}]},  # no test
    {"avoid": [{"feature": "a", "at_least": 1, "below": 2, "reason": "x"}]},  # two tests
    {"avoid": [{"feature": "a", "at_least": "1", "reason": "x"}]},  # not a number
    {"avoid": [{"feature": "a", "is": 1, "reason": "x"}]},  # not true/false
    {"avoid": [{"at_least": 1, "reason": "x"}]},  # no signal
    {"avoid": [{"feature": "a", "at_least": 1}]},  # no reason
    {"block": []},  # unknown section
])  # fmt: skip
def test_bad_rules_are_refused(config):
    with pytest.raises(RulesError):
        parse_rules(config)
