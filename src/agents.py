"""
agents.py — LLM agent wrappers for publer-guard.

PRINCIPLE: LLMs PROPOSE, deterministic code DECIDES.
No agent here issues a pass/fail verdict. The Verifier (src/verifier.py) does that.

Each agent:
  - Accepts a narrow, scoped slice of data — never the whole PipelineState.
  - Returns a typed Pydantic object parsed from the model's JSON output.
  - On bad JSON or schema mismatch raises ValueError → orchestrator retries.
"""

from __future__ import annotations

import json
import os
import re
import time
from typing import Literal, Optional, Protocol, TypeVar, runtime_checkable

from pydantic import BaseModel, ValidationError

from .state import CallMetric, CsvRow, Violation


# ---------------------------------------------------------------------------
# Output models  (parsed from each agent's JSON response)
# ---------------------------------------------------------------------------

class ViolationRef(BaseModel):
    """Identifies a violation by its stable two-part key."""
    rule_id: str
    row_index: int


class TriageDecision(BaseModel):
    """
    Triage output.
    fix_order: auto-fixable violations in priority order for the Fixer.
    human_only: violations that must not be auto-fixed.
    """
    fix_order: list[ViolationRef]
    human_only: list[ViolationRef]


class FixProposal(BaseModel):
    """
    Fixer output.

    Deliberately has NO media_url field. The Fixer cannot write a URL — the
    only legal move for a media_url_permanent violation is action="call_lookup_media"
    with a lookup_hint. Any URL the Fixer might fabricate is simply
    inexpressible in this schema.
    """
    action: Literal["edit_text", "clear_link", "call_lookup_media", "cannot_fix"]
    new_text: Optional[str] = None      # set when action == "edit_text"
    lookup_hint: Optional[str] = None   # set when action == "call_lookup_media"
    reason: str


class CriticNote(BaseModel):
    """Critic output: why a fix failed and what the Fixer should try instead."""
    explanation: str
    suggestion: str


# ---------------------------------------------------------------------------
# LLM client protocol + implementations
# ---------------------------------------------------------------------------

@runtime_checkable
class LLMClient(Protocol):
    def complete(self, *, model: str, system: str, user: str) -> str:
        """Send one prompt, return the model's text response (expected: JSON string)."""
        ...


class AnthropicClient:
    """
    Production LLM client — thin wrapper over the Anthropic SDK.
    API key comes from the environment; never hardcoded.
    """

    def __init__(self) -> None:
        try:
            from dotenv import load_dotenv
            load_dotenv()
        except ImportError:
            pass  # python-dotenv optional; key may already be in env

        api_key = os.environ.get("ANTHROPIC_API_KEY")
        if not api_key:
            raise RuntimeError(
                "ANTHROPIC_API_KEY is not set. "
                "Add it to .env or your shell environment."
            )

        import anthropic  # lazy import — never pulled in by tests
        self._client = anthropic.Anthropic(api_key=api_key)
        self.metrics: list[CallMetric] = []

    def complete(self, *, model: str, system: str, user: str) -> str:
        start = time.perf_counter()
        msg = self._client.messages.create(
            model=model,
            max_tokens=1024,
            system=system,
            messages=[{"role": "user", "content": user}],
        )
        latency_ms = (time.perf_counter() - start) * 1000.0
        usage = getattr(msg, "usage", None)
        self.metrics.append(CallMetric(
            model=model,
            input_tokens=getattr(usage, "input_tokens", 0) or 0,
            output_tokens=getattr(usage, "output_tokens", 0) or 0,
            latency_ms=round(latency_ms, 1),
            estimated=False,
        ))
        return msg.content[0].text


class FakeLLMClient:
    """
    Test double for LLMClient.

    Accepts a list of canned response strings; returns them in FIFO order.
    Records the last call's arguments so tests can assert on the prompt content.
    Raises RuntimeError if the response queue is exhausted — a broken test is
    better than a silent wrong answer.
    """

    def __init__(self, responses: list[str]) -> None:
        self._queue: list[str] = list(responses)
        # Inspectable by tests
        self.last_model: str = ""
        self.last_system: str = ""
        self.last_user: str = ""
        self.call_count: int = 0
        self.metrics: list[CallMetric] = []

    def complete(self, *, model: str, system: str, user: str) -> str:
        self.last_model = model
        self.last_system = system
        self.last_user = user
        self.call_count += 1
        if not self._queue:
            raise RuntimeError(
                "FakeLLMClient: response queue exhausted. "
                "Add more canned responses to the test."
            )
        response = self._queue.pop(0)
        # Tokens approximated from text length (~4 chars/token); estimated=True
        # so monitoring output is clearly flagged as illustrative, not billed.
        self.metrics.append(CallMetric(
            model=model,
            input_tokens=(len(system) + len(user)) // 4,
            output_tokens=len(response) // 4,
            latency_ms=0.0,
            estimated=True,
        ))
        return response


# ---------------------------------------------------------------------------
# Shared parsing helper
# ---------------------------------------------------------------------------

_T = TypeVar("_T", bound=BaseModel)


def _extract_json(raw: str) -> str:
    """
    Best-effort recovery of a JSON object from a model response.

    The system prompts demand bare JSON, but a real model occasionally wraps
    it in ```code fences``` or adds a sentence of preamble. We strip fences and
    slice to the outermost {...} so a cosmetically-dirty-but-valid response
    still parses. Genuinely malformed output still fails downstream and is
    handled as a caught failure by the orchestrator (retry / fallback).
    """
    s = raw.strip()
    if s.startswith("```"):
        s = re.sub(r"^```[a-zA-Z0-9]*\s*", "", s)
        s = re.sub(r"\s*```$", "", s).strip()
    start, end = s.find("{"), s.rfind("}")
    if start != -1 and end != -1 and end > start:
        s = s[start:end + 1]
    return s


def _parse_or_raise(raw: str, model_cls: type[_T], agent_name: str) -> _T:
    """Parse raw JSON string into model_cls; re-raise as ValueError with context."""
    cleaned = _extract_json(raw)
    try:
        return model_cls.model_validate_json(cleaned)
    except ValidationError as exc:
        raise ValueError(
            f"{agent_name}: response could not be parsed as {model_cls.__name__}.\n"
            f"Error: {exc}\n"
            f"Raw response: {raw!r}"
        ) from exc


# ---------------------------------------------------------------------------
# System prompts
# ---------------------------------------------------------------------------

_TRIAGE_SYSTEM = """\
You are the Triage agent for publer-guard, an automated Publer CSV validation pipeline.

Your role: given a list of rule violations, decide the repair order and separate
violations an automated agent can fix from those that require human intervention.

Input: JSON {"violations": [{"rule_id": "...", "row_index": N, "severity": "...",
              "auto_fixable": true|false, "message": "..."}, ...]}.

Output: JSON ONLY — no prose, no markdown, no code fences.
Schema:
{
  "fix_order":  [{"rule_id": "...", "row_index": N}, ...],
  "human_only": [{"rule_id": "...", "row_index": N}, ...]
}

Rules:
- fix_order: auto-fixable violations in priority order. Structural errors
  (e.g. column_count) first if auto-fixable; within a row, fix blocking errors
  before dependent ones.
- human_only: violations where auto_fixable is false, OR where safe repair
  requires human judgment. column_count (always auto_fixable=false) → human_only.
- Every input violation must appear in exactly one list.
- Return only the JSON object. Nothing else.\
"""

_FIXER_SYSTEM = """\
You are the Fixer agent for publer-guard, an automated Publer CSV validation pipeline.

Your role: given ONE rule violation and the ONE affected row, propose the minimal
edit that resolves the violation without breaking anything else in the row.

╔═══════════════════════════════════════════════════════════╗
║  HARD CONSTRAINTS                                          ║
║  1. NO FABRICATED MEDIA URLS.                              ║
║     You have no output field for a media URL.              ║
║     For "media_url_permanent" your ONLY legal action is    ║
║     action="call_lookup_media" with a lookup_hint          ║
║     (filename or hash extracted from the temporary URL).   ║
║     Never invent or guess a cdn.publer.com path.           ║
║  2. DO NOT REMOVE required content.                        ║
║     When fixing twitter_length, keep the CTA handle and    ║
║     any required hashtags — shorten by trimming prose.     ║
╚═══════════════════════════════════════════════════════════╝

Input: JSON {"violation": {"rule_id": "...", "row_index": N, "message": "..."},
             "row": {"text": "...", "platform": "...", "date": "...",
                     "link": "...", "media_url": "...", "label": "...", "title": "..."},
             "previous_attempt_failed": "..." | null}.

If "previous_attempt_failed" is present, it is feedback from the Critic explaining
why your last fix was rejected. Use it to avoid the same mistake.

Output: JSON ONLY — no prose, no markdown, no code fences.
Schema:
{
  "action":       "edit_text" | "clear_link" | "call_lookup_media" | "cannot_fix",
  "new_text":     "..." | null,
  "lookup_hint":  "..." | null,
  "reason":       "..."
}

- action="edit_text":         set new_text to the corrected text. lookup_hint must be null.
- action="clear_link":        use ONLY for link_empty violations. Sets the Link column to "".
                               new_text and lookup_hint must be null.
- action="call_lookup_media": set lookup_hint to a filename or hash for the media library.
                               new_text must be null. Use ONLY for media_url_permanent.
- action="cannot_fix":        explain in reason. Both new_text and lookup_hint must be null.
- reason is always required.
- Return only the JSON object. Nothing else.\
"""

_CRITIC_SYSTEM = """\
You are the Critic agent for publer-guard, an automated Publer CSV validation pipeline.

Your role: a Fixer proposal was rejected by the deterministic Verifier. Explain
concisely why it failed and give a focused, actionable hint for the next attempt.

Input: JSON {
  "violation": {"rule_id": "...", "message": "..."},
  "row":       {"text": "...", "platform": "..."},
  "proposal":  {"action": "...", "new_text": ..., "lookup_hint": ..., "reason": "..."},
  "gate_failure": "which gate or rule rejected the proposal"
}.

Output: JSON ONLY — no prose, no markdown, no code fences.
Schema:
{
  "explanation": "...",
  "suggestion":  "..."
}

- explanation: one or two sentences on why the fix failed the specific gate.
- suggestion:  a concrete, specific hint the Fixer can act on immediately.
- Return only the JSON object. Nothing else.\
"""


# ---------------------------------------------------------------------------
# Agents
# ---------------------------------------------------------------------------

class TriageAgent:
    """
    Triage agent — model: claude-haiku-4-5.

    Receives ONLY compact violation metadata: rule_id, row_index, severity,
    auto_fixable, message. No row text, no media URLs, no CSV content.
    """

    MODEL = os.environ.get("TRIAGE_MODEL", "claude-haiku-4-5")

    def __init__(self, client: LLMClient) -> None:
        self._client = client

    @property
    def client(self) -> LLMClient:
        return self._client

    def triage(self, violations: list[Violation]) -> TriageDecision:
        compact = [
            {
                "rule_id": v.rule_id,
                "row_index": v.row_index,
                "severity": v.severity.value,
                "auto_fixable": v.auto_fixable,
                "message": v.message,
            }
            for v in violations
        ]
        user = json.dumps({"violations": compact})
        raw = self._client.complete(model=self.MODEL, system=_TRIAGE_SYSTEM, user=user)
        return _parse_or_raise(raw, TriageDecision, "TriageAgent")


class FixerAgent:
    """
    Fixer agent — model: claude-sonnet-4-6.

    propose_fix() accepts exactly ONE Violation and ONE CsvRow — nothing more.
    The FixProposal schema has no media_url field, so the Fixer physically
    cannot write a fabricated URL regardless of what the model outputs.
    """

    MODEL = os.environ.get("FIXER_MODEL", "claude-sonnet-4-6")

    def __init__(self, client: LLMClient) -> None:
        self._client = client

    @property
    def client(self) -> LLMClient:
        return self._client

    def propose_fix(
        self,
        violation: Violation,
        row: CsvRow,
        *,
        critic_note: Optional[str] = None,
    ) -> FixProposal:
        payload = {
            "violation": {
                "rule_id": violation.rule_id,
                "row_index": violation.row_index,
                "message": violation.message,
            },
            "row": {
                "text": row.text,
                "platform": row.platform.value,
                "date": row.date,
                "link": row.link,
                "media_url": row.media_url,
                "label": row.label,
                "title": row.title,
            },
            "previous_attempt_failed": critic_note,
        }
        user = json.dumps(payload)
        raw = self._client.complete(model=self.MODEL, system=_FIXER_SYSTEM, user=user)
        return _parse_or_raise(raw, FixProposal, "FixerAgent")


class CriticAgent:
    """
    Critic agent — model: claude-opus-4-8.

    Only invoked on Fixer failures. Receives: violation, current row state,
    the rejected proposal, and the gate failure reason. Nothing else.
    """

    MODEL = os.environ.get("CRITIC_MODEL", "claude-opus-4-8")

    def __init__(self, client: LLMClient) -> None:
        self._client = client

    @property
    def client(self) -> LLMClient:
        return self._client

    def critique(
        self,
        violation: Violation,
        row: CsvRow,
        proposal: FixProposal,
        gate_failure: str,
    ) -> CriticNote:
        payload = {
            "violation": {
                "rule_id": violation.rule_id,
                "message": violation.message,
            },
            "row": {
                "text": row.text,
                "platform": row.platform.value,
            },
            "proposal": proposal.model_dump(),
            "gate_failure": gate_failure,
        }
        user = json.dumps(payload)
        raw = self._client.complete(model=self.MODEL, system=_CRITIC_SYSTEM, user=user)
        return _parse_or_raise(raw, CriticNote, "CriticAgent")
