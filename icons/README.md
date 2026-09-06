# Pictogrammes

Dépose ici tes 9 pictogrammes meteoblue avec **exactement** ces noms de fichier
(la casse compte) :

| Fichier attendu       | État du ciel                  |
|------------------------|-------------------------------|
| `clair.png`            | Ciel clair                    |
| `peu-nuageux.png`      | Peu nuageux                   |
| `tres-nuageux.png`     | Très nuageux                  |
| `couvert.png`          | Couvert                       |
| `voile.png`            | Ciel voilé (nuages élevés)    |
| `pluie-faible.png`     | Pluie faible                  |
| `pluie-forte.png`      | Pluie forte                   |
| `neige.png`            | Neige                         |
| `brouillard.png`       | Brouillard                    |

Si tu préfères garder les noms de fichiers d'origine de meteoblue, il suffit
d'adapter l'objet `PICTO_MAP` en haut du `<script>` dans `index.html` pour
faire correspondre chaque clé à ton nom de fichier réel — pas besoin de
toucher au script Python.

Formats acceptés : PNG, SVG ou WebP (change juste l'extension dans
`PICTO_MAP` si besoin).
