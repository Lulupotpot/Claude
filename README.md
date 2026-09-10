# Tableau de bord — Marathon de Lille (25 octobre 2026)

Un script Python qui lit les données COROS et écrit un `index.html` autonome,
ouvrable directement dans un navigateur.

```bash
python dashboard.py        # actualise les données et reconstruit la page
open index.html            # macOS   (Linux : xdg-open, Windows : start)
```

Aucune dépendance à installer : bibliothèque standard Python 3.8+ uniquement.
La page ne charge qu'une ressource externe, Chart.js depuis un CDN.

## Ce que contient la page

| Section | Contenu |
|---|---|
| Compte à rebours | jours et semaines avant la course |
| Pastilles | VO₂ max, CTL, TSB, volume 4 semaines, FC de repos, VFC |
| Charge d'entraînement | CTL / ATL / TSB sur 12 semaines |
| Kilométrage hebdomadaire | 12 semaines + droite de tendance |
| Récupération | VFC nocturne, FC au repos, sommeil (6 semaines) |
| Jours faciles | FC et allure par sortie, footings vs séances qualité |
| VO₂ max et projections | prédictions COROS face aux records personnels |
| Sorties récentes | 4 dernières semaines, détail par séance |

Chaque graphique a un bouton **Voir le tableau** qui affiche les mêmes chiffres
sous forme de tableau. Un bouton bascule le thème clair / sombre.

## Actualisation autonome : le jeton COROS

Le script **ne demande jamais de mot de passe**. Il cherche un jeton déjà en
cache, dans cet ordre :

1. la variable d'environnement `COROS_ACCESS_TOKEN` ;
2. `~/.coros/token.json` ;
3. `.coros_token.json` à côté du script.

Les fichiers JSON doivent contenir une clé `accessToken` :

```json
{ "accessToken": "collez-le-jeton-ici" }
```

Pour récupérer le jeton une seule fois : se connecter à <https://t.coros.com>,
ouvrir les outils de développement du navigateur, onglet **Réseau**, cliquer sur
n'importe quelle requête vers `teamapi.coros.com` et copier la valeur de
l'en-tête de requête `accesstoken`.

`python dashboard.py --status` indique si un jeton a été trouvé.

### Sans jeton

Le script reconstruit la page à partir de `data/coros_snapshot.json`, l'export
des données COROS capturé le 10 septembre 2026. La page se régénère correctement
mais **les données ne changent pas** tant qu'aucun jeton n'est disponible : le
pied de page indique alors « source : instantané local ».

Un appel qui échoue (jeton expiré, réseau coupé) n'interrompt jamais la
génération : le script le signale et retombe sur le dernier instantané.

## Automatiser

Pour actualiser tous les matins à 7 h sans rien ouvrir :

```cron
0 7 * * *  cd /home/user/Claude && /usr/bin/python3 dashboard.py >> dashboard.log 2>&1
```

## Options

```
python dashboard.py --status     # état des sources de données
python dashboard.py --no-fetch   # reconstruire sans appeler l'API
python dashboard.py --out /chemin/page.html
```

## Réglages

Les constantes en haut de `dashboard.py` :

| Constante | Rôle | Valeur |
|---|---|---|
| `RACE_NAME`, `RACE_DATE` | la course visée | Marathon de Lille, 25/10/2026 |
| `PERSONAL_BESTS` | records déclarés, comparés aux prédictions | 5 km 21:49 · 10 km 44:17 · 15 km 1:22 |
| `HR_MAX` | FC maximale estimée (Tanaka, 28 ans) | 191 |
| `EASY_HR_CEILING` | au-dessus, un footing n'est plus facile | 149 bpm |
| `LONG_RUN_KM` | seuil « sortie longue » | 15 km |
| `QUALITY_KEYWORDS` | mots-clés qui marquent une séance qualité | fractionné, seuil, allure… |

## Notes sur les données

- **Charge sur 12 semaines.** L'API COROS ne renvoie sa charge que sur ~31 jours.
  Au-delà, la série est reconstituée à partir du TRIMP de chaque séance (toutes
  activités confondues), avec une constante de temps et un facteur d'échelle
  ajustés sur la période où les deux sources se recouvrent. Ces points sont
  tracés en pointillé sur fond grisé et signalés « estimé » dans le tableau.
- **VO₂ max.** COROS ne renvoie que la valeur courante, sans historique. Le
  script en archive une par jour dans `data/vo2max_history.json` ; la courbe
  de tendance apparaît dès qu'il y a deux relevés.
- **Sommeil.** Chaque nuit est datée du matin du réveil, comme dans COROS. Les
  nuits de moins d'une heure sont des artefacts de synchronisation et sont
  écartées.
- **Type de séance.** Déduit du nom de la séance et de sa distance, donc de
  l'intention. La FC affichée vient de la montre : c'est la confrontation des
  deux qui répond à « mes jours faciles sont-ils faciles ? ».
