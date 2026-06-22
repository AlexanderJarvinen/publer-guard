"""
cli.py — command-line entry point for publer-guard.

Usage:
  python -m src.cli INPUT.csv --platform twitter [--output-dir DIR] [--max-retries N]

Each Publer CSV belongs to one social-media profile, so all rows share one platform.
Pass --platform to tell the linter which rules apply.

Outputs (written to output-dir, defaulting to the input file's directory):
  <stem>_fixed.csv    — corrected rows, ready for Publer import
  <stem>_report.json  — per-violation outcomes (summary + details)
  <stem>_trace.json   — full run trace (replayable, useful for demos)
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path

from .agents import AnthropicClient, CriticAgent, FixerAgent, TriageAgent
from .orchestrator import Orchestrator
from .state import CsvRow, FixOutcome, Platform, PipelineState
from .verifier import Verifier


# ─────────────────────────────────────────────────────────────────────────────
# CSV I/O
# ─────────────────────────────────────────────────────────────────────────────

def parse_csv(
    path: Path, platform: Platform
) -> tuple[list[str], list[list[str]], list[CsvRow]]:
    """
    Returns (header, raw_rows, csv_rows).
    raw_rows: split data lines (header excluded) — passed to lint_all.
    csv_rows: typed CsvRow objects in the same order.
    """
    with path.open(encoding="utf-8-sig") as f:
        reader = csv.reader(f)
        header = next(reader)
        raw_rows: list[list[str]] = []
        csv_rows: list[CsvRow] = []
        for idx, fields in enumerate(reader):
            raw_rows.append(fields)
            # Pad/trim to 6 fields; column_count violations are caught by the linter.
            padded = (fields + [""] * 6)[:6]
            csv_rows.append(CsvRow(
                row_index=idx,
                platform=platform,
                date=padded[0],
                text=padded[1],
                link=padded[2],
                media_url=padded[3],
                title=padded[4],
                label=padded[5],
            ))
    return header, raw_rows, csv_rows


def write_fixed_csv(path: Path, header: list[str], rows: list[CsvRow]) -> None:
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(header)
        for row in rows:
            writer.writerow([
                row.date, row.text, row.link, row.media_url, row.title, row.label
            ])


# ─────────────────────────────────────────────────────────────────────────────
# Report
# ─────────────────────────────────────────────────────────────────────────────

def _final_attempts(state: PipelineState) -> list:
    """Return only the last FixAttempt per violation — the final outcome."""
    final: dict = {}
    for a in state.attempts:
        final[(a.rule_id, a.row_index)] = a
    return list(final.values())


def build_report(state: PipelineState) -> dict:
    # Use final outcomes (last attempt per violation) for summary counts.
    final = _final_attempts(state)
    fixed = [a for a in final if a.outcome == FixOutcome.FIXED]
    escalated = [a for a in final if a.outcome == FixOutcome.ESCALATED]
    unfixed = [a for a in final if a.outcome == FixOutcome.UNFIXED]

    fixable = [v for v in state.violations if v.auto_fixable]
    first_attempt_fixes = [a for a in fixed if a.attempt_number == 1]
    first_attempt_rate = (
        len(first_attempt_fixes) / len(fixable) if fixable else 1.0
    )

    return {
        "summary": {
            "total_violations": len(state.violations),
            "fixed": len(fixed),
            "escalated_to_human": len(escalated),
            "unfixed_after_retries": len(unfixed),
            "rows_modified": len({a.row_index for a in fixed}),
            "first_attempt_fix_rate": round(first_attempt_rate, 3),
        },
        "violations": [v.model_dump() for v in state.violations],
        "attempts": [a.model_dump() for a in state.attempts],
        "escalations": [v.model_dump() for v in state.escalations],
    }


# ─────────────────────────────────────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="publer-guard — validate and repair Publer bulk-upload CSVs"
    )
    parser.add_argument("input", type=Path, help="Input CSV file")
    parser.add_argument(
        "--platform",
        required=True,
        choices=[p.value for p in Platform],
        help="Social media platform for all rows in this file",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=None,
        help="Output directory (default: same directory as input)",
    )
    parser.add_argument(
        "--max-retries",
        type=int,
        default=2,
        help="Max Fixer attempts per violation (default: 2)",
    )
    args = parser.parse_args()

    input_path: Path = args.input.resolve()
    platform = Platform(args.platform)
    output_dir = (args.output_dir or input_path.parent).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    stem = input_path.stem

    print(f"publer-guard: loading {input_path}")

    try:
        header, raw_rows, csv_rows = parse_csv(input_path, platform)
    except FileNotFoundError:
        print(f"error: file not found: {input_path}", file=sys.stderr)
        sys.exit(1)

    print(f"  {len(csv_rows)} data row(s), platform={platform.value}")

    client = AnthropicClient()
    orchestrator = Orchestrator(
        triage=TriageAgent(client),
        fixer=FixerAgent(client),
        critic=CriticAgent(client),
        verifier=Verifier(),
    )
    state = PipelineState(
        rows=csv_rows,
        max_retries_per_violation=args.max_retries,
    )

    print("  Running pipeline…")
    state = orchestrator.run(state)

    # ── Write outputs ─────────────────────────────────────────────────────────
    fixed_path = output_dir / f"{stem}_fixed.csv"
    report_path = output_dir / f"{stem}_report.json"
    trace_path = output_dir / f"{stem}_trace.json"

    write_fixed_csv(fixed_path, header, state.rows)
    report = build_report(state)
    report_path.write_text(
        json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    trace_path.write_text(
        json.dumps([e.model_dump() for e in state.trace], indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    # ── Summary ───────────────────────────────────────────────────────────────
    s = report["summary"]
    print(f"\nDone.")
    print(f"  Violations found:      {s['total_violations']}")
    print(f"  Fixed:                 {s['fixed']}")
    print(f"  Escalated to human:    {s['escalated_to_human']}")
    print(f"  Unfixed after retries: {s['unfixed_after_retries']}")
    print(f"  Rows modified:         {s['rows_modified']}")
    print(f"  First-attempt fix rate:{s['first_attempt_fix_rate']:.0%}")
    print(f"\nOutputs written to {output_dir}:")
    print(f"  {fixed_path.name}")
    print(f"  {report_path.name}")
    print(f"  {trace_path.name}")

    if s["escalated_to_human"] or s["unfixed_after_retries"]:
        print(f"\n  Manual review required — see {report_path.name}")
        sys.exit(2)


if __name__ == "__main__":
    main()
