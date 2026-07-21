# Migration backend Matroska natif

- Les jobs et `exact-job` v1 existants restent valides sans modification.
- `mux_backend` est facultatif ; son absence équivaut à `auto`.
- `--mux-backend ffmpeg` permet de forcer temporairement le comportement
  historique. `native` est strict et ne replie jamais.
- La preview JSON conserve les anciens champs `command`/`command_text` et ajoute
  des champs structurés ; les consommateurs existants ne doivent rien retirer.
- Les sorties remux restent exclusivement MKV.
- Muxiveo n'utilise plus `mkvmerge` au runtime ou au packaging. Seuls le job CI
  oracle et `scripts/concat_video.py` exécuté en standalone peuvent l'utiliser.
- Les sorties sont reproductibles pour un même plan mais ne sont pas promises
  identiques octet à octet à celles de MKVToolNix.
