# Virtual Leagues Collector

Collecteur de données pour les ligues de football virtuel ("Instant Leagues") de
bet261.mg (infrastructure sporty-tech.net) : cotes pré-match, résultats, buts
minute par minute, sur plusieurs ligues en parallèle, avec archivage automatique
saison par saison et un tableau de bord web de pilotage.

## Fonctionnement

- **`collector.py`** — collecteur d'une ligue. Interroge l'API en continu
  (round courant + résultats récents), fusionne cotes et résultats, écrit dans
  `data/{entryPointId}_{ligue}/{matches,goals,odds}.csv`. N'écrit un match que
  s'il a des cotes captées à temps (pas de ligne sans "feature"). Attend le
  round #1 d'une saison avant de commencer à collecter, puis continue en
  continu saison après saison, en archivant automatiquement chaque saison
  terminée dans `data/{...}/seasons/`.
- **`api_client.py`** — wrapper HTTP vers l'API interne (headers requis,
  gestion des pannes réseau vs erreurs définitives).
- **`app.py`** — tableau de bord web local (Flask) pour démarrer/arrêter la
  collecte par ligue, suivre le round courant en direct, voir les logs, et
  déclencher la fusion des données. Lancer avec `python app.py`, puis ouvrir
  `http://localhost:5000`.
- **`merge_dataset.py`** — fusionne matches/goals/odds en un seul jeu de
  données "plat" prêt pour l'analyse (`dataset.csv` par ligue, ou
  `dataset_global.csv` pour toutes les ligues avec `--all`).
- **`leagues.json`** — registre des ligues connues (nom → `entryPointId`).

## Utilisation

```bash
# Collecter une ligue (voir leagues.json pour les noms disponibles)
python collector.py --league english-league

# Dashboard pour piloter toutes les ligues
python app.py

# Générer le jeu de données d'analyse (une ligue, ou --all)
python merge_dataset.py --league english-league
python merge_dataset.py --all
```

## Dépendances

`requests`, `flask`, `psutil`, `pandas` (pour l'analyse).

## Notes

- `data/` n'est pas versionné (voir `.gitignore`) — c'est un dossier de sortie
  qui grossit en continu au fil de la collecte, pas du code.
- `backfill_odds.py` est un outil ponctuel écrit pour un bug de collecte
  aujourd'hui corrigé à la racine — conservé pour référence, pas utilisé par
  le flux normal.
