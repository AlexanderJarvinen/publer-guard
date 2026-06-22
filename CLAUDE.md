# CLAUDE.md — build guide for this repo

You are helping build **publer-guard**, a multi-agent system that
validates and repairs Publer bulk-upload CSVs against mechanical rules.
Read `README.md` first — it is the architecture of record. This file
tells you HOW to build, in what order, and the principles you must not
violate.

## The one principle that governs everything

**LLMs propose. Deterministic code decides.**

- No LLM agent ever issues a "pass / fail" verdict. Verdicts come only
  from the deterministic Linter and Verifier (plain Python) or from a
  tool call against ground truth.
- If you ever find yourself writing "ask the model whether the fix is
  correct" — stop. That is the exact anti-pattern this project exists to
  avoid. The Linter re-check is the source of truth.

## Architecture recap (see README for full detail)

Orchestrator (plain code) owns `PipelineState` and runs the loop:
`lint → triage → fix → verify → (critic on failure) → repeat`.

- **Linter** — deterministic, NOT an LLM. Produces `Violation`s.
- **Triage** — LLM (Haiku 4.5). Orders violations, flags human-only ones.
- **Fixer** — LLM (Sonnet 4.6). Proposes ONE row edit for ONE violation.
  Sees only that violation + that row. Never the whole CSV.
- **Verifier** — deterministic, NOT an LLM. Re-runs Linter + 3 gates.
- **Critic** — LLM (Opus 4.8). Only on a failed fix. Explains why.

## The rules the Linter enforces (CONFIRMED with the user — do not change)

All are **ERROR** severity (they block publish and the Fixer attempts them):

1. `column_count` — exactly 6 columns: Date, Text, Link, Media URL, Title, Label
2. `date_format` — Date parses as `YYYY/MM/DD HH:MM`
3. `link_empty` — the Link column must be empty (non-empty caused Publer
   "Invalid URL attached" import errors)
4. `media_url_permanent` — Media URL must NOT contain `/uploads/tmp/`
   (temporary link, expires). Repaired ONLY via the `lookup_media` tool,
   never by inventing a URL.
5. `twitter_length` — for `Platform.TWITTER`, `len(text) <= 280`
6. `cta_format` — CTA per platform:
   - Twitter / Facebook: flat URL `IG: instagram.com/alex_y_yarvinen`
     (no markdown)
   - Telegram: markdown link allowed
   - Instagram band account (`INSTAGRAM_BAND`): native `@alex_y_yarvinen`
   - `INSTAGRAM_FUNNEL` (the @alex_y_yarvinen account itself): NO CTA at all
7. `hashtags_2084` — if the row is part of the 2084 series, the required
   hashtag set must be present in Text.
8. `no_cyrillic` — Latin script required on every platform EXCEPT Telegram.
   Telegram is intentionally a Russian-language channel, so Cyrillic is
   legitimate there and the rule is skipped. On all other platforms,
   any Cyrillic character is an ERROR (e.g. Serbian text left in Cyrillic
   instead of Latin). Deliberately narrow: full Serbian-diacritics
   validation needs language detection, which can't be grounded without an
   LLM judgment, so it's out of scope on purpose — Cyrillic presence is a
   fact; "is this correct Serbian" is not.

### 2084 series detection (CONFIRMED)
A row belongs to the 2084 series iff its **Label** contains `2084`
(case-insensitive). This is independent of the hashtag check, so rule 7
can actually catch a 2084 post that is MISSING its hashtags. Mark this
with a clear comment — the exact Label marker is user-tunable in one line.

Required 2084 hashtag set (baseline; user will tune):
`#arcticdreams #2084 #orwell #extrememetal #blackeneddeathmetal`
plus the rotating set documented by the user.

## The three anti-hallucination gates (in the Verifier)

1. **Spec conformance** — re-run the Linter on the edited row; if the rule
   still fails, reject.
2. **Content preservation** — compare `CsvRow.content_fingerprint()`
   before/after; if a required hashtag set, the CTA, or the media
   disappeared, reject even if the target rule now passes.
3. **No fabrication** — if the edit introduces a `media_url` that was not
   in the input row, reject as fabrication. The only legal way to change a
   media URL is the `lookup_media` tool returning a real permanent URL;
   otherwise escalate to human.

## Tools

- `lookup_media(filename_or_hint) -> permanent_url | None`
  - Interview build: **mocked** with a fixture matching the real Publer
    `GET /api/v1/media` response shape (permanent path
    `https://cdn.publer.com/uploads/.../...`). Keep the fixture in
    `fixtures/`.
  - Real build (later): hits `GET https://app.publer.com/api/v1/media`
    with `Authorization: Bearer-API <token>` and
    `Publer-Workspace-Id: <id>`. Same function signature; only the body
    changes. Do NOT build the real call now.

## Build order (do these in sequence; commit after each)

1. ✅ `src/state.py` — typed state (DONE, already in repo).
2. `src/linter.py` — the 8 rules above, each returning `Violation`s.
   Pure functions, fully unit-testable with no API calls.
3. `tests/test_linter.py` — a passing and a failing example per rule.
   This is the grounding for the whole system; write it thoroughly.
4. `src/tools.py` — `lookup_media` mocked from a fixture.
5. `src/agents.py` — Triage, Fixer, Critic. Thin wrappers over the
   Anthropic SDK with structured (JSON) outputs parsed into Pydantic.
   Each agent gets ONLY its scoped slice of state.
6. `src/verifier.py` — re-run linter + the 3 gates. Deterministic.
7. `src/orchestrator.py` — the loop, retries, escalation, trace logging.
8. `src/cli.py` — read a CSV, run the pipeline, write corrected CSV +
   JSON report + trace.
9. `eval/` — 5–6 known CSV cases (some the system fixes first try, some
   it must fail/escalate). A runner that prints pass/fail counts and
   first-attempt-fix rate (the degradation metric).

## Models / cost

Read model from env or a small config, per agent:
- Triage → `claude-haiku-4-5`
- Fixer → `claude-sonnet-4-6`
- Critic → `claude-opus-4-8`
(Confirm exact model strings against the console before first run.)

## Hard guardrails

- Never write the real `ANTHROPIC_API_KEY` into any file. It lives only
  in `.env` (gitignored). Read it via `python-dotenv`.
- Keep every LLM call's input SCOPED. If you're about to pass the whole
  CSV to the Fixer, you're doing it wrong.
- Every run appends to `state.trace`. The trace is the demo material —
  make it readable.
- The interview build STOPS at a verified CSV + report. It does NOT
  publish. Do not add a real posting call.
