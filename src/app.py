"""
app.py — Flask web UI for publer-guard.

Run from the project root:
  python -m src.app

Opens at http://localhost:5000
"""

from __future__ import annotations

import csv
import io
import tempfile
import zipfile
from pathlib import Path

from flask import Flask, jsonify, render_template, request, send_file, send_from_directory

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass  # env vars read from system environment if python-dotenv is absent

from .agents import AnthropicClient, CriticAgent, FixerAgent, TriageAgent
from .cli import build_report, parse_csv, write_fixed_csv
from .ingest import (
    PLAN_EXTENSIONS,
    PUBLER_HEADER,
    PlanLayoutError,
    PlanSlices,
    row_to_publer,
    slice_plan,
)
from .orchestrator import Orchestrator
from .state import Platform, PipelineState
from .verifier import Verifier

app = Flask(__name__, template_folder=str(Path(__file__).parent.parent / "templates"))

_OUTPUT_DIR = Path(__file__).parent.parent / "output"


@app.route("/")
def index():
    platforms = [p.value for p in Platform]
    return render_template("index.html", platforms=platforms)


def _run_one(uploaded, platform: Platform, max_retries: int) -> dict:
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
        state = PipelineState(rows=csv_rows, max_retries_per_violation=max_retries)
        state = orch.run(state)

        _OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        stem = Path(uploaded.filename).stem
        fixed_name = f"{stem}_fixed.csv"
        write_fixed_csv(_OUTPUT_DIR / fixed_name, header, state.rows)

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


def _run_plan_file(plan, max_retries: int) -> dict:
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
    fixed_name = f"{plan.name}_fixed.csv"
    with (_OUTPUT_DIR / fixed_name).open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f)
        writer.writerow(PUBLER_HEADER)
        for row in state.rows:
            writer.writerow(row_to_publer(row))

    display = f"{plan.name}.csv"
    report = build_report(state)
    report["fixed_csv"] = fixed_name
    report["filename"] = display
    report["platform"] = plan.platform.value
    for v in report["violations"]:
        v["file"] = display
    for a in report["attempts"]:
        a["file"] = display
    return report


_MAX_SKIPPED_DETAIL = 200   # enough to find the gaps; keeps the payload sane


def _ingestion_report(sliced: PlanSlices) -> dict:
    """Account for the plan rows ingestion dropped.

    A half-filled row silently vanishes, which makes a missing output file
    baffling. This turns that into a stated number with a reason: which unit,
    which sheet row, and which mandatory column was empty.
    """
    by_unit: dict[str, dict] = {}
    for row in sliced.skipped:
        entry = by_unit.setdefault(row.unit, {"unit": row.unit, "count": 0, "missing": {}})
        entry["count"] += 1
        for field_name in row.missing:
            entry["missing"][field_name] = entry["missing"].get(field_name, 0) + 1

    produced = {plan.unit for plan in sliced.files}
    return {
        "skipped_rows": len(sliced.skipped),
        # Busiest units first — that's where the plan needs attention.
        "by_unit": sorted(by_unit.values(), key=lambda e: -e["count"]),
        # The ones that explain an absent file entirely.
        "units_without_file": sorted(u for u in by_unit if u not in produced),
        "rows": [
            {"unit": r.unit, "sheet_row": r.sheet_row, "missing": list(r.missing)}
            for r in sliced.skipped[:_MAX_SKIPPED_DETAIL]
        ],
        "rows_truncated": len(sliced.skipped) > _MAX_SKIPPED_DETAIL,
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
    total_fixable = sum(len([v for v in r["violations"] if v.get("auto_fixable")]) for r in reports)
    total_first_fixes = sum(
        len([a for a in r["attempts"] if a.get("outcome") == "fixed" and a.get("attempt_number") == 1])
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
            }
            for r in reports
        ],
    }


@app.route("/run", methods=["POST"])
def run():
    files = request.files.getlist("files")
    platforms = request.form.getlist("platforms")
    files = [f for f in files if f and f.filename]

    if not files:
        return jsonify({"error": "Файлы не выбраны"}), 400
    if len(platforms) != len(files):
        return jsonify({"error": "Каждому файлу должна соответствовать платформа"}), 400

    non_csv = [f.filename for f in files if not f.filename.lower().endswith(".csv")]
    if non_csv:
        return jsonify({"error": "Только .csv файлы: " + ", ".join(non_csv)}), 400

    max_retries = int(request.form.get("max_retries", 2))

    try:
        reports = []
        for uploaded, platform_str in zip(files, platforms):
            if not platform_str:
                return jsonify({"error": f"Не выбрана платформа для {uploaded.filename}"}), 400
            try:
                platform = Platform(platform_str)
            except ValueError:
                return jsonify({"error": f"Неизвестная платформа: {platform_str}"}), 400
            reports.append(_run_one(uploaded, platform, max_retries))

        return jsonify(_merge_reports(reports))

    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@app.route("/run-plan", methods=["POST"])
def run_plan():
    """Ingest a spreadsheet content plan: slice it into per-unit CSVs,
    run each through the pipeline, and return one merged report."""
    uploaded = request.files.get("file")
    if not uploaded or not uploaded.filename:
        return jsonify({"error": "Файл плана не выбран"}), 400

    ext = Path(uploaded.filename).suffix.lower()
    if ext not in PLAN_EXTENSIONS:
        allowed = " / ".join(PLAN_EXTENSIONS)
        return jsonify({"error": f"Только {allowed} файлы контент-плана"}), 400

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
                f" Пропущено незаполненных строк: {len(sliced.skipped)}."
                if sliced.skipped else ""
            )
            return jsonify({
                "error": "В плане не найдено ни одного поста "
                         f"(лист «Контент-план»?).{detail}"
            }), 400

        reports = [_run_plan_file(plan, max_retries) for plan in sliced.files]
        merged = _merge_reports(reports)
        merged["ingestion"] = _ingestion_report(sliced)
        return jsonify(merged)

    except PlanLayoutError as exc:
        # Layout is wrong — nothing was converted. Every mismatch is returned,
        # however many: fixing the sheet means seeing all of them, so the UI
        # pages through the list rather than the server truncating it.
        return jsonify({
            "error": "Макет не совпадает с шаблоном — необходимо поправить макет.",
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
def download(filename: str):
    return send_from_directory(_OUTPUT_DIR, filename, as_attachment=True)


@app.route("/download-all")
def download_all():
    """Bundle the requested fixed CSVs into a single ZIP, built in-memory.

    Filenames come from the query string (?files=a.csv&files=b.csv). Only
    basenames of existing files inside the output dir are allowed — this
    guards against path traversal.
    """
    requested = request.args.getlist("files")
    if not requested:
        return jsonify({"error": "Нет файлов для архива"}), 400

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
        return jsonify({"error": "Файлы не найдены"}), 404

    buf.seek(0)
    return send_file(
        buf,
        mimetype="application/zip",
        as_attachment=True,
        download_name="publer-guard_fixed_csvs.zip",
    )


if __name__ == "__main__":
    app.run(debug=True, port=5000)
