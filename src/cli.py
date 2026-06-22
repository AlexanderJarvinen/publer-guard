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
            # Publer export format (7 columns):
            #   0: Scheduled Date  1: Type  2: Text  3: Media URL
            #   4: Hashtags        5: Tagged Users   6: Title (used as label)
            # Pad to 7 fields; column_count violations are caught by the linter.
            padded = (fields + [""] * 7)[:7]
            text = padded[2].strip()
            hashtags = padded[4].strip()
            if hashtags:
                text = text + "\n\n" + hashtags
            csv_rows.append(CsvRow(
                row_index=idx,
                platform=platform,
                date=padded[0].strip(),
                text=text,
                link="",          # no Link column in this format
                media_url=padded[3].strip(),
                title=padded[1].strip(),   # Type field (Photo / Video / …)
                label=padded[6].strip(),   # Title field → used for series detection
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


# Standard Anthropic API rates, USD per 1M tokens. Verified Jun 2026 against
# public pricing pages; confirm in the console before trusting cost figures.
_PRICING_PER_MTOK: dict[str, tuple[float, float]] = {
    "claude-haiku-4-5":  (1.0, 5.0),
    "claude-sonnet-4-6": (3.0, 15.0),
    "claude-opus-4-8":   (5.0, 25.0),
}


def _rate_for(model: str) -> tuple[float, float]:
    """Match by prefix so dated strings (…-20251001) still resolve."""
    for key, rate in _PRICING_PER_MTOK.items():
        if model.startswith(key):
            return rate
    return (0.0, 0.0)


def _percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    k = max(0, min(len(s) - 1, int(round((pct / 100.0) * (len(s) - 1)))))
    return s[k]


def build_monitoring(state: PipelineState) -> dict:
    """Point 7 — cost / latency / volume rolled up from per-call metrics."""
    by_model: dict[str, dict] = {}
    for m in state.metrics:
        agg = by_model.setdefault(
            m.model,
            {"calls": 0, "input_tokens": 0, "output_tokens": 0, "latencies": []},
        )
        agg["calls"] += 1
        agg["input_tokens"] += m.input_tokens
        agg["output_tokens"] += m.output_tokens
        if m.latency_ms:
            agg["latencies"].append(m.latency_ms)

    total_cost = 0.0
    models_out: dict[str, dict] = {}
    for model, a in by_model.items():
        in_rate, out_rate = _rate_for(model)
        cost = a["input_tokens"] / 1e6 * in_rate + a["output_tokens"] / 1e6 * out_rate
        total_cost += cost
        models_out[model] = {
            "calls": a["calls"],
            "input_tokens": a["input_tokens"],
            "output_tokens": a["output_tokens"],
            "est_cost_usd": round(cost, 6),
        }

    all_latencies = [m.latency_ms for m in state.metrics if m.latency_ms]
    return {
        "total_calls": len(state.metrics),
        "total_input_tokens": sum(m.input_tokens for m in state.metrics),
        "total_output_tokens": sum(m.output_tokens for m in state.metrics),
        "est_cost_usd": round(total_cost, 6),
        # True ⇒ FakeLLMClient run; figures are illustrative, not billed.
        "tokens_estimated": any(m.estimated for m in state.metrics),
        "latency_ms": (
            {
                "p50": round(_percentile(all_latencies, 50), 1),
                "p95": round(_percentile(all_latencies, 95), 1),
                "total": round(sum(all_latencies), 1),
            }
            if all_latencies else None
        ),
        "by_model": models_out,
    }


def build_post_fix_lint(state: PipelineState) -> dict:
    """Point 3 — result of the full re-lint after all fixes are applied."""
    initial_keys = {(v.rule_id, v.row_index) for v in state.violations}
    introduced = [
        v for v in state.final_violations
        if (v.rule_id, v.row_index) not in initial_keys
    ]
    residual = [
        v for v in state.final_violations
        if (v.rule_id, v.row_index) in initial_keys
    ]
    return {
        "clean": len(state.final_violations) == 0,
        "introduced_by_fix": [v.model_dump() for v in introduced],
        "residual_known": len(residual),
        "total_remaining": len(state.final_violations),
    }


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
        "post_fix_lint": build_post_fix_lint(state),
        "monitoring": build_monitoring(state),
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
        help="Output directory (default: ./output/)",
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
    output_dir = (args.output_dir or Path("output")).resolve()
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

    mon = report["monitoring"]
    pfl = report["post_fix_lint"]
    cost_note = "  (estimated — Fake client)" if mon["tokens_estimated"] else ""
    print(f"\n  LLM calls:             {mon['total_calls']}")
    print(f"  Tokens in/out:         {mon['total_input_tokens']}/{mon['total_output_tokens']}")
    print(f"  Est. cost:             ${mon['est_cost_usd']:.4f}{cost_note}")
    if mon["latency_ms"]:
        lat = mon["latency_ms"]
        print(f"  Latency p50/p95 (ms):  {lat['p50']}/{lat['p95']}")
    print(
        f"  Post-fix re-lint:      "
        f"{'CLEAN' if pfl['clean'] else str(pfl['total_remaining']) + ' remaining'}"
    )
    if pfl["introduced_by_fix"]:
        print(
            f"  [!] NEW violations introduced by fixes: "
            f"{len(pfl['introduced_by_fix'])} (see {report_path.name})"
        )

    if (
        s["escalated_to_human"]
        or s["unfixed_after_retries"]
        or pfl["introduced_by_fix"]
    ):
        print(f"\n  Manual review required — see {report_path.name}")
        sys.exit(2)


if __name__ == "__main__":
    main()
