"""Typed settings: a JSON Schema for config.json and fleet.json, built from the defaults and checked on load.

Each setting takes its type from its default value. ``NULLABLE`` gives the type of settings whose default is
null, and ``CHOICES`` the allowed values of settings that pick one of several (read from the code where the code
defines them, so they cannot drift). Objects reject unknown keys, so a misspelt setting is an error with a
suggestion instead of being silently ignored. ``rameness config schema`` writes the schema for editors.
"""

from __future__ import annotations

import copy
import difflib
import functools
import json

JSON = {bool: "boolean", int: "integer", float: "number", str: "string", list: "array", dict: "object"}
PY = {"boolean": bool, "integer": int, "number": (int, float), "string": str, "array": list, "object": dict}


class ConfigError(ValueError):
    pass


# ---------------------------------------------------------------------------------------------- the tables

CONFIG_NULLABLE = {
    "model": "string", "fast_model": "string", "base_url": "string", "api_key_env": "string",
    "jev.serve": "string", "jev.serve_device": "string", "jev.option_text": "string", "jev.max_state_chars": "integer",
    "jev.laya_url": "string", "jev.laya_model": "string", "jev.laya_max_len": "integer",
    "jev.laya_head_max_len": "integer", "jev.laya_device": "string", "jev.kev_url": "string", "jev.kev_model": "string",
    "jev.clef_url": "string", "jev.clef_model": "string", "jev.clef_python": "string", "jev.serve_gpu": "integer",
    "jev.typesafe_url": "string", "jev.typesafe_model": "string", "context.budget_tokens": "integer",
    "web.search_url": "string", "sops.save_to": "string", "registry.fork": "string",
}
FLEET_NULLABLE = {"manager_slot": "string", "godmode.max_cycles": "integer"}

# free-form objects: any keys (model_policies' values are partial configs, checked against the whole schema)
CONFIG_FREE = {"hook_args"}
FLEET_FREE = set()


def config_choices() -> dict:
    from .config import PROVIDER_PRESETS
    return {
        "provider": sorted({"anthropic", "openai", "deepseek", "ollama", "fake", *PROVIDER_PRESETS}),
        "task_scope.mode": ["jev", "specific", "open_ended", "off"],
        "hook_selection": ["jev", "all"],
        "thinking.mode": ["jev", "fixed"],
        "jev.backend": ["auto", "laya-local", "laya", "kev", "clef", "typesafe", "lexical"],
        "jev.serve": ["laya", "kev", "clef"],
        "jev.serve_device": ["cuda", "cpu"],
        "jev.laya_device": ["cuda", "cpu"],
        "jev.option_text": ["keywords", "sentences"],
        "permissions.mode": ["ask", "auto", "readonly"],
        "registry.auto_pull": ["gated", "ask", "off"],
    }


def fleet_choices() -> dict:
    from .fleet.sessions import BACKENDS
    return {
        "backend": ["auto", *sorted(BACKENDS)],
        "mode": ["review", "local", "pr"],
        "privacy": ["any", "local-only"],
        "worker_permissions": ["ask", "auto", "readonly"],
        "work_mode": ["team", "direct"],
        "autonomy": ["restrictive", "balanced", "autopilot", "godmode"],
    }


def _dataclass_object(cls, required: tuple[str, ...]) -> dict:
    """An object schema from a dataclass's fields (slots, environments)."""
    import dataclasses
    import typing
    hints = typing.get_type_hints(cls)
    props = {}
    for f in dataclasses.fields(cls):
        t = hints[f.name]
        origin = typing.get_origin(t) or t
        args = [a for a in typing.get_args(t) if a is not type(None)]
        if origin in (list, dict, bool, int, float, str):
            props[f.name] = {"type": JSON[origin]}
        elif args and (typing.get_origin(args[0]) or args[0]) in JSON:   # X | None
            props[f.name] = {"type": [JSON[typing.get_origin(args[0]) or args[0]], "null"]}
    return {"type": "object", "properties": props, "required": list(required), "additionalProperties": False}


def fleet_items() -> dict:
    from .fleet.envs import Environment
    from .fleet.slots import Slot
    slot = _dataclass_object(Slot, ("id", "kind"))
    for runtime in ("available", "models", "status", "busy_server"):     # filled in by probing, not configured
        slot["properties"].pop(runtime, None)
    env = _dataclass_object(Environment, ("id",))
    env["properties"].pop("info", None)
    return {"slots": {"type": "array", "items": slot}, "environments": {"type": "array", "items": env},
            "decision_policy": {"type": "object",
                                "additionalProperties": {"enum": ["auto", "jev", "director"]}}}


# ---------------------------------------------------------------------------------------------- the schema

def _schema_of(value, path: str, nullable: dict, choices: dict, free: set, special: dict) -> dict:
    if path in special:
        return copy.deepcopy(special[path])
    if value is None:
        if path not in nullable:
            raise KeyError(f"no type for the null default {path}")
        s = {"type": [nullable[path], "null"]}
    elif isinstance(value, dict):
        if path in free:
            return {"type": "object"}
        s = {"type": "object", "properties": {k: _schema_of(v, f"{path}.{k}" if path else k, nullable, choices, free,
                                                             special) for k, v in value.items()},
             "additionalProperties": False}
    elif isinstance(value, list):
        item = type(value[0]) if value else str
        s = {"type": "array", "items": {"type": JSON[item]}}
    else:
        s = {"type": JSON[type(value)] if not isinstance(value, float) else "number"}
        if path in nullable:
            s["type"] = [s["type"], "null"]
    if path in choices:
        s["enum"] = list(choices[path]) + ([None] if path in nullable else [])
    return s


@functools.cache
def config_schema() -> dict:
    from .config import DEFAULTS
    special = {"model_policies": {"type": "object", "additionalProperties": {"$ref": "#"}}}
    s = _schema_of(DEFAULTS, "", CONFIG_NULLABLE, config_choices(), CONFIG_FREE, special)
    s["properties"]["$schema"] = {"type": "string"}
    return {"$schema": "https://json-schema.org/draft/2020-12/schema", "title": "rameness config.json", **s}


@functools.cache
def fleet_schema() -> dict:
    from .fleet.manager import FLEET_DEFAULTS
    s = _schema_of(FLEET_DEFAULTS, "", FLEET_NULLABLE, fleet_choices(), FLEET_FREE, fleet_items())
    s["properties"]["$schema"] = {"type": "string"}
    return {"$schema": "https://json-schema.org/draft/2020-12/schema", "title": "rameness fleet.json", **s}


# ---------------------------------------------------------------------------------------------- checking

def _type_name(v) -> str:
    return "null" if v is None else JSON.get(type(v), type(v).__name__)


def _check(value, schema: dict, root: dict, path: str, errors: list[str]) -> None:
    if "$ref" in schema:
        schema = root
    types = schema.get("type")
    if types:
        types = types if isinstance(types, list) else [types]
        ok = any(value is None if t == "null" else
                 isinstance(value, PY[t]) and not (isinstance(value, bool) and t in ("integer", "number"))
                 for t in types)
        if not ok:
            errors.append(f"{path or '(top level)'}: expected {' or '.join(types)}, got {_type_name(value)} {json.dumps(value)[:60]}")
            return
    if "enum" in schema and value not in schema["enum"]:
        errors.append(f"{path}: {json.dumps(value)} is not one of {', '.join(json.dumps(x) for x in schema['enum'])}")
        return
    if isinstance(value, dict):
        props = schema.get("properties", {})
        extra = schema.get("additionalProperties", True)
        for k, v in value.items():
            sub = f"{path}.{k}" if path else k
            if k in props:
                _check(v, props[k], root, sub, errors)
            elif extra is False:
                near = difflib.get_close_matches(k, list(props), n=1)
                errors.append(f"{sub}: unknown setting" + (f" (did you mean {near[0]!r}?)" if near else ""))
            elif isinstance(extra, dict):
                _check(v, extra, root, sub, errors)
        for k in schema.get("required", []):
            if k not in value:
                errors.append(f"{path}: missing {k!r}")
    elif isinstance(value, list) and "items" in schema:
        for i, v in enumerate(value):
            _check(v, schema["items"], root, f"{path}[{i}]", errors)


def problems(data, schema: dict) -> list[str]:
    errors: list[str] = []
    _check(data, schema, schema, "", errors)
    return errors


def check_file(path, data, schema: dict) -> None:
    """Raise ConfigError listing every problem in one config file."""
    errs = problems(data, schema)
    if errs:
        raise ConfigError(f"{path}:\n  " + "\n  ".join(errs))


def read(path, schema: dict) -> dict:
    """A config file, parsed and checked."""
    try:
        data = json.loads(path.read_text())
    except json.JSONDecodeError as e:
        raise ConfigError(f"{path}: not valid JSON ({e})") from None
    check_file(path, data, schema)
    return data
