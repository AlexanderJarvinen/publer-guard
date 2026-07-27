"""
Tests for the two additions that close the point-7 (monitoring) and the
Gate-1 blind-spot gaps:

  1. Post-fix full re-lint catches a violation that a fix INTRODUCED on a
     different rule (Gate 1 only re-checks the same rule, so it can't).
  2. Per-call cost/latency metrics are recorded and rolled up into the report.
"""

from __future__ import annotations

import json

from src.agents import CriticAgent, FakeLLMClient, FixerAgent, TriageAgent
from src.cli import build_report
from src.orchestrator import Orchestrator
from src.state import CsvRow, Platform, PipelineState


def _orchestrator(fake: FakeLLMClient) -> Orchestrator:
    return Orchestrator(
        triage=TriageAgent(fake),
        fixer=FixerAgent(fake),
        critic=CriticAgent(fake),
    )


def test_relint_flags_violation_introduced_by_a_fix():
    # Original: a Twitter post over 280 chars whose CTA is a correct flat URL.
    long_text = (
        "Descend into Room 101 with us, where two plus two stops equaling four "
        "and the cold takes hold over and over again, verse after verse, until "
        "the tundra itself forgets the sound of warmth and the machinery grinds "
        "on through the endless frozen night without pause or mercy or end. "
        "IG: instagram.com/alex_y_yarvinen"
    )
    assert len(long_text) > 280

    row = CsvRow(
        row_index=0,
        platform=Platform.TWITTER,
        date="2026/06/24 18:00",
        text=long_text,
        label="promo",
    )

    # Fixer trims under 280 BUT rewrites the CTA as a markdown link, which is
    # illegal on Twitter (cta_format). Gate 1 only checks twitter_length, so it
    # accepts the edit. Only the full re-lint catches the introduced cta_format.
    bad_fix = "Room 101 awaits. [IG](https://instagram.com/alex_y_yarvinen)"
    assert len(bad_fix) <= 280

    fake = FakeLLMClient([
        json.dumps({
            "fix_order": [{"rule_id": "twitter_length", "row_index": 0}],
            "human_only": [],
        }),
        json.dumps({
            "action": "edit_text",
            "new_text": bad_fix,
            "lookup_hint": None,
            "reason": "Trimmed prose to fit the 280-char limit.",
        }),
    ])

    state = _orchestrator(fake).run(PipelineState(rows=[row]))
    report = build_report(state)
    pfl = report["post_fix_lint"]

    # The original twitter_length fix was "accepted" by the per-violation gates…
    assert any(
        a.rule_id == "twitter_length" and a.outcome.value == "fixed"
        for a in state.attempts
    )
    # …but the full re-lint is not clean: a new cta_format violation appeared.
    assert pfl["clean"] is False
    introduced_rules = {v["rule_id"] for v in pfl["introduced_by_fix"]}
    assert "cta_format" in introduced_rules
    # And it is correctly classified as introduced, not residual.
    assert "twitter_length" not in introduced_rules


def test_relint_is_clean_when_fix_introduces_nothing():
    row = CsvRow(
        row_index=0,
        platform=Platform.FACEBOOK,
        date="2026/06/24 18:00",
        text="Album out now.",
        link="https://ffm.to/letargin",   # link_empty fires
        label="promo",
    )
    fake = FakeLLMClient([
        json.dumps({
            "fix_order": [{"rule_id": "link_empty", "row_index": 0}],
            "human_only": [],
        }),
        json.dumps({
            "action": "clear_link",
            "new_text": None,
            "lookup_hint": None,
            "reason": "Clearing Link column.",
        }),
    ])
    state = _orchestrator(fake).run(PipelineState(rows=[row]))
    pfl = build_report(state)["post_fix_lint"]
    assert pfl["clean"] is True
    assert pfl["introduced_by_fix"] == []


def test_metrics_recorded_and_costed():
    row = CsvRow(
        row_index=0,
        platform=Platform.FACEBOOK,
        date="2026/06/24 18:00",
        text="Album out now.",
        link="https://ffm.to/letargin",
        label="promo",
    )
    fake = FakeLLMClient([
        json.dumps({
            "fix_order": [{"rule_id": "link_empty", "row_index": 0}],
            "human_only": [],
        }),
        json.dumps({
            "action": "clear_link",
            "new_text": None,
            "lookup_hint": None,
            "reason": "Clearing Link column.",
        }),
    ])
    state = _orchestrator(fake).run(PipelineState(rows=[row]))

    # Two LLM calls: triage + fixer (no critic on a first-try success).
    assert len(state.metrics) == 2
    assert all(m.estimated for m in state.metrics)

    mon = build_report(state)["monitoring"]
    assert mon["total_calls"] == 2
    assert mon["tokens_estimated"] is True
    assert mon["total_input_tokens"] > 0
    assert mon["est_cost_usd"] >= 0.0
    # Triage runs on Haiku, Fixer on Sonnet — both should appear, costed.
    assert TriageAgent.MODEL in mon["by_model"]
    assert FixerAgent.MODEL in mon["by_model"]


def test_clean_csv_records_no_metrics():
    # No violations ⇒ no triage, no fixer, no LLM calls at all.
    row = CsvRow(
        row_index=0,
        platform=Platform.FACEBOOK,
        date="2026/06/24 18:00",
        text="Album out now.",
        label="promo",
    )
    fake = FakeLLMClient([])
    state = _orchestrator(fake).run(PipelineState(rows=[row]))
    mon = build_report(state)["monitoring"]
    assert mon["total_calls"] == 0
    assert mon["est_cost_usd"] == 0.0
    assert build_report(state)["post_fix_lint"]["clean"] is True
