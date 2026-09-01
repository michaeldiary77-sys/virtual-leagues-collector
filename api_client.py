"""Wrapper HTTP pour l'API interne de la ligue virtuelle (bet261.mg / sporty-tech.net).

Ces endpoints n'exigent pas de cookie de session : un header Referer/Origin
et un User-Agent de navigateur suffisent (vérifié empiriquement, sinon 403).
"""

import time
import requests

BASE_URL = "https://hg-event-api-prod.sporty-tech.net/api/instantleagues"

HEADERS = {
    "Origin": "https://bet261.mg",
    "Referer": "https://bet261.mg/",
    "Accept": "application/json, text/plain, */*",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "fr-FR,fr;q=0.9,en;q=0.8",
}

MAX_RETRIES = 3
RETRY_DELAY_SECONDS = 5


class _NetworkUnavailable:
    """Sentinel : le serveur n'a pas pu être joint (DNS/connexion/timeout) après
    plusieurs tentatives. À distinguer d'une réponse HTTP définitive (ex: 400/403),
    qui elle signifie vraiment "ce round/cette session n'existe pas" — voir
    collector.py::capture_odds qui traite ces deux cas différemment (une panne
    réseau ne doit jamais être interprétée comme "session de ligue terminée")."""


NETWORK_UNAVAILABLE = _NetworkUnavailable()


def _get_json(url: str, params: dict):
    """Renvoie le JSON décodé, ou None si le serveur a répondu une erreur
    définitive (mauvais statut/corps vide après retries), ou NETWORK_UNAVAILABLE
    si le serveur n'a pas pu être joint du tout (panne réseau transitoire)."""
    network_failed = False
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = requests.get(url, params=params, headers=HEADERS, timeout=15)
            network_failed = False
            if resp.status_code != 200:
                print(f"  [api] {url} -> HTTP {resp.status_code} (tentative {attempt}/{MAX_RETRIES})")
                time.sleep(RETRY_DELAY_SECONDS)
                continue
            if not resp.text.strip():
                print(f"  [api] {url} -> réponse vide (tentative {attempt}/{MAX_RETRIES})")
                time.sleep(RETRY_DELAY_SECONDS)
                continue
            return resp.json()
        except (requests.RequestException, ValueError) as exc:
            network_failed = True
            print(f"  [api] erreur sur {url}: {exc} (tentative {attempt}/{MAX_RETRIES})")
            time.sleep(RETRY_DELAY_SECONDS)
    return NETWORK_UNAVAILABLE if network_failed else None


def get_next_round(last_round_id: int, event_category_id: int):
    """Renvoie les métadonnées légères du round suivant (id, roundNumber, expectedStart)."""
    url = f"{BASE_URL}/round/{last_round_id}"
    params = {"eventCategoryId": event_category_id, "getNext": "true"}
    data = _get_json(url, params)
    if data is NETWORK_UNAVAILABLE:
        return NETWORK_UNAVAILABLE
    if data is None:
        return None
    return data.get("round")


def get_round_full(round_id: int, event_category_id: int):
    """Renvoie le round complet : équipes + cotes (eventBetTypes) pour tous ses matchs."""
    url = f"{BASE_URL}/round/{round_id}"
    params = {"eventCategoryId": event_category_id, "getNext": "false"}
    data = _get_json(url, params)
    if data is NETWORK_UNAVAILABLE:
        return NETWORK_UNAVAILABLE
    if data is None:
        return None
    return data.get("round")


def get_current_matches(entry_point_id: int):
    """Renvoie jusqu'à 10 rounds à partir du round courant (sans eventCategoryId !).

    round[0] contient déjà les cotes complètes (eventBetTypes) du round en cours —
    c'est la source la plus simple et la plus fiable pour les cotes pré-match,
    aucune valeur de session à fournir/rafraîchir manuellement. round[1..9] ne
    contiennent que expectedStart/id/eventCategoryId (pas encore de cotes).
    """
    url = f"{BASE_URL}/{entry_point_id}/matches"
    data = _get_json(url, {})
    if data is NETWORK_UNAVAILABLE:
        return NETWORK_UNAVAILABLE
    if data is None:
        return None
    return data.get("rounds", [])


def get_results(entry_point_id: int, take: int = 50, skip: int = 0) -> dict | None:
    """Renvoie la fenêtre glissante récente des rounds terminés (scores + buts)."""
    url = f"{BASE_URL}/{entry_point_id}/results"
    params = {"skip": skip, "take": take}
    return _get_json(url, params)


def get_playout(round_id: int, event_category_id: int, entry_point_id: int):
    """Renvoie le déroulé des buts (par match id) d'un round donné — reste
    accessible avec l'ancien eventCategoryId même après la bascule de saison,
    contrairement à /results qui devient vide au moment exact où le tout
    dernier round d'une saison se termine (confirmé empiriquement le
    2026-08-28 : /results passe de "round 45 visible" à "vide" sans jamais
    montrer le round 46). Sert de filet de secours pour ce round précis.
    """
    url = f"{BASE_URL}/round/{round_id}/playout"
    params = {"eventCategoryId": event_category_id, "parentEventCategoryId": entry_point_id}
    data = _get_json(url, params)
    if data is NETWORK_UNAVAILABLE:
        return NETWORK_UNAVAILABLE
    if data is None:
        return None
    return data.get("matches", [])
