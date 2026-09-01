"""
verifier.py — deterministic post-fix checker for publer-guard.

Applies a FixProposal to create a patched row, then runs three anti-hallucination gates:
  Gate 1 — Spec conformance:    the target rule must no longer fail.
  Gate 2 — Content preservation: required elements (hashtags, CTA, media) must remain.
  Gate 3 — No fabrication:      media URL may only change via a lookup_media tool call.

PRINCIPLE: LLMs PROPOSE. This module DECIDES.
No LLM call is ever made here. The verdict is purely mechanical.
"""

from __future__ import annotations

from typing import Callable, Optional

from pydantic import BaseModel

from .agents import FixProposal
from .linter import _CTA_HANDLE, lint_row
from .state import CsvRow, Violation


class VerifierResult(BaseModel):
    accepted: bool
    gate_failure: Optional[str] = None
    new_row: Optional[CsvRow] = None
    # True when the failure means "escalate to human" rather than "retry with Critic"
    escalate: bool = False


class Verifier:
    """
    Deterministic verifier. Accepts one original row, one violation, and one FixProposal.
    Returns VerifierResult(accepted=True, new_row=...) on success, or
    VerifierResult(accepted=False, gate_failure=...) on rejection.

    escalate=True signals the orchestrator to skip Critic/retry and hand off to a human.
    """

    def __init__(
        self, lookup_media_fn: Optional[Callable[[str], Optional[str]]] = None
    ) -> None:
        if lookup_media_fn is not None:
            self._lookup = lookup_media_fn
        else:
            from .tools import lookup_media as _default
            self._lookup = _default

    def verify(
        self,
        original_row: CsvRow,
        violation: Violation,
        proposal: FixProposal,
    ) -> VerifierResult:
        # ── 0. Build the new row ─────────────────────────────────────────────
        if proposal.action == "cannot_fix":
            return VerifierResult(
                accepted=False,
                gate_failure="Fixer returned cannot_fix — escalate to human",
                escalate=True,
            )

        if proposal.action == "edit_text":
            if not proposal.new_text:
                return VerifierResult(
                    accepted=False,
                    gate_failure="Gate 1: edit_text action but new_text is empty or None",
                )
            new_row = original_row.model_copy(update={"text": proposal.new_text})

        elif proposal.action == "clear_link":
            new_row = original_row.model_copy(update={"link": ""})

        elif proposal.action == "call_lookup_media":
            if not proposal.lookup_hint:
                return VerifierResult(
                    accepted=False,
                    gate_failure="Gate 3: call_lookup_media but lookup_hint is empty",
                )
            permanent_url = self._lookup(proposal.lookup_hint)
            if permanent_url is None:
                return VerifierResult(
                    accepted=False,
                    gate_failure=(
                        f"Gate 3: no_fabrication — lookup_media('{proposal.lookup_hint}') "
                        "returned None; permanent URL not found. Escalate to human."
                    ),
                    escalate=True,
                )
            new_row = original_row.model_copy(update={"media_url": permanent_url})

        else:
            return VerifierResult(
                accepted=False,
                gate_failure=f"Gate 1: unknown action '{proposal.action}'",
            )

        # ── Gate 1 — Spec conformance ─────────────────────────────────────────
        gate1 = self._check_gate1(new_row, violation)
        if gate1:
            return VerifierResult(accepted=False, gate_failure=gate1, new_row=new_row)

        # ── Gate 2 — Content preservation ─────────────────────────────────────
        gate2 = self._check_gate2(original_row, new_row, violation)
        if gate2:
            return VerifierResult(accepted=False, gate_failure=gate2, new_row=new_row)

        # ── Gate 3 — No fabrication ────────────────────────────────────────────
        gate3 = self._check_gate3(original_row, new_row, proposal)
        if gate3:
            return VerifierResult(accepted=False, gate_failure=gate3, new_row=new_row)

        return VerifierResult(accepted=True, new_row=new_row)

    # ── Gate implementations ──────────────────────────────────────────────────

    @staticmethod
    def _check_gate1(new_row: CsvRow, violation: Violation) -> Optional[str]:
        """The violated rule must no longer fire on the patched row."""
        still_failing = [v for v in lint_row(new_row) if v.rule_id == violation.rule_id]
        if still_failing:
            return (
                f"Gate 1: spec_conformance — rule '{violation.rule_id}' still fails "
                f"after the proposed fix: {still_failing[0].message}"
            )
        return None

    @staticmethod
    def _check_gate2(
        original: CsvRow, fixed: CsvRow, violation: Violation
    ) -> Optional[str]:
        """
        Required content must not silently disappear.

        Compares CsvRow.content_fingerprint() before/after — the mechanism
        the spec designates for this gate. A fix that shortens a Twitter
        post and drops hashtags in the process is caught here mechanically,
        on every row, not just series rows.
        """
        before = original.content_fingerprint()
        after = fixed.content_fingerprint()

        # 2a. Media must still be present if it was before.
        if before["has_media"] and not after["has_media"]:
            return "Gate 2: content_preservation — media_url was removed by the fix"

        # 2b. Every hashtag present before must survive the fix (fixing
        #     hashtags_2084 itself is exempt — that fixer ADDS hashtags).
        if violation.rule_id != "hashtags_2084":
            before_tags = {h.lower() for h in before["hashtags"]}
            after_tags = {h.lower() for h in after["hashtags"]}
            removed = before_tags - after_tags
            if removed:
                return (
                    f"Gate 2: content_preservation — hashtags removed by fix: "
                    f"{', '.join(sorted(removed))}"
                )

        # 2c. CTA handle must remain if it was there and we weren't asked to reformat it.
        if _CTA_HANDLE in original.text and violation.rule_id != "cta_format":
            if _CTA_HANDLE not in fixed.text:
                return (
                    f"Gate 2: content_preservation — CTA handle '{_CTA_HANDLE}' was removed "
                    f"(violation was '{violation.rule_id}', not 'cta_format')"
                )

        return None

    @staticmethod
    def _check_gate3(
        original: CsvRow, fixed: CsvRow, proposal: FixProposal
    ) -> Optional[str]:
        """Media URL may only change as the result of a successful lookup_media call."""
        if proposal.action == "call_lookup_media":
            # URL change was sanctioned — just defensively confirm it's not a tmp link.
            if "/uploads/tmp/" in fixed.media_url:
                return (
                    "Gate 3: no_fabrication — lookup_media returned a temporary URL "
                    "(must never happen)"
                )
            return None

        # All other actions (edit_text, clear_link): media_url must be unchanged.
        if fixed.media_url != original.media_url:
            return (
                f"Gate 3: no_fabrication — media_url changed during a '{proposal.action}' fix "
                f"(original: {original.media_url!r} → fixed: {fixed.media_url!r}). "
                "Media URL may only change via call_lookup_media."
            )
        return None
