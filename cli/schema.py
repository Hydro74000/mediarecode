"""JSON schema for the public CLI job contracts."""

from __future__ import annotations

from typing import Any

from cli.constants import FLAG_NAMES, TRACK_TYPES
from core.version import APP_NAME, APP_SCHEMA_BASE_URL


def _flag_properties() -> dict[str, dict[str, str]]:
    return {name: {"type": "boolean"} for name in FLAG_NAMES}


def _selector_schema(flag_properties: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": True,
        "properties": {
            "source": {"type": "integer"},
            "source_index": {"type": "integer"},
            "type": {"enum": sorted(TRACK_TYPES)},
            "track_type": {"enum": sorted(TRACK_TYPES)},
            "position": {"type": "integer"},
            "type_index": {"type": "integer"},
            "id": {"type": "integer"},
            "mkv_tid": {"type": "integer"},
            "stream": {"type": "integer"},
            "codec": {"type": "string"},
            "codecs": {"type": "array", "items": {"type": "string"}},
            "language": {"type": "string"},
            "languages": {"type": "array", "items": {"type": "string"}},
            "channels": {"type": "string"},
            "atmos": {"type": "boolean"},
            "audio_object": {"type": "string"},
            "title": {"type": "string"},
            "title_contains": {"type": "string"},
            "display_contains": {"type": "string"},
            "entry_id": {"type": "string"},
            "resolution": {
                "oneOf": [
                    {"type": "string"},
                    {
                        "type": "object",
                        "additionalProperties": True,
                        "properties": {
                            "width": {"type": "integer"},
                            "height": {"type": "integer"},
                            "bucket": {"type": "string"},
                        },
                    },
                ]
            },
            "video_flags_hex": {"type": "string"},
            "video_flags": {"oneOf": [{"type": "string"}, {"type": "integer"}]},
            "flags": {"type": "object", "additionalProperties": False, "properties": flag_properties},
        },
    }


def _track_edit_schema(flag_properties: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": True,
        "properties": {
            "selector": {"$ref": "#/$defs/selector"},
            "source": {"type": "integer"},
            "source_index": {"type": "integer"},
            "id": {"type": "integer"},
            "mkv_tid": {"type": "integer"},
            "stream": {"type": "integer"},
            "enabled": {"type": "boolean"},
            "language": {"type": "string"},
            "title": {"type": "string"},
            "flags": {"type": "object", "additionalProperties": False, "properties": flag_properties},
            "time_shift_ms": {"type": "integer"},
            "sync_rewrite_mode": {"type": "string", "enum": ["", "offset"]},
        },
    }


def _variables_schema() -> dict[str, Any]:
    alias_section = {"type": "object", "additionalProperties": {"type": "string"}}
    return {
        "type": "object",
        "additionalProperties": True,
        "properties": {
            "aliases": {
                "type": "object",
                "additionalProperties": alias_section,
                "properties": {
                    "*": alias_section,
                },
            },
            "codec_names": {
                "type": "object",
                "additionalProperties": {"type": "string"},
            },
        },
    }


def build_cli_json_schema() -> dict[str, Any]:
    flag_properties = _flag_properties()
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": f"{APP_SCHEMA_BASE_URL}/cli-job-v1.json",
        "title": f"{APP_NAME} CLI job v1",
        "type": "object",
        "additionalProperties": True,
        "required": ["version"],
        "properties": {
            "version": {"const": 1},
            "sources": {
                "oneOf": [
                    {"type": "string"},
                    {"type": "array", "minItems": 1, "items": {"$ref": "#/$defs/source"}},
                ]
            },
            "input": {
                "oneOf": [
                    {"type": "string"},
                    {"type": "array", "items": {"type": "string"}},
                ]
            },
            "output": {"type": "string"},
            "output_template": {"type": "string"},
            "output_all": {"type": "boolean"},
            "work_dir": {"type": "string"},
            "file_title": {"type": "string"},
            "variables": _variables_schema(),
            "tracks": {"type": "array", "items": {"$ref": "#/$defs/track_edit"}},
            "track_order": {"type": "array", "items": {"$ref": "#/$defs/track_order_item"}},
            "audio_variants": {"type": "array", "items": {"$ref": "#/$defs/audio_variant"}},
            "chapters": {"oneOf": [{"const": False}, {"$ref": "#/$defs/chapters"}]},
            "tmdb": {"oneOf": [{"type": "boolean"}, {"$ref": "#/$defs/tmdb"}]},
            "extra_attachments": {"type": "array", "items": {"type": "string"}},
            "tag_overrides": {"type": "object", "additionalProperties": {"type": "string"}},
        },
        "$defs": {
            "source": {
                "oneOf": [
                    {"type": "string"},
                    {
                        "type": "object",
                        "additionalProperties": True,
                        "required": ["path"],
                        "properties": {
                            "path": {"type": "string"},
                            "playlist": {"type": "integer"},
                            "attachments": {
                                "oneOf": [
                                    {"type": "boolean"},
                                    {"enum": ["all", "none"]},
                                    {"type": "array", "items": {"oneOf": [{"type": "string"}, {"type": "integer"}]}},
                                ]
                            },
                            "copy_tags": {"type": "boolean"},
                        },
                    },
                ]
            },
            "selector": _selector_schema(flag_properties),
            "track_edit": _track_edit_schema(flag_properties),
            "track_order_item": {
                "oneOf": [
                    {
                        "type": "object",
                        "additionalProperties": True,
                        "properties": {
                            "selector": {"$ref": "#/$defs/selector"},
                            "source": {"type": "integer"},
                            "source_index": {"type": "integer"},
                            "id": {"type": "integer"},
                            "mkv_tid": {"type": "integer"},
                            "stream": {"type": "integer"},
                        },
                    },
                    {
                        "type": "array",
                        "minItems": 2,
                        "maxItems": 3,
                        "prefixItems": [{"type": "integer"}, {"type": "integer"}, {"type": "string"}],
                    },
                ]
            },
            "audio_variant": {
                "type": "object",
                "additionalProperties": True,
                "properties": {
                    "selector": {"$ref": "#/$defs/selector"},
                    "source_selector": {"$ref": "#/$defs/selector"},
                    "codec": {"type": "string"},
                    "target_codec": {"type": "string"},
                    "bitrate_kbps": {"type": "integer"},
                    "enabled": {"type": "boolean"},
                    "language": {"type": "string"},
                    "title": {"type": "string"},
                    "flags": {"type": "object", "additionalProperties": False, "properties": flag_properties},
                },
            },
            "chapters": {
                "type": "object",
                "additionalProperties": True,
                "properties": {
                    "source_index": {"type": "integer"},
                    "include_source": {"type": "boolean"},
                    "import": {"type": "string"},
                    "add": {"type": "array", "items": {"$ref": "#/$defs/chapter"}},
                },
            },
            "chapter": {
                "type": "object",
                "additionalProperties": True,
                "properties": {
                    "timestamp": {"oneOf": [{"type": "string"}, {"type": "number"}]},
                    "timecode": {"oneOf": [{"type": "string"}, {"type": "number"}]},
                    "time": {"oneOf": [{"type": "string"}, {"type": "number"}]},
                    "timecode_s": {"oneOf": [{"type": "string"}, {"type": "number"}]},
                    "chaptername": {"type": "string"},
                    "name": {"type": "string"},
                    "title": {"type": "string"},
                },
            },
            "tmdb": {
                "type": "object",
                "additionalProperties": True,
                "properties": {
                    "enabled": {"type": "boolean"},
                    "kind": {"enum": ["all", "movie", "tv"]},
                    "query": {"type": "string"},
                    "title": {"type": "string"},
                    "year": {"type": "string"},
                    "season": {"type": "string"},
                    "episode": {"type": "string"},
                    "language": {"type": "string"},
                    "api_key": {"type": "string"},
                    "bearer_token": {"type": "string"},
                    "cover": {"type": "boolean"},
                    "auto_detect_episode": {"type": "boolean"},
                    "auto_metadata": {"type": "boolean"},
                    "id": {"type": "integer"},
                    "tmdb_id": {"type": "integer"},
                },
            },
        },
    }


def build_cli_json_schema_bundle() -> dict[str, Any]:
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "title": "Muxiveo CLI schemas",
        "oneOf": [
            build_cli_json_schema(),
            build_exact_job_schema_v1(),
            build_decision_profile_schema_v1(),
        ],
    }


def build_exact_job_schema_v1() -> dict[str, Any]:
    """Schema for exact remux jobs used as strict reusable templates."""
    base = build_cli_json_schema()
    schema = dict(base)
    schema["$id"] = f"{APP_SCHEMA_BASE_URL}/exact-job-v1.json"
    schema["title"] = f"{APP_NAME} exact job v1"
    schema["properties"] = dict(base["properties"])
    schema["properties"]["version"] = {"const": 1}
    schema["properties"]["kind"] = {"const": "exact-job"}
    schema["required"] = ["version", "kind"]
    return schema


def build_decision_profile_schema_v1() -> dict[str, Any]:
    """Schema for decision-profile v1 low-code automapping profiles."""
    flag_properties = _flag_properties()
    condition_schema: dict[str, Any] = {
        "type": "object",
        "additionalProperties": True,
        "properties": {
            "all": {"type": "array", "items": {"$ref": "#/$defs/condition"}},
            "any": {"type": "array", "items": {"$ref": "#/$defs/condition"}},
            "not": {"$ref": "#/$defs/condition"},
            "field": {"type": "string"},
            "op": {"type": "string"},
            "value": {},
            "expr": {"type": "string"},
            "required": {"type": "boolean"},
            "weight": {"type": "integer"},
        },
    }
    action_schema: dict[str, Any] = {
        "type": "object",
        "additionalProperties": True,
        "required": ["type"],
        "properties": {
            "type": {
                "enum": [
                    "set_enabled",
                    "set_language",
                    "set_title",
                    "set_time_shift_ms",
                    "set_flags",
                    "add_track_tags",
                    "remove_track_tags",
                    "set_order_priority",
                    "create_audio_variant",
                ]
            },
            "value": {},
            "mode": {"enum": ["priority", "override", "add"]},
            "write_mode": {"enum": ["priority", "override", "add"]},
            "pattern": {"type": "string"},
            "codec": {"type": "string"},
            "target_codec": {"type": "string"},
            "bitrate_kbps": {"type": "integer"},
            "language": {"type": "string"},
            "title": {"type": "string"},
            "title_pattern": {"type": "string"},
            "enabled": {"type": "boolean"},
            "flags": {"type": "object", "additionalProperties": False, "properties": flag_properties},
        },
    }
    rule_schema: dict[str, Any] = {
        "type": "object",
        "additionalProperties": True,
        "required": ["id", "match", "actions"],
        "properties": {
            "id": {"type": "string"},
            "label": {"type": "string"},
            "group_id": {"type": "string"},
            "tags": {"type": "array", "items": {"type": "string"}},
            "enabled": {"type": "boolean"},
            "priority": {"type": "integer"},
            "mode": {"enum": ["priority", "override", "add"]},
            "write_mode": {"enum": ["priority", "override", "add"]},
            "scope": {"enum": ["all", "first", "best"]},
            "tie_break": {"type": "string"},
            "match": {"$ref": "#/$defs/condition"},
            "actions": {"type": "array", "items": {"$ref": "#/$defs/action"}},
        },
    }
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": f"{APP_SCHEMA_BASE_URL}/decision-profile-v1.json",
        "title": f"{APP_NAME} decision profile v1",
        "type": "object",
        "additionalProperties": True,
        "required": ["version", "kind", "name"],
        "properties": {
            "version": {"const": 1},
            "kind": {"const": "decision-profile"},
            "name": {"type": "string"},
            "description": {"type": "string"},
            "tags": {"type": "array", "items": {"type": "string"}},
            "variables": {
                **_variables_schema(),
            },
            "groups": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": True,
                    "properties": {
                        "id": {"type": "string"},
                        "label": {"type": "string"},
                        "enabled": {"type": "boolean"},
                        "priority": {"type": "integer"},
                    },
                },
            },
            "selection_policy": {
                "type": "object",
                "additionalProperties": True,
                "properties": {
                    "disable_unmatched_types": {
                        "type": "array",
                        "items": {"enum": sorted(TRACK_TYPES)},
                    }
                },
            },
            "rules": {"type": "array", "items": {"$ref": "#/$defs/rule"}},
        },
        "$defs": {
            "condition": condition_schema,
            "action": action_schema,
            "rule": rule_schema,
        },
    }
