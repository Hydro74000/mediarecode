"""Argparse construction for muxiveo --cli."""

from __future__ import annotations

import argparse

from cli.commands import cmd_batch, cmd_inspect, cmd_preview, cmd_profile, cmd_remux, cmd_run, cmd_schema, cmd_tools, cmd_validate
from core.version import APP_EXECUTABLE_NAME, APP_NAME


def _add_common_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--config", help="Fichier JSON job/template.")
    parser.add_argument("--ffmpeg", help="Chemin ffmpeg override.")
    parser.add_argument("--ffprobe", help="Chemin ffprobe override.")
    parser.add_argument("--mediainfo", help="Chemin mediainfo override.")
    parser.add_argument("--work-dir", help="Répertoire de travail override.")
    parser.add_argument("--threads", type=int, help="Nombre de threads ffmpeg.")
    parser.add_argument(
        "--mux-backend", choices=("auto", "native", "ffmpeg"), default=None,
        help="Backend MKV : auto (défaut), native ou ffmpeg. Prioritaire sur le job.",
    )
    parser.add_argument(
        "--output-template",
        dest="output_template",
        default="",
        help=(
            "Template du nom de sortie. Placeholders disponibles : "
            "{source_name},{title},{year},{season},{episode},{episode_title},"
            "{season_episode},{group}, keywords pistes comme {audio-lang:best} "
            "(+ {season_num},{episode_num} pour formats numériques). Extension "
            ".mkv ajoutée si absente."
        ),
    )
    parser.add_argument(
        "--output-all",
        action="store_true",
        help="Force les keywords pistes de --output-template en mode all (toutes les valeurs finales trouvées).",
    )
    parser.add_argument("--log-format", choices=("text", "jsonl"), default="text")
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Affiche la sortie ffmpeg en direct (progression, codecs, timing). Par défaut, seule la progression des étapes de workflow est affichée.",
    )


def _add_tmdb_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--auto-tmdb", action="store_true", help="Chercher TMDB automatiquement et prendre le premier résultat.")
    parser.add_argument("--tmdb", action="store_true", help="Alias historique de --auto-tmdb.")
    parser.add_argument("--tmdb-id", type=int, help="ID TMDB explicite.")
    parser.add_argument("--tmdb-apikey", dest="tmdb_apikey", default="", help=f"Clé API TMDB v3 (surcharge la config {APP_NAME} et le JSON job).")
    parser.add_argument("--no-cover", action="store_true", help="Ne pas ajouter la cover TMDB.")
    parser.add_argument("--no-attach", action="store_true", help="Ne pas inclure d'attachments ni de cover TMDB.")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog=f"{APP_EXECUTABLE_NAME} --cli", description=f"{APP_NAME} headless CLI")
    sub = parser.add_subparsers(dest="command", required=True)

    inspect = sub.add_parser("inspect", help="Inspecter une ou plusieurs sources.")
    _add_common_options(inspect)
    inspect.add_argument("input", nargs="+")
    inspect.add_argument("--config-template", action="store_true")
    inspect.add_argument("--output")
    inspect.set_defaults(func=cmd_inspect)

    schema = sub.add_parser("schema", help="Afficher le schéma JSON du contrat CLI.")
    schema.add_argument("--output", help="Ecrire le schéma JSON dans un fichier.")
    schema.add_argument("--version", dest="schema_version", choices=("1", "exact-job", "decision-profile", "all"), default="1")
    schema.add_argument("--log-format", choices=("text", "jsonl"), default="text")
    schema.set_defaults(func=cmd_schema)

    tools = sub.add_parser("tools", help="Afficher les chemins d'outils résolus par Muxiveo.")
    tools.add_argument("--log-format", choices=("text", "jsonl"), default="text")
    tools.set_defaults(func=cmd_tools)

    for name, help_text, func in (
        ("validate", "Valider une config remux.", cmd_validate),
        ("preview", "Afficher la commande ffmpeg prévue.", cmd_preview),
        ("remux", "Exécuter un remux.", cmd_remux),
        ("run", "Exécuter un job remux.", cmd_run),
    ):
        p = sub.add_parser(name, help=help_text)
        _add_common_options(p)
        p.add_argument("--profile", help="Profil décisionnel à appliquer aux entrées.")
        p.add_argument("-i", "--input", action="append")
        p.add_argument("-o", "--output")
        _add_tmdb_options(p)
        p.add_argument("--force", action="store_true", help="Autoriser l'écrasement de la sortie.")
        p.add_argument("--dry-run", action="store_true", help="Valider et afficher la commande sans exécuter.")
        p.add_argument("--nfo", dest="nfo", action="store_true", default=None)
        p.add_argument("--no-nfo", dest="nfo", action="store_false")
        p.add_argument("--writing-application", default="")
        if name in {"validate", "preview"}:
            p.add_argument("--json", dest="json_output", action="store_true", help="Sortie JSON structurée.")
        if name == "remux":
            p.add_argument("--save", help="Sauvegarder les options/règles en template réutilisable.")
        p.set_defaults(func=func)

    batch = sub.add_parser("batch", help="Appliquer un template à plusieurs jobs.")
    _add_common_options(batch)
    batch.add_argument("--template")
    batch.add_argument("--profile", help="Profil décisionnel à appliquer au lot.")
    batch.add_argument("--batch", help="JSON contenant `jobs` ou `inputs`.")
    batch.add_argument("-i", "--input", action="append", help="Entrée batch simple; répétable.")
    batch.add_argument("--input-dir", action="append", help="Dossier à scanner pour créer un job par vidéo; répétable.")
    batch.add_argument("--recursive", action="store_true", help="Scanner récursivement les dossiers fournis avec --input-dir.")
    batch.add_argument("--include", action="append", help="Glob de fichiers à inclure lors du scan de dossiers; répétable.")
    batch.add_argument("--exclude", action="append", help="Glob de fichiers à exclure lors du scan de dossiers; répétable.")
    batch.add_argument("--output-dir")
    _add_tmdb_options(batch)
    batch.add_argument("--force", action="store_true")
    batch.add_argument("--dry-run", action="store_true", help="Valider et afficher les commandes sans exécuter.")
    batch.add_argument("--continue-on-error", action="store_true")
    batch.add_argument("--summary", help="Ecrire un rapport JSON final.")
    batch.add_argument("--nfo", dest="nfo", action="store_true", default=None)
    batch.add_argument("--no-nfo", dest="nfo", action="store_false")
    batch.add_argument("--writing-application", default="")
    batch.set_defaults(func=cmd_batch)

    profile = sub.add_parser("profile", help="Valider, prévisualiser ou appliquer un profil décisionnel.")
    profile_sub = profile.add_subparsers(dest="profile_command", required=True)

    profile_validate = profile_sub.add_parser("validate", help="Valider un decision-profile v1.")
    _add_common_options(profile_validate)
    profile_validate.add_argument("--profile", required=True)
    profile_validate.add_argument("--json", dest="json_output", action="store_true")
    profile_validate.set_defaults(func=cmd_profile)

    profile_preview = profile_sub.add_parser("preview", help="Prévisualiser un profil sur une ou plusieurs sources.")
    _add_common_options(profile_preview)
    profile_preview.add_argument("--profile", required=True)
    profile_preview.add_argument("-i", "--input", action="append", required=True)
    profile_preview.add_argument("-o", "--output")
    _add_tmdb_options(profile_preview)
    profile_preview.add_argument("--json", dest="json_output", action="store_true")
    profile_preview.set_defaults(func=cmd_profile)

    profile_apply = profile_sub.add_parser("apply", help="Appliquer un profil et remuxer.")
    _add_common_options(profile_apply)
    profile_apply.add_argument("--profile", required=True)
    profile_apply.add_argument("-i", "--input", action="append", required=True)
    profile_apply.add_argument("-o", "--output", required=True)
    _add_tmdb_options(profile_apply)
    profile_apply.add_argument("--force", action="store_true")
    profile_apply.add_argument("--dry-run", action="store_true")
    profile_apply.set_defaults(func=cmd_profile)

    profile_batch = profile_sub.add_parser("batch", help="Appliquer un profil à un lot de fichiers.")
    _add_common_options(profile_batch)
    profile_batch.add_argument("--profile", required=True)
    profile_batch.add_argument("-i", "--input", action="append")
    profile_batch.add_argument("--input-dir", action="append")
    profile_batch.add_argument("--recursive", action="store_true")
    profile_batch.add_argument("--include", action="append")
    profile_batch.add_argument("--exclude", action="append")
    profile_batch.add_argument("--output-dir", required=True)
    _add_tmdb_options(profile_batch)
    profile_batch.add_argument("--dry-run", action="store_true")
    profile_batch.add_argument("--force", action="store_true")
    profile_batch.add_argument("--continue-on-error", action="store_true")
    profile_batch.add_argument("--summary")
    profile_batch.set_defaults(func=cmd_profile)
    return parser
