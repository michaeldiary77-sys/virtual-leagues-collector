"""Persistance des équipes d'une saison à l'autre — teste l'hypothèse d'une
force d'équipe FIXE dans le simulateur (pas re-tirée au hasard chaque saison).

Si confirmé (corrélation significative des stats d'une équipe entre saisons),
c'est un signal fondamentalement différent des tests précédents : l'historique
d'une équipe redeviendrait un vrai prédicteur légitime, pas du gambler's
fallacy — à condition de vérifier ensuite que les cotes ne l'intègrent pas
déjà (sinon pas d'edge, juste une confirmation que le moteur est cohérent).

Utilise les saisons ARCHIVÉES (data/{id}_{slug}/seasons/*.csv), jamais la
saison en cours (partielle, fausserait les moyennes par équipe).

Usage :
    python analysis/cross_season.py --league english-league
    python analysis/cross_season.py --all
"""

import argparse
import csv
import glob
import os
import sys
from collections import defaultdict

import numpy as np
from scipy import stats

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from collector import build_paths, load_leagues_registry  # noqa: E402

REPORTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "reports")


def to_float(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def load_archived_seasons(entry_point_id: int, slug: str) -> list[dict]:
    """Renvoie une liste de saisons, chacune {"season_id":, "start":, "rows": [...]},
    triée par date de début — uniquement les saisons archivées (complètes)."""
    paths = build_paths(entry_point_id, slug)
    pattern = os.path.join(paths["data_dir"], "seasons", "*.csv")
    seasons = []
    for path in sorted(glob.glob(pattern)):
        with open(path, "r", newline="", encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
        if not rows:
            continue
        season_id = rows[0]["season_id"]
        start = min(r["expected_start"] for r in rows)
        seasons.append({"season_id": season_id, "start": start, "rows": rows, "path": path})
    seasons.sort(key=lambda s: s["start"])
    return seasons


def standings(rows: list[dict]) -> list[dict]:
    """Classement classique (points, buts pour/contre) pour une saison."""
    table = defaultdict(lambda: {"points": 0, "gf": 0, "ga": 0, "played": 0, "w": 0, "d": 0, "l": 0})
    for r in rows:
        h, a = to_float(r["home_score"]), to_float(r["away_score"])
        if h is None or a is None:
            continue
        home, away = r["home_team"], r["away_team"]
        table[home]["gf"] += h
        table[home]["ga"] += a
        table[away]["gf"] += a
        table[away]["ga"] += h
        table[home]["played"] += 1
        table[away]["played"] += 1
        if h > a:
            table[home]["points"] += 3
            table[home]["w"] += 1
            table[away]["l"] += 1
        elif h < a:
            table[away]["points"] += 3
            table[away]["w"] += 1
            table[home]["l"] += 1
        else:
            table[home]["points"] += 1
            table[away]["points"] += 1
            table[home]["d"] += 1
            table[away]["d"] += 1

    rows_out = []
    for team, stat in table.items():
        rows_out.append({"team": team, **stat, "gd": stat["gf"] - stat["ga"]})
    rows_out.sort(key=lambda t: (-t["points"], -t["gd"], -t["gf"]))
    return rows_out


def team_season_stats(rows: list[dict]) -> dict:
    """{team: {"avg_gf":, "avg_ga":, "points":, "played":}} pour une saison."""
    table = standings(rows)
    return {t["team"]: {
        "avg_gf": t["gf"] / t["played"] if t["played"] else None,
        "avg_ga": t["ga"] / t["played"] if t["played"] else None,
        "points": t["points"],
        "played": t["played"],
    } for t in table}


def persistence_analysis(seasons: list[dict]) -> dict | None:
    if len(seasons) < 2:
        return None

    per_season_stats = [team_season_stats(s["rows"]) for s in seasons]
    all_teams = set()
    for stats_dict in per_season_stats:
        all_teams |= set(stats_dict)

    # apparie saison N / saison N+1 pour chaque équipe présente dans les deux
    pairs_gf, pairs_points = [], []
    for i in range(len(per_season_stats) - 1):
        s1, s2 = per_season_stats[i], per_season_stats[i + 1]
        for team in all_teams:
            if team in s1 and team in s2 and s1[team]["avg_gf"] is not None and s2[team]["avg_gf"] is not None:
                pairs_gf.append((s1[team]["avg_gf"], s2[team]["avg_gf"]))
                pairs_points.append((s1[team]["points"], s2[team]["points"]))

    if len(pairs_gf) < 5:
        return {"n_pairs": len(pairs_gf), "note": "Pas assez d'équipes communes entre saisons consécutives."}

    gf1, gf2 = [p[0] for p in pairs_gf], [p[1] for p in pairs_gf]
    pts1, pts2 = [p[0] for p in pairs_points], [p[1] for p in pairs_points]

    r_gf, p_gf = stats.pearsonr(gf1, gf2)
    r_pts, p_pts = stats.pearsonr(pts1, pts2)

    return {
        "n_seasons": len(seasons),
        "n_pairs": len(pairs_gf),
        "corr_avg_goals_for": {"r": float(r_gf), "p_value": float(p_gf)},
        "corr_points": {"r": float(r_pts), "p_value": float(p_pts)},
    }


def run_report(entry_point_id: int, slug: str) -> None:
    seasons = load_archived_seasons(entry_point_id, slug)
    league_dir = os.path.join(REPORTS_DIR, slug)
    os.makedirs(league_dir, exist_ok=True)

    lines = [f"# Persistance inter-saisons — {slug} (entryPointId={entry_point_id})",
              f"Saisons archivées disponibles : {len(seasons)}", ""]

    if not seasons:
        lines.append("Aucune saison archivée pour l'instant — attendre qu'au moins une saison se termine "
                      "(le collecteur archive automatiquement à chaque transition détectée).")
        print("\n".join(lines))
        return

    lines.append("## Classement(s) archivé(s)")
    for s in seasons:
        lines.append(f"\n### Saison {s['season_id']} (début {s['start']})")
        table = standings(s["rows"])
        lines.append("| # | Équipe | Pts | J | V | N | D | BP | BC | Diff |")
        lines.append("|---|---|---|---|---|---|---|---|---|---|")
        for i, t in enumerate(table[:10], 1):
            lines.append(f"| {i} | {t['team']} | {t['points']} | {t['played']} | {t['w']} | {t['d']} | "
                          f"{t['l']} | {int(t['gf'])} | {int(t['ga'])} | {int(t['gd']):+d} |")
        lines.append("(top 10 affiché)")
    lines.append("")

    lines.append("## Test de persistance (corrélation saison N vs N+1)")
    result = persistence_analysis(seasons)
    if result is None:
        lines.append(f"Il faut au moins 2 saisons archivées pour tester ça — actuellement {len(seasons)}. "
                      "Laisse le collecteur tourner : une nouvelle saison s'archive automatiquement à chaque fin de saison.")
    elif "note" in result:
        lines.append(f"({result['note']} — {result['n_pairs']} paire(s) trouvée(s))")
    else:
        rg, pg = result["corr_avg_goals_for"]["r"], result["corr_avg_goals_for"]["p_value"]
        rp, pp = result["corr_points"]["r"], result["corr_points"]["p_value"]
        lines.append(f"- {result['n_pairs']} équipe(s) comparée(s) entre saisons consécutives")
        lines.append(f"- **Buts marqués/match** : r={rg:.3f}, p={pg:.3f} — "
                      f"{'PERSISTANCE significative (force fixe probable)' if pg < 0.05 else 'pas de persistance détectée (compatible avec un tirage aléatoire par saison)'}")
        lines.append(f"- **Points de classement** : r={rp:.3f}, p={pp:.3f} — "
                      f"{'PERSISTANCE significative' if pp < 0.05 else 'pas de persistance détectée'}")
        lines.append("")
        lines.append("⚠️ Avec seulement 2-3 saisons, la puissance statistique reste limitée — "
                      "à re-vérifier au fur et à mesure que plus de saisons s'accumulent.")

    report_text = "\n".join(lines)
    with open(os.path.join(league_dir, "cross_season.md"), "w", encoding="utf-8") as f:
        f.write(report_text)
    print(report_text)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--league", default=None)
    parser.add_argument("--all", action="store_true")
    args = parser.parse_args()

    registry = load_leagues_registry()
    if args.all:
        for slug, entry_point_id in registry.items():
            print(f"\n{'=' * 70}\n{slug}\n{'=' * 70}")
            run_report(entry_point_id, slug)
        return

    if not args.league or args.league not in registry:
        raise SystemExit(f"--league doit être l'une de : {list(registry)}")
    run_report(registry[args.league], args.league)


if __name__ == "__main__":
    main()
