# publer-guard

A multi-agent system that turns a hand-filled content plan into
**validated, import-ready Publer CSVs**. It ingests the plan, slices it
into per-platform files, checks every row against a set of **mechanical,
platform-specific rules**, repairs what it can, and proves each repair by
re-running a deterministic checker. It ends in a real artifact: corrected
CSVs plus a JSON validation report and a replayable trace.

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

## Workflow

```
  ODS/XLS content plan          publer-guard
  (one spreadsheet,      ┌──────────────────────────────────────┐
   filled by hand)  ───▶ │  Ingestion → Lint → Fix → Verify      │ ───▶  corrected CSVs
                         │  (converter)  (deterministic + agents) │       + report + trace
                         └──────────────────────────────────────┘             │
                                                                               ▼
                                                                  human uploads to Publer
```

You upload the `.ods`/`.xls` content plan (or, if you already have them,
the split per-platform CSVs). publer-guard converts the plan into
per-platform Publer CSVs, validates and repairs each one, and hands back
corrected, import-ready files. A human makes the final call on uploading
to Publer.

---

## Why this problem

When you bulk-schedule posts through Publer via CSV, a handful of
mechanical mistakes silently break the import or the publish:

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
- Serbian copy accidentally left in **Cyrillic** on a platform that
  should be Latin.

These are not matters of taste. Each is a binary, checkable fact. That's
exactly what makes the problem a good fit for grounded, mechanical
validation rather than vibes.

---

## Input: the content plan

The source is a single spreadsheet (`.ods`/`.xls`), sheet
**`Контент-план`**, where each platform is a **block of columns** starting
at row 0. The ingestion step reads each block and its content types:

| Block label (row 0) | Platform          | Content types                |
|---------------------|-------------------|------------------------------|
| `FACEBOOK`          | `facebook`        | posts, клипы (reels)         |
| `@arcticdreamsofficial` | `instagram_band`   | posts, reels, stories    |
| `@alex_y_yarvinen`  | `instagram_funnel`| posts, reels, stories        |
| `YOUTUBE`           | `youtube`         | posts, video, shorts         |
| `TIKTOK`            | `tiktok`          | video                        |
| `TELEGRAM`          | `telegram`        | posts                        |
| `TWITTER/X`         | `twitter`         | posts                        |

Each platform+type that has content becomes one output CSV, with the
`Platform` assigned automatically from the block label. Hashtags are
merged into the post text; the date/time cells become the `YYYY/MM/DD HH:MM`
`Date` field.

---

## Output format — the Publer 12-column template

Every CSV publer-guard emits (and reads back on re-run) is Publer's
official bulk-import template: `utf-8-sig` (BOM), comma-delimited, exactly
**12 columns**. `column_count` enforces this.

| # | Column (template header)                         | Used by rule / role |
|---|--------------------------------------------------|---------------------|
| 0 | `Date - Intl. format or prompt`                  | `date_format` — must be `YYYY/MM/DD HH:MM` |
| 1 | `Text`                                           | `twitter_length`, `cta_format`, `hashtags_2084`, `no_cyrillic` — the published caption (hashtags merged in) |
| 2 | `Link(s) - Separated by comma for FB carousels`  | `link_empty` — must be empty |
| 3 | `Media URL(s) - Separated by comma`              | `media_url_permanent` — no `/uploads/tmp/` |
| 4 | `Title - For the video, pin, PDF ..`             | video/pin title |
| 5 | `Label(s) - Separated by comma`                  | 2084-series detection (Label contains `2084`) |
| 6 | `Alt text(s) - Separated by \|\|`                | — |
| 7 | `Comment(s) - Separated by \|\|`                 | — |
| 8 | `Pin board, FB album, or Google category`        | — |
| 9 | `Post subtype - I.e. story, reel, PDF ..`        | — |
| 10| `CTA - For Facebook links or Google`             | `cta_format` (dedicated CTA column) |
| 11| `Reminder - For stories, reels, shorts, and TikToks` | — |

`Label` doubles as Publer's internal organisational tag and may legitimately
be Cyrillic (it is never published), so `no_cyrillic` checks published text
fields, not the Label column.

---

## Architecture

```
  ┌─────────────────────────────────────────────────────────────┐
  │  Ingestion (deterministic converter)                         │
  │  .ods/.xls content plan  ──▶  per-platform 12-col Publer CSV  │
  └───────────────────────────────┬─────────────────────────────┘
                                   ▼
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
| **Ingestion** | **deterministic** | — | Reads the `.ods`/`.xls` content plan and emits per-platform, per-type CSVs in the 12-column Publer template, assigning `Platform` from the block label. A pure spreadsheet→CSV transform with a fixed column map — no LLM. |
| **Orchestrator** | plain code | — | Owns `PipelineState`, runs lint→triage→fix→verify→critic loop, enforces retry limits and gates. Holds all control flow so agents stay dumb and replaceable. |
| **Linter** | **deterministic** | — | The ground truth. Pure Python rules, each returns a structured `Violation`. An agent cannot create or dismiss a violation. |
| **Triage** | LLM | Haiku 4.5 | Groups violations, decides fix order, separates auto-fixable from human-only. Cheap model — this is light reasoning, not generation. |
| **Fixer** | LLM | Sonnet 4.6 | Proposes a single row edit for a single violation. Sees only that violation + that row — never the whole file or other agents' reasoning. |
| **Verifier** | **deterministic** | — | Re-runs the Linter on the proposed edit and applies three anti-hallucination gates. Issues the only binary verdict in the system. |
| **Critic** | LLM | Opus 4.8 | Only invoked when a fix fails verification. Explains *why* and hands a focused note back to the Fixer. Expensive but rare. |

The model tiering (Haiku / Sonnet / Opus) is a cost lever, not decoration:
the cheap model does the cheap thinking, the expensive model is reserved
for the rare hard case. Ingestion is deliberately **not** an agent —
mapping spreadsheet columns to CSV columns is a fixed, mechanical
transform with a single correct answer.

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

## Deliverables

Two front-ends over the same pipeline:

- **CLI** (`src/cli.py`) — `python -m src.cli INPUT.csv --platform twitter`.
  Writes `<stem>_fixed.csv`, `<stem>_report.json`, `<stem>_trace.json`.
- **Web UI** (`src/app.py`, Flask) — `python -m src.app`, then
  <http://localhost:5000>. Supports:
  - **content-plan upload** — drop an `.ods`/`.xls` plan and it's sliced
    into per-platform CSVs automatically;
  - **multi-file input** — one `Platform` per file, add rows with **+**;
  - **bulk upload** — pick many CSVs in one dialog (or drag-and-drop);
    each becomes a file+platform row. Client- and server-side `.csv`
    validation;
  - a merged **results view** — violations table (tagged by source file),
    post-fix re-lint status, and per-model cost/token monitoring;
  - **downloads** — each corrected CSV, all corrected CSVs as one **ZIP**,
    and the merged **report** as JSON or CSV;
  - themed **toast** notifications (no native `alert`).

Every run appends to `PipelineState.trace`; the trace is the demo
material and the audit log.

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
- **An LLM in the ingestion/converter step.** Rejected: mapping
  spreadsheet columns to CSV columns is a fixed, mechanical transform. An
  LLM there would only add nondeterminism to a step that has a single
  correct answer.
- **An agent framework (LangGraph etc.)**. Rejected for this size: the
  control flow is a short, explicit loop. Hand-writing the orchestrator
  means every line is defensible and there's no framework magic to
  explain under questioning.
