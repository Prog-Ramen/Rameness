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
    "sampling": "auto",               # auto: the model family's recommended sampling (rameness.llm.SAMPLING_PROFILES)
                                      # | server: the server's defaults | {"temperature": ..., ...}: explicit
    "hooks": {                        # candidate SOPs Rameness runs itself at lifecycle events (rameness/hooks.py);
        "on_start": [],               # JEV picks the ones that fit each task
        "after_output": ["code.syntax_check"],    # e.g. syntax errors come back with the write that caused them
        "before_finish": [],
        "on_end": [],
    },
    "model_policies": {},             # per model family harness settings, {"<name part>": {<config keys>}}, merged
                                      # over everything else when the model's name contains the key (first match)
    "delegation": False,              # the delegate tool: JEV-gated sub-agents with a fresh, small context
    "delegate_max_turns": 40,
    "state_ledger": True,             # the note tool: facts, decisions, issues, artifacts kept across compaction
    # experimental, off: an A/B on occamy (2 runs per arm) showed no measurable effect
    "vision_describe_first": False,   # an image in a tool result asks: describe what it shows, then compare
    "vision_final_look": False,       # the finish review asks a seeing model to look at the final result
    "draft_then_revise": True,        # design, write whole files as a first draft, then fix every defect a check shows at once
    "batch_workflow": True,           # clear the whole todo list together; automated test plan as one reusable check; status lines
    "task_scope": {                   # JEV decides: specific (build every requirement first, then finish) or
        "mode": "jev",                # open_ended (tested core, then feature cycles). jev | specific | open_ended | off
        "feature_cycles": 3,          # open-ended tasks: improvement cycles after the core is built and reviewed
        "branches": True,             # each cycle on git branch rameness/cycle-N in a sibling folder; merged into
                                      # the project only once it passes its checks, so the project stays verified
    },
    "group_review": {"min_files": 2},  # after a burst of 2+ whole-file writes, review them together (0 = off)
    "group_debug": {                  # after N turns of investigating without changing the work, ask for one probe
        "after_turns": 6,             # that tests several causes at once; again every `repeat` turns (0 = off)
        "repeat": 12,
    },
    "review_pass": True,              # with draft_then_revise: review the draft, then check parts before the whole
    "scope_discipline": False,        # experimental: "do what was asked, nothing more" instead of "improve if time allows"
    "review_scaled": False,           # experimental: short runs (< review_full_steps) get a brief finish review
    "proportional_checks": True,      # scale verification with the size of the task (A/B: same scores, 37% faster)
    "edit_fuzzy": False,              # experimental: apply edits whose old text differs only in whitespace
    "dedup_reads": True,              # a re-read of an unchanged file still in context returns a short stub
    "line_anchors": True,             # read_file shows "N:hh|" anchors and edit_lines edits a line range by them
    "edit_guard": True,               # undo an edit_file that breaks a file the after_output checks passed before
    "hook_selection": "jev",          # jev: JEV chooses per task | all: run every candidate
    "hook_threshold": 0.25,           # JEV probability a candidate needs to be chosen
    "hook_args": {                    # extra arguments per hook SOP, e.g.
        "code.syntax_check": {"commands": {}},    # {".kt": "kotlinc -script {f}", ".rs": null}
    },
    "todo_reminder_turns": 30,        # remind the agent of its open todos after this many turns without todo_write
    "thinking": {                     # per-turn thinking budget (rameness/thinking.py)
        "mode": "jev",                # jev: JEV picks a level each turn | fixed: always maximum
        # reasoning tokens per level (for servers that take a budget; Claude gets an effort level instead)
        "minimal": 512, "brief": 2048, "normal": 4096, "deep": 8192, "maximum": 16384,
    },
    "finish_checks": 3,               # times the agent is sent back when it stops with open todos, untested edits,
                                      # or before its one self-review against the task
    "request_timeout": 3600,          # seconds per model reply (OpenAI-compatible servers, non-streaming): a local
                                      # model writing a large file can take well over 10 minutes for one reply
    "progress_review": {              # experimental: on, but not yet shown to help (it rarely acts)
        "every": 10,                  # JEV checks the work is heading the right way every N agent turns; 0 = off
        "min_confidence": 0.5,        # intervene (drifting / stalled / finish) only on a verdict this probable
    },
    "jev": {
        "backend": "auto",            # auto | laya-local | laya | kev | typesafe | lexical. auto: the first local model
                                      # in local_order whose server answers, else typesafe if
                                      # TYPESAFE_API_KEY is set, else lexical (offline, degraded)
        "local_order": ["kev", "laya", "laya-local"],   # servers first (one shared copy), then in-process
        "serve": "laya",              # the local decision server `rameness up` starts: laya | kev | None
        "serve_device": None,         # cuda | cpu | None (GPU if visible). cpu if the GPU is busy with a big model
        "kev_checkpoint": "jaredpalmer/kev-4b",
        "option_text": "keywords",    # what the model reads per option: keywords | sentences (Option.desc)
        "max_state_chars": None,      # state budget per decision; default follows the model's window
                                      # (Laya base ~1200 chars, Laya 1024-token checkpoints ~3000, Kev/TypeSafe 8000)
        "laya_url": None,             # default http://127.0.0.1:8000/v1/systemone (`rameness jev up laya`)
        "laya_model": None,           # checkpoint, default "typed-decisions" (measured best); "english", "multilingual"
        "laya_max_len": None,         # laya-local only: window in tokens; None = the checkpoint's trained size
                                      # (encoder allows 8192, but wider measured worse: rameness jev bench)
        "laya_head_max_len": None,    # laya-local only: tokens for the question + options; None = trained size
        "laya_device": None,          # laya-local only: cuda | cpu | None (auto)
        "kev_url": None,              # default http://127.0.0.1:8008/v1/systemone (`python -m kev.serve`)
        "kev_model": None,
        "typesafe_url": None,         # TypeSafe's hosted Jev (subscription): https://api.typesafe.ai/v1/systemone
        "typesafe_model": None,       # default "jev-latest"
        "high_effort_above": 0.10,    # use high reasoning effort when JEV gives "high" more than this probability
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
        "budget_tokens": None,         # the model's context window; None = ask the provider (llama-server /props,
                                      # 200k for Anthropic), else 200k
        "compact_at": 0.93,           # compact when the context passes this share of the window (as Claude Code)
        "reply_tokens": 32000,        # output tokens asked per call; compaction keeps this much (+2%) of the window free
        "offload_chars": 30000,       # tool output above this is stored and shown as a preview + ref
        "read_chars": 100000,         # read_file output is shown whole up to this (the agent asked for the file)
        "keep_recent_turns": 4,
    },
    "learning": {
        "enabled": True,
        "min_repeats": 2,
        "sop_threshold": 0.6,
        "auto_generate": True,
    },
    "registry": {
        "public": "https://github.com/Prog-Ramen/RamenSOPs.git",   # `sop propose` opens PRs here
        "auto_propose": True,             # after successful tasks, propose validated, shareable private SOPs
        "fork": None,                     # push proposals to this fork; default: origin, else a `gh repo fork`
        "remote_index": "https://raw.githubusercontent.com/Prog-Ramen/RamenSOPs/registry/sops/index.json",
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
    apply_model_policy(cfg)
    return cfg


def apply_model_policy(cfg: dict) -> str | None:
    """Merge the settings for the configured model's family (model_policies) over the config: different models
    need different checkpoints, budgets and compaction. Returns the matched family."""
    model = str(cfg.get("model") or "").lower()
    for family, overrides in (cfg.get("model_policies") or {}).items():
        if family.lower() in model and isinstance(overrides, dict):
            _merge(cfg, copy.deepcopy(overrides))
            cfg["_model_policy"] = family
            return family
    return None
