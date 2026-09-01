"""Modèle de Poisson bivarié par équipe (Dixon-Coles) : estime une force
d'attaque et de défense par équipe, plutôt que de traiter tous les matchs
comme un seul Poisson global (erreur méthodologique de la partie A, item 3).

Sert deux objectifs :
  1. Re-tester proprement l'aléatoire du moteur, contrôlé par équipe.
  2. Backtester si le modèle bat le marché (value betting) — avec split
     train/test STRICT pour éviter le biais de circularité (jamais évaluer
     sur les données qui ont servi à estimer les paramètres).

Usage :
    python analysis/dixon_coles.py --league english-league
    python analysis/dixon_coles.py --all
"""

import argparse
import csv
import os
import sys

import numpy as np
from scipy.optimize import minimize
from scipy.stats import poisson, norm

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from collector import build_paths, load_leagues_registry  # noqa: E402
import merge_dataset  # noqa: E402

REPORTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "reports")
MAX_GOALS = 8  # plafond pour les matrices de probabilité de score


# ---------------------------------------------------------------------------
# Chargement
# ---------------------------------------------------------------------------

def load_rows(entry_point_id: int, slug: str) -> list[dict]:
    merge_dataset.merge_one(entry_point_id, slug)
    paths = build_paths(entry_point_id, slug)
    dataset_path = paths["matches_csv"].replace("matches.csv", "dataset.csv")
    with open(dataset_path, "r", newline="", encoding="utf-8") as f:
        rows = [r for r in csv.DictReader(f) if r["had_odds"] == "True"]
    rows.sort(key=lambda r: r["expected_start"])
    return rows


def to_float(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Estimation (maximum de vraisemblance)
# ---------------------------------------------------------------------------

def dc_tau(hg: int, ag: int, lh: float, la: float, rho: float) -> float:
    """Correction de Dixon-Coles pour la légère corrélation aux scores faibles."""
    if hg == 0 and ag == 0:
        return 1 - lh * la * rho
    if hg == 0 and ag == 1:
        return 1 + lh * rho
    if hg == 1 and ag == 0:
        return 1 + la * rho
    if hg == 1 and ag == 1:
        return 1 - rho
    return 1.0


def fit_dixon_coles(matches: list[dict], teams: list[str]) -> dict:
    """matches : liste de {"home":, "away":, "hg":, "ag":}. Renvoie les
    paramètres ajustés (attack/defence par équipe, home_adv, rho)."""
    n = len(teams)
    team_idx = {t: i for i, t in enumerate(teams)}
    # attack[teams[0]] fixé à 0 pour lever l'indétermination (attack+c, defence-c
    # laisse la vraisemblance inchangée sinon)
    # x = [attack_1..attack_{n-1}, defence_0..defence_{n-1}, home_adv, rho]

    def unpack(x):
        attack = np.concatenate([[0.0], x[:n - 1]])
        defence = x[n - 1:2 * n - 1]
        home_adv = x[2 * n - 1]
        rho = x[2 * n]
        return attack, defence, home_adv, rho

    def neg_log_lik(x):
        attack, defence, home_adv, rho = unpack(x)
        ll = 0.0
        for m in matches:
            i, j = team_idx[m["home"]], team_idx[m["away"]]
            lh = np.exp(attack[i] + defence[j] + home_adv)
            la = np.exp(attack[j] + defence[i])
            tau = max(dc_tau(m["hg"], m["ag"], lh, la, rho), 1e-10)
            p = tau * poisson.pmf(m["hg"], lh) * poisson.pmf(m["ag"], la)
            ll += np.log(max(p, 1e-12))
        return -ll

    # attack(n-1) + defence(n) + home_adv(1) + rho(1) = 2n+1 paramètres
    x0 = np.zeros(2 * n + 1)
    res = minimize(neg_log_lik, x0, method="L-BFGS-B",
                    bounds=[(-3, 3)] * (2 * n - 1) + [(-2, 2)] + [(-0.2, 0.2)])
    attack, defence, home_adv, rho = unpack(res.x)
    return {
        "teams": teams,
        "attack": dict(zip(teams, attack)),
        "defence": dict(zip(teams, defence)),
        "home_adv": float(home_adv),
        "rho": float(rho),
        "converged": bool(res.success),
        "log_lik": -float(res.fun),
    }


def predict_lambdas(model: dict, home: str, away: str) -> tuple[float, float] | None:
    if home not in model["attack"] or away not in model["attack"]:
        return None
    lh = np.exp(model["attack"][home] + model["defence"][away] + model["home_adv"])
    la = np.exp(model["attack"][away] + model["defence"][home])
    return float(lh), float(la)


def score_matrix(lh: float, la: float, rho: float, max_goals: int = MAX_GOALS) -> np.ndarray:
    mat = np.zeros((max_goals + 1, max_goals + 1))
    for i in range(max_goals + 1):
        for j in range(max_goals + 1):
            tau = dc_tau(i, j, lh, la, rho)
            mat[i, j] = max(tau, 1e-10) * poisson.pmf(i, lh) * poisson.pmf(j, la)
    mat /= mat.sum()  # renormalise (le plafond de buts tronque la queue)
    return mat


def market_probs_from_matrix(mat: np.ndarray) -> dict:
    n = mat.shape[0]
    p_home = sum(mat[i, j] for i in range(n) for j in range(n) if i > j)
    p_draw = sum(mat[i, i] for i in range(n))
    p_away = sum(mat[i, j] for i in range(n) for j in range(n) if i < j)
    p_over25 = sum(mat[i, j] for i in range(n) for j in range(n) if i + j > 2.5)
    p_btts = sum(mat[i, j] for i in range(1, n) for j in range(1, n))
    return {"1": p_home, "X": p_draw, "2": p_away, "over_2_5": p_over25, "btts_yes": p_btts}


# ---------------------------------------------------------------------------
# Backtest : split train/test STRICT, jamais évaluer sur les données d'entraînement
# ---------------------------------------------------------------------------

def devig(o1: float, ox: float, o2: float) -> tuple[float, float, float]:
    raw = [1 / o1, 1 / ox, 1 / o2]
    s = sum(raw)
    return raw[0] / s, raw[1] / s, raw[2] / s


def backtest(rows: list[dict], train_frac: float = 0.7, edge_threshold: float = 0.04) -> dict:
    split = int(len(rows) * train_frac)
    train_rows, test_rows = rows[:split], rows[split:]

    teams = sorted({r["home_team"] for r in train_rows} | {r["away_team"] for r in train_rows})
    train_matches = []
    for r in train_rows:
        h, a = to_float(r["home_score"]), to_float(r["away_score"])
        if h is None or a is None:
            continue
        train_matches.append({"home": r["home_team"], "away": r["away_team"], "hg": int(h), "ag": int(a)})

    model = fit_dixon_coles(train_matches, teams)

    bets = []
    model_brier, book_brier = [], []
    for r in test_rows:
        o1, ox, o2 = to_float(r["odds_1"]), to_float(r["odds_X"]), to_float(r["odds_2"])
        h, a = to_float(r["home_score"]), to_float(r["away_score"])
        if not (o1 and ox and o2 and h is not None and a is not None):
            continue
        lambdas = predict_lambdas(model, r["home_team"], r["away_team"])
        if lambdas is None:
            continue  # équipe jamais vue à l'entraînement
        lh, la = lambdas
        mat = score_matrix(lh, la, model["rho"])
        model_p = market_probs_from_matrix(mat)
        book_p1, book_px, book_p2 = devig(o1, ox, o2)

        actual = "1" if h > a else ("X" if h == a else "2")
        for outcome, odds, book_p in (("1", o1, book_p1), ("X", ox, book_px), ("2", o2, book_p2)):
            model_brier.append((model_p[outcome] - (1 if actual == outcome else 0)) ** 2)
            book_brier.append((book_p - (1 if actual == outcome else 0)) ** 2)
            edge = model_p[outcome] - book_p
            if edge > edge_threshold:
                profit = (odds - 1) if actual == outcome else -1
                bets.append({"round": r["round_number"], "match": f"{r['home_team']} vs {r['away_team']}",
                              "outcome": outcome, "odds": odds, "edge": edge, "profit": profit})

    result = {
        "n_train": len(train_matches),
        "n_test": len(test_rows),
        "model_converged": model["converged"],
        "model_brier": float(np.mean(model_brier)) if model_brier else None,
        "book_brier": float(np.mean(book_brier)) if book_brier else None,
        "n_bets": len(bets),
        "bets": bets,
    }
    if bets:
        profits = [b["profit"] for b in bets]
        total_profit = sum(profits)
        roi = 100 * total_profit / len(profits)
        se = np.std(profits, ddof=1) / np.sqrt(len(profits)) if len(profits) > 1 else None
        z = (np.mean(profits) / se) if se else None
        p_value = 2 * (1 - norm.cdf(abs(z))) if z is not None else None
        result.update({"total_profit_units": total_profit, "roi_pct": roi,
                        "z_stat": float(z) if z is not None else None,
                        "p_value": float(p_value) if p_value is not None else None})
    return result


# ---------------------------------------------------------------------------
# Rapport
# ---------------------------------------------------------------------------

def run_report(entry_point_id: int, slug: str) -> None:
    rows = load_rows(entry_point_id, slug)
    league_dir = os.path.join(REPORTS_DIR, slug)
    os.makedirs(league_dir, exist_ok=True)

    lines = [f"# Dixon-Coles — {slug} (entryPointId={entry_point_id})",
              f"Échantillon total : {len(rows)} matchs avec cotes", ""]

    if len(rows) < 60:
        lines.append("⚠️  Échantillon trop petit pour un split train/test fiable (<60 matchs). "
                      "Résultats indicatifs uniquement.")

    result = backtest(rows)
    lines.append(f"## Split train/test (70% / 30%, chronologique)")
    lines.append(f"- Entraînement : {result['n_train']} matchs — modèle convergé : {result['model_converged']}")
    lines.append(f"- Test (jamais vu par le modèle) : {result['n_test']} matchs")
    lines.append("")

    lines.append("## Le modèle bat-il le marché ? (Brier score sur le test, plus bas = meilleur)")
    if result["model_brier"] is not None:
        lines.append(f"- Modèle Dixon-Coles : {result['model_brier']:.4f}")
        lines.append(f"- Cotes du bookmaker (dévigorées) : {result['book_brier']:.4f}")
        better = result["model_brier"] < result["book_brier"]
        lines.append(f"- **{'Le modèle est légèrement meilleur' if better else 'Le marché reste meilleur ou équivalent'}**")
    else:
        lines.append("(pas assez de données de test)")
    lines.append("")

    lines.append(f"## Backtest de value betting (seuil d'edge = 4 points de probabilité, 1X2 uniquement)")
    lines.append(f"- Paris déclenchés : {result['n_bets']} sur {result['n_test']} matchs de test")
    if result["n_bets"] > 0:
        lines.append(f"- Profit total : {result['total_profit_units']:.2f} unités "
                      f"(ROI = {result['roi_pct']:.1f}% sur les paris pris)")
        if result.get("p_value") is not None:
            verdict = ("PAS distinguable du hasard (attendu avec si peu de paris)"
                       if result["p_value"] > 0.05
                       else "statistiquement notable, mais A CONFIRMER sur plus de donnees avant d'agir")
            lines.append(f"- z = {result['z_stat']:.2f}, p = {result['p_value']:.3f} — {verdict}")
        lines.append("")
        lines.append("⚠️ **Avec seulement quelques dizaines de paris, l'intervalle de confiance sur ce ROI est énorme.** "
                      "Un résultat positif ici n'est PAS une preuve d'edge réel — juste une hypothèse à re-tester "
                      "sur beaucoup plus de saisons avant d'envisager de miser de l'argent réel.")
    else:
        lines.append("Aucun pari déclenché — le modèle ne trouve pas d'écart significatif avec le marché à ce seuil.")
    lines.append("")

    report_text = "\n".join(lines)
    report_path = os.path.join(league_dir, "dixon_coles.md")
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
