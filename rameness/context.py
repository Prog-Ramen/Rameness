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
        n += len(str(m.get("content") or "")) + len(m.get("reasoning") or "")
        n += 6000 * len(m.get("images") or [])          # an image is ~1-2k tokens, whatever its file size
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
    def __init__(self, store: ArtifactStore, jev: Jev, budget_tokens: int = 200000,
                 offload_chars: int = 30000, keep_recent_turns: int = 4, read_chars: int = 100000,
                 compact_at: float = 0.93, reply_tokens: int = 32000):
        self.store = store
        self.jev = jev
        self.budget = budget_tokens         # the model's context window
        self.compact_at = compact_at        # compact when the context passes this share of it (Claude Code: ~93%)
        self.offload_chars = offload_chars
        self.read_chars = read_chars        # read_file output: the agent asked for the file, show it whole
        self.keep_recent = keep_recent_turns
        self.evicted = 0
        # Room kept for the next reply: the output each request asks for, plus 2% of the window for estimate error.
        # A server refuses a request whose prompt plus requested output exceeds the window, so compacting only
        # past (window - a smaller reserve) let a run overflow first and recover with a failed request.
        self.reply_tokens = reply_tokens
        self.reserve = min(reply_tokens + int(budget_tokens * 0.02), int(budget_tokens * 0.25))
        self.scale = 1.0                    # server tokens per estimated token (calibrate)

    def ingest(self, tool: str, text: str) -> str:
        """Called on every tool result before it enters the transcript."""
        if len(text) <= (self.read_chars if tool == "read_file" else self.offload_chars):
            return text
        ref = self.store.put(text, tool)
        n = text.count("\n") + 1
        if tool == "read_file":
            head, tail = text[: self.offload_chars // 2], text[-self.offload_chars // 4:]
            hint = ""
        else:
            # Command and search output this big is usually a too-broad command, not something to read through:
            # a short preview, and the way to ask a better question.
            head, tail = text[: self.offload_chars // 4], text[-self.offload_chars // 15:]
            hint = (" This output is too broad to be useful: narrow the command instead - a more specific pattern, "
                    "-l to list matching files, exclude vendored/minified/generated files, or head/tail.")
        return (f"{head}\n... [{len(text)} chars / {n} lines total; full output stored as ref={ref}. "
                f"Use recall(ref, start, end) or recall(ref, grep=...) to read more.{hint}] ...\n{tail}")

    # ------------------------------------------------------------------ size

    def calibrate(self, system: str, sent: list[dict], reported: int) -> None:
        """Learn the server's tokens per estimated token from what it reports (code, tool schemas and the chat
        template make a characters/4 estimate run low, by a third in practice)."""
        est = estimate_tokens(sent, system)
        if reported and est > 2000:
            ratio = min(max(reported / est, 0.5), 3.0)
            self.scale = ratio if self.scale == 1.0 else 0.7 * self.scale + 0.3 * ratio

    def size(self, system: str, messages: list[dict]) -> int:
        return int(estimate_tokens(messages, system) * self.scale)

    def limit(self) -> int:
        """Compact above this: the configured share of the window, but always leaving room for the next reply."""
        return int(min(self.budget * self.compact_at, self.budget - self.reserve))

    def reply_room(self, system: str, messages: list[dict], reported: int = 0) -> int:
        """Output tokens to request: the configured reply size, cut to what the window still holds once the prompt
        is in (the backstop for a window too small for the full reserve). Never below 1024."""
        used = max(self.size(system, messages), reported)
        return max(1024, min(self.reply_tokens, self.budget - used - int(self.budget * 0.02)))

    def needs_compaction(self, system: str, messages: list[dict], reported: int = 0) -> bool:
        """``reported``: the context size the model server last reported (prompt + reply tokens), which is
        exact; the calibrated estimate covers what was added since and servers that report nothing."""
        return max(self.size(system, messages), reported) > self.limit()

    # ------------------------------------------------------------------ compaction

    def compact(self, system: str, messages: list[dict], goal: str, target: int | None = None) -> list[dict]:
        """Shrink the conversation below ``target`` (default: two thirds of the limit), oldest material first and
        the recent turns untouched:
          1. old tool results, least relevant first (JEV ranks them against the goal), into the artifact store
          2. the reasoning of old turns
          3. the bodies of old file writes and edits in the model's own tool calls (the file holds them)
          4. long old messages, shortened
          5. whole old turns, keeping the task (the harness re-injects the plan and notes)"""
        target = target or int(self.limit() * 0.65)
        out = [dict(m) for m in messages]
        small = lambda: self.size(system, out) <= target
        asst = [i for i, m in enumerate(out) if m["role"] == "assistant"]
        cutoff = asst[-self.keep_recent] if len(asst) >= self.keep_recent else 0

        old_tools = [i for i, m in enumerate(out) if m["role"] == "tool" and not m.get("evicted") and i < cutoff]
        if old_tools and not small():
            opts = [Option(str(i), f"{out[i].get('name', '')}: {str(out[i]['content'])[:400]}") for i in old_tools]
            d = self.jev.activate("Which of these earlier observations are still needed for the current goal?",
                                  goal, opts)
            for i in sorted(old_tools, key=lambda i: d.probs[str(i)]):
                if small():
                    break
                content = str(out[i]["content"])
                ref = self.store.put(content, out[i].get("name", "tool"))
                first = content.strip().splitlines()[0][:160] if content.strip() else ""
                out[i]["content"] = f"[evicted from context; ref={ref}; relevance={d.probs[str(i)]:.2f}] {first}"
                out[i]["evicted"] = True
                self.evicted += 1
        for i in asst:
            if small() or i >= cutoff:
                break
            m = out[i]
            if m.get("reasoning"):
                m["reasoning"] = ""
                m.pop("raw", None)
        for i in asst:
            if small() or i >= cutoff:
                break
            m = out[i]
            calls = m.get("tool_calls") or []
            if any(len(json.dumps(c.input)) > 600 for c in calls):
                m["tool_calls"] = [_shrink_call(c) for c in calls]
                m.pop("raw", None)
        for i, m in enumerate(out):
            if small() or i >= cutoff:
                break
            if i > 0 and m["role"] in ("assistant", "user") and len(str(m.get("content") or "")) > 1500:
                m["content"] = str(m["content"])[:600] + " [...shortened in compaction]"
                m.pop("raw", None)
        trimmed = 0
        while not small():
            asst = [i for i, m in enumerate(out) if m["role"] == "assistant"]
            if len(asst) <= self.keep_recent:
                break
            a, b = asst[0], asst[1]                  # one whole turn: the reply and the results that follow it
            del out[a:b]
            trimmed += 1
        if trimmed:
            out.insert(1, {"role": "user", "content": f"[rameness: {trimmed} earlier turns were trimmed to fit "
                                                          f"the context window; their files are on disk]"})
        return out


def _shrink_call(call):
    """A tool call whose large arguments (a written file's body, an edit's text) are replaced by their size: the
    file on disk holds the content, the conversation only needs to know it was written."""
    from .llm import ToolCall
    inp = {}
    for k, v in (call.input or {}).items():
        if isinstance(v, str) and len(v) > 400:
            inp[k] = f"<{len(v)} chars, {v.count(chr(10)) + 1} lines; in the file, not repeated here>"
        else:
            inp[k] = v
    return ToolCall(call.id, call.name, inp)
