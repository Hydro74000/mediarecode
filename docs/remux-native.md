# Backend Matroska natif

Muxiveo produit les MKV finaux avec son lecteur/writer EBML interne. MKVToolNix
n'est ni une dépendance runtime, ni un outil du setup, ni un composant du
packaging. L'équivalence visée avec `mkvmerge` est sémantique : mêmes pistes,
payloads, ordre de décodage, timestamps, propriétés, chapitres, tags,
attachments et signalisation HDR/Dolby Vision. La disposition binaire peut
différer.

## Sélection du backend

Le job v1 accepte `"mux_backend": "auto" | "native" | "ffmpeg"`. Le champ est
facultatif et vaut `auto` par défaut. L'interface propose le même choix pour le
job courant et initialise le sélecteur depuis `[remux] mux_backend`.

- `auto` inspecte les sources et les préparations, puis utilise le natif si le
  contrat est transposable. Sinon le repli FFmpeg est explicite dans le log.
- `native` interdit tout repli. Une incompatibilité arrête le job avant la
  création de la sortie finale.
- `ffmpeg` force le backend historique.

La preview texte commence par le backend réel. La preview JSON garde la
commande FFmpeg v1 comme référence et ajoute `selected_backend`, `plan_version`,
`execution_preview`, `preparation_commands` et `native_diagnostics`.

## Garanties natives

- sortie exclusivement `.mkv`, multi-pistes et déterministe pour un même plan ;
- UIDs dérivés des sources, UIDs d'origine, sélection, ordre et métadonnées,
  indépendants du chemin de sortie, des identifiants UI et de l'heure ;
- conservation de l'ordre de décodage par piste, y compris HEVC avec B-frames ;
- timestamps en nanosecondes avec `TimestampScale` exact et offsets négatifs
  coupés avant zéro ;
- `SimpleBlock`, `BlockGroup`, lacing Xiph/fixed/EBML, durées, références,
  `CodecState`, `DiscardPadding` et `BlockAdditions` ;
- propriétés vidéo/audio/sous-titres imbriquées, CodecPrivate, langues legacy
  et BCP-47, flags, couleur/mastering, CLL/FALL et `BlockAdditionMapping` ;
- chapitres, tags ciblés et attachments recopiés avec remappage de leurs UIDs ;
- statistiques mkvmerge de chaque piste régénérées (`BPS`, `DURATION`,
  `NUMBER_OF_FRAMES`, `NUMBER_OF_BYTES`) pour conserver le compteur
  d'éléments dans MediaInfo et lors d'une réinspection ;
- Cues de keyframes avec `CueRelativePosition`, plus index audio seul ;
- écriture dans `.partial`, validation interne puis `ffprobe`, et renommage
  atomique. Un échec supprime le partiel et préserve une sortie antérieure.

Les MKV/WebM et variantes Matroska sont lus directement. Les autres sources
acceptées sont canonicalisées par FFmpeg dans un MKV temporaire en copie de
flux ; les sous-titres incompatibles et variantes audio sont préparés avant
l'écriture native finale.

## Diagnostics et limites

Le mode natif refuse explicitement une piste chiffrée, un EBML illisible, un
codec de sous-titres nécessitant OCR ou une opération que la préparation ne
peut pas matérialiser. En `auto`, FFmpeg n'est choisi que s'il peut respecter le
contrat ; une erreur I/O commune reste fatale.

Le corpus suivi dans `tests/corpus/matroska` est synthétique et vérifié par
SHA-256. La CI native Linux/Windows/macOS vérifie l'absence de `mkvmerge`. Un job
Ubuntu séparé installe MKVToolNix uniquement comme oracle et compare les
rapports normalisés, y compris les hashes des payloads.

## `concat_video.py`

Le script standalone est l'unique exception MKVToolNix :

- `--mkvmerge` force cette voie ;
- sans switch : Muxiveo, puis `mkvmerge`, puis FFmpeg ;
- la voie Muxiveo transmet toujours `mux_backend: native` et récupère ses
  chemins d'outils via `Muxiveo --cli tools` ;
- si Muxiveo n'est pas installé ou détectable, le script garde sa résolution
  locale des outils.

Cette exception ne doit jamais être importée dans le runtime Muxiveo.
