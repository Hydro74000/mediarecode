# Muxiveo CLI

`Muxiveo-cli` est le point d'entrée headless de Muxiveo. Il ne lance pas l'interface graphique et reste strictement non interactif.

## Lancement

```bash
python3 main.py --cli --help
./Muxiveo-cli --help
```

Dans les builds packages, le CLI utilise le même bundle que l'application GUI :

- Linux/AppImage : entrée `Muxiveo-cli` à côté de `Muxiveo`;
- Windows : entrée `Muxiveo-cli.exe`;
- macOS : entrée `Muxiveo-cli` dans `Muxiveo.app/Contents/MacOS/`;
- fallback commun : `Muxiveo --cli ...`.

Depuis les sources, `python3 main.py --cli ...` lance aussi le mode CLI sans initialiser l'interface graphique.

## Commandes

| Commande | Rôle |
|---|---|
| `inspect` | inspecte une source et sort du JSON |
| `inspect --config-template` | génère un template JSON de départ |
| `schema` | affiche le schéma JSON public du contrat CLI |
| `tools` | affiche en JSON les chemins d'outils effectivement résolus par Muxiveo |
| `validate` | valide un job/template sans exécuter ffmpeg |
| `preview` | affiche la commande ffmpeg prévue |
| `remux` | exécute un remux headless |
| `batch` | applique un template à plusieurs jobs |
| `validate --profile` | valide un `decision-profile`, ou son application si `-i` est fourni |
| `preview --profile` | applique un profil en dry preview JSON/commande |
| `run/remux --profile` | applique un profil et remuxe |
| `batch --profile` | applique un profil à un dossier |

Exemples :

```bash
Muxiveo-cli inspect source.mkv
Muxiveo-cli inspect source.mkv --config-template --output sortie.mkv
Muxiveo-cli schema --output Muxiveo-cli.schema.json
Muxiveo-cli schema --version decision-profile
Muxiveo-cli tools
Muxiveo-cli preview --config docs/cli/middle.json
Muxiveo-cli preview --config docs/cli/middle.json --json
Muxiveo-cli validate --config docs/cli/middle.json --json
Muxiveo-cli remux -i source.mkv -o sortie.mkv
Muxiveo-cli remux --config docs/cli/middle.json --dry-run
Muxiveo-cli batch --template docs/cli/complexe-toutes-options-template.json --batch docs/cli/complexe-toutes-options-batch.json --force
Muxiveo-cli batch --template template.json --batch batch.json --dry-run --log-format jsonl
Muxiveo-cli batch --template template.json --batch batch.json --summary summary.json
Muxiveo-cli batch --template exact-job.json --input-dir "Serie/Saison 01" --output-dir "out/Saison 01" --dry-run
Muxiveo-cli batch --template exact-job.json --input-dir "Serie" --recursive --include "*.mkv" --exclude "*sample*" --output-dir "out"
Muxiveo-cli batch --template exact-job.json --input-dir "Serie" --recursive --output-dir "out" --auto-tmdb
Muxiveo-cli batch --template exact-job.json --input-dir "Serie" --recursive --output-dir "out" --auto-tmdb \
    --output-template "{title}.S{season}E{episode}.{episode_title}-{group}"
Muxiveo-cli preview --profile profil.json -i source.mkv --json
Muxiveo-cli run --profile profil.json -i source.mkv -o sortie.mkv
Muxiveo-cli batch --profile profil.json --input-dir "Serie" --recursive --output-dir "out" --auto-tmdb --dry-run
```

## Contrat JSON

Les deux contrats publics principaux sont :

- `kind: "exact-job", version: 1` pour un traitement strict;
- `kind: "decision-profile", version: 1` pour l'automapping low-code.

Le schéma peut être exporté avec `Muxiveo-cli schema`.

Job minimal :

```json
{
  "version": 1,
  "kind": "exact-job",
  "sources": [
    {"path": "source.mkv"}
  ],
  "output": "sortie.mkv"
}
```

La sortie remux est exclusivement `.mkv`. Le champ additif `mux_backend` est
facultatif et son absence conserve tous les jobs v1 existants :

```json
{
  "version": 1,
  "kind": "exact-job",
  "sources": [{"path": "source.mkv"}],
  "output": "sortie.mkv",
  "mux_backend": "auto"
}
```

- `auto` inspecte le plan complet, choisit le writer natif lorsqu'il peut tenir
  le contrat et journalise tout repli FFmpeg avec sa raison exacte ;
- `native` est strict et ne replie jamais ;
- `ffmpeg` conserve le chemin historique.

`--mux-backend auto|native|ffmpeg` surcharge le job pour `validate`, `preview`,
`run`, `remux`, `batch` et les variantes basées sur un profil. La préférence
persistée `[remux] mux_backend = auto` ne remplace pas un choix explicite du job.

### `sources`

Chaque source peut être une chaîne ou un objet :

```json
{
  "path": "source.mkv",
  "attachments": "none",
  "copy_tags": false
}
```

`attachments` accepte :

- `"none"` ou `false`;
- `"all"` ou `true`;
- une liste de noms ou indices.

### `decision-profile`

Un profil décisionnel ne contient ni source ni sortie. Il décrit des règles
réutilisables :

```json
{
  "version": 1,
  "kind": "decision-profile",
  "name": "VF + VO",
  "variables": {
    "aliases": {
      "*": {
        "EAC3": "DDP",
        "AC3": "Dolby Digital"
      },
      "lang_name": {
        "French": "Français"
      }
    }
  },
  "groups": [
    {"id": "audio", "label": "Audio", "enabled": true, "priority": 200}
  ],
  "rules": [
    {
      "id": "rename_fr",
      "label": "Renommer VF",
      "group_id": "audio",
      "priority": 100,
      "write_mode": "priority",
      "scope": "all",
      "match": {
        "all": [
          {"field": "type", "op": "is", "value": "audio"},
          {"field": "language", "op": "is", "value": "fr-FR"},
          {"field": "source_title", "op": "contains", "expr": "(VFQ | VFF) & !Commentary", "required": false}
        ]
      },
      "actions": [
        {"type": "set_enabled", "value": true},
        {"type": "set_title", "pattern": "{lang_name} {codec} {channels} {audio_object}"},
        {"type": "remove_track_tags", "value": ["old_tag"]}
      ]
    }
  ]
}
```

`write_mode` définit ce qui se passe si plusieurs règles écrivent le même champ :

- `"priority"` : la plus forte priorité gagne; une priorité égale reste un conflit;
- `"override"` : cette règle remplace une valeur déjà proposée;
- `"add"` : cette règle complète sans écraser; pour un titre, le fragment est ajouté.

Les expressions acceptent aussi `!` en raccourci et `NOT(...)` pour exclure un critère.
Le keyword `{none}` cible une valeur vide dans le champ courant. Les actions
`remove_track_tags` sont appliquées avant les `add_track_tags` de la même règle.

Les conditions peuvent utiliser soit `value`, soit `expr`. `expr` accepte la
même syntaxe que l'éditeur de profils :

```json
{"field": "source_title", "op": "contains", "expr": "(VFQ | VFF) & Forced"}
```

Dans une expression :

- `&` signifie AND;
- `|` signifie OR;
- les parenthèses fixent la priorité;
- les atoms `{atmos}` ou `{dtsx}` ciblent les keywords booléens correspondants;
- les champs restent combinés entre eux par AND dans un bloc `all`.

Pour chercher `&`, `|`, `(` ou `)` comme texte réel, utilisez des guillemets ou
un échappement :

```json
{"field": "source_title", "op": "contains", "expr": "\"A | B\" | Dolby \\& DTS"}
```

Une condition qui contient à la fois `value` et `expr` est invalide.

Commandes :

```bash
Muxiveo-cli validate --profile profil.json
Muxiveo-cli preview --profile profil.json -i source.mkv --json
Muxiveo-cli run --profile profil.json -i source.mkv -o sortie.mkv
Muxiveo-cli batch --profile profil.json --input-dir "Serie" --recursive --output-dir "out"
```

`--profile` accepte soit un chemin complet, soit le nom d'un profil enregistré
dans `<dossier de config muxiveo>/profiles/decision/`. L'extension `.json`
est optionnelle : `--profile BestOfAll` cherchera aussi `BestOfAll.json`.

Keywords de renommage disponibles :

```text
{type} {source_index} {track_index}
{language} {lang} {lang_name} {source_language}
{title} {source_title}
{codec} {codec_raw} {codec_name} {channels} {channel_layout} {audio_object} {atmos} {dtsx}
{resolution} {width} {height} {hdr} {video_flags_hex}
{video_hdr} {video_hdr10} {video_hdr10plus}
{video_dolby_vision} {video_hlg} {video_sdr}
{flags} {flag_enabled} {flag_default} {flag_forced}
{flag_hearing_impaired} {flag_visual_impaired}
{flag_original} {flag_commentary}
{track_tags}
```

`variables.aliases` remplace des valeurs au rendu des patterns de titre et des
templates de sortie :

```json
{
  "variables": {
    "aliases": {
      "*": {"EAC3": "DDP"},
      "lang_name": {"French": "Français"}
    }
  }
}
```

- `*` est global.
- `lang_name`, `codec`, `codec_name`, etc. ciblent un keyword précis.
- priorité : alias ciblé > alias global > valeur originale.
- matching insensible à la casse et aux espaces de bord.
- pas de remplacement récursif.
- les aliases n'affectent pas les critères décisionnels.

`variables.codec_names` reste lu pour compatibilité avec les anciens profils,
mais les nouveaux profils doivent utiliser `variables.aliases`. Pour forcer le
codec technique brut, utilisez `{codec_raw}`.

`{lang_name}` masque la région pour la variante d'origine et la conserve entre
parenthèses pour les variantes régionales non standard :

| Langue | `{lang_name}` |
|---|---|
| `fr-FR` | `French` |
| `fr-CA` | `French (Canada)` |
| `pt-PT` | `Portuguese` |
| `pt-BR` | `Portuguese (Brazil)` |

### `tracks`

Les edits explicites modifient une piste ciblée par index ou par sélecteur :

```json
{
  "tracks": [
    {
      "source": 0,
      "id": 2,
      "enabled": true,
      "language": "fr-FR",
      "title": "French EAC3 5.1 Atmos",
      "flags": {"default": true},
      "time_shift_ms": 0
    }
  ]
}
```

### `track_order`

`track_order` permet de fixer l'ordre de sortie après filtrage. Chaque entrée
peut être un objet ou un tableau court :

```json
{
  "track_order": [
    {"source": 0, "id": 0},
    [0, 2]
  ]
}
```

Les erreurs de forme sont reportees avec le chemin exact, par exemple
`track_order[1][1]`.

### `chapters`

```json
{
  "chapters": {
    "source_index": 0,
    "include_source": true,
    "import": "chapters.ffmetadata",
    "add": [
      {"timestamp": "00:00:00.000", "chaptername": "Opening"}
    ]
  }
}
```

Imports acceptes :

- JSON avec `chapters` ou `entries`;
- ffmetadata;
- OGM simple.

### `tmdb`

TMDB est opt-in. Aucun appel réseau n'est fait si `tmdb` est absent ou faux.

```json
{
  "tmdb": {
    "enabled": true,
    "kind": "tv",
    "query": "Series",
    "season": "1",
    "episode": "1",
    "language": "fr-FR"
  }
}
```

Si `id` ou `tmdb_id` est présent, cet ID est utilisé. Sinon, le premier résultat de recherche est retenu.
Dans un JSON réutilisable, `season` et `episode` peuvent valoir `"auto"` pour
être relus depuis le nom de chaque source. Les booléens
`auto_detect_episode` et `auto_metadata` correspondent au comportement
activé par `--auto-tmdb` : détection source prioritaire, puis remplacement des
métadonnées TMDB statiques exportées par le GUI.

En CLI, `--auto-tmdb` active la recherche TMDB sans modifier le JSON. La
requête est déduite du nom de fichier, le premier résultat est retenu, et
Muxiveo détecte automatiquement `S01E02` ou `01x02` pour renseigner saison
et épisode. Si le template vient d'un export GUI et contient déjà le titre,
les tags TMDB ou `tmdb.season` / `tmdb.episode` du premier épisode, `--auto-tmdb`
les régénère pour chaque fichier traité. `--tmdb` reste disponible comme alias
historique.

Options utiles :

| Option | Effet |
|--------|-------|
| `--auto-tmdb` | cherche TMDB et applique tags + titre conteneur + cover |
| `--tmdb-id ID` | utilise un ID TMDB précis au lieu de choisir le premier résultat |
| `--tmdb-apikey KEY` | surcharge la clé API TMDB (priorité sur la config Muxiveo et le JSON job) |
| `--no-cover` | applique les tags TMDB mais n'ajoute pas la cover |
| `--no-attach` | n'inclut aucun attachment source, aucun extra attachment, ni cover TMDB |

Sans `--tmdb-apikey`, la clé est lue dans `~/.config/muxiveo/config.ini`
(`metadata/tmdb_api_key` ou `tmdb_bearer_token`). À défaut, la variable
d'environnement `MUXIVEO_TMDB_BEARER_TOKEN` puis un token Bearer embarqué
servent de repli (parité GUI), donc `--auto-tmdb` fonctionne sans configuration
préalable.

Exemple batch :

```bash
Muxiveo-cli batch \
  --template exact-job.json \
  --input-dir "Serie" \
  --recursive \
  --output-dir "out" \
  --auto-tmdb
```

Avec un profil décisionnel :

```bash
Muxiveo-cli batch \
  --profile BestOfAll \
  --input-dir "Serie" \
  --recursive \
  --output-dir "out" \
  --auto-tmdb \
  --no-cover
```

Depuis un `exact-job` exporté par le GUI pour un épisode de série, utilisez
`--auto-tmdb` et un template de sortie pour rendre le titre conteneur, les tags
SEASON/EPISODE/SUBTITLE/SYNOPSIS et le nom de fichier dynamiques :
Les exports GUI récents ajoutent aussi un bloc `tmdb` désactivé avec
`season: "auto"` / `episode: "auto"` quand ils détectent des tags de série ;
`--auto-tmdb` l'active pour le batch.

```bash
Muxiveo-cli batch \
  --template "/home/hydromel/Vidéos/Spider-Noir.S01E01.MULTi.VF2.HDR.DV.2160p.WEB.H265-SUPPLY-MVO.exact-job.json" \
  --input-dir "/home/hydromel/Téléchargements/sabnzbd/complete/Spider-Noir.S*E*.MULTi.VF2.HDR.DV.2160p.WEB.H265-SUPPLY" \
  --output-dir "/home/hydromel/Vidéos/Spider-Noir" \
  --auto-tmdb \
  --output-template "{title:release}.{season_episode}.{episode_title:release}.{audio-multi}.{audio-fr-tag}.{video-resolution:best}.{video-source}.{video-hdr:best}.{video-dolby-vision}.{video-codec-release:best}-{group}" \
  --dry-run
```

`--input-dir` accepte aussi les globs de dossiers ; l'exemple ci-dessus crée un
job pour les MKV présents dans chaque dossier `Spider-Noir.SxxEyy...`.

### `batch`

Le batch fusionne chaque job avec le template :

```json
{
  "jobs": [
    {
      "sources": [{"path": "S01E01.mkv"}],
      "output": "S01E01.remux.mkv",
      "tmdb": {"episode": "1"}
    }
  ]
}
```

Le fichier batch est validé avant le premier job. Les entrées de `jobs` ou
`inputs` doivent être des objets job ou des chemins texte.

Le batch peut aussi créer les jobs depuis un dossier :

```bash
Muxiveo-cli batch \
  --template exact-job.json \
  --input-dir "Serie" \
  --recursive \
  --output-dir "out" \
  --dry-run
```

Dans ce mode, chaque fichier vidéo/conteneur compatible devient un job. Les
fichiers audio seuls et sous-titres seuls sont ignorés. Le parcours des
sous-dossiers est activé uniquement avec `--recursive`.

Filtres disponibles :

| Option | Effet |
|--------|-------|
| `--input-dir DIR` | ajoute un dossier à scanner ; option répétable |
| `--recursive` | inclut les sous-dossiers |
| `--include GLOB` | limite les fichiers retenus ; option répétable |
| `--exclude GLOB` | ignore les fichiers correspondants ; option répétable |
| `--output-dir DIR` | génère les sorties en `.mkv` sous ce dossier |

Avec `--recursive` et `--output-dir`, l'arborescence relative est conservée :

```text
Serie/Saison 01/E01.mkv  ->  out/Saison 01/E01.mkv
Serie/Saison 02/E01.mkv  ->  out/Saison 02/E01.mkv
```

`--batch FILE` ne se mélange pas avec `-i/--input` ou `--input-dir` : utilisez
soit le batch JSON, soit le mode direct.

### `output_template` — composer le nom de sortie

`--output-template` (ou le champ JSON `output_template`) permet de composer le
nom du fichier de sortie à partir de placeholders alimentés par TMDB et par
parsing du nom de fichier source. L'option est disponible sur toutes les
sous-commandes.

```bash
Muxiveo-cli batch \
  --template exact-job.json \
  --input-dir "Serie" --recursive --output-dir "out" --auto-tmdb \
  --output-template "{title}.S{season}E{episode}.{episode_title}.{year}-{group}"

Muxiveo-cli batch \
  --profile BestOfAll --input-dir "Films" --output-dir "out" \
  --output-template "{title:release}.{year}.{audio-multi}.{audio-fr-tag}.{audio-codec-release:best}.{audio-channels:best}.{audio-immersive}.{video-source}.{video-resolution:best}.{video-10bit}.{video-hdr:best}.{video-dolby-vision}.{video-codec-release:best}-{group}"
```

Tokens disponibles :

| Token | Source | Exemple |
|---|---|---|
| `{source_name}` | nom du fichier source sans extension | `Devil.May.Cry.2025.S01E02.MULTi.1080p.WEB.x264` |
| `{title}` | titre TMDB (film ou série) | `Devil May Cry` |
| `{title:release}` | titre TMDB normalisé release ASCII à points | `Devil.May.Cry` |
| `{year}` | année TMDB | `2025` |
| `{episode_title}` | titre d'épisode TMDB | `Pilote` |
| `{episode_title:release}` | titre d'épisode normalisé release ASCII à points | `Le.Pilote` |
| `{season}` | saison zéro-paddée 2 chiffres | `01` |
| `{episode}` | épisode zéro-paddé 2 chiffres | `02` |
| `{season_num}` | saison brute (int) — combinable `:03d` | `1` |
| `{episode_num}` | épisode brut (int) | `2` |
| `{season_episode}` | code combiné style scene | `S01E02` |
| `{group}` | tag de release extrait du nom source | `RARBG`, `NTb` |
| `{audio-lang:best}` / `{audio-lang:all}` | langue(s) audio des pistes finales | `fr-FR`, `fr-FR+en-US` |
| `{audio-lang-name:all}` | nom(s) de langue audio régionalisés | `French+French (Canada)` |
| `{sub-lang:all}` | langue(s) sous-titres des pistes finales | `fr-FR+en-US` |
| `{audio-multi}` | `MULTi` si plusieurs familles audio | `MULTi` |
| `{audio-fr-tag}` | tag FR audio final | `VFF`, `VFQ`, `VF2` |
| `{sub-vostfr}` | `VOSTFR` si sous-titre FR sans audio FR | `VOSTFR` |
| `{audio-codec-release:best}` | meilleur codec audio release | `TrueHD`, `DDP` |
| `{audio-channels:best}` | meilleurs canaux audio | `7.1`, `5.1` |
| `{audio-immersive}` | audio immersif final | `Atmos` |
| `{video-source}` | source extraite du nom source | `BluRay`, `WEB` |
| `{video-resolution:best}` | meilleure résolution vidéo release | `2160p` |
| `{video-10bit}` | tag 10 bits si détecté ou implicite HDR10+/DV | `10Bits` |
| `{video-hdr:best}` | meilleur HDR release | `HDR10P`, `HDR`, `HLG` |
| `{video-dolby-vision}` | tag Dolby Vision | `DV` |
| `{video-codec-release:best}` | codec vidéo release | `x265`, `x264`, `AV1` |

Règles :

- **Priorité** : `--output / -o` explicite > `--output-template` (rendu) > `output` direct (job/JSON).
- **Texte libre** mélangeable avec les tokens : `"{title}.{year}.texte libre.{group}"`.
- **Keywords pistes** : les préfixes canoniques sont `video-*`, `audio-*`,
  `sub-*`; `subtitle-*` et les variantes `_` sont acceptés en alias.
- **Décisions** : les keywords pistes se basent sur les pistes finales activées,
  après profil ou edits exact-job. Les modifiers disponibles sont `:best`,
  `:first` et `:all`; `:best` est le défaut.
- **Aliases** : `variables.aliases` s'applique aussi aux keywords de pistes
  rendus par `--output-template`. Par exemple `{audio-codec:best}` peut rendre
  `DDP` avec `{"variables": {"aliases": {"*": {"EAC3": "DDP"}}}}`.
- **`--output-all`** : force les keywords pistes en mode `all`, même si le
  template contient `:best` ou aucun modifier.
- **Tokens inconnus** rendent une chaîne vide (pas d'erreur).
- **Nettoyage release** : les segments vides sont compactés pour éviter les
  doubles points ou les séparateurs `.-` / `-.`.
- **Extension** : si le template ne se termine pas par une extension vidéo connue
  (`.mkv`, `.mp4`, `.m4v`, `.mov`, `.avi`, `.webm`, `.mka`, `.m4a`, `.ts`, `.m2ts`),
  `.mkv` est ajouté automatiquement.
- **Sanitization** : les caractères interdits filesystem (`/ \ : * ? " < > |`)
  présents dans les valeurs de tokens sont remplacés par `.`.
- **Batch + `--output-dir`** : le template est résolu dans `--output-dir`. Si le
  template ne discrimine pas deux sources (collision de sortie rendue), une erreur
  explicite est levée et le batch s'arrête (sauf `--continue-on-error`).
- **Sans TMDB** : `{source_name}` et `{group}` restent utilisables ; les tokens
  TMDB rendent vide.

Le champ peut également être posé dans le JSON job :

```json
{
  "version": 1,
  "kind": "exact-job",
  "sources": [{"path": "S01E02.mkv"}],
  "output_template": "{title}.S{season}E{episode}.{episode_title}-{group}",
  "output_all": false,
  "tmdb": {"enabled": true}
}
```

`--output-template` en ligne de commande surcharge la valeur JSON.

Avec `--summary FILE`, le batch ecrit un rapport JSON final :

```json
{
  "total": 1,
  "successes": 1,
  "failures": 0,
  "exit_code": 0,
  "jobs": [
    {
      "job_index": 0,
      "input": "S01E01.mkv",
      "output": "S01E01.remux.mkv",
      "status": "success",
      "exit_code": 0
    }
  ]
}
```

### Sorties JSON de validation et preview

`validate --json` et `preview --json` gardent les mêmes codes retour que les
commandes texte, mais ecrivent un objet JSON sur stdout.

Exemple `preview --json` :

```json
{
  "valid": true,
  "errors": [],
  "output": "sortie.mkv",
  "sources": [{"index": 0, "path": "source.mkv"}],
  "track_order": [{"source": 0, "id": 0}],
  "selected_backend": "native",
  "plan_version": 1,
  "preparation_commands": [],
  "native_diagnostics": [],
  "execution_preview": {"backend": "native", "action": "internal_matroska_write"},
  "command": ["ffmpeg", "-hide_banner", "..."],
  "command_text": "ffmpeg \\\n    -hide_banner \\\n    ..."
}
```

`command` et la commande FFmpeg de référence restent présents pour la
compatibilité des consommateurs v1. `selected_backend`, `plan_version`,
`execution_preview`, `preparation_commands` et `native_diagnostics` décrivent
l'exécution réelle. La preview texte commence par le backend choisi. En JSONL,
l'événement `mux_backend` expose séparément le backend demandé/sélectionné, le
repli, sa raison et les diagnostics.

## Verbosité

Par défaut, la sortie brute de ffmpeg (lignes `frame=`, `fps=`, `bitrate=`,
`time=` …) n'est **pas** affichée — seuls les évènements de workflow (début
d'étape, fin de tâche, erreurs) le sont. Pour suivre la progression ffmpeg en
direct, ajouter `--verbose` :

```bash
Muxiveo-cli remux -i source.mkv -o sortie.mkv --verbose
Muxiveo-cli batch --template t.json --input-dir Serie --output-dir out --verbose
```

`--verbose` s'applique à toutes les sous-commandes (`remux`, `run`, `batch`,
`profile apply`, `profile batch`).

## Codes retour

| Code | Signification |
|---:|---|
| 0 | succes |
| 2 | arguments, JSON ou contrat invalide |
| 3 | validation metier echouee |
| 4 | outil externe introuvable |
| 5 | sortie existante sans `--force` |
| 6 | echec workflow |
| 7 | batch partiellement echoue |

## Batch JSON Lines

Avec `--log-format jsonl`, le batch emet des evenements structurés :

```json
{"level":"info","message":"Batch job 1 demarre","event":"batch_job","job_index":0,"input":"S01E01.mkv","output":"S01E01.remux.mkv","status":"started"}
{"level":"info","message":"Batch job 1 termine","event":"batch_job","job_index":0,"input":"S01E01.mkv","output":"S01E01.remux.mkv","status":"success"}
{"level":"info","message":"Batch termine : 1/1 succes.","event":"batch_summary","total":1,"failures":0}
```
