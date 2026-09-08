"""Flask server for the networking-process visualization.

Run:  python3 -m viz.server   (default http://127.0.0.1:5000)
"""

from __future__ import annotations

from pathlib import Path

from flask import Flask, jsonify, request, send_from_directory

from viz.trace import build_scenario_trace, list_scenarios

STATIC_DIR = Path(__file__).parent / "static"

app = Flask(__name__, static_folder=None)


@app.get("/")
def index():
    return send_from_directory(STATIC_DIR, "index.html")


@app.get("/static/<path:filename>")
def static_files(filename: str):
    return send_from_directory(STATIC_DIR, filename)


@app.get("/api/scenarios")
def api_scenarios():
    return jsonify(list_scenarios())


@app.get("/api/trace")
def api_trace():
    scenario = request.args.get("scenario", "tier1")
    return jsonify(build_scenario_trace(scenario))


def main() -> None:
    app.run(host="127.0.0.1", port=5000, debug=False)


if __name__ == "__main__":
    main()
