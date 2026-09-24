"""Model slots: every model the fleet can put an agent on, and how much capacity it has.

Sources:

* configured endpoints in fleet.json ``slots``::

      {"id": "opus", "kind": "anthropic", "model": "claude-opus-5", "capacity": 4, "traits": "strongest reasoning"}
      {"id": "gpu-llama", "kind": "llama-server", "url": "http://10.0.0.7:8080", "traits": "local private"}
      {"id": "ds", "kind": "openai", "url": "https://api.deepseek.com", "model": "deepseek-chat",
       "api_key_env": "DEEPSEEK_API_KEY"}

* discovery: OpenAI-compatible servers on local ports (llama-server 8080-8082,
  ollama 11434, vLLM 8000, LM Studio 1234, plus ``scan_ports``). llama-server
  reports its real parallel slots via ``/props`` (``total_slots``) and ``/slots``.
* CLI agents found on PATH (claude, codex, pi, opencode, aider, gemini, goose) -
  these are *runtimes* that bring their own model, so they appear as slots too.

A slot's ``text`` is what the JEV scores when assigning work; ``capacity`` and
live ``busy`` counts are hard constraints the JEV never overrides.
"""

from __future__ import annotations

import json
import os
import shutil
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

DEFAULT_PORTS = {8080: "llama-server", 8081: "llama-server", 8082: "llama-server", 11434: "ollama",
                 8000: "vllm", 1234: "lmstudio"}

CLI_AGENTS = {
    # name: (argv template for a headless one-shot run, traits)
    "claude": (["claude", "-p", "{task}", "--permission-mode", "acceptEdits"], "claude code agent strong coding reasoning tools"),
    "codex": (["codex", "exec", "--full-auto", "{task}"], "openai codex agent coding"),
    "pi": (["pi", "-p", "{task}"], "pi coding agent"),
    "opencode": (["opencode", "run", "{task}"], "opencode coding agent"),
    "aider": (["aider", "--yes-always", "--message", "{task}"], "aider pair programming edits git"),
    "gemini": (["gemini", "-p", "{task}"], "gemini cli agent large context"),
    "goose": (["goose", "run", "-t", "{task}"], "goose agent"),
}

API_TRAITS = {
    "claude-opus": "strongest reasoning planning architecture hard debugging large context 1m",
    "claude-fable": "frontier most capable long horizon",
    "claude-sonnet": "strong coding fast balanced",
    "claude-haiku": "fast cheap simple quick",
    "deepseek": "cheap strong coding reasoning",
    "gpt": "strong general coding",
}


def _get(url: str, timeout: float = 1.5):
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return json.loads(r.read())


@dataclass
class Slot:
    id: str
    kind: str                                   # anthropic | openai | llama-server | ollama | vllm | lmstudio | cli
    model: str = ""
    url: str = ""
    capacity: int = 1
    traits: str = ""
    api_key_env: str | None = None
    local: bool = False
    argv: list[str] = field(default_factory=list)       # cli agents
    available: bool = True
    models: list[str] = field(default_factory=list)
    status: str = ""
    busy_server: int = 0                        # busy slots reported by the server itself (llama-server)
    tool_mode: str = "auto"                     # native | prompt | auto  (prompted tool calling for weak servers)

    @property
    def is_cli(self) -> bool:
        return self.kind == "cli"

    @property
    def text(self) -> str:
        loc = "local private offline free on-prem" if self.local else "remote api hosted"
        return f"{self.id} {self.kind} {self.model} {self.traits} {loc}"

    @property
    def base_url(self) -> str:
        u = self.url.rstrip("/")
        return u if u.endswith("/v1") or self.kind in ("anthropic", "cli") else u + "/v1"

    def summary(self, busy: int = 0) -> dict:
        return {"id": self.id, "kind": self.kind, "model": self.model, "url": self.url, "capacity": self.capacity,
                "busy": max(busy, self.busy_server), "local": self.local, "available": self.available,
                "traits": self.traits, "models": self.models[:20], "status": self.status}


def probe_server(slot: Slot) -> Slot:
    base = slot.url.rstrip("/").removesuffix("/v1")
    try:
        data = _get(base + "/v1/models")
        slot.models = [m.get("id") or m.get("name") for m in data.get("data", data.get("models", []))]
        slot.available = True
        slot.status = "up"
    except Exception as e:
        slot.available = False
        slot.status = f"down ({type(e).__name__})"
        return slot
    if not slot.model and slot.models:
        slot.model = slot.models[0]
    if slot.kind == "llama-server":
        try:
            props = _get(base + "/props")
            slot.capacity = int(props.get("total_slots", slot.capacity))
            ctx = props.get("default_generation_settings", {}).get("n_ctx")
            if ctx:
                slot.traits += f" ctx {ctx}"
        except Exception:
            pass
        try:
            slot.busy_server = sum(1 for s in _get(base + "/slots") if s.get("is_processing"))
        except Exception:
            pass
    return slot


def discover(cfg: dict) -> list[Slot]:
    slots: list[Slot] = []
    for d in cfg.get("slots", []):
        known = set(Slot.__dataclass_fields__)
        s = Slot(**{k: v for k, v in d.items() if k in known})
        if s.kind not in ("anthropic", "cli", "openai") or s.url.startswith(("http://localhost", "http://127.")):
            s.local = s.local or "localhost" in s.url or "127.0.0.1" in s.url
        slots.append(s)
    if cfg.get("discover") is False:
        return slots

    # anthropic from credentials
    if os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"):
        for model, cap in (("claude-opus-5", 4), ("claude-sonnet-5", 6), ("claude-haiku-4-5", 8)):
            if not any(s.model == model for s in slots):
                tr = next((v for k, v in API_TRAITS.items() if model.startswith(k)), "")
                slots.append(Slot(model.replace("claude-", ""), "anthropic", model, capacity=cap, traits=tr))
    if os.environ.get("DEEPSEEK_API_KEY") and not any(s.kind == "openai" and "deepseek" in s.url for s in slots):
        slots.append(Slot("deepseek", "openai", "deepseek-chat", "https://api.deepseek.com", 6,
                          API_TRAITS["deepseek"], "DEEPSEEK_API_KEY"))

    # local servers on ports
    known_urls = {s.url.rstrip("/").removesuffix("/v1") for s in slots}
    ports = dict(DEFAULT_PORTS)
    for p in cfg.get("scan_ports", []):
        ports.setdefault(int(p), "openai")
    cands = [Slot(f"{kind}-{port}", kind, url=f"http://127.0.0.1:{port}", local=True,
                  traits="local private offline free")
             for port, kind in ports.items() if f"http://127.0.0.1:{port}" not in known_urls
             and f"http://localhost:{port}" not in known_urls]
    to_probe = [s for s in slots if s.url and s.kind not in ("anthropic", "replay")] + cands
    with ThreadPoolExecutor(8) as ex:
        list(ex.map(probe_server, to_probe))
    slots += [c for c in cands if c.available]
    # one slot per model on multi-model servers (ollama / lmstudio)
    expanded = []
    for s in slots:
        if s.kind in ("ollama", "lmstudio") and len(s.models) > 1 and s.id.endswith(tuple(str(p) for p in ports)):
            for m in s.models:
                expanded.append(Slot(f"{s.id}/{m}", s.kind, m, s.url, max(1, s.capacity), s.traits, local=True,
                                     models=[m], status=s.status, available=s.available))
        else:
            expanded.append(s)
    slots = expanded

    # CLI agents on PATH
    for name, (argv, traits) in CLI_AGENTS.items():
        if shutil.which(name) and not any(s.id == f"cli:{name}" for s in slots):
            slots.append(Slot(f"cli:{name}", "cli", name, capacity=cfg.get("cli_capacity", 2), traits=traits,
                              argv=argv, status="installed"))
    return slots


def provider_for(slot: Slot):
    """A rameness model provider speaking to this slot."""
    from ..llm import AnthropicProvider, OpenAICompatProvider
    if slot.kind == "anthropic":
        return AnthropicProvider(slot.model, slot.model)
    if slot.kind == "replay":                    # scripted responses (tests, demos); url = script path
        from ..llm import ReplayProvider
        return ReplayProvider(slot.url)
    if slot.is_cli:
        raise ValueError(f"{slot.id} is a CLI agent, not a model endpoint")
    key = os.environ.get(slot.api_key_env) if slot.api_key_env else None
    return OpenAICompatProvider(slot.model, slot.model, slot.base_url, key, tool_mode=slot.tool_mode)
