"""Prometheus metrics for a running Rameness manager (GET /metrics on its HTTP API).

Counters and histograms are kept in this process (decisions, turns, model tokens); the team's state (agents, open
escalations, chats) is read from the store when scraped. No dependency: the text exposition format is written here.
"""

from __future__ import annotations

import re
import threading

BUCKETS = (0.05, 0.1, 0.25, 0.5, 1, 2, 5, 10, 30, 60, 120, 300)

HELP = {
    "rameness_jev_decisions_total": ("counter", "Decisions made by the decision model (JEV), by backend and kind."),
    "rameness_jev_decision_seconds": ("histogram", "Time a decision took, by decision backend."),
    "rameness_turns_total": ("counter", "Messages handled through the chat-turn API, by how the manager routed them."),
    "rameness_turn_first_text_seconds": ("histogram", "Time from a message to the first words of its reply."),
    "rameness_turn_seconds": ("histogram", "Time from a message to the end of its reply."),
    "rameness_model_tokens_total": ("counter", "Tokens used by the manager's own model calls, by direction."),
    "rameness_model_requests_total": ("counter", "Model requests made by the manager itself."),
}

_lock = threading.Lock()
_counters: dict[tuple, float] = {}
_hists: dict[tuple, list] = {}           # key -> [bucket counts..., sum, count]


def _key(name: str, labels: dict) -> tuple:
    return (name, tuple(sorted((k, str(v)) for k, v in labels.items() if v is not None)))


def inc(name: str, value: float = 1, **labels) -> None:
    with _lock:
        k = _key(name, labels)
        _counters[k] = _counters.get(k, 0) + value


def observe(name: str, seconds: float, **labels) -> None:
    with _lock:
        k = _key(name, labels)
        h = _hists.setdefault(k, [0] * len(BUCKETS) + [0.0, 0])
        for i, b in enumerate(BUCKETS):
            if seconds <= b:
                h[i] += 1
        h[-2] += seconds
        h[-1] += 1


def _esc(v) -> str:
    return str(v).replace("\\", "\\\\").replace("\n", " ").replace('"', '\\"')


def _labels(pairs) -> str:
    pairs = [(re.sub(r"[^a-zA-Z0-9_]", "_", k), v) for k, v in pairs]
    return "{" + ",".join(f'{k}="{_esc(v)}"' for k, v in pairs) + "}" if pairs else ""


def render(gauges: list[tuple[str, str, list[tuple[dict, float]]]] = ()) -> str:
    """The exposition text: this process's counters and histograms, plus ``gauges`` given as
    (name, help, [(labels, value), ...])."""
    out: list[str] = []
    with _lock:
        counters, hists = dict(_counters), {k: list(v) for k, v in _hists.items()}
    for name, (kind, text) in HELP.items():
        rows = [(k, v) for k, v in (counters.items() if kind == "counter" else hists.items()) if k[0] == name]
        out += [f"# HELP {name} {text}", f"# TYPE {name} {kind}"]
        for (_, pairs), v in sorted(rows):
            if kind == "counter":
                out.append(f"{name}{_labels(pairs)} {v}")
                continue
            for b, n in zip(BUCKETS, v):
                out.append(f"{name}_bucket{_labels(list(pairs) + [('le', b)])} {n}")
            out.append(f"{name}_bucket{_labels(list(pairs) + [('le', '+Inf')])} {v[-1]}")
            out.append(f"{name}_sum{_labels(pairs)} {round(v[-2], 6)}")
            out.append(f"{name}_count{_labels(pairs)} {v[-1]}")
    for name, text, samples in gauges:
        out += [f"# HELP {name} {text}", f"# TYPE {name} gauge"]
        out += [f"{name}{_labels(sorted(lab.items()))} {val}" for lab, val in samples]
    return "\n".join(out) + "\n"


def reset() -> None:
    """For tests."""
    with _lock:
        _counters.clear()
        _hists.clear()
