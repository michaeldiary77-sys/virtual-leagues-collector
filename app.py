"""Tableau de bord web pour piloter les collecteurs multi-ligues (voir collector.py).

Un seul processus à garder ouvert : gère les collecteurs comme sous-processus
enfants, sert une page de suivi/pilotage à http://localhost:5000.

Usage :
    python app.py
"""

import csv
import json
import os
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone

import psutil
from flask import Flask, jsonify, request, send_from_directory

import api_client
from collector import build_paths, load_leagues_registry, SEASON_LENGTH_BY_LEAGUE
import merge_dataset

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
PYTHON = sys.executable
ROUND_CHECK_INTERVAL_SECONDS = 30

app = Flask(__name__, static_folder="static", static_url_path="")

# slug -> {round_number, event_category_id, expected_start, checked_at}.
# Alimenté en continu par round_checker_loop() (thread de fond), indépendamment
# de si un collecteur tourne ou non pour cette ligue — c'est ce qui permet au
# dashboard d'afficher le round actuel en direct sans bouton manuel.
LATEST_ROUNDS: dict = {}

# slug -> pid (int). Reconstruit au démarrage à partir de status.json + psutil,
# donc pas de dépendance à la mémoire du process app.py entre deux redémarrages.
RUNNING_PIDS: dict[str, int] = {}


def read_json(path: str):
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return None


def count_csv_rows(path: str, filter_col: str | None = None, filter_val: str | None = None) -> int:
    if not os.path.exists(path):
        return 0
    count = 0
    with open(path, "r", newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            if filter_col is None or row.get(filter_col) == filter_val:
                count += 1
    return count


def is_collector_process(pid: int, slug: str) -> bool:
    try:
        proc = psutil.Process(pid)
        if not proc.is_running():
            return False
        cmdline = " ".join(proc.cmdline())
        return "collector.py" in cmdline and slug in cmdline
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return False


def round_checker_loop() -> None:
    """Tourne en continu dans un thread de fond : rafraîchit LATEST_ROUNDS pour
    toutes les ligues, qu'un collecteur soit actif ou non pour chacune."""
    while True:
        for slug, entry_point_id in load_leagues_registry().items():
            rounds = api_client.get_current_matches(entry_point_id)
            if rounds is api_client.NETWORK_UNAVAILABLE or not rounds:
                continue
            current = rounds[0]
            LATEST_ROUNDS[slug] = {
                "round_number": current.get("roundNumber"),
                "event_category_id": current.get("eventCategoryId"),
                "expected_start": current.get("expectedStart"),
                "checked_at": datetime.now(timezone.utc).isoformat(),
            }
        time.sleep(ROUND_CHECK_INTERVAL_SECONDS)


def reconcile_running_pids() -> None:
    """Au démarrage de app.py : retrouve les collecteurs déjà en cours (lancés
    avant un redémarrage du dashboard) via le pid stocké dans status.json."""
    for slug, entry_point_id in load_leagues_registry().items():
        paths = build_paths(entry_point_id, slug)
        status = read_json(paths["status_file"])
        if status and status.get("pid") and is_collector_process(status["pid"], slug):
            RUNNING_PIDS[slug] = status["pid"]


def get_league_info(slug: str, entry_point_id: int) -> dict:
    paths = build_paths(entry_point_id, slug)
    status = read_json(paths["status_file"])

    pid = RUNNING_PIDS.get(slug)
    running = pid is not None and is_collector_process(pid, slug)
    if not running and pid is not None:
        RUNNING_PIDS.pop(slug, None)

    return {
        "slug": slug,
        "entry_point_id": entry_point_id,
        "running": running,
        "pid": pid if running else None,
        "status": status,
        "live_round": LATEST_ROUNDS.get(slug),
        "season_length": SEASON_LENGTH_BY_LEAGUE.get(slug),
        "matches_total": count_csv_rows(paths["matches_csv"]),
        "matches_with_odds": count_csv_rows(paths["matches_csv"], "had_odds", "True"),
    }


@app.route("/")
def index():
    return send_from_directory(app.static_folder, "index.html")


@app.route("/api/leagues")
def api_leagues():
    registry = load_leagues_registry()
    return jsonify([get_league_info(slug, eid) for slug, eid in registry.items()])


@app.route("/api/leagues/<slug>/start", methods=["POST"])
def api_start(slug):
    registry = load_leagues_registry()
    if slug not in registry:
        return jsonify({"error": f"ligue inconnue: {slug}"}), 404

    entry_point_id = registry[slug]
    paths = build_paths(entry_point_id, slug)

    pid = RUNNING_PIDS.get(slug)
    if pid is not None and is_collector_process(pid, slug):
        return jsonify({"error": "déjà en cours", "pid": pid}), 409

    # Détecte un collecteur lancé ailleurs (pas suivi par ce process app.py)
    # dont le statut a été mis à jour très récemment.
    status = read_json(paths["status_file"])
    if status and status.get("pid") and is_collector_process(status["pid"], slug):
        RUNNING_PIDS[slug] = status["pid"]
        return jsonify({"error": "déjà en cours (détecté via status.json)", "pid": status["pid"]}), 409

    os.makedirs(paths["data_dir"], exist_ok=True)
    log_path = os.path.join(paths["data_dir"], "collector.log")
    log_fh = open(log_path, "w", encoding="utf-8")
    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}  # sinon le sous-processus écrit dans l'encodage console Windows
    proc = subprocess.Popen(
        [PYTHON, "-u", "collector.py", "--league", slug],
        stdout=log_fh, stderr=subprocess.STDOUT, cwd=PROJECT_DIR, env=env,
    )
    RUNNING_PIDS[slug] = proc.pid
    return jsonify({"started": True, "pid": proc.pid})


@app.route("/api/leagues/<slug>/stop", methods=["POST"])
def api_stop(slug):
    registry = load_leagues_registry()
    if slug not in registry:
        return jsonify({"error": f"ligue inconnue: {slug}"}), 404

    paths = build_paths(registry[slug], slug)
    pid = RUNNING_PIDS.get(slug)
    if pid is None:
        status = read_json(paths["status_file"])
        if status and status.get("pid") and is_collector_process(status["pid"], slug):
            pid = status["pid"]

    if pid is None or not is_collector_process(pid, slug):
        RUNNING_PIDS.pop(slug, None)
        return jsonify({"error": "pas en cours"}), 409

    try:
        psutil.Process(pid).terminate()
    except psutil.NoSuchProcess:
        pass
    RUNNING_PIDS.pop(slug, None)
    return jsonify({"stopped": True})


@app.route("/api/leagues/<slug>/log")
def api_log(slug):
    registry = load_leagues_registry()
    if slug not in registry:
        return jsonify({"error": f"ligue inconnue: {slug}"}), 404
    n_lines = request.args.get("lines", default=200, type=int)
    paths = build_paths(registry[slug], slug)
    log_path = os.path.join(paths["data_dir"], "collector.log")
    if not os.path.exists(log_path):
        return jsonify({"lines": []})
    with open(log_path, "r", encoding="utf-8", errors="replace") as f:
        lines = f.readlines()
    return jsonify({"lines": [l.rstrip("\n") for l in lines[-n_lines:]]})


@app.route("/api/merge", methods=["POST"])
def api_merge():
    scope = request.args.get("scope", default="all")
    registry = load_leagues_registry()

    if scope == "all":
        all_rows = []
        for slug, entry_point_id in registry.items():
            all_rows.extend(merge_dataset.merge_one(entry_point_id, slug))
        merge_dataset.write_csv("data/dataset_global.csv", all_rows)
        return jsonify({"merged": "all", "total_rows": len(all_rows)})

    if scope not in registry:
        return jsonify({"error": f"ligue inconnue: {scope}"}), 404
    rows = merge_dataset.merge_one(registry[scope], scope)
    return jsonify({"merged": scope, "rows": len(rows)})


if __name__ == "__main__":
    reconcile_running_pids()
    threading.Thread(target=round_checker_loop, daemon=True).start()
    app.run(host="127.0.0.1", port=5000, debug=False, threaded=True)
