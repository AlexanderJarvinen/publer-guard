# publer-guard

A multi-agent system that validates and repairs Publer bulk-upload CSVs
against a set of **mechanical, platform-specific rules**, then proves the
repair by re-running a deterministic checker. It ends in a real
artifact: a corrected, import-ready CSV plus a JSON validation report and
a replayable trace.

It is built around one hard principle:

> **LLMs propose. Deterministic code decides.**
> No agent's opinion is ever treated as a verdict. Every "pass / fail"
> is produced by plain Python checking a spec, or by a tool call against
> ground truth — never by an LLM saying "looks good."

This is a deliberate answer to the usual failure mode of content-quality
agents, where the only signal is "an LLM thinks it's fine." Here, quality
of *judgment* (is this a good post?) is explicitly out of scope and left
to a human. The system guarantees only **conformance to a verifiable
spec** — and that is something code can check.

---

## Why this problem

The real pain it comes from: when you bulk-schedule posts through Publer
via CSV, a handful of mechanical mistakes silently break the import or
the publish:

- A media URL grabbed too early is a **temporary** link
  (`.../uploads/tmp/...`) that expires before the post goes out — the
  media arrives broken or missing.
- A stray non-empty **Link** column triggers Publer's
  "Invalid URL attached" import error.
- A post that's fine on Instagram is **over the 280-char limit** on
  Twitter/X.
- The **call-to-action** format differs per platform (flat URL on
  Twitter/Facebook, markdown allowed on Telegram, native @-mention
  inside Instagram, and the funnel endpoint itself must carry *no* CTA).

These are not matters of taste. Each is a binary, checkable fact. That's
exactly what makes the problem a good fit for grounded, mechanical
validation rather than vibes.

---

## Architecture

```
                         ┌────────────────────┐
                         │    Orchestrator    │
                         │  owns PipelineState │
                         │  runs the loop,     │
                         │  enforces gates     │
                         └─────────┬──────────┘
                                   │ scoped slices of state
          ┌────────────────┬───────┴────────┬─────────────────┐
          ▼                ▼                ▼                 ▼
   ┌────────────┐   ┌────────────┐   ┌────────────┐   ┌────────────┐
   │   Linter   │   │   Triage   │   │   Fixer    │   │   Critic   │
   │determinstic│   │   (LLM,    │   │   (LLM,    │   │   (LLM,    │
   │  NOT LLM   │   │  Haiku)    │   │  Sonnet)   │   │  Opus)     │
   │            │   │            │   │            │   │            │
   │ ground     │   │ groups &   │   │ proposes a │   │ explains   │
   │ truth:     │   │ orders     │   │ row edit   │   │ why a fix  │
   │ produces   │   │ violations,│   │ for ONE    │   │ failed,    │
   │ Violations │   │ flags      │   │ violation  │   │ feeds back │
   │            │   │ human-only │   │            │   │ to Fixer   │
   └────────────┘   └────────────┘   └─────┬──────┘   └────────────┘
                                            │ proposed edit
                                            ▼
                         ┌────────────────────────────┐
                         │          Verifier          │
                         │      deterministic, NOT LLM │
                         │  re-runs the Linter + three │
                         │  anti-hallucination gates   │
                         └────────────────────────────┘
                                            │
                                            ▼
                         corrected CSV + report + trace
                         (stops here for human approval —
                          this is the human-in-the-loop line)
```

### Components

| Component | Kind | Model | Responsibility |
|-----------|------|-------|----------------|
| **Orchestrator** | plain code | — | Owns `PipelineState`, runs lint→triage→fix→verify→critic loop, enforces retry limits and gates. Holds all control flow so agents stay dumb and replaceable. |
| **Linter** | **deterministic** | — | The ground truth. Pure Python rules, each returns a structured `Violation`. An agent cannot create or dismiss a violation. |
| **Triage** | LLM | Haiku 4.5 | Groups violations, decides fix order, separates auto-fixable from human-only. Cheap model — this is light reasoning, not generation. |
| **Fixer** | LLM | Sonnet 4.6 | Proposes a single row edit for a single violation. Sees only that violation + that row — never the whole file or other agents' reasoning. |
| **Verifier** | **deterministic** | — | Re-runs the Linter on the proposed edit and applies three anti-hallucination gates. Issues the only binary verdict in the system. |
| **Critic** | LLM | Opus 4.8 | Only invoked when a fix fails verification. Explains *why* and hands a focused note back to the Fixer. Expensive but rare. |

The model tiering (Haiku / Sonnet / Opus) is a cost lever, not decoration:
the cheap model does the cheap thinking, the expensive model is reserved
for the rare hard case.

---

## Context management

The state object (`src/state.py`, `PipelineState`) is the single source
of truth. Two patterns keep it from bloating across a long run:

1. **Scoped slices, not the whole transcript.** The Fixer is handed one
   `Violation` and one `CsvRow`. It never sees the full CSV, the Triage
   reasoning, or the Critic's earlier notes. This keeps each LLM call
   small, cheap, and focused, and it means one agent's hallucination
   can't leak into another's context.

2. **Structured summaries between agents, not raw history.** State moves
   as typed objects (`Violation`, `FixAttempt`), not as a growing chat
   log. The "memory" of the run is the append-only `trace` and
   `attempts` lists, which are auditable but are *not* fed back into
   prompts wholesale.

**Stale context** is handled by treating the CSV rows as the only live
truth. After any accepted edit, the Linter re-reads the row from state —
no agent ever works from a cached copy of a row it edited three steps
ago.

---

## Validation and anti-hallucination

This is the part the system is built around. There are **three
mechanical gates**, all enforced by deterministic code in the Verifier:

### Gate 1 — Spec conformance
A violation either exists in code or it doesn't. After a Fixer edit, the
Linter re-runs against the edited row. If the rule still fails, the fix
is rejected. The Fixer cannot "argue" that it fixed something.

### Gate 2 — Content preservation
The classic reward-hacking mode: to get a Twitter post under 280 chars,
the Fixer quietly deletes the hashtags or the CTA. Caught by comparing
`CsvRow.content_fingerprint()` before and after: if a required element
(the 2084 hashtag set, the CTA, the media) disappeared, the edit is
rejected even though the length rule now "passes."

### Gate 3 — No fabrication
A temporary media URL cannot be repaired by *inventing* a plausible
permanent one — the real permanent URL has an ID and hash assigned by
Publer that are impossible to derive from the temp link. So the Fixer is
**forbidden from writing a media URL that wasn't in the input**. Its only
legal move is to call the `lookup_media` tool, which queries the Publer
Media Library for the real permanent URL. If the tool can't find it, the
violation is **escalated to a human** — never guessed.

> Gate 2 and Gate 3 are the "one I missed at first, then fixed" story:
> early versions had neither, the Fixer cheated (dropped hashtags;
> fabricated a `cdn.publer.com/...` URL), the cheating passed the naive
> length/format check, and it was only caught by eye afterwards. The
> gates were added to trap each mode mechanically.

---

## Human-in-the-loop

The human sits at exactly one place: **the publish gate at the end.**

The system guarantees the CSV is *spec-conformant*. It does not and
cannot judge whether a post is *good*. So it stops at a corrected,
verified CSV and a report, and a human decides whether to upload. It also
escalates to the human mid-run for anything it must not guess — chiefly a
missing permanent media URL (Gate 3).

This line is also the boundary between the interview build and the real
tool: the real version continues past human approval into an actual
`POST /api/v1/posts/schedule` call. The interview version stops here on
purpose, because auto-publishing would erase the human-in-the-loop step
the whole design depends on.

---

## Failure handling mid-pipeline

- **A sub-agent returns garbage** → it fails schema parsing (Pydantic) or
  Gate 1, and the orchestrator retries with the error fed back, up to
  `max_retries_per_violation`.
- **Retries exhausted** → the violation is marked `UNFIXED` and surfaced
  in the report, not silently dropped.
- **Idempotency** → fixes are applied to a working copy of the row keyed
  by `row_index`; re-running the pipeline on the same input is
  deterministic and never double-applies an edit.

---

## What was rejected

- **A single monolithic LLM call** ("here's the CSV, fix it"). Rejected:
  no place to ground validation, no scoped context, and the model both
  proposes and judges its own work — exactly the ungrounded-vibes failure
  mode this project exists to avoid.
- **A direct Publer API integration** (upload with `direct_upload: true`
  and post). Rejected *as the core*: it's the best real-world fix for the
  temp-URL problem, but it's a deterministic script with no agents, no
  proposing/judging split, and nothing to validate. It survives instead
  as a single **tool** (`lookup_media`) the Fixer may call — which is the
  correct place for it.
- **An agent framework (LangGraph etc.)**. Rejected for this size: the
  control flow is a short, explicit loop. Hand-writing the orchestrator
  means every line is defensible and there's no framework magic to
  explain under questioning.

---

## Status

Architecture document — implementation in progress. Run instructions,
output examples, and the eval harness numbers will be filled in once the
code stabilizes.
