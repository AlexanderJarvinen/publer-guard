"""
app.py — Flask web UI for publer-guard.

Run from the project root:
  python -m src.app

Opens at http://localhost:5000
"""

from __future__ import annotations

import csv
import io
import json
import tempfile
import zipfile
from collections import Counter
from collections.abc import Iterator
from pathlib import Path

from flask import (
    Flask,
    Response,
    jsonify,
    render_template,
    request,
    send_file,
    send_from_directory,
    stream_with_context,
)
from werkzeug.datastructures import FileStorage

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass  # env vars read from system environment if python-dotenv is absent

from .agents import AnthropicClient, CriticAgent, FixerAgent, TriageAgent
from .cli import build_report, parse_csv, write_fixed_csv
from .ingest import (
    PLAN_EXTENSIONS,
    PLAN_SPECS,
    PUBLER_HEADER,
    PlanCsv,
    PlanLayoutError,
    PlanSlices,
    row_to_publer,
    slice_plan,
)
from .orchestrator import Orchestrator
from .state import PipelineState, Platform
from .verifier import Verifier

app = Flask(__name__, template_folder=str(Path(__file__).parent.parent / "templates"))

_OUTPUT_DIR = Path(__file__).parent.parent / "output"


def _unique_output_path(name: str) -> Path:
    """A path in output/ that will not silently overwrite an earlier run:
    same-named plans get a ' (2)', ' (3)', … suffix instead."""
    candidate = _OUTPUT_DIR / name
    stem, suffix = candidate.stem, candidate.suffix
    counter = 2
    while candidate.exists():
        candidate = _OUTPUT_DIR / f"{stem} ({counter}){suffix}"
        counter += 1
    return candidate


@app.route("/")
def index() -> str:
    """Serve the single-page UI."""
    # A page load starts a fresh session: the download buttons from the
    # previous one are gone with it, so its files are unreachable — clear
    # them instead of letting output/ grow forever on a server.
    if _OUTPUT_DIR.exists():
        for f in _OUTPUT_DIR.iterdir():
            if f.is_file():
                f.unlink()
    platforms = [p.value for p in Platform]
    return render_template("index.html", platforms=platforms)


def _run_one(uploaded: FileStorage, platform: Platform, max_retries: int) -> dict:
    """Run the pipeline on a single uploaded CSV and return its report,
    with every violation/attempt tagged by source filename."""
    with tempfile.NamedTemporaryFile(suffix=".csv", delete=False) as tmp:
        uploaded.save(tmp.name)
        tmp_path = Path(tmp.name)

    try:
        header, raw_rows, csv_rows = parse_csv(tmp_path, platform)

        client = AnthropicClient()
        orch = Orchestrator(
            triage=TriageAgent(client),
            fixer=FixerAgent(client),
            critic=CriticAgent(client),
            verifier=Verifier(),
        )
        state = PipelineState(
            rows=csv_rows, raw_rows=raw_rows, max_retries_per_violation=max_retries
        )
        state = orch.run(state)

        _OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        stem = Path(uploaded.filename or "upload").stem
        fixed_path = _unique_output_path(f"{stem}_fixed.csv")
        fixed_name = fixed_path.name
        write_fixed_csv(fixed_path, state.rows, raw_rows)

        report = build_report(state)
        report["fixed_csv"] = fixed_name
        report["filename"] = uploaded.filename
        report["platform"] = platform.value

        # Tag rows so the merged view knows which file each came from.
        for v in report["violations"]:
            v["file"] = uploaded.filename
        for a in report["attempts"]:
            a["file"] = uploaded.filename
        return report

    finally:
        tmp_path.unlink(missing_ok=True)


def _run_plan_file(plan: PlanCsv, max_retries: int) -> dict:
    """Run the pipeline on one PlanCsv sliced from a content plan. Rows are
    already CsvRow objects with the platform assigned; output is written as a
    12-column Publer import template."""
    client = AnthropicClient()
    orch = Orchestrator(
        triage=TriageAgent(client),
        fixer=FixerAgent(client),
        critic=CriticAgent(client),
        verifier=Verifier(),
    )
    state = PipelineState(rows=plan.rows, max_retries_per_violation=max_retries)
    state = orch.run(state)

    _OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    fixed_path = _unique_output_path(f"{plan.name}_fixed.csv")
    fixed_name = fixed_path.name
    with fixed_path.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f)
        writer.writerow(PUBLER_HEADER)
        for row in state.rows:
            writer.writerow(row_to_publer(row))

    display = f"{plan.name}.csv"
    report = build_report(state)
    report["fixed_csv"] = fixed_name
    report["filename"] = display
    report["platform"] = plan.platform.value
    # What this particular CSV is missing, said where it gets downloaded.
    report["row_count"] = len(plan.rows)
    report["unfinished_rows"] = list(plan.unfinished)
    for v in report["violations"]:
        v["file"] = display
    for a in report["attempts"]:
        a["file"] = display
    return report


def _ingestion_report(sliced: PlanSlices) -> dict:
    """Everything ingestion wants to tell the author about the plan.

    Two kinds, both advisory: a row started and not finished (it never becomes
    a post), and a finished post with no hashtags (it publishes as written).
    Every warning is returned — the UI pages through them — with the unit, the
    sheet row and the cells at issue.
    """
    by_unit: dict[str, dict] = {}
    for warning in sliced.warnings:
        entry = by_unit.setdefault(
            warning.unit, {"unit": warning.unit, "count": 0, "missing": {}}
        )
        entry["count"] += 1
        for label in warning.labelled():
            entry["missing"][label] = entry["missing"].get(label, 0) + 1

    produced = {plan.unit for plan in sliced.files}
    kinds = Counter(w.kind for w in sliced.warnings)
    return {
        "warnings": len(sliced.warnings),
        "incomplete_rows": kinds["incomplete"],
        "posts_without_hashtags": kinds["no_hashtags"],
        "malformed_hashtags": kinds["malformed_hashtags"],
        # Busiest units first — that's where the plan needs attention.
        "by_unit": sorted(by_unit.values(), key=lambda e: -e["count"]),
        # Named from the specs, not from the warnings: a unit can end up with
        # no file having produced no warning at all, and that still needs saying.
        "units_without_file": [s.suffix for s in PLAN_SPECS if s.suffix not in produced],
        "rows": [
            {
                "unit": w.unit,
                "sheet_row": w.sheet_row,
                "kind": w.kind,
                "columns": list(w.columns),
                "message": w.message(),
            }
            for w in sliced.warnings
        ],
    }


def _merge_reports(reports: list[dict]) -> dict:
    """Aggregate several single-file reports into one combined report that
    keeps the same top-level shape the frontend already renders."""
    summary_keys = [
        "total_violations", "fixed", "escalated_to_human",
        "unfixed_after_retries", "rows_modified",
    ]
    summary = {k: sum(r["summary"][k] for r in reports) for k in summary_keys}

    # Recompute first-attempt rate across the combined fixable population.
    total_fixable = sum(
        len([v for v in r["violations"] if v.get("auto_fixable")]) for r in reports
    )
    total_first_fixes = sum(
        len([
            a for a in r["attempts"]
            if a.get("outcome") == "fixed" and a.get("attempt_number") == 1
        ])
        for r in reports
    )
    summary["first_attempt_fix_rate"] = round(
        total_first_fixes / total_fixable if total_fixable else 1.0, 3
    )

    violations = [v for r in reports for v in r["violations"]]
    attempts = [a for r in reports for a in r["attempts"]]
    escalations = [e for r in reports for e in r["escalations"]]

    # Post-fix lint — combined.
    post_fix_lint = {
        "clean": all(r["post_fix_lint"]["clean"] for r in reports),
        "introduced_by_fix": [v for r in reports for v in r["post_fix_lint"]["introduced_by_fix"]],
        "residual_known": sum(r["post_fix_lint"]["residual_known"] for r in reports),
        "total_remaining": sum(r["post_fix_lint"]["total_remaining"] for r in reports),
    }

    # Monitoring — sum the numeric fields, merge by_model.
    by_model: dict = {}
    for r in reports:
        for model, m in r["monitoring"].get("by_model", {}).items():
            agg = by_model.setdefault(
                model,
                {"calls": 0, "input_tokens": 0, "output_tokens": 0, "est_cost_usd": 0.0},
            )
            agg["calls"] += m["calls"]
            agg["input_tokens"] += m["input_tokens"]
            agg["output_tokens"] += m["output_tokens"]
            agg["est_cost_usd"] = round(agg["est_cost_usd"] + m["est_cost_usd"], 6)
    monitoring = {
        "total_calls": sum(r["monitoring"]["total_calls"] for r in reports),
        "total_input_tokens": sum(r["monitoring"]["total_input_tokens"] for r in reports),
        "total_output_tokens": sum(r["monitoring"]["total_output_tokens"] for r in reports),
        "est_cost_usd": round(sum(r["monitoring"]["est_cost_usd"] for r in reports), 6),
        "tokens_estimated": any(r["monitoring"]["tokens_estimated"] for r in reports),
        "by_model": by_model,
    }

    return {
        "summary": summary,
        "violations": violations,
        "attempts": attempts,
        "escalations": escalations,
        "post_fix_lint": post_fix_lint,
        "monitoring": monitoring,
        "files": [
            {
                "filename": r["filename"],
                "platform": r["platform"],
                "fixed_csv": r["fixed_csv"],
                "summary": r["summary"],
                # Absent for plain CSV uploads — nothing was sliced there.
                "row_count": r.get("row_count"),
                "unfinished_rows": r.get("unfinished_rows", []),
            }
            for r in reports
        ],
    }


@app.route("/run", methods=["POST"])
def run() -> Response | tuple[Response, int]:
    """Validate and repair one or more uploaded CSVs; return a merged report."""
    files = request.files.getlist("files")
    platforms = request.form.getlist("platforms")
    files = [f for f in files if f and f.filename]

    if not files:
        return jsonify({"error": "No files selected"}), 400
    if len(platforms) != len(files):
        return jsonify({"error": "Each file needs a platform"}), 400

    non_csv = [
        f.filename or "?" for f in files
        if not (f.filename or "").lower().endswith(".csv")
    ]
    if non_csv:
        return jsonify({"error": "Only .csv files are accepted: " + ", ".join(non_csv)}), 400

    max_retries = int(request.form.get("max_retries", 2))

    try:
        reports = []
        # strict=False: a missing platform entry is reported per-file below,
        # not blown up as a ValueError before any file is processed.
        for uploaded, platform_str in zip(files, platforms, strict=False):
            if not platform_str:
                return jsonify({"error": f"No platform selected for {uploaded.filename}"}), 400
            try:
                platform = Platform(platform_str)
            except ValueError:
                return jsonify({"error": f"Unknown platform: {platform_str}"}), 400
            reports.append(_run_one(uploaded, platform, max_retries))

        return jsonify(_merge_reports(reports))

    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@app.route("/run-plan", methods=["POST"])
def run_plan() -> Response | tuple[Response, int]:
    """Ingest a spreadsheet content plan: slice it into per-unit CSVs,
    run each through the pipeline, and return one merged report."""
    uploaded = request.files.get("file")
    if not uploaded or not uploaded.filename:
        return jsonify({"error": "No plan file selected"}), 400

    ext = Path(uploaded.filename).suffix.lower()
    if ext not in PLAN_EXTENSIONS:
        allowed = " / ".join(PLAN_EXTENSIONS)
        return jsonify({"error": f"Only {allowed} content-plan files are accepted"}), 400

    max_retries = int(request.form.get("max_retries", 2))

    with tempfile.NamedTemporaryFile(suffix=ext, delete=False) as tmp:
        uploaded.save(tmp.name)
        tmp_path = Path(tmp.name)

    try:
        # Pass the upload's real name: output files are prefixed with it,
        # and tmp_path is a random temp name.
        sliced = slice_plan(tmp_path, source_name=uploaded.filename)
        if not sliced.files:
            detail = (
                f" Rows left unfinished: {len(sliced.warnings)}."
                if sliced.warnings else ""
            )
            return jsonify({
                "error": "No posts found in the plan "
                         f"(is the sheet named “CONTENT PLAN”?).{detail}"
            }), 400

        # Reading is done; everything below works from memory. Freeing the
        # upload here keeps the cleanup out of the streaming generator, which
        # outlives this function.
        tmp_path.unlink(missing_ok=True)

        # A plan with real violations in it takes a minute of model round
        # trips. Streaming a line per file turns that from a hung browser tab
        # into something with a progress bar.
        def report_progress() -> Iterator[str]:
            """Yield one NDJSON line per file processed, then the report."""
            total = len(sliced.files)
            try:
                reports = []
                for done, plan in enumerate(sliced.files):
                    yield json.dumps({"progress": {
                        "done": done,
                        "total": total,
                        "unit": plan.unit,
                        "rows": len(plan.rows),
                    }}, ensure_ascii=False) + "\n"
                    reports.append(_run_plan_file(plan, max_retries))

                merged = _merge_reports(reports)
                merged["ingestion"] = _ingestion_report(sliced)
                yield json.dumps({"result": merged}, ensure_ascii=False) + "\n"
            except Exception as exc:   # noqa: BLE001 — reported, not swallowed
                # The status line is already 200 by now, so a failure has to
                # travel as a payload rather than a status code.
                yield json.dumps({"error": str(exc)}, ensure_ascii=False) + "\n"

        return Response(
            stream_with_context(report_progress()),
            mimetype="application/x-ndjson",
        )

    except PlanLayoutError as exc:
        # Layout is wrong — nothing was converted. Every mismatch is returned,
        # however many: fixing the sheet means seeing all of them, so the UI
        # pages through the list rather than the server truncating it.
        return jsonify({
            "error": "The sheet layout does not match the template — fix it first.",
            "layout_mismatches": [
                {
                    "column": m.column,
                    "expected": m.expected,
                    "found": m.found,
                    "units": list(m.units),
                    "message": m.message(),
                }
                for m in exc.mismatches
            ],
        }), 400

    except Exception as exc:
        return jsonify({"error": str(exc)}), 500

    finally:
        tmp_path.unlink(missing_ok=True)


@app.route("/download/<path:filename>")
def download(filename: str) -> Response:
    """Serve one fixed CSV from the output directory."""
    return send_from_directory(_OUTPUT_DIR, filename, as_attachment=True)


@app.route("/download-all")
def download_all() -> Response | tuple[Response, int]:
    """Bundle the requested fixed CSVs into a single ZIP, built in-memory.

    Filenames come from the query string (?files=a.csv&files=b.csv). Only
    basenames of existing files inside the output dir are allowed — this
    guards against path traversal.
    """
    requested = request.args.getlist("files")
    if not requested:
        return jsonify({"error": "No files requested for the archive"}), 400

    buf = io.BytesIO()
    added = 0
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name in requested:
            safe = Path(name).name           # strip any directory component
            path = _OUTPUT_DIR / safe
            if path.is_file():
                zf.write(path, arcname=safe)
                added += 1

    if not added:
        return jsonify({"error": "Files not found"}), 404

    buf.seek(0)
    return send_file(
        buf,
        mimetype="application/zip",
        as_attachment=True,
        download_name="publer-guard_fixed_csvs.zip",
    )


if __name__ == "__main__":
    app.run(debug=True, port=5000)
