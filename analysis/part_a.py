"""Partie A du catalogue d'analyses : vérifier l'équité du système AVANT de
chercher un edge exploitable (marge du bookmaker, calibration des cotes,
tests de hasard, stabilité des équipes).

Usage :
    python analysis/part_a.py --league english-league
    python analysis/part_a.py --all          # toutes les ligues avec des données
"""

import argparse
import csv
import math
import os
import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

import numpy as np
from scipy import stats
import matplotlib
matplotlib.use("Agg")  # pas d'affichage interactif, on sauvegarde en PNG
import matplotlib.pyplot as plt

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from collector import build_paths, load_leagues_registry  # noqa: E402
import merge_dataset  # noqa: E402

REPORTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "reports")

# (nom du marché, colonnes de cotes, somme "juste" théorique sans marge)
# Double Chance : chaque issue est l'union de 2 des 3 issues 1X2 mutuellement
# exclusives => somme des probabilités "justes" = 2, pas 1.
MARKETS = {
    "1X2": (["odds_1", "odds_X", "odds_2"], 1.0),
    "Double Chance": (["odds_dc_1x", "odds_dc_x2", "odds_dc_12"], 2.0),
    "Over/Under 2.5": (["odds_over_2_5", "odds_under_2_5"], 1.0),
    "BTTS": (["odds_btts_yes", "odds_btts_no"], 1.0),
}


def load_rows(entry_point_id: int, slug: str) -> list[dict]:
    merge_dataset.merge_one(entry_point_id, slug)  # régénère dataset.csv à partir des données actuelles
    paths = build_paths(entry_point_id, slug)
    dataset_path = paths["matches_csv"].replace("matches.csv", "dataset.csv")
    with open(dataset_path, "r", newline="", encoding="utf-8") as f:
        rows = [r for r in csv.DictReader(f) if r["had_odds"] == "True"]
    return rows


def to_float(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# 1. Marge du bookmaker (overround) par marché
# ---------------------------------------------------------------------------

def overround_analysis(rows: list[dict]) -> dict:
    results = {}
    for market, (cols, fair_sum) in MARKETS.items():
        margins = []
        for r in rows:
            vals = [to_float(r.get(c)) for c in cols]
            if all(v is not None and v > 0 for v in vals):
                implied = sum(1 / v for v in vals)
                margins.append(implied - fair_sum)
        if margins:
            results[market] = {
                "n": len(margins),
                "mean_margin_pct": 100 * float(np.mean(margins)),
                "std_margin_pct": 100 * float(np.std(margins)),
            }
    return results


# ---------------------------------------------------------------------------
# 2. Calibration des cotes 1X2 (probabilité implicite vs fréquence réelle)
# ---------------------------------------------------------------------------

def calibration_analysis(rows: list[dict], out_png: str | None = None) -> dict | None:
    data = []
    for r in rows:
        o1, ox, o2 = to_float(r.get("odds_1")), to_float(r.get("odds_X")), to_float(r.get("odds_2"))
        if not (o1 and ox and o2 and o1 > 0 and ox > 0 and o2 > 0):
            continue
        raw = [1 / o1, 1 / ox, 1 / o2]
        total = sum(raw)
        p_home = raw[0] / total  # normalisé : enlève la marge du bookmaker
        actual = 1 if r["home_win"] == "Oui" else 0
        data.append((p_home, actual))

    if len(data) < 20:
        return None

    data.sort(key=lambda x: x[0])
    n_buckets = max(1, min(8, len(data) // 30))
    buckets = np.array_split(np.array(data), n_buckets)

    table = []
    for b in buckets:
        if len(b) == 0:
            continue
        table.append({
            "n": len(b),
            "predicted_prob": float(np.mean(b[:, 0])),
            "observed_freq": float(np.mean(b[:, 1])),
        })

    brier = float(np.mean([(p - a) ** 2 for p, a in data]))

    if out_png and table:
        fig, ax = plt.subplots(figsize=(5, 5))
        ax.plot([0, 1], [0, 1], "--", color="gray", label="Calibration parfaite")
        ax.plot([t["predicted_prob"] for t in table], [t["observed_freq"] for t in table],
                "o-", color="#5b8dee", label="Observé")
        ax.set_xlabel("Probabilité implicite (cote, sans marge)")
        ax.set_ylabel("Fréquence réelle de victoire domicile")
        ax.set_title("Calibration — marché 1X2")
        ax.legend()
        fig.tight_layout()
        fig.savefig(out_png, dpi=120)
        plt.close(fig)

    return {"table": table, "brier_score": brier, "n": len(data)}


# ---------------------------------------------------------------------------
# 3. Tests de hasard : runs test (séquence 1X2) + Chi² (buts vs Poisson)
# ---------------------------------------------------------------------------

def runs_test(binary_sequence: list[int]):
    """Test de Wald-Wolfowitz. Renvoie (z, p_value) ou (None, None) si non calculable."""
    seq = list(binary_sequence)
    n = len(seq)
    n1 = sum(seq)
    n0 = n - n1
    if n1 == 0 or n0 == 0 or n < 2:
        return None, None
    runs = 1
    for i in range(1, n):
        if seq[i] != seq[i - 1]:
            runs += 1
    mean_runs = (2 * n1 * n0) / n + 1
    var_runs = (2 * n1 * n0 * (2 * n1 * n0 - n)) / (n ** 2 * (n - 1))
    if var_runs <= 0:
        return None, None
    z = (runs - mean_runs) / math.sqrt(var_runs)
    p = 2 * (1 - stats.norm.cdf(abs(z)))
    return z, p


def randomness_tests(rows: list[dict], out_png: str | None = None) -> dict:
    ordered = sorted(rows, key=lambda r: r["expected_start"])
    home_win_seq = [1 if r["home_win"] == "Oui" else 0 for r in ordered]
    z, p = runs_test(home_win_seq)

    totals = []
    for r in rows:
        h, a = to_float(r.get("home_score")), to_float(r.get("away_score"))
        if h is not None and a is not None:
            totals.append(int(h + a))

    chi2_result = None
    if totals:
        lam = float(np.mean(totals))
        max_goals = max(totals)
        cap = min(max_goals, 8)  # regroupe la queue de distribution pour garder des effectifs >= 5
        observed = [totals.count(k) for k in range(cap)]
        observed.append(sum(1 for t in totals if t >= cap))
        expected_probs = [stats.poisson.pmf(k, lam) for k in range(cap)]
        expected_probs.append(1 - sum(expected_probs))
        expected = [p_ * len(totals) for p_ in expected_probs]

        # fusionne les classes avec un effectif attendu < 5 (condition standard du test du Chi²)
        obs_merged, exp_merged = [], []
        obs_acc, exp_acc = 0, 0
        for o, e in zip(observed, expected):
            obs_acc += o
            exp_acc += e
            if exp_acc >= 5:
                obs_merged.append(obs_acc)
                exp_merged.append(exp_acc)
                obs_acc, exp_acc = 0, 0
        if exp_acc > 0:
            if exp_merged:
                obs_merged[-1] += obs_acc
                exp_merged[-1] += exp_acc
            else:
                obs_merged.append(obs_acc)
                exp_merged.append(exp_acc)

        if len(obs_merged) >= 2:
            chi2_stat, chi2_p = stats.chisquare(obs_merged, f_exp=exp_merged)
            chi2_result = {"lambda": lam, "chi2": float(chi2_stat), "p_value": float(chi2_p),
                            "n_classes": len(obs_merged), "n": len(totals)}

        if out_png:
            fig, ax = plt.subplots(figsize=(6, 4))
            max_plot = min(max(totals), 10)
            xs = list(range(max_plot + 1))
            obs_counts = [totals.count(k) / len(totals) for k in xs]
            exp_counts = [stats.poisson.pmf(k, lam) for k in xs]
            width = 0.4
            ax.bar([x - width / 2 for x in xs], obs_counts, width=width, label="Observé", color="#5b8dee")
            ax.bar([x + width / 2 for x in xs], exp_counts, width=width, label=f"Poisson(λ={lam:.2f})", color="#e0a742")
            ax.set_xlabel("Total de buts par match")
            ax.set_ylabel("Proportion")
            ax.set_title("Distribution des buts vs Poisson")
            ax.legend()
            fig.tight_layout()
            fig.savefig(out_png, dpi=120)
            plt.close(fig)

    return {
        "runs_test": {"z": z, "p_value": p, "n": len(ordered)},
        "chi2_goals_vs_poisson": chi2_result,
    }


# ---------------------------------------------------------------------------
# 4. Stabilité des équipes (1re moitié vs 2e moitié de la saison disponible)
# ---------------------------------------------------------------------------

def team_stability(rows: list[dict]) -> dict | None:
    by_team_half = {}
    round_numbers = [int(r["round_number"]) for r in rows if r["round_number"]]
    if not round_numbers:
        return None
    median_round = float(np.median(round_numbers))

    for r in rows:
        rn = to_float(r.get("round_number"))
        h, a = to_float(r.get("home_score")), to_float(r.get("away_score"))
        if rn is None or h is None or a is None:
            continue
        half = "1" if rn <= median_round else "2"
        for team, goals in ((r["home_team"], h), (r["away_team"], a)):
            by_team_half.setdefault(team, {"1": [], "2": []})[half].append(goals)

    pairs = []
    for team, halves in by_team_half.items():
        if halves["1"] and halves["2"]:
            pairs.append((float(np.mean(halves["1"])), float(np.mean(halves["2"]))))

    if len(pairs) < 5:
        return {"n_teams": len(pairs), "note": "Pas assez d'équipes avec des matchs dans les deux moitiés."}

    first_half = [p[0] for p in pairs]
    second_half = [p[1] for p in pairs]
    stat, p_value = stats.wilcoxon(first_half, second_half)
    return {
        "n_teams": len(pairs),
        "mean_goals_first_half": float(np.mean(first_half)),
        "mean_goals_second_half": float(np.mean(second_half)),
        "wilcoxon_stat": float(stat),
        "p_value": float(p_value),
        "note": "Comparaison intra-saison (1ère vs 2e moitié) — un vrai test inter-saisons "
                "demandera plusieurs saisons complètes archivées.",
    }


# ---------------------------------------------------------------------------
# Rapport
# ---------------------------------------------------------------------------

def run_report(entry_point_id: int, slug: str) -> None:
    rows = load_rows(entry_point_id, slug)
    league_dir = os.path.join(REPORTS_DIR, slug)
    os.makedirs(league_dir, exist_ok=True)

    lines = [f"# Partie A — {slug} (entryPointId={entry_point_id})", f"Échantillon : {len(rows)} matchs avec cotes", ""]

    if len(rows) < 20:
        lines.append("⚠️  Échantillon trop petit (<20 matchs) pour des tests statistiques fiables. "
                      "Résultats indicatifs seulement, à revérifier avec plus de données.")

    lines.append("## 1. Marge du bookmaker (overround) par marché")
    overround = overround_analysis(rows)
    for market, res in overround.items():
        lines.append(f"- **{market}** (n={res['n']}) : marge moyenne = {res['mean_margin_pct']:.2f}% "
                      f"(écart-type {res['std_margin_pct']:.2f}%)")
    if not overround:
        lines.append("(aucune donnée exploitable)")
    lines.append("")

    lines.append("## 2. Calibration des cotes 1X2")
    calib = calibration_analysis(rows, out_png=os.path.join(league_dir, "calibration_1x2.png"))
    if calib:
        lines.append(f"Brier score = {calib['brier_score']:.4f} (0 = parfait, 0.25 = niveau du hasard pur pour p=0.5)")
        lines.append("| n | probabilité implicite | fréquence réelle |")
        lines.append("|---|---|---|")
        for t in calib["table"]:
            lines.append(f"| {t['n']} | {t['predicted_prob']:.3f} | {t['observed_freq']:.3f} |")
        lines.append(f"(graphique : {league_dir}/calibration_1x2.png)")
    else:
        lines.append("(pas assez de données)")
    lines.append("")

    lines.append("## 3. Tests de hasard")
    rand = randomness_tests(rows, out_png=os.path.join(league_dir, "goals_vs_poisson.png"))
    rt = rand["runs_test"]
    if rt["p_value"] is not None:
        lines.append(f"- **Runs test** (séquence victoires domicile, n={rt['n']}) : z={rt['z']:.3f}, "
                      f"p={rt['p_value']:.3f} — {'RAS (pas de séquence suspecte)' if rt['p_value'] > 0.05 else 'ATYPIQUE, à creuser'}")
    else:
        lines.append("- Runs test : pas calculable (pas assez de variation dans la séquence)")
    c2 = rand["chi2_goals_vs_poisson"]
    if c2:
        lines.append(f"- **Chi² buts vs Poisson(λ={c2['lambda']:.2f})** (n={c2['n']}) : "
                      f"chi2={c2['chi2']:.2f}, p={c2['p_value']:.3f} — "
                      f"{'distribution cohérente avec Poisson' if c2['p_value'] > 0.05 else 'écart significatif à Poisson'}")
        lines.append(f"(graphique : {league_dir}/goals_vs_poisson.png)")
    else:
        lines.append("- Chi² buts vs Poisson : pas calculable")
    lines.append("")

    lines.append("## 4. Stabilité des équipes (1ère vs 2e moitié de la saison en cours)")
    stab = team_stability(rows)
    if stab and "p_value" in stab:
        lines.append(f"- {stab['n_teams']} équipes comparées : moyenne buts 1ère moitié = "
                      f"{stab['mean_goals_first_half']:.2f}, 2e moitié = {stab['mean_goals_second_half']:.2f}")
        lines.append(f"- Wilcoxon signé : stat={stab['wilcoxon_stat']:.2f}, p={stab['p_value']:.3f} — "
                      f"{'pas de différence significative (stable)' if stab['p_value'] > 0.05 else 'différence significative entre moitiés'}")
        lines.append(f"- Note : {stab['note']}")
    else:
        lines.append(f"(pas assez de données : {stab})")
    lines.append("")

    report_text = "\n".join(lines)
    report_path = os.path.join(league_dir, "rapport.md")
    with open(report_path, "w", encoding="utf-8") as f:
        f.write(report_text)

    print(report_text)
    print(f"\n[rapport enregistré : {report_path}]")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--league", default=None)
    parser.add_argument("--all", action="store_true")
    args = parser.parse_args()

    registry = load_leagues_registry()
    if args.all:
        for slug, entry_point_id in registry.items():
            paths = build_paths(entry_point_id, slug)
            if os.path.exists(paths["matches_csv"]):
                with open(paths["matches_csv"], encoding="utf-8") as f:
                    if sum(1 for _ in f) <= 1:
                        continue
                print(f"\n{'=' * 70}\n{slug}\n{'=' * 70}")
                run_report(entry_point_id, slug)
        return

    if not args.league or args.league not in registry:
        raise SystemExit(f"--league doit être l'une de : {list(registry)}")
    run_report(registry[args.league], args.league)


if __name__ == "__main__":
    main()
