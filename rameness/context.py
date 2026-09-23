"""Context management.

Three tiers:

* active      - what the model sees this turn
* retrievable - large tool outputs and evicted observations, stored as
                artifacts under ``.rameness/artifacts`` and replaced in the
                transcript by a short preview + ``ref`` the model can ``recall``
* pinned      - the task statement, org context and the selected SOP
                interfaces; never evicted

Compaction runs only at checkpoints when the estimate crosses the budget. The
JEV ranks older observations by relevance to the current goal; the least
relevant are replaced by stubs. Nothing is deleted - every stub carries its ref.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

from .jev import Jev, Option


def estimate_tokens(messages: list[dict], system: str = "") -> int:
    n = len(system)
    for m in messages:
        n += len(str(m.get("content") or ""))
        for c in m.get("tool_calls", []) or []:
            n += len(json.dumps(c.input))
    return n // 4


class ArtifactStore:
    def __init__(self, root: Path):
        self.root = root

    def put(self, text: str, label: str = "") -> str:
        ref = "art_" + hashlib.sha1(text.encode("utf-8", "replace")).hexdigest()[:10]
        self.root.mkdir(parents=True, exist_ok=True)
        p = self.root / f"{ref}.txt"
        if not p.exists():
            p.write_text(text)
            (self.root / f"{ref}.json").write_text(json.dumps({"label": label, "chars": len(text)}))
        return ref

    def get(self, ref: str) -> str:
        if not re.fullmatch(r"art_[0-9a-f]{10}", ref):
            raise KeyError(f"bad ref {ref!r}")
        p = self.root / f"{ref}.txt"
        if not p.exists():
            raise KeyError(f"unknown ref {ref!r}")
        return p.read_text()

    def recall(self, ref: str, start: int = 1, end: int | None = None, grep: str | None = None) -> str:
        lines = self.get(ref).splitlines()
        if grep:
            rx = re.compile(grep, re.I)
            hits = [f"{i}: {l}" for i, l in enumerate(lines, 1) if rx.search(l)]
            return "\n".join(hits[:200]) or "(no matches)"
        end = end or min(len(lines), start + 399)
        return "\n".join(f"{i}: {l}" for i, l in enumerate(lines[start - 1:end], start))


class ContextManager:
    def __init__(self, store: ArtifactStore, jev: Jev, budget_tokens: int = 120000,
                 offload_chars: int = 6000, keep_recent_turns: int = 4):
        self.store = store
        self.jev = jev
        self.budget = budget_tokens
        self.offload_chars = offload_chars
        self.keep_recent = keep_recent_turns
        self.evicted = 0

    def ingest(self, tool: str, text: str) -> str:
        """Called on every tool result before it enters the transcript."""
        if len(text) <= self.offload_chars:
            return text
        ref = self.store.put(text, tool)
        head, tail = text[: self.offload_chars // 2], text[-self.offload_chars // 4:]
        n = text.count("\n") + 1
        return (f"{head}\n... [{len(text)} chars / {n} lines total; full output stored as ref={ref}. "
                f"Use recall(ref, start, end) or recall(ref, grep=...) to read more] ...\n{tail}")

    def needs_compaction(self, system: str, messages: list[dict]) -> bool:
        return estimate_tokens(messages, system) > self.budget * 0.8

    def compact(self, system: str, messages: list[dict], goal: str) -> list[dict]:
        """Evict low-relevance old tool results into the artifact store."""
        tool_idx = [i for i, m in enumerate(messages) if m["role"] == "tool" and not m.get("evicted")]
        # keep the tool results that belong to the last N assistant turns
        asst = [i for i, m in enumerate(messages) if m["role"] == "assistant"]
        cutoff = asst[-self.keep_recent] if len(asst) >= self.keep_recent else 0
        old = [i for i in tool_idx if i < cutoff]
        if not old:
            return messages
        opts = [Option(str(i), f"{messages[i].get('name', '')}: {str(messages[i]['content'])[:400]}") for i in old]
        d = self.jev.activate("Which of these earlier observations are still needed for the current goal?", goal, opts)
        # evict least relevant first until under 60% of budget
        order = sorted(old, key=lambda i: d.probs[str(i)])
        out = [dict(m) for m in messages]
        for i in order:
            if estimate_tokens(out, system) < self.budget * 0.6:
                break
            content = str(out[i]["content"])
            ref = self.store.put(content, out[i].get("name", "tool"))
            first = content.strip().splitlines()[0][:160] if content.strip() else ""
            out[i]["content"] = f"[evicted from context; ref={ref}; relevance={d.probs[str(i)]:.2f}] {first}"
            out[i]["evicted"] = True
            self.evicted += 1
        return out
