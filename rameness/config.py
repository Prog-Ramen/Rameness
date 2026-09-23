"""Layered configuration: defaults <- ~/.rameness/config.json <- ./.rameness/config.json."""

from __future__ import annotations

import copy
import json
import os
from pathlib import Path

DEFAULTS: dict = {
    "provider": "anthropic",          # anthropic | openai | deepseek | ollama | fake
    "model": None,                    # main (reasoning) model; provider preset if None
    "fast_model": None,               # cheap model for arg extraction / LLM-JEV / SOP generation
    "base_url": None,
    "api_key_env": None,
    "anthropic_fallbacks": True,      # server-side refusal fallbacks (Opus 5 / Fable)
    "max_turns": 40,
    "jev": {
        "backend": "cascade",         # lexical | llm | http | cascade | cascade-http
        "url": None,                  # for backend=http: an open-source JEV served over HTTP
        "uncertain_band": [0.3, 0.7],
        "llm_timeout": 20,                # seconds; slower escalations fall back to the lexical pass
        "activate_threshold": 0.55,
        "explore_threshold": 0.3,
        "beam": 4,
        "max_sops": 8,
    },
    "permissions": {
        "mode": "ask",                # ask | auto | readonly
        "sop_allow": ["fs:read", "compute"],
    },
    "context": {
        "budget_tokens": 120000,
        "offload_chars": 6000,
        "keep_recent_turns": 4,
    },
    "learning": {
        "enabled": True,
        "min_repeats": 2,
        "sop_threshold": 0.6,
        "auto_generate": True,
    },
    "registry": {
        "public": "https://github.com/Prog-Ramen/RamenSOPs.git",   # where reviewed SOPs are released
        "staging": None,                  # a PRIVATE repo where proposals are reviewed (e.g. Prog-Ramen/RamenSOPs-staging)
        "release": "pr",                  # pr | direct
        "staging_assume_private": False,  # for non-GitHub hosts whose visibility can't be checked
        "remote_index": "https://raw.githubusercontent.com/Prog-Ramen/RamenSOPs/main/sops/index.json",
        "auto_pull": "gated",             # gated (JEV + comfort gate + permission guard) | ask (always ask) | off
        "coverage_margin": 0.15,          # consult the remote registry when the best local SOP scores below
                                          # activate_threshold + this margin
    },
}

PROVIDER_PRESETS = {
    "anthropic": {"model": "claude-opus-5", "fast_model": "claude-haiku-4-5", "api_key_env": "ANTHROPIC_API_KEY"},
    "openai": {"model": "gpt-5", "fast_model": "gpt-5-mini", "api_key_env": "OPENAI_API_KEY"},
    "deepseek": {"model": "deepseek-chat", "fast_model": "deepseek-chat",
                 "base_url": "https://api.deepseek.com", "api_key_env": "DEEPSEEK_API_KEY"},
    "ollama": {"model": "qwen3:14b", "fast_model": "qwen3:4b",
               "base_url": "http://localhost:11434/v1", "api_key_env": None},
    "llama-server": {"model": "local", "fast_model": "local",
                     "base_url": "http://localhost:8080/v1", "api_key_env": None},
    "fake": {"model": "fake", "fast_model": "fake"},
}


def _merge(base: dict, over: dict) -> dict:
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            _merge(base[k], v)
        else:
            base[k] = v
    return base


def user_home() -> Path:
    return Path(os.environ.get("RAMENESS_HOME", Path.home() / ".rameness"))


def project_dir(cwd: Path | None = None) -> Path:
    return (cwd or Path.cwd()) / ".rameness"


def load(cwd: Path | None = None, overrides: dict | None = None) -> dict:
    cfg = copy.deepcopy(DEFAULTS)
    for p in (user_home() / "config.json", project_dir(cwd) / "config.json"):
        if p.exists():
            _merge(cfg, json.loads(p.read_text()))
    if overrides:
        _merge(cfg, {k: v for k, v in overrides.items() if v is not None})
    preset = PROVIDER_PRESETS.get(cfg["provider"], {})
    for k, v in preset.items():
        if cfg.get(k) is None:
            cfg[k] = v
    cfg["_cwd"] = str(cwd or Path.cwd())
    return cfg
