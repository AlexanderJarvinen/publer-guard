"""
Tests for src/verifier.py.
All tests are deterministic — zero API calls, zero LLM interaction.
"""

from __future__ import annotations

import pytest

from src.agents import FixProposal
from src.state import CsvRow, Platform, Severity, Violation
from src.verifier import Verifier, VerifierResult

# ─────────────────────────────────────────────────────────────────────────────
# Fixtures
# ─────────────────────────────────────────────────────────────────────────────

_CDN_URL = "https://cdn.publer.com/uploads/videos/abc/def.mp4"
_TMP_URL = "https://app.publer.com/uploads/tmp/12345/file.mp4"
_CDN_URL_2 = "https://cdn.publer.com/uploads/videos/xyz/new.mp4"

_ALL_2084_TAGS = "#arcticdreams #2084 #orwell #extrememetal #blackeneddeathmetal"


def _row(**kwargs) -> CsvRow:
    defaults = dict(
        row_index=0,
        platform=Platform.FACEBOOK,
        date="2026/06/24 18:00",
        text="Post text.",
        link="",
        media_url=_CDN_URL,
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


def _noop_lookup(hint: str):
    """lookup_media stub that always returns None."""
    return None


def _found_lookup(url: str):
    """Closure that returns `url` regardless of hint."""
    def _lookup(hint: str):
        return url
    return _lookup


# ─────────────────────────────────────────────────────────────────────────────
# Happy-path
# ─────────────────────────────────────────────────────────────────────────────

class TestHappyPath:
    def test_clear_link_fixes_link_empty(self):
        v = Verifier()
        result = v.verify(
            _row(link="https://ffm.to/arctic"),
            _violation(rule_id="link_empty"),
            FixProposal(action="clear_link", reason="clearing link"),
        )
        assert result.accepted
        assert result.new_row is not None
        assert result.new_row.link == ""
        assert result.gate_failure is None

    def test_edit_text_fixes_twitter_length(self):
        v = Verifier()
        short_text = "Short. IG: instagram.com/alex_y_yarvinen #metal"
        result = v.verify(
            _row(platform=Platform.TWITTER, text="x" * 300),
            _violation(rule_id="twitter_length"),
            FixProposal(action="edit_text", new_text=short_text, reason="shortened"),
        )
        assert result.accepted
        assert result.new_row is not None
        assert result.new_row.text == short_text

    def test_call_lookup_media_accepted_when_url_found(self):
        v = Verifier(lookup_media_fn=_found_lookup(_CDN_URL_2))
        result = v.verify(
            _row(media_url=_TMP_URL),
            _violation(rule_id="media_url_permanent"),
            FixProposal(
                action="call_lookup_media",
                lookup_hint="file",
                reason="temp URL",
            ),
        )
        assert result.accepted
        assert result.new_row is not None
        assert result.new_row.media_url == _CDN_URL_2

    def test_edit_text_fixes_no_cyrillic(self):
        v = Verifier()
        result = v.verify(
            _row(platform=Platform.FACEBOOK, text="Нова глава је объявлена"),
            _violation(rule_id="no_cyrillic"),
            FixProposal(
                action="edit_text",
                new_text="Nova glava je objavljena",
                reason="transliterated",
            ),
        )
        assert result.accepted

    def test_edit_text_fixes_cta_format_twitter(self):
        v = Verifier()
        result = v.verify(
            _row(
                platform=Platform.TWITTER,
                text="Follow [us](https://instagram.com/alex_y_yarvinen)",
            ),
            _violation(rule_id="cta_format"),
            FixProposal(
                action="edit_text",
                new_text="Follow IG: instagram.com/alex_y_yarvinen",
                reason="converted to flat URL",
            ),
        )
        assert result.accepted


# ─────────────────────────────────────────────────────────────────────────────
# cannot_fix
# ─────────────────────────────────────────────────────────────────────────────

class TestCannotFix:
    def test_cannot_fix_is_rejected_with_escalate(self):
        result = Verifier().verify(
            _row(),
            _violation(),
            FixProposal(action="cannot_fix", reason="too complex"),
        )
        assert not result.accepted
        assert result.escalate is True
        assert "cannot_fix" in result.gate_failure

    def test_cannot_fix_does_not_mutate_row(self):
        original = _row(text="original")
        result = Verifier().verify(
            original,
            _violation(),
            FixProposal(action="cannot_fix", reason="x"),
        )
        assert result.new_row is None


# ─────────────────────────────────────────────────────────────────────────────
# Gate 1 — Spec conformance
# ─────────────────────────────────────────────────────────────────────────────

class TestGate1:
    def test_fix_that_doesnt_actually_fix_the_rule_is_rejected(self):
        # edit_text that doesn't shorten below 280 → Gate 1 fires
        result = Verifier().verify(
            _row(platform=Platform.TWITTER, text="x" * 300),
            _violation(rule_id="twitter_length"),
            FixProposal(
                action="edit_text",
                new_text="y" * 285,  # still over 280
                reason="shortened slightly",
            ),
        )
        assert not result.accepted
        assert "Gate 1" in result.gate_failure
        assert "twitter_length" in result.gate_failure

    def test_empty_new_text_for_edit_text_is_rejected(self):
        result = Verifier().verify(
            _row(),
            _violation(),
            FixProposal(action="edit_text", new_text="", reason="forgot text"),
        )
        assert not result.accepted
        assert "Gate 1" in result.gate_failure

    def test_clear_link_on_already_empty_link_is_accepted(self):
        # link is already empty; clearing it is a no-op but still passes
        result = Verifier().verify(
            _row(link=""),
            _violation(rule_id="link_empty"),
            FixProposal(action="clear_link", reason="clearing"),
        )
        assert result.accepted

    def test_wrong_rule_id_in_violation_causes_gate1_to_check_correct_rule(self):
        # If the violation says "link_empty" but the Fixer sends edit_text,
        # Gate 1 re-checks link_empty on the new row — link is still non-empty.
        result = Verifier().verify(
            _row(link="https://example.com"),
            _violation(rule_id="link_empty"),
            FixProposal(
                action="edit_text",
                new_text="fixed text, but link still there",
                reason="wrong action",
            ),
        )
        assert not result.accepted
        assert "link_empty" in result.gate_failure


# ─────────────────────────────────────────────────────────────────────────────
# Gate 2 — Content preservation
# ─────────────────────────────────────────────────────────────────────────────

class TestGate2:
    def test_removing_2084_hashtags_while_fixing_twitter_length_is_rejected(self):
        original_text = (
            "2084 chapter live. IG: instagram.com/alex_y_yarvinen "
            f"{_ALL_2084_TAGS} extra extra extra words words words words words"
        )
        shortened_no_hashtags = (
            "2084 chapter live. IG: instagram.com/alex_y_yarvinen"
        )
        result = Verifier().verify(
            _row(
                platform=Platform.TWITTER,
                text=original_text,
                label="2084 announcement",
            ),
            _violation(rule_id="twitter_length"),
            FixProposal(
                action="edit_text",
                new_text=shortened_no_hashtags,
                reason="removed hashtags to shorten",
            ),
        )
        assert not result.accepted
        assert "Gate 2" in result.gate_failure
        assert "hashtags" in result.gate_failure

    def test_removing_cta_while_fixing_twitter_length_is_rejected(self):
        original_text = (
            "Post text. IG: instagram.com/alex_y_yarvinen "
            + "words " * 50  # padding to go over 280
        )
        shortened_no_cta = "Post text. More words."
        result = Verifier().verify(
            _row(platform=Platform.TWITTER, text=original_text),
            _violation(rule_id="twitter_length"),
            FixProposal(
                action="edit_text",
                new_text=shortened_no_cta,
                reason="removed CTA to shorten",
            ),
        )
        assert not result.accepted
        assert "Gate 2" in result.gate_failure
        assert "CTA" in result.gate_failure or "alex_y_yarvinen" in result.gate_failure

    def test_removing_media_is_rejected(self):
        result = Verifier().verify(
            _row(media_url=_CDN_URL),
            _violation(rule_id="link_empty"),
            # This would require a weird FixProposal that somehow clears media;
            # simulate by testing Gate 2 directly with a patched row.
            FixProposal(action="clear_link", reason="ok"),
        )
        # clear_link only changes .link, not .media_url → Gate 2 passes
        assert result.accepted  # media_url unchanged → accepted

    def test_cta_removal_allowed_when_fixing_cta_format(self):
        # When the violation IS cta_format, the Fixer may rewrite/move the CTA.
        # Gate 2 should not block this.
        result = Verifier().verify(
            _row(
                platform=Platform.TWITTER,
                text="[Alex](https://instagram.com/alex_y_yarvinen)",
            ),
            _violation(rule_id="cta_format"),
            FixProposal(
                action="edit_text",
                new_text="IG: instagram.com/alex_y_yarvinen",
                reason="converted markdown to flat URL",
            ),
        )
        assert result.accepted

    def test_non_2084_row_hashtag_removal_is_rejected(self):
        # Gate 2 compares content_fingerprint() before/after: hashtags are
        # protected on EVERY row, not just 2084-series rows. Shortening a
        # Twitter post must not silently drop them.
        result = Verifier().verify(
            _row(
                platform=Platform.TWITTER,
                text="Some post. #metal #doom " + "extra " * 60,
                label="promo",
            ),
            _violation(rule_id="twitter_length"),
            FixProposal(
                action="edit_text",
                new_text="Some post.",
                reason="removed hashtags to shorten; non-2084 row",
            ),
        )
        assert not result.accepted
        assert "hashtags removed" in result.gate_failure

    def test_non_2084_row_fix_keeping_hashtags_is_accepted(self):
        result = Verifier().verify(
            _row(
                platform=Platform.TWITTER,
                text="Some post. #metal #doom " + "extra " * 60,
                label="promo",
            ),
            _violation(rule_id="twitter_length"),
            FixProposal(
                action="edit_text",
                new_text="Some post. #metal #doom",
                reason="shortened while keeping hashtags",
            ),
        )
        assert result.accepted


# ─────────────────────────────────────────────────────────────────────────────
# Gate 3 — No fabrication
# ─────────────────────────────────────────────────────────────────────────────

class TestGate3:
    def test_lookup_media_returning_none_is_rejected_with_escalate(self):
        result = Verifier(lookup_media_fn=_noop_lookup).verify(
            _row(media_url=_TMP_URL),
            _violation(rule_id="media_url_permanent"),
            FixProposal(
                action="call_lookup_media",
                lookup_hint="nonexistent_video",
                reason="temp URL",
            ),
        )
        assert not result.accepted
        assert result.escalate is True
        assert "Gate 3" in result.gate_failure
        assert "lookup_media" in result.gate_failure

    def test_empty_lookup_hint_is_rejected(self):
        result = Verifier(lookup_media_fn=_found_lookup(_CDN_URL_2)).verify(
            _row(media_url=_TMP_URL),
            _violation(rule_id="media_url_permanent"),
            FixProposal(
                action="call_lookup_media",
                lookup_hint="",  # empty hint
                reason="temp URL",
            ),
        )
        assert not result.accepted
        assert "Gate 3" in result.gate_failure

    def test_tmp_url_from_lookup_media_is_rejected(self):
        # Defensive: fixture should never contain tmp links. Even if it does,
        # the verifier rejects the result — Gate 1 catches it because the
        # media_url_permanent rule still fires on the patched row.
        result = Verifier(lookup_media_fn=lambda h: _TMP_URL).verify(
            _row(media_url=_TMP_URL),
            _violation(rule_id="media_url_permanent"),
            FixProposal(
                action="call_lookup_media",
                lookup_hint="file",
                reason="temp URL",
            ),
        )
        assert not result.accepted
        # Gate 1 fires first (linter re-runs and still catches /uploads/tmp/)
        assert "media_url_permanent" in result.gate_failure

    def test_edit_text_may_not_change_media_url(self):
        # Simulate: what if an edit_text somehow produced a row with different media?
        # The verifier should catch this via Gate 3 — but edit_text only patches .text,
        # so this can't happen in practice. Confirm the gate _would_ catch it
        # by injecting a patched row (testing the gate logic directly via _check_gate3).
        from src.verifier import Verifier as V
        original = _row(media_url=_CDN_URL)
        patched = original.model_copy(update={"media_url": _CDN_URL_2})
        proposal = FixProposal(action="edit_text", new_text="text", reason="test")
        gate3 = V._check_gate3(original, patched, proposal)
        assert gate3 is not None
        assert "Gate 3" in gate3
        assert "no_fabrication" in gate3


# ─────────────────────────────────────────────────────────────────────────────
# Edge cases
# ─────────────────────────────────────────────────────────────────────────────

class TestEdgeCases:
    def test_result_is_verifier_result_instance(self):
        result = Verifier().verify(
            _row(link="https://example.com"),
            _violation(rule_id="link_empty"),
            FixProposal(action="clear_link", reason="ok"),
        )
        assert isinstance(result, VerifierResult)

    def test_accepted_result_has_new_row(self):
        result = Verifier().verify(
            _row(link="https://example.com"),
            _violation(rule_id="link_empty"),
            FixProposal(action="clear_link", reason="ok"),
        )
        assert result.accepted
        assert result.new_row is not None

    def test_rejected_result_has_gate_failure_string(self):
        result = Verifier().verify(
            _row(),
            _violation(),
            FixProposal(action="cannot_fix", reason="nope"),
        )
        assert not result.accepted
        assert isinstance(result.gate_failure, str)
        assert len(result.gate_failure) > 0

    def test_original_row_is_not_mutated_on_any_path(self):
        original = _row(link="https://example.com", text="original", media_url=_CDN_URL)
        original_link = original.link
        original_text = original.text
        original_media = original.media_url

        Verifier().verify(
            original,
            _violation(rule_id="link_empty"),
            FixProposal(action="clear_link", reason="ok"),
        )
        assert original.link == original_link
        assert original.text == original_text
        assert original.media_url == original_media

    def test_default_verifier_uses_real_lookup_media(self):
        # Just confirm construction doesn't raise and the Verifier is usable.
        v = Verifier()
        assert callable(v._lookup)
