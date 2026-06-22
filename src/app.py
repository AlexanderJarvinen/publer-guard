"""
app.py — Flask web UI for publer-guard.

Run from the project root:
  python -m src.app

Opens at http://localhost:5000
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from flask import Flask, jsonify, render_template, request, send_from_directory

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass  # env vars read from system environment if python-dotenv is absent

from .agents import AnthropicClient, CriticAgent, FixerAgent, TriageAgent
from .cli import build_report, parse_csv, write_fixed_csv
from .orchestrator import Orchestrator
from .state import Platform, PipelineState
from .verifier import Verifier

app = Flask(__name__, template_folder=str(Path(__file__).parent.parent / "templates"))

_OUTPUT_DIR = Path(__file__).parent.parent / "output"


@app.route("/")
def index():
    platforms = [p.value for p in Platform]
    return render_template("index.html", platforms=platforms)


@app.route("/run", methods=["POST"])
def run():
    uploaded = request.files.get("file")
    if not uploaded:
        return jsonify({"error": "Файл не выбран"}), 400

    platform_str = request.form.get("platform", "facebook")
    max_retries = int(request.form.get("max_retries", 2))

    try:
        platform = Platform(platform_str)
    except ValueError:
        return jsonify({"error": f"Неизвестная платформа: {platform_str}"}), 400

    # Save upload to a temp file, run pipeline, clean up
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
        return jsonify(report)

    except Exception as exc:
        return jsonify({"error": str(exc)}), 500

    finally:
        tmp_path.unlink(missing_ok=True)


@app.route("/download/<path:filename>")
def download(filename: str):
    return send_from_directory(_OUTPUT_DIR, filename, as_attachment=True)


if __name__ == "__main__":
    app.run(debug=True, port=5000)
