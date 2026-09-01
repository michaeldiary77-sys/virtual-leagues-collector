"""Rattrapage ponctuel : récupère les cotes de rounds déjà terminés (via round/{n})
et les rattache aux lignes déjà écrites dans data/matches.csv (had_odds=False),
sans dépendre de /results (dont la fenêtre de rétention est trop courte).

Usage :
    python backfill_odds.py --event-category-id 162816 --from-round 29 --to-round 38
"""

import argparse
import csv
import os

import api_client
from collector import (
    MATCHES_CSV, MATCHES_HEADER, ODDS_CSV, ODDS_HEADER,
    make_match_key, append_rows,
)


def load_matches() -> list[dict]:
    with open(MATCHES_CSV, "r", newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def save_matches(rows: list[dict]) -> None:
    tmp_path = MATCHES_CSV + ".tmp"
    with open(tmp_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=MATCHES_HEADER)
        writer.writeheader()
        writer.writerows(rows)
    os.replace(tmp_path, MATCHES_CSV)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--entry-point-id", type=int, default=8035)
    parser.add_argument("--event-category-id", type=int, required=True)
    parser.add_argument("--from-round", type=int, required=True)
    parser.add_argument("--to-round", type=int, required=True)
    args = parser.parse_args()

    matches = load_matches()
    missing_by_key = {m["match_key"]: m for m in matches if m["had_odds"] == "False"}
    print(f"[backfill] {len(missing_by_key)} match(s) sans cotes dans matches.csv "
          f"(sur {len(matches)} au total)")

    odds_rows = []
    patched = 0

    for round_id in range(args.from_round, args.to_round + 1):
        round_full = api_client.get_round_full(round_id, args.event_category_id)
        if not round_full or not round_full.get("matches"):
            print(f"[backfill] round {round_id} : rien à récupérer")
            continue

        expected_start = round_full.get("expectedStart")
        found_this_round = 0
        for match in round_full["matches"]:
            home = match["homeTeam"]["name"]
            away = match["awayTeam"]["name"]
            key = make_match_key(args.entry_point_id, expected_start, home, away)

            target = missing_by_key.get(key)
            if target is None:
                continue

            target["had_odds"] = "True"
            patched += 1
            found_this_round += 1

            for bet_type in match.get("eventBetTypes", []):
                for item in bet_type.get("eventBetTypeItems", []):
                    odds_rows.append({
                        "match_key": key,
                        "bet_type_name": bet_type.get("name"),
                        "bet_type_id": bet_type.get("betTypeId"),
                        "outcome_short_name": item.get("shortName"),
                        "odds": item.get("odds"),
                    })

        print(f"[backfill] round {round_id} (expectedStart={expected_start}) : "
              f"{found_this_round} match(s) rattachés")

    if patched:
        save_matches(matches)
        append_rows(ODDS_CSV, ODDS_HEADER, odds_rows)
        print(f"[backfill] terminé : {patched} match(s) mis à jour avec had_odds=True, "
              f"{len(odds_rows)} lignes de cotes ajoutées à {ODDS_CSV}")
    else:
        print("[backfill] aucun match rattaché — rien à écrire")


if __name__ == "__main__":
    main()
