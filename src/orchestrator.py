"""
orchestrator.py — the control loop for publer-guard.

Runs:  lint → triage → (fix → verify → [critic on failure]) → repeat

PRINCIPLE: LLMs PROPOSE. The Orchestrator ENFORCES.
All Fixer proposals pass through the deterministic Verifier before any row is mutated.
No agent's opinion is ever treated as a verdict.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Optional

from .agents import (
    CriticAgent,
    FixerAgent,
    TriageAgent,
    TriageDecision,
    ViolationRef,
)
from .linter import lint_all
from .state import CsvRow, FixAttempt, FixOutcome, PipelineState, TraceEvent, Violation
from .verifier import Verifier

# Fixing one violation is a round trip to a model, sometimes three. Rows are
# independent of each other, so they need not wait in line. Kept modest: the
# ceiling here is someone's rate limit, not this machine.
MAX_PARALLEL_ROWS = 6


@dataclass
class _RowWork:
    """One row's fixing session, kept off the shared state.

    Everything a fix produces — the rewritten row, its attempts, escalations
    and trace lines — accumulates here and is merged back in a fixed order
    once all rows are done. That keeps two things true under parallelism:
    `attempts[-1]` still means "the attempt I just made", and the trace reads
    the same on every run regardless of which thread finished first.
    """
    row: CsvRow
    attempts: list[FixAttempt] = field(default_factory=list)
    escalations: list[Violation] = field(default_factory=list)
    trace: list[TraceEvent] = field(default_factory=list)

    def log(self, step: str, detail: str, **payload) -> None:
        self.trace.append(TraceEvent(step=step, detail=detail, payload=payload))


def _synthetic_raw(row: CsvRow) -> list[str]:
    """A 12-field Publer-template line for rows that have no source CSV."""
    return [row.date, row.text, row.link, row.media_url, row.title, row.label,
            "", "", "", "", "", ""]


def _fallback_triage(violations: list[Violation]) -> TriageDecision:
    """
    Deterministic triage used when the Triage LLM returns unparseable output.
    Triage is an optimization (ordering + human-only flagging), NOT a hard
    dependency: auto-fixable violations go to the fixer in input order, the
    rest are escalated. This keeps the run alive without the LLM.
    """
    fix_order = [
        ViolationRef(rule_id=v.rule_id, row_index=v.row_index)
        for v in violations if v.auto_fixable
    ]
    human_only = [
        ViolationRef(rule_id=v.rule_id, row_index=v.row_index)
        for v in violations if not v.auto_fixable
    ]
    return TriageDecision(fix_order=fix_order, human_only=human_only)


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
        max_parallel_rows: int = MAX_PARALLEL_ROWS,
    ) -> None:
        self._triage = triage
        self._fixer = fixer
        self._critic = critic
        self._verifier = verifier or Verifier()
        self._max_parallel_rows = max(1, max_parallel_rows)

    def run(self, state: PipelineState) -> PipelineState:
        # ── Step 1: Lint ──────────────────────────────────────────────────────
        # Use the file's real raw lines so column_count judges actual
        # structure. Rows built without a source CSV (plan ingestion) get
        # synthesized 12-field lines — trivially valid by construction.
        raw_rows = state.raw_rows or [_synthetic_raw(r) for r in state.rows]
        state.violations = lint_all(raw_rows, state.rows)
        state.log(
            "lint",
            f"Linted {len(state.rows)} row(s); found {len(state.violations)} violation(s)",
        )

        if not state.violations:
            state.log("lint", "No violations — CSV is already clean")
            return state

        # ── Step 2: Triage ────────────────────────────────────────────────────
        try:
            decision = self._triage.triage(state.violations)
            state.log(
                "triage",
                (
                    f"Triage complete: {len(decision.fix_order)} auto-fixable, "
                    f"{len(decision.human_only)} human-only"
                ),
            )
        except ValueError as exc:
            # Caught failure: Triage LLM returned unparseable output. Degrade to
            # deterministic triage instead of crashing the run (point 5).
            decision = _fallback_triage(state.violations)
            state.log(
                "triage",
                (
                    f"Triage LLM output unparseable ({type(exc).__name__}); "
                    f"fell back to deterministic triage: "
                    f"{len(decision.fix_order)} auto-fixable, "
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
        # Violations on the SAME row are fixed one after another: each rewrites
        # the text the next one has to read. Different rows share nothing, so
        # they run at once — which is where the wall time goes, a file of six
        # identical CTA fixes being the common case.
        by_row: dict[int, list[Violation]] = {}
        for ref in decision.fix_order:
            v = _find_violation(state.violations, ref.rule_id, ref.row_index)
            if v is None:
                state.log(
                    "fix",
                    f"Skipped: triage referenced unknown violation "
                    f"{ref.rule_id}@{ref.row_index}",
                )
                continue
            by_row.setdefault(v.row_index, []).append(v)

        if by_row:
            batch = list(by_row.items())
            workers = min(self._max_parallel_rows, len(batch))
            retries = state.max_retries_per_violation
            state.log(
                "fix",
                f"Fixing {sum(len(vs) for _, vs in batch)} violation(s) across "
                f"{len(batch)} row(s), {workers} row(s) at a time",
            )

            def fix_one(item: tuple[int, list[Violation]]) -> _RowWork:
                row_index, violations = item
                return self._fix_row(state.row(row_index), violations, retries)

            if workers > 1:
                with ThreadPoolExecutor(max_workers=workers) as pool:
                    done = list(pool.map(fix_one, batch))
            else:
                done = [fix_one(item) for item in batch]

            # Merged in submission order, not completion order.
            for (row_index, _), work in zip(batch, done):
                position = next(
                    i for i, r in enumerate(state.rows) if r.row_index == row_index
                )
                state.rows[position] = work.row
                state.attempts.extend(work.attempts)
                state.escalations.extend(work.escalations)
                state.trace.extend(work.trace)

        # ── Step 5: Final full-file re-lint — safety net beyond Gate 1 ────────
        # Gate 1 only re-checks the SAME rule on the patched row. A fix can
        # still introduce a DIFFERENT violation (e.g. trimming a Twitter post
        # to length turns a flat-URL CTA into a markdown link, breaking
        # cta_format). A full re-lint of the final rows is the only thing that
        # catches that class of failure.
        # Fixes edit fields, never structure, so the original raw lines
        # still describe the file's column layout.
        state.final_violations = lint_all(raw_rows, state.rows)
        initial_keys = {(v.rule_id, v.row_index) for v in state.violations}
        introduced = [
            v for v in state.final_violations
            if (v.rule_id, v.row_index) not in initial_keys
        ]
        if introduced:
            state.log(
                "relint",
                f"POST-FIX RE-LINT — {len(introduced)} NEW violation(s) introduced "
                f"by fixes: "
                + ", ".join(f"{v.rule_id}@row{v.row_index}" for v in introduced),
                introduced=[v.model_dump() for v in introduced],
            )
        else:
            remaining = len(state.final_violations)
            state.log(
                "relint",
                f"POST-FIX RE-LINT — no new violations introduced; "
                f"{remaining} known violation(s) remain (escalated/unfixed).",
            )

        # ── Step 6: Drain LLM call metrics for monitoring (point 7) ───────────
        seen_clients: set[int] = set()
        for agent in (self._triage, self._fixer, self._critic):
            client = agent.client
            if id(client) in seen_clients:
                continue
            seen_clients.add(id(client))
            state.metrics.extend(getattr(client, "metrics", []))

        return state

    # ─────────────────────────────────────────────────────────────────────────

    def _fix_row(
        self, row: CsvRow, violations: list[Violation], max_retries: int
    ) -> _RowWork:
        """Work through one row's violations in order, on a private copy."""
        work = _RowWork(row=row)
        for violation in violations:
            self._fix_violation(work, violation, max_retries)
        return work

    def _fix_violation(
        self, work: _RowWork, violation: Violation, max_retries: int
    ) -> None:
        critic_note: Optional[str] = None

        for attempt in range(1, max_retries + 1):
            row = work.row

            # ── Fixer ─────────────────────────────────────────────────────────
            try:
                proposal = self._fixer.propose_fix(
                    violation, row, critic_note=critic_note
                )
            except ValueError as exc:
                # Caught failure: Fixer returned unparseable output (e.g. prose
                # or broken JSON). Record it, then retry with a corrective note
                # telling the model to emit bare JSON (point 5).
                work.log(
                    "fix",
                    (
                        f"Attempt {attempt}/{max_retries}: "
                        f"Fixer output unparseable ({type(exc).__name__}) for "
                        f"{violation.rule_id}@row{violation.row_index} — retrying"
                    ),
                    rule_id=violation.rule_id,
                    row_index=violation.row_index,
                    attempt=attempt,
                )
                work.attempts.append(FixAttempt(
                    attempt_number=attempt,
                    rule_id=violation.rule_id,
                    row_index=violation.row_index,
                    model_used=self._fixer.model,
                    outcome=FixOutcome.UNFIXED,
                    gate_rejection="unparseable_output",
                ))
                critic_note = (
                    "Your previous response was not valid JSON. Respond with ONLY "
                    "the JSON object specified in your instructions — no prose, no "
                    "code fences, nothing else."
                )
                continue
            work.log(
                "fix",
                (
                    f"Attempt {attempt}/{max_retries}: "
                    f"{violation.rule_id}@row{violation.row_index} → action={proposal.action}"
                ),
                rule_id=violation.rule_id,
                row_index=violation.row_index,
                attempt=attempt,
                action=proposal.action,
            )

            # ── cannot_fix short-circuit ──────────────────────────────────────
            if proposal.action == "cannot_fix":
                work.escalations.append(violation)
                work.attempts.append(FixAttempt(
                    attempt_number=attempt,
                    rule_id=violation.rule_id,
                    row_index=violation.row_index,
                    model_used=self._fixer.model,
                    outcome=FixOutcome.ESCALATED,
                    gate_rejection="cannot_fix",
                ))
                work.log(
                    "verify",
                    f"Escalated (cannot_fix): {violation.rule_id}@row{violation.row_index}",
                )
                return

            # ── Verifier ──────────────────────────────────────────────────────
            result = self._verifier.verify(row, violation, proposal)

            if result.accepted:
                # Commit the fix to this row's private copy — the next
                # violation on the same row must see it, and the shared state
                # is not touched until every row is done.
                assert result.new_row is not None
                work.row = result.new_row
                work.attempts.append(FixAttempt(
                    attempt_number=attempt,
                    rule_id=violation.rule_id,
                    row_index=violation.row_index,
                    model_used=self._fixer.model,
                    proposed_text=proposal.new_text,
                    proposed_media_url=result.new_row.media_url,
                    outcome=FixOutcome.FIXED,
                ))
                work.log(
                    "verify",
                    f"Fix ACCEPTED: {violation.rule_id}@row{violation.row_index} "
                    f"(attempt {attempt})",
                )
                return

            # ── Fix rejected ──────────────────────────────────────────────────
            work.log(
                "verify",
                f"Fix REJECTED (attempt {attempt}): {result.gate_failure}",
                gate_failure=result.gate_failure,
            )
            work.attempts.append(FixAttempt(
                attempt_number=attempt,
                rule_id=violation.rule_id,
                row_index=violation.row_index,
                model_used=self._fixer.model,
                proposed_text=proposal.new_text,
                outcome=FixOutcome.UNFIXED,
                gate_rejection=result.gate_failure,
            ))

            # Immediate escalation for lookup_media failures — no point retrying
            if result.escalate:
                work.escalations.append(violation)
                work.attempts[-1] = work.attempts[-1].model_copy(
                    update={"outcome": FixOutcome.ESCALATED}
                )
                work.log(
                    "verify",
                    f"Escalated (gate said escalate=True): "
                    f"{violation.rule_id}@row{violation.row_index}",
                )
                return

            if attempt < max_retries:
                # ── Critic ────────────────────────────────────────────────────
                try:
                    critic_result = self._critic.critique(
                        violation, row, proposal,
                        result.gate_failure or "unknown gate failure",
                    )
                    critic_note = (
                        f"{critic_result.explanation} "
                        f"Suggestion: {critic_result.suggestion}"
                    )
                    work.log("critic", f"Critic note: {critic_note}")
                    # Attach note to the attempt record for the trace
                    work.attempts[-1] = work.attempts[-1].model_copy(
                        update={"critic_note": critic_note}
                    )
                except ValueError as exc:
                    # Caught failure: Critic output unparseable. Non-critical —
                    # retry the Fixer without a note rather than crash.
                    critic_note = None
                    work.log(
                        "critic",
                        (
                            f"Critic output unparseable ({type(exc).__name__}); "
                            f"retrying Fixer without a critic note"
                        ),
                    )

        # ── All retries exhausted ─────────────────────────────────────────────
        work.log(
            "verify",
            f"All {max_retries} attempt(s) exhausted for "
            f"{violation.rule_id}@row{violation.row_index} — UNFIXED",
        )
