"""
orchestrator.py — the control loop for publer-guard.

Runs:  lint → triage → (fix → verify → [critic on failure]) → repeat

PRINCIPLE: LLMs PROPOSE. The Orchestrator ENFORCES.
All Fixer proposals pass through the deterministic Verifier before any row is mutated.
No agent's opinion is ever treated as a verdict.
"""

from __future__ import annotations

from typing import Optional

from .agents import CriticAgent, FixerAgent, TriageAgent
from .linter import lint_all
from .state import CsvRow, FixAttempt, FixOutcome, PipelineState, Violation
from .verifier import Verifier


def _find_violation(
    violations: list[Violation], rule_id: str, row_index: int
) -> Optional[Violation]:
    for v in violations:
        if v.rule_id == rule_id and v.row_index == row_index:
            return v
    return None


class Orchestrator:
    def __init__(
        self,
        triage: TriageAgent,
        fixer: FixerAgent,
        critic: CriticAgent,
        verifier: Optional[Verifier] = None,
    ) -> None:
        self._triage = triage
        self._fixer = fixer
        self._critic = critic
        self._verifier = verifier or Verifier()

    def run(self, state: PipelineState) -> PipelineState:
        # ── Step 1: Lint ──────────────────────────────────────────────────────
        raw_rows = [
            [r.date, r.text, r.link, r.media_url, r.title, r.label]
            for r in state.rows
        ]
        state.violations = lint_all(raw_rows, state.rows)
        state.log(
            "lint",
            f"Linted {len(state.rows)} row(s); found {len(state.violations)} violation(s)",
        )

        if not state.violations:
            state.log("lint", "No violations — CSV is already clean")
            return state

        # ── Step 2: Triage ────────────────────────────────────────────────────
        decision = self._triage.triage(state.violations)
        state.log(
            "triage",
            (
                f"Triage complete: {len(decision.fix_order)} auto-fixable, "
                f"{len(decision.human_only)} human-only"
            ),
        )

        # ── Step 3: Escalate human-only violations ───────────────────────────
        for ref in decision.human_only:
            v = _find_violation(state.violations, ref.rule_id, ref.row_index)
            if v is None:
                state.log(
                    "triage",
                    f"Triage referenced unknown violation {ref.rule_id}@{ref.row_index}; skipping",
                )
                continue
            state.escalations.append(v)
            state.attempts.append(FixAttempt(
                attempt_number=0,
                rule_id=v.rule_id,
                row_index=v.row_index,
                model_used="—",
                outcome=FixOutcome.ESCALATED,
                gate_rejection="triage: human_only",
            ))
            state.log("triage", f"Escalated (human-only): {v.rule_id} @ row {v.row_index}")

        # ── Step 4: Fix auto-fixable violations ───────────────────────────────
        for ref in decision.fix_order:
            v = _find_violation(state.violations, ref.rule_id, ref.row_index)
            if v is None:
                state.log(
                    "fix",
                    f"Skipped: triage referenced unknown violation "
                    f"{ref.rule_id}@{ref.row_index}",
                )
                continue
            self._fix_violation(state, v)

        return state

    # ─────────────────────────────────────────────────────────────────────────

    def _fix_violation(self, state: PipelineState, violation: Violation) -> None:
        critic_note: Optional[str] = None

        for attempt in range(1, state.max_retries_per_violation + 1):
            row = state.row(violation.row_index)

            # ── Fixer ─────────────────────────────────────────────────────────
            proposal = self._fixer.propose_fix(
                violation, row, critic_note=critic_note
            )
            state.log(
                "fix",
                (
                    f"Attempt {attempt}/{state.max_retries_per_violation}: "
                    f"{violation.rule_id}@row{violation.row_index} → action={proposal.action}"
                ),
                rule_id=violation.rule_id,
                row_index=violation.row_index,
                attempt=attempt,
                action=proposal.action,
            )

            # ── cannot_fix short-circuit ──────────────────────────────────────
            if proposal.action == "cannot_fix":
                state.escalations.append(violation)
                state.attempts.append(FixAttempt(
                    attempt_number=attempt,
                    rule_id=violation.rule_id,
                    row_index=violation.row_index,
                    model_used=FixerAgent.MODEL,
                    outcome=FixOutcome.ESCALATED,
                    gate_rejection="cannot_fix",
                ))
                state.log(
                    "verify",
                    f"Escalated (cannot_fix): {violation.rule_id}@row{violation.row_index}",
                )
                return

            # ── Verifier ──────────────────────────────────────────────────────
            result = self._verifier.verify(row, violation, proposal)

            if result.accepted:
                # Commit the fix
                idx = next(
                    i for i, r in enumerate(state.rows)
                    if r.row_index == violation.row_index
                )
                assert result.new_row is not None
                state.rows[idx] = result.new_row
                state.attempts.append(FixAttempt(
                    attempt_number=attempt,
                    rule_id=violation.rule_id,
                    row_index=violation.row_index,
                    model_used=FixerAgent.MODEL,
                    proposed_text=proposal.new_text,
                    proposed_media_url=result.new_row.media_url,
                    outcome=FixOutcome.FIXED,
                ))
                state.log(
                    "verify",
                    f"Fix ACCEPTED: {violation.rule_id}@row{violation.row_index} "
                    f"(attempt {attempt})",
                )
                return

            # ── Fix rejected ──────────────────────────────────────────────────
            state.log(
                "verify",
                f"Fix REJECTED (attempt {attempt}): {result.gate_failure}",
                gate_failure=result.gate_failure,
            )
            state.attempts.append(FixAttempt(
                attempt_number=attempt,
                rule_id=violation.rule_id,
                row_index=violation.row_index,
                model_used=FixerAgent.MODEL,
                proposed_text=proposal.new_text,
                outcome=FixOutcome.UNFIXED,
                gate_rejection=result.gate_failure,
            ))

            # Immediate escalation for lookup_media failures — no point retrying
            if result.escalate:
                state.escalations.append(violation)
                state.attempts[-1] = state.attempts[-1].model_copy(
                    update={"outcome": FixOutcome.ESCALATED}
                )
                state.log(
                    "verify",
                    f"Escalated (gate said escalate=True): "
                    f"{violation.rule_id}@row{violation.row_index}",
                )
                return

            if attempt < state.max_retries_per_violation:
                # ── Critic ────────────────────────────────────────────────────
                critic_result = self._critic.critique(
                    violation, row, proposal,
                    result.gate_failure or "unknown gate failure",
                )
                critic_note = (
                    f"{critic_result.explanation} "
                    f"Suggestion: {critic_result.suggestion}"
                )
                state.log("critic", f"Critic note: {critic_note}")
                # Attach note to the attempt record for the trace
                state.attempts[-1] = state.attempts[-1].model_copy(
                    update={"critic_note": critic_note}
                )

        # ── All retries exhausted ─────────────────────────────────────────────
        state.log(
            "verify",
            f"All {state.max_retries_per_violation} attempt(s) exhausted for "
            f"{violation.rule_id}@row{violation.row_index} — UNFIXED",
        )
