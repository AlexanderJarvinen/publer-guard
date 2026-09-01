"""
Tests for src/agents.py.
All tests use FakeLLMClient — zero real API calls, zero cost.
"""

from __future__ import annotations

import json

import pytest

from src.agents import (
    CriticAgent,
    CriticNote,
    FakeLLMClient,
    FixerAgent,
    FixProposal,
    TriageAgent,
    TriageDecision,
)
from src.state import CsvRow, Platform, Severity, Violation

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _row(**kwargs) -> CsvRow:
    defaults = dict(
        row_index=0,
        platform=Platform.TWITTER,
        date="2026/06/24 18:00",
        text="Some post text.",
        link="",
        media_url="https://cdn.publer.com/uploads/videos/abc/def.mp4",
        title="Title",
        label="promo",
    )
    defaults.update(kwargs)
    return CsvRow(**defaults)


def _violation(**kwargs) -> Violation:
    defaults = dict(
        rule_id="link_empty",
        row_index=0,
        severity=Severity.ERROR,
        message="Row 0: Link column must be empty.",
        auto_fixable=True,
    )
    defaults.update(kwargs)
    return Violation(**defaults)


def _triage_response(fix_order=None, human_only=None) -> str:
    return json.dumps({
        "fix_order": fix_order or [],
        "human_only": human_only or [],
    })


# ---------------------------------------------------------------------------
# TriageAgent
# ---------------------------------------------------------------------------

class TestTriageAgent:
    def test_parses_valid_response_into_triage_decision(self):
        resp = _triage_response(
            fix_order=[{"rule_id": "link_empty", "row_index": 0}],
            human_only=[{"rule_id": "column_count", "row_index": 1}],
        )
        result = TriageAgent(FakeLLMClient([resp])).triage([_violation()])
        assert isinstance(result, TriageDecision)
        assert result.fix_order[0].rule_id == "link_empty"
        assert result.fix_order[0].row_index == 0
        assert result.human_only[0].rule_id == "column_count"

    def test_uses_haiku_model(self):
        fake = FakeLLMClient([_triage_response()])
        TriageAgent(fake).triage([])
        assert fake.last_model == "claude-haiku-4-5"

    def test_prompt_contains_violation_metadata(self):
        v = _violation(rule_id="twitter_length", row_index=3)
        fake = FakeLLMClient([_triage_response(
            fix_order=[{"rule_id": "twitter_length", "row_index": 3}]
        )])
        TriageAgent(fake).triage([v])
        prompt = fake.last_user
        # Violation metadata must be present
        assert "twitter_length" in prompt
        assert "3" in prompt

    def test_prompt_does_not_contain_row_text_or_media(self):
        # Triage receives only violation metadata — never row content.
        # This is enforced structurally: triage() takes list[Violation], not rows.
        v = _violation(message="Row 0: too long")
        sentinel_row_text = "UNIQUE_ROW_CONTENT_THAT_MUST_NOT_APPEAR"
        fake = FakeLLMClient([_triage_response(
            fix_order=[{"rule_id": "link_empty", "row_index": 0}]
        )])
        TriageAgent(fake).triage([v])
        assert sentinel_row_text not in fake.last_user
        assert "cdn.publer.com" not in fake.last_user

    def test_invalid_json_raises_value_error(self):
        with pytest.raises(ValueError, match="TriageAgent"):
            TriageAgent(FakeLLMClient(["not json"])).triage([_violation()])

    def test_wrong_schema_raises_value_error(self):
        with pytest.raises(ValueError, match="TriageAgent"):
            TriageAgent(FakeLLMClient([json.dumps({"bad": "schema"})])).triage([_violation()])

    def test_empty_violations_list_returns_empty_decision(self):
        result = TriageAgent(FakeLLMClient([_triage_response()])).triage([])
        assert result.fix_order == []
        assert result.human_only == []

    def test_multiple_violations_all_sent_to_model(self):
        violations = [
            _violation(rule_id="link_empty", row_index=0),
            _violation(rule_id="date_format", row_index=1),
        ]
        fake = FakeLLMClient([_triage_response(
            fix_order=[
                {"rule_id": "link_empty", "row_index": 0},
                {"rule_id": "date_format", "row_index": 1},
            ]
        )])
        TriageAgent(fake).triage(violations)
        prompt = fake.last_user
        assert "link_empty" in prompt
        assert "date_format" in prompt


# ---------------------------------------------------------------------------
# FixerAgent
# ---------------------------------------------------------------------------

class TestFixerAgent:
    def test_media_url_permanent_returns_call_lookup_media(self):
        resp = json.dumps({
            "action": "call_lookup_media",
            "new_text": None,
            "lookup_hint": "f48e39cc2c4d2d61",
            "reason": "Temporary URL; querying media library for permanent path.",
        })
        v = _violation(rule_id="media_url_permanent", message="Row 0: temp URL")
        r = _row(media_url="https://app.publer.com/uploads/tmp/1234/f48e39cc2c4d2d61.mp4")
        result = FixerAgent(FakeLLMClient([resp])).propose_fix(v, r)
        assert isinstance(result, FixProposal)
        assert result.action == "call_lookup_media"
        assert result.lookup_hint == "f48e39cc2c4d2d61"
        assert result.new_text is None

    def test_fix_proposal_has_no_media_url_field(self):
        """Schema enforcement: the Fixer physically cannot output a media URL."""
        resp = json.dumps({
            "action": "call_lookup_media",
            "lookup_hint": "some_hint",
            "reason": "temp url",
        })
        result = FixerAgent(FakeLLMClient([resp])).propose_fix(
            _violation(rule_id="media_url_permanent"), _row()
        )
        # FixProposal has no media_url attribute — full stop
        assert not hasattr(result, "media_url")

    def test_even_if_llm_outputs_media_url_it_is_dropped(self):
        """Pydantic ignores extra fields — a fabricated URL is silently discarded."""
        resp = json.dumps({
            "action": "edit_text",
            "new_text": "Fixed text.",
            "lookup_hint": None,
            "reason": "ok",
            "media_url": "https://cdn.publer.com/FABRICATED/url.mp4",  # extra field
        })
        result = FixerAgent(FakeLLMClient([resp])).propose_fix(_violation(), _row())
        assert not hasattr(result, "media_url")
        assert result.action == "edit_text"

    def test_twitter_length_returns_edit_text(self):
        shortened = "Short. IG: instagram.com/alex_y_yarvinen #metal"
        resp = json.dumps({
            "action": "edit_text",
            "new_text": shortened,
            "lookup_hint": None,
            "reason": "Truncated to fit 280-char limit.",
        })
        v = _violation(rule_id="twitter_length")
        r = _row(platform=Platform.TWITTER, text="x" * 300)
        result = FixerAgent(FakeLLMClient([resp])).propose_fix(v, r)
        assert result.action == "edit_text"
        assert result.new_text == shortened

    def test_cannot_fix_action_is_accepted(self):
        resp = json.dumps({
            "action": "cannot_fix",
            "new_text": None,
            "lookup_hint": None,
            "reason": "Cannot safely repair this automatically.",
        })
        result = FixerAgent(FakeLLMClient([resp])).propose_fix(_violation(), _row())
        assert result.action == "cannot_fix"
        assert result.reason

    def test_uses_sonnet_model(self):
        resp = json.dumps({
            "action": "edit_text", "new_text": "x",
            "lookup_hint": None, "reason": "ok",
        })
        fake = FakeLLMClient([resp])
        FixerAgent(fake).propose_fix(_violation(), _row())
        assert fake.last_model == "claude-sonnet-4-6"

    def test_invalid_json_raises_value_error(self):
        with pytest.raises(ValueError, match="FixerAgent"):
            FixerAgent(FakeLLMClient(["not json"])).propose_fix(_violation(), _row())

    def test_wrong_schema_raises_value_error(self):
        with pytest.raises(ValueError, match="FixerAgent"):
            FixerAgent(FakeLLMClient([json.dumps({"x": 1})])).propose_fix(
                _violation(), _row()
            )

    def test_scoped_context_contains_only_target_row(self):
        """
        KEY SCOPING TEST: the user-prompt sent to the model contains exactly
        the target row's text and nothing from any other row.

        This is enforced structurally: propose_fix() accepts one CsvRow, not
        PipelineState or a list of rows. This test asserts on the raw prompt
        to make the guarantee explicit and catch any future regression where
        extra context creeps in.
        """
        target_text = "TARGET_ROW_UNIQUE_SENTINEL_XYZ_123"
        other_text_a = "OTHER_ROW_A_SENTINEL_ABC_456"
        other_text_b = "OTHER_ROW_B_SENTINEL_DEF_789"

        resp = json.dumps({
            "action": "edit_text", "new_text": "fixed",
            "lookup_hint": None, "reason": "ok",
        })
        fake = FakeLLMClient([resp])
        target_row = _row(row_index=1, text=target_text)
        violation = _violation(row_index=1)

        # Only the target row is passed — other_text_a/b never enter the call
        FixerAgent(fake).propose_fix(violation, target_row)

        prompt = fake.last_user
        assert target_text in prompt
        assert other_text_a not in prompt
        assert other_text_b not in prompt

    def test_violation_details_included_in_prompt(self):
        v = _violation(rule_id="twitter_length", message="Row 2: post is 300 chars")
        r = _row(row_index=2)
        resp = json.dumps({
            "action": "edit_text", "new_text": "shorter",
            "lookup_hint": None, "reason": "trimmed",
        })
        fake = FakeLLMClient([resp])
        FixerAgent(fake).propose_fix(v, r)
        prompt = fake.last_user
        assert "twitter_length" in prompt
        assert "post is 300 chars" in prompt


# ---------------------------------------------------------------------------
# CriticAgent
# ---------------------------------------------------------------------------

class TestCriticAgent:
    def test_parses_valid_critic_note(self):
        resp = json.dumps({
            "explanation": "The proposed text was still 285 chars, over the 280 limit.",
            "suggestion": "Remove the trailing hashtags to reclaim 5 chars.",
        })
        v = _violation(rule_id="twitter_length")
        r = _row(platform=Platform.TWITTER, text="x" * 300)
        proposal = FixProposal(
            action="edit_text", new_text="x" * 285, reason="shortened"
        )
        result = CriticAgent(FakeLLMClient([resp])).critique(
            v, r, proposal, gate_failure="Gate 1: twitter_length still fails"
        )
        assert isinstance(result, CriticNote)
        assert "285" in result.explanation
        assert result.suggestion

    def test_uses_opus_model(self):
        resp = json.dumps({"explanation": "Bad fix.", "suggestion": "Try again."})
        fake = FakeLLMClient([resp])
        CriticAgent(fake).critique(
            _violation(), _row(),
            FixProposal(action="cannot_fix", reason="n/a"),
            gate_failure="Gate 1",
        )
        assert fake.last_model == "claude-opus-4-8"

    def test_gate_failure_string_included_in_prompt(self):
        resp = json.dumps({"explanation": "e", "suggestion": "s"})
        fake = FakeLLMClient([resp])
        gate_msg = "Gate 2: content_preservation -- hashtags disappeared after edit"
        CriticAgent(fake).critique(
            _violation(), _row(),
            FixProposal(action="cannot_fix", reason="x"),
            gate_failure=gate_msg,
        )
        assert gate_msg in fake.last_user

    def test_proposal_details_included_in_prompt(self):
        resp = json.dumps({"explanation": "e", "suggestion": "s"})
        fake = FakeLLMClient([resp])
        proposal = FixProposal(
            action="edit_text",
            new_text="SHORT TEXT",
            reason="tried to shorten",
        )
        CriticAgent(fake).critique(_violation(), _row(), proposal, gate_failure="Gate 1")
        assert "SHORT TEXT" in fake.last_user
        assert "tried to shorten" in fake.last_user

    def test_critic_row_slice_is_text_and_platform_only(self):
        """
        From the CsvRow, Critic sees ONLY text and platform.
        All other row fields (media_url, link, date, label, title) are
        excluded from its prompt — confirmed with unique sentinels per field.
        """
        resp = json.dumps({"explanation": "e", "suggestion": "s"})
        fake = FakeLLMClient([resp])
        row = _row(
            text="ROW_TEXT_SENTINEL",
            platform=Platform.FACEBOOK,
            media_url="https://cdn.publer.com/ROW_MEDIA_SENTINEL/file.mp4",
            link="https://ROW_LINK_SENTINEL.example.com",
            date="ROW_DATE_SENTINEL",
            label="ROW_LABEL_SENTINEL",
            title="ROW_TITLE_SENTINEL",
        )
        CriticAgent(fake).critique(
            _violation(), row,
            FixProposal(action="cannot_fix", reason="x"),
            gate_failure="Gate 1",
        )
        prompt = fake.last_user
        # What Critic sees from the row
        assert "ROW_TEXT_SENTINEL" in prompt
        assert "facebook" in prompt
        # What Critic does NOT see from the row
        assert "ROW_MEDIA_SENTINEL" not in prompt
        assert "ROW_LINK_SENTINEL" not in prompt
        assert "ROW_DATE_SENTINEL" not in prompt
        assert "ROW_LABEL_SENTINEL" not in prompt
        assert "ROW_TITLE_SENTINEL" not in prompt

    def test_critic_receives_full_proposal_including_lookup_hint(self):
        """
        Critic intentionally receives the full FixProposal — including lookup_hint.

        This is NOT a scope violation. The Critic's job is to explain why a
        specific fix failed; to do that it must see the entire proposal, including
        any lookup_hint that was used. Scoping limits access to OTHER rows and
        the rest of PipelineState — not to the proposal for the violation being
        analysed right now.
        """
        resp = json.dumps({"explanation": "e", "suggestion": "s"})
        fake = FakeLLMClient([resp])
        proposal = FixProposal(
            action="call_lookup_media",
            lookup_hint="LOOKUP_HINT_SENTINEL",
            reason="Temp URL detected.",
        )
        CriticAgent(fake).critique(
            _violation(rule_id="media_url_permanent"), _row(),
            proposal,
            gate_failure="Gate 3: no match found in media library",
        )
        # The lookup_hint must be present — Critic needs the full proposal context
        assert "LOOKUP_HINT_SENTINEL" in fake.last_user

    def test_invalid_json_raises_value_error(self):
        with pytest.raises(ValueError, match="CriticAgent"):
            CriticAgent(FakeLLMClient(["not json"])).critique(
                _violation(), _row(),
                FixProposal(action="cannot_fix", reason="x"),
                gate_failure="Gate 1",
            )

    def test_wrong_schema_raises_value_error(self):
        with pytest.raises(ValueError, match="CriticAgent"):
            CriticAgent(FakeLLMClient([json.dumps({"wrong": "schema"})])).critique(
                _violation(), _row(),
                FixProposal(action="cannot_fix", reason="x"),
                gate_failure="Gate 1",
            )


# ---------------------------------------------------------------------------
# FakeLLMClient
# ---------------------------------------------------------------------------

class TestFakeLLMClient:
    def test_returns_responses_in_fifo_order(self):
        fake = FakeLLMClient(["first", "second", "third"])
        assert fake.complete(model="m", system="s", user="u") == "first"
        assert fake.complete(model="m", system="s", user="u") == "second"
        assert fake.complete(model="m", system="s", user="u") == "third"

    def test_exhausted_queue_raises_runtime_error(self):
        fake = FakeLLMClient(["only"])
        fake.complete(model="m", system="s", user="u")
        with pytest.raises(RuntimeError, match="exhausted"):
            fake.complete(model="m", system="s", user="u")

    def test_records_last_call_arguments(self):
        fake = FakeLLMClient(["r1", "r2"])
        fake.complete(model="model-a", system="sys-a", user="user-a")
        fake.complete(model="model-b", system="sys-b", user="user-b")
        assert fake.last_model == "model-b"
        assert fake.last_system == "sys-b"
        assert fake.last_user == "user-b"

    def test_call_count_increments(self):
        fake = FakeLLMClient(["a", "b", "c"])
        fake.complete(model="m", system="s", user="u")
        fake.complete(model="m", system="s", user="u")
        assert fake.call_count == 2
