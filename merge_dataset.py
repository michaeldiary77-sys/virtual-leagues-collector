"""Fusionne matches.csv, goals.csv et odds.csv d'une ligue en un seul jeu de
données "plat" (1 ligne par match), prêt pour l'analyse.

Usage :
    python merge_dataset.py --league english-league
    python merge_dataset.py --league coupe-du-monde
    python merge_dataset.py --entry-point-id 9999 --league-name nouvelle-ligue
    python merge_dataset.py --all                      # toutes les ligues de leagues.json,
                                                          # + un data/dataset_global.csv combiné

Les fonctions de construction (build_row / build_rows_from_matches) sont
réutilisées par collector.py pour archiver une saison terminée au même format.
"""

import argparse
import csv
from collections import defaultdict

from collector import build_paths, load_leagues_registry, resolve_league

OUTPUT_HEADER = [
    "match_key", "league", "season_id", "round_number", "expected_start", "home_team", "away_team",
    "home_score", "away_score",
    "odds_1", "odds_X", "odds_2", "odds_dc_1x", "odds_dc_x2", "odds_dc_12",
    "odds_over_2_5", "odds_under_2_5", "odds_btts_yes", "odds_btts_no",
    "over_2_5", "btts", "home_win", "draw", "away_win",
    "first_goal_team", "last_goal_minute", "goal_timeline", "had_odds",
]

# (bet_type_name dans odds.csv, outcome_short_name) -> colonne de sortie.
# Valeurs confirmées en interrogeant l'API en direct (2026-08-27), identiques
# quelle que soit la ligue (même moteur de jeu, mêmes marchés).
CURATED_ODDS_MAP = {
    ("1X2", "1"): "odds_1",
    ("1X2", "X"): "odds_X",
    ("1X2", "2"): "odds_2",
    ("Double Chance", "1X"): "odds_dc_1x",
    ("Double Chance", "X2"): "odds_dc_x2",
    ("Double Chance", "12"): "odds_dc_12",
    ("+/-", "> 2.5"): "odds_over_2_5",
    ("+/-", "< 2.5"): "odds_under_2_5",
    ("G/NG", "Oui"): "odds_btts_yes",
    ("G/NG", "Non"): "odds_btts_no",
}


def to_int_or_none(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def oui_non(value: bool | None) -> str:
    if value is None:
        return ""
    return "Oui" if value else "Non"


def index_curated_odds(odds_rows) -> dict:
    """Lignes d'odds.csv (itérable de dicts) -> {match_key: {colonne_sortie: cote}}."""
    odds_by_match = defaultdict(dict)
    for row in odds_rows:
        column = CURATED_ODDS_MAP.get((row["bet_type_name"], row["outcome_short_name"]))
        if column is not None:
            odds_by_match[row["match_key"]][column] = row["odds"]
    return odds_by_match


def index_goals(goals_rows) -> dict:
    """Lignes de goals.csv (itérable de dicts) -> {match_key: [(minute, scoring_team), ...] triée}."""
    goals_by_match = defaultdict(list)
    for row in goals_rows:
        minute = to_int_or_none(row["minute"])
        if minute is None:
            continue
        goals_by_match[row["match_key"]].append((minute, row["scoring_team"]))
    for key in goals_by_match:
        goals_by_match[key].sort(key=lambda g: g[0])
    return goals_by_match


def load_curated_odds(odds_csv: str) -> dict:
    with open(odds_csv, "r", newline="", encoding="utf-8") as f:
        return index_curated_odds(csv.DictReader(f))


def load_goals(goals_csv: str) -> dict:
    with open(goals_csv, "r", newline="", encoding="utf-8") as f:
        return index_goals(csv.DictReader(f))


def build_row(m: dict, league_slug: str, goals_by_match: dict, odds_by_match: dict) -> dict:
    """Construit une ligne du format dataset.csv à partir d'une ligne de matches.csv
    (déjà lue, en dict) + des index de buts/cotes correspondants."""
    key = m["match_key"]
    home_score = to_int_or_none(m["home_score"])
    away_score = to_int_or_none(m["away_score"])

    odds = odds_by_match.get(key, {})
    goals = goals_by_match.get(key, [])

    total_goals = None
    if home_score is not None and away_score is not None:
        total_goals = home_score + away_score

    row = {
        "match_key": key,
        "league": league_slug,
        "season_id": m.get("season_id", ""),
        "round_number": m["round_number"],
        "expected_start": m["expected_start"],
        "home_team": m["home_team"],
        "away_team": m["away_team"],
        "home_score": home_score if home_score is not None else "",
        "away_score": away_score if away_score is not None else "",
        "over_2_5": oui_non(total_goals > 2.5 if total_goals is not None else None),
        "btts": oui_non(
            (home_score > 0 and away_score > 0)
            if home_score is not None and away_score is not None else None
        ),
        "home_win": oui_non(
            home_score > away_score
            if home_score is not None and away_score is not None else None
        ),
        "draw": oui_non(
            home_score == away_score
            if home_score is not None and away_score is not None else None
        ),
        "away_win": oui_non(
            away_score > home_score
            if home_score is not None and away_score is not None else None
        ),
        "first_goal_team": goals[0][1] if goals else "",
        "last_goal_minute": goals[-1][0] if goals else "",
        "goal_timeline": "[" + ", ".join(str(g[0]) for g in goals) + "]" if goals else "[]",
        "had_odds": m["had_odds"],
    }
    for column in CURATED_ODDS_MAP.values():
        row[column] = odds.get(column, "")
    return row


def build_rows_from_matches(league_slug: str, matches_rows: list, goals_rows: list, odds_rows: list) -> list[dict]:
    """Variante en mémoire de build_rows — utilisée par collector.py pour archiver
    un sous-ensemble (une saison) sans passer par les fichiers complets de la ligue."""
    goals_by_match = index_goals(goals_rows)
    odds_by_match = index_curated_odds(odds_rows)
    return [build_row(m, league_slug, goals_by_match, odds_by_match) for m in matches_rows]


def build_rows(league_slug: str, paths: dict) -> list[dict]:
    odds_by_match = load_curated_odds(paths["odds_csv"])
    goals_by_match = load_goals(paths["goals_csv"])
    with open(paths["matches_csv"], "r", newline="", encoding="utf-8") as f:
        return [build_row(m, league_slug, goals_by_match, odds_by_match) for m in csv.DictReader(f)]


def write_csv(output_path: str, rows: list[dict]) -> None:
    with open(output_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=OUTPUT_HEADER)
        writer.writeheader()
        writer.writerows(rows)
    with_odds = sum(1 for r in rows if r["had_odds"] == "True")
    print(f"[merge] {len(rows)} match(s) écrits dans {output_path} (dont {with_odds} avec cotes complètes)")


def merge_one(entry_point_id: int, slug: str) -> list[dict]:
    paths = build_paths(entry_point_id, slug)
    rows = build_rows(slug, paths)
    output_path = paths["matches_csv"].replace("matches.csv", "dataset.csv")
    write_csv(output_path, rows)
    return rows


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--league", default=None, help="Nom de ligue défini dans leagues.json.")
    parser.add_argument("--entry-point-id", type=int, default=None, help="entryPointId direct.")
    parser.add_argument("--league-name", default=None, help="Nom de dossier avec --entry-point-id.")
    parser.add_argument("--all", action="store_true",
                         help="Fusionne toutes les ligues de leagues.json, plus un data/dataset_global.csv combiné.")
    args = parser.parse_args()

    if args.all:
        registry = load_leagues_registry()
        if not registry:
            raise SystemExit("leagues.json est vide ou introuvable.")
        all_rows = []
        for slug, entry_point_id in registry.items():
            all_rows.extend(merge_one(entry_point_id, slug))
        write_csv("data/dataset_global.csv", all_rows)
        return

    entry_point_id, slug = resolve_league(args)
    merge_one(entry_point_id, slug)


if __name__ == "__main__":
    main()
