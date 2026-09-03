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

## Collecte automatique via GitHub Actions (sans garder le PC allumé)

`.github/workflows/collect.yml` lance un cycle de collecte pour chaque ligue
toutes les 5 minutes (planifié + déclenchement manuel possible depuis
l'onglet Actions). Nécessite un dépôt **public** (minutes illimitées) et le
flag `--persist-season-gate` sur `collector.py`, qui fait persister
`season_started`/`season_id` dans `state.json` au lieu de repartir à zéro à
chaque exécution — indispensable puisque chaque run planifié est un
processus neuf, sans mémoire du précédent.

Les données collectées sont poussées sur une **branche Git séparée `data`**
(pas sur `main`, qui reste juste le code).

⚠️ **Ne pas faire `git checkout data -- data/`** dans ce dossier — ça
écraserait le `data/` local du collecteur manuel (dashboard). Utiliser plutôt
un *worktree* séparé, isolé du dossier de travail principal :

```bash
# Une seule fois, pour créer le dossier séparé (sibling de ce repo) :
git worktree add ../virtual-leagues-collector-data data

# Ensuite, à chaque fois pour rafraîchir avec les dernières données :
cd ../virtual-leagues-collector-data
git pull
```

Les données GitHub Actions sont alors dans
`../virtual-leagues-collector-data/data/`, complètement séparées du `data/`
local utilisé par `app.py`/le dashboard.

À noter : contrairement au collecteur local (interrogation fiable toutes les
75s), un workflow planifié GitHub Actions peut être retardé lors des pics de
charge — la couverture des cotes (qui dépend de capter le round pendant
qu'il est "courant") sera donc un peu moins bonne que celle du collecteur
local continu. Les résultats, eux, ne sont pas affectés.

## Notes

- `data/` n'est pas versionné sur `main` (voir `.gitignore`) — c'est un
  dossier de sortie qui grossit en continu au fil de la collecte, pas du
  code. Il vit sur la branche `data` quand la collecte passe par GitHub
  Actions (voir ci-dessus).
- `backfill_odds.py` est un outil ponctuel écrit pour un bug de collecte
  aujourd'hui corrigé à la racine — conservé pour référence, pas utilisé par
  le flux normal.
