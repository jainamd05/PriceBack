import json

import pytest

from app import llm
from app.policy import PolicyError, compile_policy


def test_rules_parse_standard_sentence():
    r = compile_policy("Refund the difference if the price drops within 14 days, up to $20 per order and $100 per day.")
    assert r["source"] == "rules"
    assert r["policy"]["window_days"] == 14
    assert r["policy"]["max_refund_per_order"] == 20
    assert r["policy"]["daily_budget"] == 100
    assert r["notes"] == []


def test_weeks_and_default_daily_budget_is_flagged():
    r = compile_policy("Within 2 weeks we refund any price drop, max $15 per order")
    assert r["policy"]["window_days"] == 14
    assert r["policy"]["daily_budget"] == 100  # DEFAULT_DAILY_BUDGET
    assert any("daily budget" in n.lower() for n in r["notes"])


def test_min_drop_threshold():
    r = compile_policy("14 days, max $20 per order, $50 per day, only for drops of at least 5%")
    assert r["policy"]["min_drop_pct"] == 5


def test_missing_cap_is_rejected_not_guessed():
    with pytest.raises(PolicyError, match="maximum refund"):
        compile_policy("Refund price drops within 14 days")


def test_missing_window_is_rejected():
    with pytest.raises(PolicyError, match="how long"):
        compile_policy("Refund price drops, up to $20 per order")


def test_out_of_range_values_rejected():
    with pytest.raises(PolicyError):
        compile_policy("Refund within 10 days, up to $5000 per order")


def test_empty_rejected():
    with pytest.raises(PolicyError):
        compile_policy("   ")


def test_llm_path_used_when_valid(monkeypatch):
    out = json.dumps({"window_days": 30, "max_refund_per_order": 25, "daily_budget": 200, "min_drop_pct": None})
    monkeypatch.setattr(llm, "complete", lambda *a, **k: f"```json\n{out}\n```")
    r = compile_policy("anything")
    assert r["source"] == "llm" and r["policy"]["window_days"] == 30


def test_llm_garbage_falls_back_to_rules(monkeypatch):
    monkeypatch.setattr(llm, "complete", lambda *a, **k: "sorry, I can't")
    r = compile_policy("within 7 days, up to $10 per order, $40 per day")
    assert r["source"] == "rules" and r["policy"]["max_refund_per_order"] == 10


def test_llm_invalid_values_fall_back_with_note(monkeypatch):
    bad = json.dumps({"window_days": 14, "max_refund_per_order": None, "daily_budget": 50})
    monkeypatch.setattr(llm, "complete", lambda *a, **k: bad)
    r = compile_policy("within 14 days, up to $20 per order, $50 per day")
    assert r["source"] == "rules"
    assert any("rejected" in n for n in r["notes"])
