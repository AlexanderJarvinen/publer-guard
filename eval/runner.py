"""
eval/runner.py — deterministic eval harness for publer-guard.

Runs 7 known CSV cases through the full pipeline using FakeLLMClient.
No API calls, no cost, fully reproducible.

Outputs:
  PASS / FAIL per case
  Total: N/7 passed
  First-attempt fix rate (the degradation metric to watch)

Run from the project root:
  python -m eval.runner
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

# Allow running as `python -m eval.runner` from project root
sys.path.insert(0, str(Path(__file__).parent.parent))

from src.agents import CriticAgent, FakeLLMClient, FixerAgent, TriageAgent
from src.cli import parse_csv
from src.orchestrator import Orchestrator
from src.state import FixOutcome, Platform, PipelineState
from src.verifier import Verifier

EVAL_DIR = Path(__file__).parent

_CDN_2084 = (
    "https://cdn.publer.com/uploads/videos/"
    "6a3711e930d9b2bc52422eff/"
    "84791791ca0c92f1acea4d46a9ead09b.mp4"
)


# ─────────────────────────────────────────────────────────────────────────────
# CSV loader (no platform column — all rows share one platform)
# ─────────────────────────────────────────────────────────────────────────────

def _load_state(name: str, platform: Platform, **kwargs) -> PipelineState:
    """Load a canonical 12-column Publer-template CSV through the production
    parser — the eval exercises the same code path as the CLI and web app."""
    _header, raw_rows, rows = parse_csv(EVAL_DIR / name, platform)
    return PipelineState(rows=rows, raw_rows=raw_rows, **kwargs)


# ─────────────────────────────────────────────────────────────────────────────
# Assertion helpers
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class Expectation:
    total_violations: int
    fixed: int
    escalated: int
    unfixed: int
    first_attempt_fixed: int  # violations fixed on attempt 1 (for rate metric)


def _final_attempts(state: PipelineState) -> dict[tuple[str, int], object]:
    """Return the last FixAttempt per (rule_id, row_index) — the final outcome."""
    final: dict = {}
    for a in state.attempts:
        final[(a.rule_id, a.row_index)] = a
    return final


def _check(state: PipelineState, exp: Expectation) -> list[str]:
    """Returns a list of failure strings, empty if everything matches."""
    failures: list[str] = []

    final = _final_attempts(state)

    def _count(outcome: FixOutcome) -> int:
        return sum(1 for a in final.values() if a.outcome == outcome)

    def _first_attempt_fixed() -> int:
        # Among violations that ended up FIXED, how many were fixed on attempt 1?
        return sum(
            1 for a in final.values()
            if a.outcome == FixOutcome.FIXED and a.attempt_number == 1
        )

    actual_violations = len(state.violations)
    actual_fixed = _count(FixOutcome.FIXED)
    actual_escalated = _count(FixOutcome.ESCALATED)
    actual_unfixed = _count(FixOutcome.UNFIXED)
    actual_first_attempt = _first_attempt_fixed()

    if actual_violations != exp.total_violations:
        failures.append(
            f"violations: expected {exp.total_violations}, got {actual_violations}"
        )
    if actual_fixed != exp.fixed:
        failures.append(f"fixed: expected {exp.fixed}, got {actual_fixed}")
    if actual_escalated != exp.escalated:
        failures.append(f"escalated: expected {exp.escalated}, got {actual_escalated}")
    if actual_unfixed != exp.unfixed:
        failures.append(f"unfixed: expected {exp.unfixed}, got {actual_unfixed}")
    if actual_first_attempt != exp.first_attempt_fixed:
        failures.append(
            f"first-attempt fixes: expected {exp.first_attempt_fixed}, "
            f"got {actual_first_attempt}"
        )
    return failures


# ─────────────────────────────────────────────────────────────────────────────
# Test cases
# ─────────────────────────────────────────────────────────────────────────────

def _triage_resp(fix_order=None, human_only=None) -> str:
    return json.dumps({
        "fix_order": fix_order or [],
        "human_only": human_only or [],
    })


def _fixer_edit_text(new_text: str, reason: str = "fixed") -> str:
    return json.dumps({
        "action": "edit_text",
        "new_text": new_text,
        "lookup_hint": None,
        "reason": reason,
    })


def _fixer_lookup_media(hint: str) -> str:
    return json.dumps({
        "action": "call_lookup_media",
        "new_text": None,
        "lookup_hint": hint,
        "reason": "Temporary media URL; querying library for permanent path.",
    })


def _critic_resp(explanation: str = "Fix failed.", suggestion: str = "Try again.") -> str:
    return json.dumps({"explanation": explanation, "suggestion": suggestion})


# ── Case 01: clean CSV — no violations, no LLM calls ─────────────────────────

def case_01_clean() -> tuple[PipelineState, Expectation]:
    fake = FakeLLMClient([])  # no calls expected
    orch = Orchestrator(
        triage=TriageAgent(fake),
        fixer=FixerAgent(fake),
        critic=CriticAgent(fake),
    )
    state = orch.run(_load_state("01_clean.csv", Platform.TWITTER))
    exp = Expectation(
        total_violations=0,
        fixed=0, escalated=0, unfixed=0,
        first_attempt_fixed=0,
    )
    return state, exp


# ── Case 02a: link_empty — non-empty Link column cleared first try ───────────
#
# The original Publer bug this project exists for: a URL in the Link column
# triggers "Invalid URL attached" on import. The only legal fix is clearing it.

def case_02_link_clear() -> tuple[PipelineState, Expectation]:
    llm_responses = [
        _triage_resp(fix_order=[{"rule_id": "link_empty", "row_index": 0}]),
        json.dumps({
            "action": "clear_link",
            "new_text": None,
            "lookup_hint": None,
            "reason": "Link column must be empty; clearing it.",
        }),
    ]
    fake = FakeLLMClient(llm_responses)
    orch = Orchestrator(
        triage=TriageAgent(fake),
        fixer=FixerAgent(fake),
        critic=CriticAgent(fake),
    )
    state = orch.run(_load_state("02_link_clear.csv", Platform.FACEBOOK))

    fixed_row = state.row(0)
    assert fixed_row.link == "", f"Expected cleared link, got {fixed_row.link!r}"

    exp = Expectation(
        total_violations=1,
        fixed=1, escalated=0, unfixed=0,
        first_attempt_fixed=1,
    )
    return state, exp


# ── Case 02b: no_cyrillic — Russian text on Facebook, fixed first try ────────

def case_02_no_cyrillic() -> tuple[PipelineState, Expectation]:
    transliterated = (
        "Novaya glava uzhe vyshla. IG: instagram.com/alex_y_yarvinen\n\n#metal"
    )
    llm_responses = [
        _triage_resp(fix_order=[{"rule_id": "no_cyrillic", "row_index": 0}]),
        _fixer_edit_text(transliterated, "Transliterated Cyrillic to Latin script."),
    ]
    fake = FakeLLMClient(llm_responses)
    orch = Orchestrator(
        triage=TriageAgent(fake),
        fixer=FixerAgent(fake),
        critic=CriticAgent(fake),
    )
    state = orch.run(_load_state("02_no_cyrillic.csv", Platform.FACEBOOK))
    exp = Expectation(
        total_violations=1,
        fixed=1, escalated=0, unfixed=0,
        first_attempt_fixed=1,
    )
    return state, exp


# ── Case 03: media_url_permanent — resolved via lookup_media ─────────────────

def case_03_tmp_url_found() -> tuple[PipelineState, Expectation]:
    llm_responses = [
        _triage_resp(fix_order=[{"rule_id": "media_url_permanent", "row_index": 0}]),
        _fixer_lookup_media("2084_part2_room101"),
    ]
    fake = FakeLLMClient(llm_responses)
    orch = Orchestrator(
        triage=TriageAgent(fake),
        fixer=FixerAgent(fake),
        critic=CriticAgent(fake),
    )
    state = orch.run(_load_state("03_tmp_url_found.csv", Platform.FACEBOOK))

    # Also verify the permanent URL was correctly applied
    fixed_row = state.row(0)
    assert fixed_row.media_url == _CDN_2084, (
        f"Expected permanent URL {_CDN_2084!r}, got {fixed_row.media_url!r}"
    )

    exp = Expectation(
        total_violations=1,
        fixed=1, escalated=0, unfixed=0,
        first_attempt_fixed=1,
    )
    return state, exp


# ── Case 04: twitter_length — fixed first try ────────────────────────────────

def case_04_twitter_length() -> tuple[PipelineState, Expectation]:
    shortened = (
        "New 2084 chapter is out — the frozen tundra echoes with machinery and ice. "
        "Room 101 awaits. Two plus two never equals four. Descend with us. "
        "IG: instagram.com/alex_y_yarvinen #extrememetal"
    )
    assert len(shortened) <= 280, f"Shortened text is {len(shortened)} chars — over limit!"

    llm_responses = [
        _triage_resp(fix_order=[{"rule_id": "twitter_length", "row_index": 0}]),
        _fixer_edit_text(shortened, "Trimmed prose to fit 280-char Twitter limit."),
    ]
    fake = FakeLLMClient(llm_responses)
    orch = Orchestrator(
        triage=TriageAgent(fake),
        fixer=FixerAgent(fake),
        critic=CriticAgent(fake),
    )
    state = orch.run(_load_state("04_twitter_length.csv", Platform.TWITTER))
    exp = Expectation(
        total_violations=1,
        fixed=1, escalated=0, unfixed=0,
        first_attempt_fixed=1,
    )
    return state, exp


# ── Case 05: twitter_length — fixed on 2nd attempt via Critic ────────────────
#
# Demonstrates the Critic path:
#   Attempt 1: Fixer shortens the post but drops #arcticdreams → Gate 2 rejects
#   Critic:    "Keep every hashtag; trim the prose instead"
#   Attempt 2: Fixer shortens AND keeps all hashtags + CTA → accepted

def case_05_length_critic() -> tuple[PipelineState, Expectation]:
    # Attempt 1: Fixer trims by sacrificing a hashtag — Gate 2 will reject
    bad_fix = (
        "New chapter is out now. Room 101 awaits. Descend with us. "
        "IG: instagram.com/alex_y_yarvinen #extrememetal"
    )
    # Attempt 2: Fixer trims the prose, keeps every hashtag and the CTA
    good_fix = (
        "New chapter is out now. Room 101 awaits. Descend with us. "
        "IG: instagram.com/alex_y_yarvinen #arcticdreams #extrememetal"
    )
    assert len(good_fix) <= 280, f"good_fix is {len(good_fix)} chars — over limit!"

    llm_responses = [
        # Triage
        _triage_resp(fix_order=[{"rule_id": "twitter_length", "row_index": 0}]),
        # Fixer attempt 1 (bad — dropped #arcticdreams)
        _fixer_edit_text(bad_fix, "Trimmed to fit 280 chars."),
        # Critic (called after Gate 2 rejects attempt 1)
        _critic_resp(
            explanation=(
                "The fix removed '#arcticdreams' from the text. Gate 2 "
                "(content_preservation) requires every original hashtag to remain."
            ),
            suggestion=(
                "Shorten the prose instead. Keep '#arcticdreams #extrememetal' "
                "and the CTA at the end."
            ),
        ),
        # Fixer attempt 2 (good — everything kept)
        _fixer_edit_text(good_fix, "Trimmed prose while keeping hashtags and CTA."),
    ]
    fake = FakeLLMClient(llm_responses)
    orch = Orchestrator(
        triage=TriageAgent(fake),
        fixer=FixerAgent(fake),
        critic=CriticAgent(fake),
    )
    state = orch.run(
        _load_state("05_length_critic.csv", Platform.TWITTER,
                    max_retries_per_violation=2)
    )
    exp = Expectation(
        total_violations=1,
        fixed=1, escalated=0, unfixed=0,
        first_attempt_fixed=0,  # fixed on 2nd attempt
    )
    return state, exp


# ── Case 06: lookup_media returns None — escalated to human ──────────────────

def case_06_lookup_not_found() -> tuple[PipelineState, Expectation]:
    llm_responses = [
        _triage_resp(fix_order=[{"rule_id": "media_url_permanent", "row_index": 0}]),
        _fixer_lookup_media("completely_unknown_video_xyz"),
    ]
    fake = FakeLLMClient(llm_responses)
    orch = Orchestrator(
        triage=TriageAgent(fake),
        fixer=FixerAgent(fake),
        critic=CriticAgent(fake),
    )
    state = orch.run(_load_state("06_lookup_not_found.csv", Platform.FACEBOOK))
    exp = Expectation(
        total_violations=1,
        fixed=0, escalated=1, unfixed=0,
        first_attempt_fixed=0,
    )
    return state, exp


# ─────────────────────────────────────────────────────────────────────────────
# Runner
# ─────────────────────────────────────────────────────────────────────────────

CASES = [
    ("01_clean",              case_01_clean,         "0 violations - clean pass"),
    ("02_link_clear",         case_02_link_clear,    "link_empty cleared first try"),
    ("02_no_cyrillic",        case_02_no_cyrillic,   "no_cyrillic fixed first try"),
    ("03_tmp_url_found",      case_03_tmp_url_found, "media_url_permanent fixed via lookup_media"),
    ("04_twitter_length",     case_04_twitter_length,"twitter_length fixed first try"),
    ("05_length_critic",      case_05_length_critic, "twitter_length fixed on 2nd attempt (Critic path)"),
    ("06_lookup_not_found",   case_06_lookup_not_found,"media_url_permanent escalated (lookup=None)"),
]


def _first_attempt_rate(states: list[PipelineState]) -> float:
    first = sum(
        1 for s in states
        for a in s.attempts
        if a.outcome == FixOutcome.FIXED and a.attempt_number == 1
    )
    fixable = sum(
        1 for s in states
        for v in s.violations
        if v.auto_fixable
    )
    return first / fixable if fixable else 1.0


def main() -> None:
    print("=" * 60)
    print("publer-guard eval harness")
    print("=" * 60)

    passed = 0
    failed = 0
    all_states: list[PipelineState] = []

    for name, fn, description in CASES:
        try:
            state, exp = fn()
            failures = _check(state, exp)
            all_states.append(state)
            if failures:
                print(f"  FAIL  {name}: {description}")
                for f in failures:
                    print(f"        FAIL: {f}")
                failed += 1
            else:
                print(f"  PASS  {name}: {description}")
                passed += 1
        except Exception as exc:
            print(f"  ERROR {name}: {description}")
            print(f"        exception: {exc}")
            failed += 1

    rate = _first_attempt_rate(all_states)
    print()
    print("=" * 60)
    print(f"Results: {passed}/{len(CASES)} passed, {failed} failed")
    print(f"First-attempt fix rate: {rate:.0%}")
    print("=" * 60)

    sys.exit(0 if failed == 0 else 1)


if __name__ == "__main__":
    main()
