# Météo Gréolières-les-Neiges — 24h

Page statique (GitHub Pages) affichant les prévisions ICON-CH1 (MétéoSuisse,
via Open-Meteo, gratuit et sans clé) pour Gréolières-les-Neiges, sur la
fenêtre **18h → 17h le lendemain**, avec correction automatique du
**trou à froid nocturne** à partir des observations de ta station Datacake.

## Comment ça marche

- Un **workflow GitHub Actions** (`.github/workflows/update.yml`) tourne
  chaque heure, interroge l'API Open-Meteo (modèle `meteoswiss_icon_ch1`) et
  écrit `data/forecast.json`.
- Entre **19h et (lever du soleil + 1h)**, si le modèle prévoit un ciel
  dégagé (nébulosité < 20 %) et un vent faible (< 10 km/h), la température
  est corrigée à la baisse d'un décalage appris (`offset_c`).
- Chaque matin, le script compare, sur les heures de la nuit qui viennent de
  s'écouler remplissant ces mêmes critères (dégagé + calme), la température
  observée à ta station Datacake et la température brute du modèle. L'écart
  moyen met à jour `offset_c` par moyenne mobile exponentielle
  (`data/bias_state.json`), avec un historique conservé dans `history`.
- `index.html` charge simplement `data/forecast.json` et affiche le tableau,
  les pictogrammes et le graphique (Chart.js via CDN). Aucun serveur requis.

## Mise en route

1. **Créer le dépôt GitHub** et y pousser tout le contenu de ce dossier.
2. **Activer GitHub Pages** : Paramètres du dépôt → Pages → Source =
   branche `main`, dossier `/ (root)`.
3. **Ajouter tes pictogrammes** dans `icons/` — voir `icons/README.md` pour
   les noms de fichiers attendus.
4. **Configurer l'accès à ta station Datacake** (pour l'apprentissage
   automatique — sans ça, la correction reste figée à sa valeur par
   défaut de -2°C) :
   - Génère un **token d'accès personnel** dans Datacake (Paramètres du
     compte → API Access Tokens).
   - Récupère l'**UUID du device** (⚠️ différent de l'identifiant `8edbfefb-...`
     de ton lien de dashboard public `/pd/...` — cet UUID se trouve dans les
     paramètres du device, ou via la requête GraphQL `allDevices`).
   - Note le **nom exact du champ température** dans ta base Datacake
     (souvent `TEMPERATURE`, mais vérifie dans la vue "Database" du device).
   - Dans le dépôt GitHub : Settings → Secrets and variables → Actions →
     New repository secret, et ajoute :
     - `DATACAKE_TOKEN`
     - `DATACAKE_DEVICE_ID`
     - `DATACAKE_TEMP_FIELD` (optionnel si c'est bien `TEMPERATURE`)
5. **Lancer le workflow une première fois** manuellement (onglet Actions →
   "Mise à jour des prévisions" → Run workflow) pour générer le premier
   `data/forecast.json`, plutôt que d'attendre la prochaine heure pleine.

## Réglages ajustables

Tout est en haut de `scripts/update_forecast.py` :

- `CLEAR_CLOUD_THRESHOLD` (20 %) et `CALM_WIND_THRESHOLD` (10 km/h) :
  critères "ciel dégagé + vent faible" déclenchant la correction et
  l'apprentissage.
- `NIGHT_CORR_START_HOUR` (19h) : début de la fenêtre de correction
  nocturne (la fin est toujours lever du soleil + 1h, calculée
  automatiquement chaque jour).
- `DEFAULT_ALPHA` (0.25) : poids donné à la dernière nuit dans la moyenne
  mobile de l'écart appris — augmente-le pour réagir plus vite aux
  changements de saison, baisse-le pour plus de stabilité.
- La logique des pictogrammes (`pick_picto`) est une estimation à partir de
  la nébulosité/précipitations/humidité du modèle (ICON-CH1 ne fournit pas
  directement un code "brouillard" fiable) — à affiner si besoin au fil de
  l'observation.

## Limites connues

- ICON-CH1 a un horizon de prévision de ~33h, mis à jour toutes les 3h :
  largement suffisant pour la fenêtre de 23h affichée, mais si le workflow
  ne tourne pas pendant plusieurs heures, il faudra le relancer pour
  rafraîchir la fenêtre.
- L'apprentissage du biais ne porte que sur les heures nocturnes
  dégagées/calmes : les nuits nuageuses ou venteuses n'alimentent pas
  l'apprentissage (elles n'ont pas besoin de correction, le trou à froid ne
  se formant pas dans ces conditions).
- Le pictogramme "brouillard" est une estimation (humidité > 95 % + vent
  très faible + couverture nuageuse élevée) faute de variable "visibilité"
  disponible sur ICON-CH1 via Open-Meteo.
