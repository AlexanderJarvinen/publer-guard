"""
Failure-handling tests (point 5): a sub-agent returns garbage instead of
clean JSON. The pipeline must NOT crash — it recovers, and the recovery is
visible in the trace and attempts.

Covers:
  - cosmetically dirty but valid JSON (code fences / preamble) still parses
  - Fixer returns unparseable output → caught, retried, recovers on next attempt
  - Triage returns unparseable output → deterministic fallback keeps the run alive
  - Critic returns unparseable output → degrades to retry without a note, no crash
"""

from __future__ import annotations

import json

from src.agents import (
    CriticAgent,
    FakeLLMClient,
    FixerAgent,
    TriageAgent,
    _extract_json,
)
from src.orchestrator import Orchestrator
from src.state import CsvRow, FixOutcome, Platform, PipelineState


def _orchestrator(fake: FakeLLMClient) -> Orchestrator:
    return Orchestrator(
        triage=TriageAgent(fake),
        fixer=FixerAgent(fake),
        critic=CriticAgent(fake),
    )


def _link_row() -> CsvRow:
    return CsvRow(
        row_index=0,
        platform=Platform.FACEBOOK,
        date="2026-06-24 18:00",
        text="Album out now.",
        link="https://ffm.to/letargin",   # link_empty fires (auto_fixable)
        label="promo",
    )


_TRIAGE_OK = json.dumps({
    "fix_order": [{"rule_id": "link_empty", "row_index": 0}],
    "human_only": [],
})
_CLEAR_LINK = json.dumps({
    "action": "clear_link", "new_text": None, "lookup_hint": None,
    "reason": "Clearing Link column.",
})


# ── _extract_json unit-level ─────────────────────────────────────────────────

def test_extract_json_strips_fences():
    fenced = "```json\n{\"a\": 1}\n```"
    assert json.loads(_extract_json(fenced)) == {"a": 1}


def test_extract_json_strips_preamble():
    noisy = 'Sure! Here is the JSON:\n{"a": 1}\nLet me know if you need more.'
    assert json.loads(_extract_json(noisy)) == {"a": 1}


# ── End-to-end recovery ──────────────────────────────────────────────────────

def test_fenced_responses_still_drive_a_successful_fix():
    fake = FakeLLMClient([
        "```json\n" + _TRIAGE_OK + "\n```",
        "```\n" + _CLEAR_LINK + "\n```",
    ])
    state = _orchestrator(fake).run(PipelineState(rows=[_link_row()]))
    assert state.row(0).link == ""
    final = [a for a in state.attempts if a.rule_id == "link_empty"][-1]
    assert final.outcome == FixOutcome.FIXED


def test_fixer_garbage_is_caught_and_retried():
    fake = FakeLLMClient([
        _TRIAGE_OK,
        "I'm sorry, I can't do that.",   # unparseable Fixer output → caught
        _CLEAR_LINK,                      # retry succeeds
    ])
    state = _orchestrator(fake).run(
        PipelineState(rows=[_link_row()], max_retries_per_violation=2)
    )
    # Recovered: link cleared on the 2nd attempt.
    assert state.row(0).link == ""
    final = [a for a in state.attempts if a.rule_id == "link_empty"][-1]
    assert final.outcome == FixOutcome.FIXED
    assert final.attempt_number == 2
    # The caught failure is recorded, not swallowed.
    assert any(a.gate_rejection == "unparseable_output" for a in state.attempts)


def test_triage_garbage_falls_back_to_deterministic():
    fake = FakeLLMClient([
        "not json at all — the model rambled",   # Triage fails → fallback
        _CLEAR_LINK,                              # Fixer still runs
    ])
    state = _orchestrator(fake).run(PipelineState(rows=[_link_row()]))
    assert state.row(0).link == ""
    assert any(
        "fell back to deterministic triage" in e.detail
        for e in state.trace if e.step == "triage"
    )


def test_critic_garbage_degrades_without_crashing():
    # Fixer attempt 1 is a valid-but-rejected proposal (clears link on a row
    # whose violation is link_empty — fine — so to force a Gate rejection we
    # instead send an edit_text that does NOT clear the link → Gate 1 fails),
    # then the Critic returns garbage, then Fixer attempt 2 succeeds.
    bad_proposal = json.dumps({
        "action": "edit_text", "new_text": "Album out now (edited).",
        "lookup_hint": None, "reason": "tried to edit text",
    })  # does not clear the link → link_empty still fails → Gate 1 rejects
    fake = FakeLLMClient([
        _TRIAGE_OK,
        bad_proposal,                 # attempt 1 → Gate 1 rejects
        "garbage critic response",    # Critic unparseable → degrade
        _CLEAR_LINK,                  # attempt 2 → succeeds
    ])
    state = _orchestrator(fake).run(
        PipelineState(rows=[_link_row()], max_retries_per_violation=2)
    )
    assert state.row(0).link == ""
    final = [a for a in state.attempts if a.rule_id == "link_empty"][-1]
    assert final.outcome == FixOutcome.FIXED
    assert any(
        "Critic output unparseable" in e.detail
        for e in state.trace if e.step == "critic"
    )
