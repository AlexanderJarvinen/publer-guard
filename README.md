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
  ODS/XLS/XLSX content plan     publer-guard
  (one spreadsheet,      ┌──────────────────────────────────────┐
   filled by hand)  ───▶ │  Ingestion → Lint → Fix → Verify      │ ───▶  corrected CSVs
                         │  (converter)  (deterministic + agents) │       + report + trace
                         └──────────────────────────────────────┘             │
                                                                               ▼
                                                                  human uploads to Publer
```

You upload the `.ods`/`.xls`/`.xlsx` content plan (or, if you already have them,
the split per-platform CSVs). publer-guard converts the plan into
per-platform Publer CSVs, validates and repairs each one, and hands back
corrected, import-ready files. A human makes the final call on uploading
to Publer.

---

## Setup

Python 3.9+.

```bash
python -m venv .venv
.venv\Scripts\activate         # Windows — source .venv/bin/activate elsewhere
pip install -r requirements.txt

cp .env.example .env           # then add your ANTHROPIC_API_KEY
```

Two entry points over the same pipeline:

```bash
python -m src.app                               # web UI at http://localhost:5000
python -m src.cli INPUT.csv --platform twitter  # writes _fixed.csv, _report.json, _trace.json
```

Neither the test suite nor the eval harness needs an API key — both run against
a fake LLM client, so a fresh clone can verify the whole pipeline offline:

```bash
pytest                  # 242 tests
python -m eval.runner   # 6 known cases; prints the first-attempt fix rate
```

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

The source is a single spreadsheet (`.ods`, `.xls` or `.xlsx`), sheet
**`Контент-план`**. One reader per format — `odfpy`, `xlrd`, `openpyxl` —
each normalised to the same internal grid, so everything downstream is
format-blind.

> `.xls` note: xlrd compares a BIFF string's declared length (UTF-16 code
> units) against `len()` of the decoded Python string, so a single emoji in a
> formula's cached string result aborts the whole file with
> `Expected CONTINUE record; found record-type 0x00BE`. `ingest.py` replaces
> that one method with a code-unit-counting version for the duration of the
> read. Every **content unit** — a post, clip, reel, video or
shorts — occupies a fixed set of **absolute columns**, declared once in
`PLAN_SPECS` (`src/ingest.py`):

| Output file suffix           | Platform           | **Date** | **Time** | **Text** | **Media** | Hashtags | People | **Title** |
|------------------------------|--------------------|------|------|------|-------|----------|--------|-------|
| `FACEBOOK_OFFICIAL_POSTS`    | `facebook`         | A    | C    | E    | G     | H        | —      | —     |
| `FACEBOOK_OFFICIAL_CLIPS`    | `facebook`         | A    | J    | K    | L     | M        | —      | —     |
| `INSTAGRAMM_OFFICIAL_POSTS`  | `instagram_band`   | O    | Q    | S    | U     | V        | W      | —     |
| `INSTAGRAMM_OFFICIAL_REELS`  | `instagram_band`   | O    | X    | Z    | AA    | AB       | AC     | —     |
| `INSTAGRAMM_EXCLUSIVE_POSTS` | `instagram_funnel` | AH   | AJ   | AL   | AN    | AO       | AP     | —     |
| `INSTAGRAMM_EXCLUSIVE_REELS` | `instagram_funnel` | AH   | AQ   | AR   | AS    | AT       | AU     | —     |
| `YOUTUBE_POST`               | `youtube`          | AZ   | BB   | BD   | BF    | —        | —      | —     |
| `YOUTUBE_VIDEO`              | `youtube`          | AZ   | BG   | BI   | BJ    | BK       | —      | BH    |
| `YOUTUBE_SHORTS`             | `youtube`          | AZ   | BO   | BN   | BP    | BQ       | —      | BM    |
| `TIKTOK`                     | `tiktok`           | BS   | BU   | BY   | BX    | BZ       | —      | —     |
| `TELEGRAMM`                  | `telegram`         | CB   | CD   | CF   | CH    | —        | —      | —     |
| `TWITTER`                    | `twitter`          | CJ   | CL   | CN   | CP    | CQ       | —      | —     |

**Bold columns are mandatory.** A plan row becomes a CSV row **only if every
mandatory column of that unit is filled** — Date, Time, Text, Media, plus
Title for the units that have one. Miss any and the row is not a content
unit; a unit with no complete row anywhere produces **no file at all**.
Hashtags and People are the only optional columns.

### Layout check

Before a single row is converted, the header is walked **left to right** and
every column must carry **exactly** the label the master template
(`FULL_FINAL_CONTENT_PLAN_HEADING.xls`) puts there, somewhere in **sheet rows
1-4**. Anything else and the file is refused, with every wrong column listed:

```
Колонка C: ожидается «Post publication time», найдено «Время публикации»
Колонка BL: отсутствует заголовок «Shorts publication time»
```

The contract is `EXPECTED_HEADERS` — **all 89 labelled columns**, not just the
ones the converter reads. That is what makes a *shifted* sheet detectable: a
plan from an earlier generation puts its blocks at different offsets, and the
mapped columns alone would often land on some neighbouring block's header and
pass individually. Checking the whole header catches it on column A.

The band is scanned rather than a single row because the template keeps the
YouTube post sub-headers one row below the rest. Matching ignores surrounding
whitespace and nothing else — a translated layout is a different layout.

Merged cells need no special handling. A header merged *down* across rows
keeps its text in its own column inside the band, so the scan finds it. A
heading merged *across* columns — the block titles `FACEBOOK`, `YOUTUBE` in
row 1 — names a group, not a column, and is never taken for a column header.

Renaming a column in the template means updating `EXPECTED_HEADERS` in the
same commit, or every upload is rejected.

Rules of the conversion:

- **Media must be a link.** "Filled in" is stricter here than for text: the
  cell has to hold an `http(s)` URL. (Whether that URL is a *temporary*
  Publer upload is not ingestion's call — the `media_url_permanent` rule
  judges it.)
- **Each unit is gated by its OWN time column**, because units sharing a date
  (FB post + clip, IG post + reel, YT post + video + shorts) publish at
  different times. A filled-in post time does not make the clip valid.
- **Text is assembled** as text → hashtags → people tag, joined by blank
  lines, into the single Publer `Text` field.
- **The day-of-week column is ignored** — it's derivable from the date and
  never reaches the CSV.
- **Output files are named** `{plan file name}_{suffix}.csv`, e.g.
  `AUG2026_PLAN_FACEBOOK_OFFICIAL_POSTS.csv`.
- **Header rows need no special handling**: a row is data only if its date
  cell parses as a real date.
- **Whatever is filled is what gets written.** A gap may be deliberate — a
  time left empty to schedule inside Publer — so ingestion never decides a row
  isn't wanted. A row with no time becomes a bare `YYYY/MM/DD`; nothing is
  invented to fill a hole.
- **A unit is skipped only when nobody started it.** A row with no content of
  its own — no text, media or title — means the unit wasn't planned that day
  and passes in silence; a unit with no such row anywhere produces no file.
  Date and time don't count as "started": the date column is shared by a whole
  block and the time column is a formula dragged down every row, so both are
  filled a month ahead and say nothing about any one unit.
- **Gaps come back as warnings, not as deletions.** Each names the unit, the
  1-based sheet row and the empty cells. A finished post with no hashtags
  warns too — optional, published either way, but more often an oversight
  than a decision.
- **A half-finished CSV says so where it is downloaded.** The file carries how
  many posts it holds and which plan rows still have gaps, because a warning
  elsewhere on the page is too easy to import straight past.
  `units_without_file` names the units that produced nothing at all.

---

## Output format — the Publer 12-column template

Every CSV publer-guard emits (and reads back on re-run) is Publer's
official bulk-import template: `utf-8-sig` (BOM), comma-delimited, exactly
**12 columns**. `column_count` enforces this.

| # | Column (template header)                         | Used by rule / role |
|---|--------------------------------------------------|---------------------|
| 0 | `Date - Intl. format or prompt`                  | `date_format` — `YYYY/MM/DD HH:MM`, or `YYYY/MM/DD` when the hour is left to Publer |
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
  │  .ods/.xls/.xlsx plan  ──▶  per-unit 12-col Publer CSVs       │
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
| **Ingestion** | **deterministic** | — | Reads the `.ods`/`.xls`/`.xlsx` content plan and emits one 12-column Publer CSV per content unit that has media, assigning `Platform` from the unit's spec. A pure spreadsheet→CSV transform with a fixed absolute-column map — no LLM. |
| **Orchestrator** | plain code | — | Owns `PipelineState`, runs lint→triage→fix→verify→critic loop, enforces retry limits and gates. Holds all control flow so agents stay dumb and replaceable. Fixes rows in parallel (see below). |
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

### Fixing rows in parallel

Violations on the **same row** run one after another — each rewrites the text
the next one reads. Different rows share nothing, so they run at once, up to
`MAX_PARALLEL_ROWS`. A file of six identical CTA repairs went from 45s to 11s.

Each row fixes on a private copy (`_RowWork`) holding its own row, attempts,
escalations and trace lines; they are merged back into `PipelineState` in
submission order once every row is done. That buys two things a shared state
would lose under threads: `attempts[-1]` still means "the attempt I just made"
(the retry path amends it), and **the trace reads identically on every run**
regardless of which thread finished first — it is the demo material, so a
report that reshuffles itself would be worse than a slow one.

### Progress

`/run-plan` streams NDJSON — a line per file before work on it starts, then a
final line with the whole report. A plan with real violations is a minute of
model round trips; without this the browser shows a spinner and no evidence
anything is happening.

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
  - **content-plan upload** — drop an `.ods`/`.xls`/`.xlsx` plan and it's sliced
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
