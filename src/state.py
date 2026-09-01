"""
Typed state object — the single source of truth that moves between agents.

Design notes (for the "context management" section of the demo):
- Each agent receives a SCOPED view of this state, never the whole thing.
  The Fixer sees one violation + one row, not the full file or the
  reasoning of other agents. This is how we fight context bloat.
- State is append-only where it matters (trace, attempts): we never
  silently overwrite history, so a long run stays auditable.
- The CSV rows are the ground truth. Agents propose edits to rows;
  deterministic code (linter/verifier) decides whether those edits are
  accepted. No agent's opinion is ever treated as a verdict.
"""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field


class Platform(str, Enum):
    """The platforms a Publer row can target. Drives platform-specific rules."""
    FACEBOOK = "facebook"
    INSTAGRAM_BAND = "instagram_band"          # @arcticdreamsofficial
    INSTAGRAM_FUNNEL = "instagram_funnel"      # @alex_y_yarvinen — the endpoint
    TWITTER = "twitter"
    TELEGRAM = "telegram"
    YOUTUBE = "youtube"
    TIKTOK = "tiktok"


class Severity(str, Enum):
    """How bad a violation is. ERROR blocks publish; WARNING is advisory."""
    ERROR = "error"
    WARNING = "warning"


class FixOutcome(str, Enum):
    """What the system decided to do about a violation."""
    FIXED = "fixed"               # deterministic verifier confirmed it's gone
    ESCALATED = "escalated"       # cannot fix safely → hand to human
    UNFIXED = "unfixed"           # retries exhausted, still failing


class CsvRow(BaseModel):
    """
    One Publer post row. Mirrors the real Publer CSV columns plus the
    declared target platform (which the CSV itself doesn't carry but the
    rules need). `row_index` is the stable identity across edits — we
    never reorder, so an agent editing row 4 always means the same row.
    """
    row_index: int
    platform: Platform
    date: str = ""          # expected "YYYY/MM/DD HH:MM"
    text: str = ""
    link: str = ""          # the column that must stay empty (Publer import bug)
    media_url: str = ""
    title: str = ""
    label: str = ""

    def content_fingerprint(self) -> dict:
        """
        A snapshot of the things a fix must NOT silently destroy.
        Used by the content-preservation gate: if a Fixer shortens text
        for Twitter and drops the hashtags or the CTA in the process,
        comparing fingerprints before/after catches it mechanically.
        """
        return {
            "hashtags": sorted(_extract_hashtags(self.text)),
            "has_media": bool(self.media_url),
            "media_url": self.media_url,   # must not be fabricated/changed
        }


class Violation(BaseModel):
    """
    A single rule failure, produced ONLY by the deterministic linter.
    This is ground truth — an agent can't create or dismiss a violation,
    it can only propose a row edit that the linter then re-checks.
    """
    rule_id: str                 # e.g. "media_url_permanent"
    row_index: int
    severity: Severity
    message: str                 # human-readable, shown in the report
    auto_fixable: bool           # can an agent attempt this, or human-only?


class FixAttempt(BaseModel):
    """One pass of the Fixer at one violation. Append-only for audit."""
    attempt_number: int
    rule_id: str
    row_index: int
    model_used: str
    proposed_text: str | None = None
    proposed_media_url: str | None = None
    outcome: FixOutcome
    gate_rejection: str | None = None   # which gate blocked it, if any
    critic_note: str | None = None


class TraceEvent(BaseModel):
    """
    One logged step in the run. This is what we show on the recording
    instead of slides: a real, replayable trace of who did what and which
    gate fired. Append-only.
    """
    step: str                    # "lint", "triage", "fix", "verify", "critic"
    detail: str
    payload: dict = Field(default_factory=dict)


class CallMetric(BaseModel):
    """
    One LLM call's cost/latency record — the raw material for production
    monitoring (point 7). Appended by the LLM client on every call.
    `estimated` is True for the FakeLLMClient (token counts approximated from
    text length, no network latency) and False for real Anthropic calls,
    where the counts come from the API `usage` object.
    """
    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    latency_ms: float = 0.0
    estimated: bool = False


class PipelineState(BaseModel):
    """
    The whole run. The orchestrator owns this; sub-agents get slices.
    """
    rows: list[CsvRow]
    # The file's real split data lines (header excluded), in row order —
    # what check_column_count judges. Lanes that build rows directly
    # (plan ingestion) leave this empty and the orchestrator synthesizes
    # structurally-valid 12-field lines, making the rule vacuous there on
    # purpose: our own code generated the columns.
    raw_rows: list[list[str]] = Field(default_factory=list)
    violations: list[Violation] = Field(default_factory=list)
    attempts: list[FixAttempt] = Field(default_factory=list)
    trace: list[TraceEvent] = Field(default_factory=list)
    escalations: list[Violation] = Field(default_factory=list)
    max_retries_per_violation: int = 2
    # Populated at the end of a run by the orchestrator:
    metrics: list[CallMetric] = Field(default_factory=list)          # point 7: cost/latency
    final_violations: list[Violation] = Field(default_factory=list)  # full post-fix re-lint

    # ---- helpers the orchestrator uses; keep logic out of agents ----

    def row(self, idx: int) -> CsvRow:
        for r in self.rows:
            if r.row_index == idx:
                return r
        raise KeyError(f"no row with index {idx}")

    def log(self, step: str, detail: str, **payload) -> None:
        self.trace.append(TraceEvent(step=step, detail=detail, payload=payload))

    def open_errors(self) -> list[Violation]:
        """Errors still outstanding (not yet escalated)."""
        escalated = {(v.rule_id, v.row_index) for v in self.escalations}
        return [
            v for v in self.violations
            if v.severity == Severity.ERROR
            and (v.rule_id, v.row_index) not in escalated
        ]


def _extract_hashtags(text: str) -> list[str]:
    """Pull #hashtags out of text. Shared by fingerprint and the linter."""
    import re
    return re.findall(r"#\w+", text)
