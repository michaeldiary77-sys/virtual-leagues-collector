"""Collecteur de données pour une ligue virtuelle instantanée (bet261.mg / sporty-tech.net).

Combine en continu :
  - les cotes pré-match (endpoint {entryPointId}/matches, round courant)
  - les résultats finaux + buts minute par minute (endpoint {entryPointId}/results)
et exporte le tout dans data/{entryPointId}_{ligue}/{matches,goals,odds}.csv.

Les deux endpoints ne dépendent que de entryPointId — aucune valeur de session
à copier/rafraîchir manuellement, aucune expiration. Une ligue = un entryPointId
= un dossier de données indépendant (voir leagues.json pour le registre connu).

Règles de collecte (voulues pour l'analyse) :
  - Rien n'est écrit tant qu'un round avec roundNumber=1 (début de saison) n'a
    pas été vu depuis le lancement du script — tout ce qui précède est jeté.
  - Un match dont les cotes n'ont pas été captées à temps (had_odds=False)
    n'est jamais écrit dans les CSV, seulement compté dans le log.
  - Une fois démarrée, la collecte continue automatiquement saison après
    saison (le repassage à roundNumber=1 n'interrompt rien).
  - À chaque nouvelle saison détectée, la précédente est archivée (format
    dataset.csv) dans data/{...}/seasons/{season_id}_{début}.csv, puis
    matches/goals/odds.csv sont réduits à la saison en cours uniquement.
    Écriture toujours atomique, archivage toujours confirmé avant toute
    purge — voir archive_and_prune_old_seasons() et la vérification faite
    au démarrage (reconcile_seasons_on_startup()) en cas de coupure survenue
    entre les deux étapes lors d'une exécution précédente.

Usage :
    python collector.py --league english-league       # ligue connue (leagues.json)
    python collector.py --league coupe-du-monde
    python collector.py --entry-point-id 9999 --league-name nouvelle-ligue  # pas encore dans le registre
    python collector.py --league english-league --once # un seul cycle (pratique pour tester)
"""

import argparse
import csv
import hashlib
import json
import os
import time
from datetime import datetime, timezone
from urllib.parse import quote

import api_client

LEAGUES_REGISTRY_FILE = "leagues.json"

LOGO_BASE = "https://storage-prod.sporty-tech.net/virtual/teams"

# Nombre de rounds par saison = 2 x (nb d'équipes - 1) : chaque équipe affronte
# toutes les autres deux fois (aller-retour). Déduit du nombre d'équipes de
# chaque ligue (observé sur le site le 2026-08-29), et confirmé empiriquement
# le 2026-08-28 : coupe-d-afrique (24 équipes) a bien basculé au round 46.
SEASON_LENGTH_BY_LEAGUE = {
    "english-league": 38,
    "coupe-du-monde": 94,
    "champions-league": 70,
    "coupe-d-afrique": 46,
    "italian-league": 38,
    "spanish-league": 38,
    "french-league": 34,
    "german-league": 34,
    "portuguese-league": 34,
}

MATCHES_HEADER = [
    "match_key", "season_id", "round_number", "expected_start", "home_team", "away_team",
    "home_logo_url", "away_logo_url", "final_score", "home_score", "away_score",
    "half_time_score", "goal_count", "had_odds", "collected_at",
]
GOALS_HEADER = ["match_key", "minute", "scoring_team", "home_score_after", "away_score_after"]
ODDS_HEADER = ["match_key", "bet_type_name", "bet_type_id", "outcome_short_name", "odds"]

# Fenêtre de rétention de /results observée empiriquement (~30 rounds / ~1h).
# On purge les entrées en attente / déjà exportées plus vieilles que ça pour
# ne pas laisser grossir state.json indéfiniment.
PENDING_TTL_SECONDS = 3 * 60 * 60


def load_leagues_registry() -> dict:
    if os.path.exists(LEAGUES_REGISTRY_FILE):
        with open(LEAGUES_REGISTRY_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def resolve_league(args) -> tuple[int, str]:
    """Renvoie (entry_point_id, slug) à partir de --league (registre) ou --entry-point-id/--league-name."""
    if args.league:
        registry = load_leagues_registry()
        if args.league not in registry:
            raise SystemExit(
                f"Ligue inconnue dans {LEAGUES_REGISTRY_FILE} : {args.league!r}. "
                f"Connues : {list(registry)}. Utilise --entry-point-id pour une ligue pas encore répertoriée."
            )
        return registry[args.league], args.league
    if args.entry_point_id:
        slug = args.league_name or str(args.entry_point_id)
        return args.entry_point_id, slug
    raise SystemExit("Fournis --league <nom> (voir leagues.json) ou --entry-point-id <id> [--league-name <nom>].")


def build_paths(entry_point_id: int, slug: str) -> dict:
    data_dir = os.path.join("data", f"{entry_point_id}_{slug}")
    return {
        "data_dir": data_dir,
        "matches_csv": os.path.join(data_dir, "matches.csv"),
        "goals_csv": os.path.join(data_dir, "goals.csv"),
        "odds_csv": os.path.join(data_dir, "odds.csv"),
        "state_file": os.path.join(data_dir, "state.json"),
        "status_file": os.path.join(data_dir, "status.json"),
    }


def write_status(paths: dict, runtime: dict, cycle_result: dict, error: str | None) -> None:
    """État "vivant" du collecteur, lu par le dashboard (app.py) — distinct de
    state.json (qui sert à la déduplication, pas à l'affichage)."""
    status = {
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "pid": os.getpid(),
        "season_started": runtime["season_started"],
        "season_id": runtime["season_id"],
        "current_round": runtime.get("current_round"),
        "last_cycle_new_matches": cycle_result.get("new_matches", 0),
        "last_cycle_no_odds_skipped": cycle_result.get("no_odds_skipped", 0),
        "last_cycle_pending_odds": cycle_result.get("pending_odds", 0),
        "last_error": error,
    }
    with open(paths["status_file"], "w", encoding="utf-8") as f:
        json.dump(status, f, ensure_ascii=False, indent=2)


def logo_url(team_name: str) -> str:
    return f"{LOGO_BASE}/{quote(team_name)}.png"


def make_match_key(entry_point_id: int, expected_start: str, home: str, away: str) -> str:
    raw = f"{entry_point_id}|{expected_start}|{home}|{away}"
    return hashlib.md5(raw.encode("utf-8")).hexdigest()[:16]


def load_state(state_file: str, entry_point_id: int) -> dict:
    if os.path.exists(state_file):
        with open(state_file, "r", encoding="utf-8") as f:
            return json.load(f)
    return {
        "entry_point_id": entry_point_id,
        "pending_odds": {},   # match_key -> {..., "captured_at": iso}
        "exported_keys": {},  # match_key -> expected_start (iso)
    }


def save_state(state: dict, state_file: str) -> None:
    with open(state_file, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def ensure_csv_headers(paths: dict) -> None:
    os.makedirs(paths["data_dir"], exist_ok=True)
    for path, header in (
        (paths["matches_csv"], MATCHES_HEADER),
        (paths["goals_csv"], GOALS_HEADER),
        (paths["odds_csv"], ODDS_HEADER),
    ):
        if not os.path.exists(path):
            with open(path, "w", newline="", encoding="utf-8") as f:
                csv.writer(f).writerow(header)


def append_rows(path: str, header: list, rows: list) -> None:
    if not rows:
        return
    with open(path, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=header)
        writer.writerows(rows)


def prune_expired(state: dict, now_iso: str) -> None:
    now = datetime.fromisoformat(now_iso.replace("Z", "+00:00"))

    def is_expired(iso_str: str) -> bool:
        try:
            ts = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
        except (ValueError, TypeError):
            return True
        return (now - ts).total_seconds() > PENDING_TTL_SECONDS

    state["pending_odds"] = {
        k: v for k, v in state["pending_odds"].items()
        if not is_expired(v.get("expected_start", ""))
    }
    state["exported_keys"] = {
        k: v for k, v in state["exported_keys"].items() if not is_expired(v)
    }


def _atomic_rewrite_csv(path: str, header: list, rows: list) -> None:
    """Écrit rows dans un fichier temporaire puis le renomme sur path — un
    renommage est indivisible, donc path n'est jamais dans un état à moitié
    écrit, même en cas de coupure pile pendant l'opération."""
    tmp_path = path + ".tmp"
    with open(tmp_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=header)
        writer.writeheader()
        writer.writerows(rows)
    os.replace(tmp_path, path)


def _sanitize_for_filename(value: str) -> str:
    return (value or "inconnu").replace(":", "-")


def archive_and_prune_old_seasons(state: dict, paths: dict, slug: str, keep_season_id) -> None:
    """Archive (format dataset.csv, via merge_dataset) toute donnée de matches/
    goals/odds.csv dont season_id != keep_season_id vers un fichier séparé sous
    data/{...}/seasons/, PUIS réduit les 3 fichiers maîtres à keep_season_id
    uniquement.

    Avant l'archivage, tente de récupérer le dernier round de chaque ancienne
    saison (jamais montré par /results, voir recover_missing_last_round) — pour
    CHAQUE saison sur le point d'être archivée, pas seulement lors d'une bascule
    "en direct". C'est le seul point d'entrée qui archive : centraliser la
    récupération ici (plutôt que dans chaque appelant) garantit qu'elle a
    toujours lieu, y compris au redémarrage (reconcile_seasons_on_startup) ou au
    tout premier round #1 vu par une exécution (capture_odds) — les deux cas où
    elle manquait auparavant, la seule où elle était appelée étant la bascule
    "en direct" pendant une exécution continue.

    Ordre STRICT et jamais inversé : récupération puis archivage d'abord
    (confirmés réussis via l'écriture atomique de _atomic_rewrite_csv/os.replace),
    purge ensuite. En cas de coupure entre les deux, rien n'est perdu — au pire
    l'archivage sera simplement refait (voir reconcile_seasons_on_startup,
    idempotent : si le fichier de saison existe déjà, on ne fait que le
    réécrire à l'identique).
    """
    if not os.path.exists(paths["matches_csv"]):
        return

    with open(paths["matches_csv"], newline="", encoding="utf-8") as f:
        seasons_present = {m["season_id"] for m in csv.DictReader(f)}

    keep_season_id_str = str(keep_season_id) if keep_season_id is not None else ""
    old_seasons = seasons_present - {keep_season_id_str}
    if not old_seasons:
        return

    for season_id in sorted(old_seasons):
        recover_missing_last_round(state, paths, season_id)

    # Relecture APRÈS récupération : recover_missing_last_round a pu ajouter
    # des lignes aux 3 fichiers maîtres (append_rows), il faut les inclure.
    with open(paths["matches_csv"], newline="", encoding="utf-8") as f:
        all_matches = list(csv.DictReader(f))
    with open(paths["goals_csv"], newline="", encoding="utf-8") as f:
        all_goals = list(csv.DictReader(f))
    with open(paths["odds_csv"], newline="", encoding="utf-8") as f:
        all_odds = list(csv.DictReader(f))

    import merge_dataset  # import différé : évite un cycle d'import avec merge_dataset.py

    seasons_dir = os.path.join(paths["data_dir"], "seasons")
    os.makedirs(seasons_dir, exist_ok=True)

    for season_id in sorted(old_seasons):
        season_matches = [m for m in all_matches if m["season_id"] == season_id]
        if not season_matches:
            continue
        keys = {m["match_key"] for m in season_matches}
        season_goals = [g for g in all_goals if g["match_key"] in keys]
        season_odds = [o for o in all_odds if o["match_key"] in keys]

        rows = merge_dataset.build_rows_from_matches(slug, season_matches, season_goals, season_odds)
        season_start = min((m["expected_start"] for m in season_matches), default="inconnu")
        filename = f"{season_id}_{_sanitize_for_filename(season_start)}.csv"
        final_path = os.path.join(seasons_dir, filename)
        tmp_path = final_path + ".tmp"

        with open(tmp_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=merge_dataset.OUTPUT_HEADER)
            writer.writeheader()
            writer.writerows(rows)
        os.replace(tmp_path, final_path)
        print(f"[season] saison {season_id} archivée : {len(rows)} match(s) -> {final_path}")

    # Purge des fichiers maîtres — uniquement après confirmation que l'archivage a réussi.
    kept_matches = [m for m in all_matches if m["season_id"] == keep_season_id_str]
    kept_keys = {m["match_key"] for m in kept_matches}
    kept_goals = [g for g in all_goals if g["match_key"] in kept_keys]
    kept_odds = [o for o in all_odds if o["match_key"] in kept_keys]

    _atomic_rewrite_csv(paths["matches_csv"], MATCHES_HEADER, kept_matches)
    _atomic_rewrite_csv(paths["goals_csv"], GOALS_HEADER, kept_goals)
    _atomic_rewrite_csv(paths["odds_csv"], ODDS_HEADER, kept_odds)
    print(f"[season] fichiers maîtres réinitialisés — {len(kept_matches)} match(s) "
          f"conservé(s) (saison en cours, season_id={keep_season_id_str})")


def reconcile_seasons_on_startup(state: dict, paths: dict, slug: str) -> None:
    """Au démarrage : si matches.csv contient plusieurs season_id à la fois,
    c'est le signe qu'un archivage a été interrompu par une coupure lors d'une
    exécution précédente (entre l'archivage et la purge, ou pendant celle-ci).
    On termine le travail — garde la saison la plus récente, archive le reste —
    avant de reprendre la collecte normalement."""
    if not os.path.exists(paths["matches_csv"]):
        return
    with open(paths["matches_csv"], newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    seasons_present = {r["season_id"] for r in rows if r["season_id"]}
    if len(seasons_present) <= 1:
        return
    latest = max(rows, key=lambda r: r["expected_start"])["season_id"]
    print(f"[season] {len(seasons_present)} saisons trouvées dans matches.csv au démarrage "
          f"(archivage précédent probablement interrompu) — finalisation avant de continuer...")
    archive_and_prune_old_seasons(state, paths, slug, keep_season_id=latest)


def recover_missing_last_round(state: dict, paths: dict, old_season_id) -> None:
    """/results de bet261 ne montre JAMAIS le tout dernier round d'une saison —
    il passe directement de "round N-1 visible" à "vide" au moment exact de la
    bascule, sans jamais exposer le résultat du dernier round (confirmé
    empiriquement le 2026-08-28 sur coupe-d-afrique). Les cotes de ce round
    restent dans pending_odds sans jamais être appariées à un résultat.

    Filet de secours : l'endpoint /round/{id}/playout reste accessible avec
    l'ancien eventCategoryId même après la bascule, et donne le détail des
    buts par match id. On l'utilise pour reconstituer le résultat de tout
    match encore en attente pour l'ancienne saison, et on l'écrit normalement
    (comme s'il venait de /results) — TOUJOURS avant l'archivage, pour qu'il
    y soit inclus.
    """
    entry_point_id = state["entry_point_id"]
    old_season_id_str = str(old_season_id) if old_season_id is not None else ""
    to_recover = {
        k: v for k, v in state["pending_odds"].items()
        if str(v.get("season_id")) == old_season_id_str
    }
    if not to_recover:
        return

    round_ids = {v["round_id"] for v in to_recover.values() if v.get("round_id") is not None}
    goals_by_match_id: dict = {}
    for round_id in round_ids:
        playout = api_client.get_playout(round_id, old_season_id, entry_point_id)
        if playout is api_client.NETWORK_UNAVAILABLE or not playout:
            print(f"[season] récupération playout impossible pour round {round_id} "
                  f"— ce(s) match(s) restera(ont) sans résultat")
            continue
        for m in playout:
            goals_by_match_id[m.get("id")] = m.get("goals", [])

    now_iso = datetime.now(timezone.utc).isoformat()
    matches_rows, goals_rows, odds_rows = [], [], []
    recovered = 0

    for key, pending in to_recover.items():
        goals = goals_by_match_id.get(pending.get("match_id"))
        if goals is None:
            continue  # pas retrouvé — reste en pending_odds, purgé plus tard par le TTL

        goals_sorted = sorted(goals, key=lambda g: g.get("minute", 0))
        home_score, away_score = 0, 0
        half_time_score = "0:0"
        for g in goals_sorted:
            h, a = int(g.get("homeScore", 0)), int(g.get("awayScore", 0))
            team = "Home" if h > home_score else "Away"
            goals_rows.append({
                "match_key": key,
                "minute": g.get("minute"),
                "scoring_team": team,
                "home_score_after": h,
                "away_score_after": a,
            })
            home_score, away_score = h, a
            if g.get("minute", 0) <= 45:
                half_time_score = f"{home_score}:{away_score}"

        matches_rows.append({
            "match_key": key,
            "season_id": pending["season_id"],
            "round_number": pending["round_number"],
            "expected_start": pending["expected_start"],
            "home_team": pending["home_team"],
            "away_team": pending["away_team"],
            "home_logo_url": logo_url(pending["home_team"]),
            "away_logo_url": logo_url(pending["away_team"]),
            "final_score": f"{home_score}:{away_score}",
            "home_score": home_score,
            "away_score": away_score,
            "half_time_score": half_time_score,  # approximé (playout ne le donne pas), buts <= 45e minute
            "goal_count": len(goals_sorted),
            "had_odds": True,
            "collected_at": now_iso,
        })

        for bet_type in pending["bet_types"]:
            for item in bet_type.get("eventBetTypeItems", []):
                odds_rows.append({
                    "match_key": key,
                    "bet_type_name": bet_type.get("name"),
                    "bet_type_id": bet_type.get("betTypeId"),
                    "outcome_short_name": item.get("shortName"),
                    "odds": item.get("odds"),
                })

        state["exported_keys"][key] = pending["expected_start"]
        del state["pending_odds"][key]
        recovered += 1

    append_rows(paths["matches_csv"], MATCHES_HEADER, matches_rows)
    append_rows(paths["goals_csv"], GOALS_HEADER, goals_rows)
    append_rows(paths["odds_csv"], ODDS_HEADER, odds_rows)
    print(f"[season] {recovered}/{len(to_recover)} match(s) manquant(s) récupéré(s) via playout "
          f"(round(s) {sorted(round_ids)})")


def capture_odds(state: dict, runtime: dict, paths: dict, slug: str) -> None:
    """Capture les cotes du round courant via /{entryPointId}/matches.

    Cet endpoint ne demande AUCUN eventCategoryId : il renvoie directement le
    round en cours (avec cotes complètes) plus les 9 rounds suivants (sans
    cotes, juste leur horaire). On ne se sert que du round courant — comme le
    cycle (75s) est plus court que la durée d'un round (~2 min), on est
    garanti de voir chaque round au moins une fois avant qu'il ne change.
    Aucune valeur à fournir/rafraîchir manuellement, aucune session qui expire.

    `runtime` (en mémoire seulement, jamais persisté) retient si on a déjà vu
    un round #1 depuis le lancement du script — tant que non, on ne capture
    rien du tout (la porte se referme aussi côté résultats, puisqu'un match
    jamais capté ici aura toujours had_odds=False et sera donc ignoré).
    """
    entry_point_id = state["entry_point_id"]
    rounds = api_client.get_current_matches(entry_point_id)

    if rounds is api_client.NETWORK_UNAVAILABLE:
        print("[odds] réseau indisponible — on réessaiera au prochain cycle")
        return
    if not rounds:
        print("[odds] aucune donnée reçue de /matches ce cycle")
        return

    current = rounds[0]
    round_number = current.get("roundNumber")
    event_category_id = current.get("eventCategoryId")
    runtime["current_round"] = round_number

    if round_number == 1 and not runtime["season_started"]:
        runtime["season_started"] = True
        runtime["season_id"] = event_category_id
        print(f"[season] round #1 détecté — début de collecte, season_id={event_category_id}")
        # Si le fichier contient déjà une saison différente (laissée par une
        # exécution précédente, arrêtée puis relancée après la bascule), on
        # l'archive maintenant (récupération du dernier round incluse) — sinon
        # elle resterait mélangée indéfiniment, cette branche ne se
        # redéclenchant qu'une fois par exécution.
        archive_and_prune_old_seasons(state, paths, slug, keep_season_id=event_category_id)
    elif round_number == 1 and runtime["season_id"] != event_category_id:
        print(f"[season] nouvelle saison détectée (round #1) — archivage de la précédente "
              f"(récupération du dernier round incluse)...")
        archive_and_prune_old_seasons(state, paths, slug, keep_season_id=event_category_id)
        runtime["season_id"] = event_category_id
        print(f"[season] archivage terminé, season_id={event_category_id}")

    if not runtime["season_started"]:
        print(f"[season] en attente du prochain round #1 (round actuel : #{round_number}, "
              f"season_id courant={event_category_id}) — rien collecté pour l'instant")
        return

    if not current.get("matches"):
        print("[odds] round courant reçu mais sans matchs/cotes exploitables")
        return

    expected_start = current.get("expectedStart")
    now_iso = datetime.now(timezone.utc).isoformat()

    added, already_pending = 0, 0
    for match in current["matches"]:
        home = match["homeTeam"]["name"]
        away = match["awayTeam"]["name"]
        key = make_match_key(entry_point_id, expected_start, home, away)
        if key in state["exported_keys"]:
            continue
        if key in state["pending_odds"]:
            already_pending += 1
            continue
        state["pending_odds"][key] = {
            "home_team": home,
            "away_team": away,
            "expected_start": expected_start,
            "round_number": round_number,
            "season_id": runtime["season_id"],
            "round_id": current.get("id"),
            "match_id": match.get("id"),
            "bet_types": match.get("eventBetTypes", []),
            "captured_at": now_iso,
        }
        added += 1
    print(f"[odds] round #{round_number} (season_id={runtime['season_id']}) : "
          f"{added} nouveau(x), {already_pending} déjà en attente")


def process_results(state: dict) -> tuple[list, list, list, dict]:
    """Récupère les résultats récents, fusionne avec les cotes en attente, prépare les lignes CSV."""
    entry_point_id = state["entry_point_id"]
    data = api_client.get_results(entry_point_id, take=50)
    matches_rows, goals_rows, odds_rows = [], [], []
    if not data or not data.get("rounds"):
        print("[results] aucune donnée reçue ce cycle")
        return matches_rows, goals_rows, odds_rows, {"new_matches": 0, "no_odds_skipped": 0}

    now_iso = datetime.now(timezone.utc).isoformat()
    new_count, skipped_known_count, no_odds_count = 0, 0, 0

    for round_ in data["rounds"]:
        expected_start = round_.get("expectedStart")
        round_number = round_.get("roundNumber")
        for match in round_.get("matches", []):
            home = match["homeTeam"]["name"]
            away = match["awayTeam"]["name"]
            key = make_match_key(entry_point_id, expected_start, home, away)

            if key in state["exported_keys"]:
                skipped_known_count += 1
                continue

            pending = state["pending_odds"].pop(key, None)
            state["exported_keys"][key] = expected_start

            if pending is None:
                # Pas de cotes captées à temps (ou saison pas encore démarrée) :
                # on ne veut pas de lignes sans features, on jette silencieusement.
                no_odds_count += 1
                continue

            matches_rows.append({
                "match_key": key,
                "season_id": pending["season_id"],
                "round_number": round_number,
                "expected_start": expected_start,
                "home_team": home,
                "away_team": away,
                "home_logo_url": logo_url(home),
                "away_logo_url": logo_url(away),
                "final_score": match.get("score"),
                "home_score": (match.get("score") or ":").split(":")[0],
                "away_score": (match.get("score") or ":").split(":")[-1],
                "half_time_score": match.get("halfTimeScore"),
                "goal_count": len(match.get("goals", [])),
                "had_odds": True,
                "collected_at": now_iso,
            })

            for goal in match.get("goals", []):
                goals_rows.append({
                    "match_key": key,
                    "minute": goal.get("minute"),
                    "scoring_team": goal.get("team"),
                    "home_score_after": goal.get("homeScore"),
                    "away_score_after": goal.get("awayScore"),
                })

            for bet_type in pending["bet_types"]:
                for item in bet_type.get("eventBetTypeItems", []):
                    odds_rows.append({
                        "match_key": key,
                        "bet_type_name": bet_type.get("name"),
                        "bet_type_id": bet_type.get("betTypeId"),
                        "outcome_short_name": item.get("shortName"),
                        "odds": item.get("odds"),
                    })

            new_count += 1

    print(f"[results] {new_count} match(s) exporté(s), {no_odds_count} ignoré(s) ce cycle "
          f"(cotes non captées), {skipped_known_count} déjà connus")
    return matches_rows, goals_rows, odds_rows, {"new_matches": new_count, "no_odds_skipped": no_odds_count}


def run_cycle(state: dict, runtime: dict, paths: dict, slug: str) -> None:
    error = None
    cycle_result = {"new_matches": 0, "no_odds_skipped": 0}
    try:
        capture_odds(state, runtime, paths, slug)
        matches_rows, goals_rows, odds_rows, cycle_result = process_results(state)

        append_rows(paths["matches_csv"], MATCHES_HEADER, matches_rows)
        append_rows(paths["goals_csv"], GOALS_HEADER, goals_rows)
        append_rows(paths["odds_csv"], ODDS_HEADER, odds_rows)

        prune_expired(state, datetime.now(timezone.utc).isoformat())
        # Toujours écrit (même si --persist-season-gate n'est pas utilisé cette
        # fois) : inoffensif, et permet à une exécution future avec le flag de
        # reprendre au bon point même si les précédentes tournaient sans lui.
        state["season_started"] = runtime["season_started"]
        state["season_id"] = runtime["season_id"]
        state["current_round"] = runtime["current_round"]
        save_state(state, paths["state_file"])

        print(f"[cycle] {len(matches_rows)} match(es) écrits, "
              f"{len(state['pending_odds'])} en attente d'un résultat")
    except Exception as exc:  # ne jamais laisser un cycle raté tuer la boucle
        error = str(exc)
        print(f"[erreur] cycle interrompu : {exc}")
    finally:
        cycle_result["pending_odds"] = len(state["pending_odds"])
        write_status(paths, runtime, cycle_result, error)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--league", default=None,
                         help="Nom de ligue défini dans leagues.json (ex: english-league, coupe-du-monde).")
    parser.add_argument("--entry-point-id", type=int, default=None,
                         help="entryPointId direct, pour une ligue pas encore dans leagues.json.")
    parser.add_argument("--league-name", default=None,
                         help="Nom de dossier à utiliser avec --entry-point-id (sinon l'id sert de nom).")
    parser.add_argument("--interval", type=int, default=75, help="Secondes entre deux cycles.")
    parser.add_argument("--once", action="store_true", help="N'exécute qu'un seul cycle puis s'arrête.")
    parser.add_argument("--persist-season-gate", action="store_true",
                         help="Reprend season_started/season_id/current_round depuis state.json "
                              "au lieu de toujours repartir à zéro. Nécessaire pour une exécution "
                              "planifiée (ex: GitHub Actions) où chaque run est un processus neuf — "
                              "sans ce flag, la porte round #1 ne s'ouvrirait jamais. Sans effet sur "
                              "l'usage local habituel (dashboard) si non fourni.")
    args = parser.parse_args()

    entry_point_id, slug = resolve_league(args)
    paths = build_paths(entry_point_id, slug)

    ensure_csv_headers(paths)
    state = load_state(paths["state_file"], entry_point_id)
    reconcile_seasons_on_startup(state, paths, slug)

    if args.persist_season_gate:
        runtime = {
            "season_started": state.get("season_started", False),
            "season_id": state.get("season_id"),
            "current_round": state.get("current_round"),
        }
        print(f"[season] --persist-season-gate actif — reprise depuis state.json "
              f"(season_started={runtime['season_started']}, season_id={runtime['season_id']})")
    else:
        # En mémoire uniquement, jamais persisté : reset à chaque lancement, pour
        # que chaque exécution attende bien son propre round #1 avant de collecter.
        runtime = {"season_started": False, "season_id": None, "current_round": None}

    print(f"Démarrage collecteur — ligue={slug}, entryPointId={entry_point_id}, "
          f"dossier={paths['data_dir']}, intervalle={args.interval}s")
    if not runtime["season_started"]:
        print("[season] en attente du round #1 pour démarrer la collecte...")

    if args.once:
        run_cycle(state, runtime, paths, slug)
        return

    while True:
        run_cycle(state, runtime, paths, slug)  # gère déjà ses propres erreurs (voir finally: write_status)
        time.sleep(args.interval)


if __name__ == "__main__":
    main()
