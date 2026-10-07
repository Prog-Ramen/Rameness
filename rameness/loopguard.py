"""Loop guard: JEV notices when an agent is stuck and puts it back on task.

Small / heavily quantized models commonly fall into loops: the same tool call
again and again, A-B-A-B oscillation, an error they keep repeating, replies that
repeat themselves, or degenerate text that repeats a phrase until max_tokens.

Every turn the guard computes cheap signals (no model calls). When any crosses
its threshold the JEV chooses what to do:

* ``continue``  - it's actually making progress (e.g. polling a build)
* ``reorient``  - inject a message restating the goal, what was tried, and what not to repeat
* ``reset``     - rebuild the context from the task plus distilled notes (cures degeneration)
* ``stop``      - end the run as failed so the manager can retry elsewhere (e.g. a stronger model)

Interventions escalate: repeated looping after a reorientation leans towards reset, then stop.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from dataclasses import dataclass, field, replace

from .jev import Jev, Option

ACTIONS = [
    Option("continue", "progress new information different varied exploring polling waiting build running",
           desc="The agent is making progress or legitimately waiting; let it keep going."),
    Option("reorient", "repeating same tool call again loop stuck circular same error oscillating retrying identical",
           desc="The agent is repeating the same action or error in a loop; interrupt it and make it change approach."),
    Option("reset", "degenerate repeated text garbled phrase repetition confused context polluted long "
                    "hallucinating nonsense",
           desc="The agent's output has degenerated into repetition or nonsense; reset its context and restart from a summary."),
    Option("stop", "no progress many interventions impossible blocked permission denied hopeless exhausted",
           desc="The task cannot progress: repeated interventions failed or the agent is blocked; stop it."),
]


WRITES = {"write_file", "edit_file", "edit_lines"}


def _sig(call) -> str:
    # the whole input: two different test scripts often share their first few hundred characters
    body = json.dumps(call.input, sort_keys=True)
    return f"{call.name}:{body[:160]}#{hashlib.sha1(body.encode('utf-8', 'replace')).hexdigest()[:10]}"


def _words(t: str) -> set[str]:
    return set(re.findall(r"[a-z0-9_]+", t.lower()))


_ERR = re.compile(r"error|exception|unexpected token|traceback|\bfail(ed|ure)?\b|cannot |not defined|is not a function|"
                  r"undefined|refused|denied|no such file|not found", re.I)
_BENIGN = re.compile(r"\b0 (failed|failures?|errors?)\b|\berrors?:? ?0\b|\bno errors?\b|\bwithout errors?\b", re.I)


def error_lines(text: str, limit: int = 5) -> list[str]:
    """The distinct error-looking lines of a tool result, numbers masked so reruns compare equal."""
    out: list[str] = []
    for ln in text.splitlines():
        if _ERR.search(ln) and not _BENIGN.search(ln):
            norm = re.sub(r"\d+", "#", " ".join(ln.split()))[:160]
            if norm and norm not in out:
                out.append(norm)
                if len(out) >= limit:
                    break
    return out


def recurring_error(err_lists: list[list[str]], need: int) -> tuple[str, int] | None:
    """The error line seen in the most results, if it shows up in at least ``need`` of them."""
    c = Counter(e for errs in err_lists for e in set(errs))
    if not c:
        return None
    line, n = c.most_common(1)[0]
    return (line, n) if n >= need else None


def degenerate(text: str, n: int = 6, min_repeats: int = 4) -> bool:
    """True when some n-word phrase repeats many times (typical quantized-model collapse)."""
    w = text.split()
    if len(w) < n * min_repeats:
        return False
    c = Counter(tuple(w[i:i + n]) for i in range(len(w) - n + 1))
    return c.most_common(1)[0][1] >= min_repeats and c.most_common(1)[0][1] * n > len(w) * 0.3


@dataclass
class LoopGuard:
    jev: Jev
    window: int = 8
    repeat_calls: int = 3          # same call this many times in the window
    error_streak: int = 4
    similar_text: float = 0.9
    cooldown: int = 3
    repeat_error: int = 5          # the same error line in this many of the last error_window results
    error_window: int = 12
    stop_confidence: float = 0.5   # ending the run needs a confident 'stop' ...
    max_interventions: int = 4     # ... unless this many interventions have already failed
    calls: list[str] = field(default_factory=list)
    outputs: list[str] = field(default_factory=list)
    errlines: list[list[str]] = field(default_factory=list)
    texts: list[str] = field(default_factory=list)
    errors: int = 0
    interventions: list[str] = field(default_factory=list)
    last_intervention_turn: int = -99

    def observe(self, response, results: list[tuple[str, bool]]) -> None:
        if any(c.name in WRITES for c in response.tool_calls):
            # the code changed: running the same test or build again is an edit-test cycle, not a loop
            self.calls.clear()
        self.calls += [_sig(c) for c in response.tool_calls if c.name not in WRITES]
        # keyed by call and result: different edits that each answer "edited <file>" are not a repeat
        self.outputs += [hashlib.sha1((_sig(c) + "\0" + o).encode("utf-8", "replace")).hexdigest()[:12]
                         for c, (o, _) in zip(response.tool_calls, results)]
        # different commands can keep hitting the same error: that is a loop too
        self.errlines = (self.errlines + [error_lines(o) for o, _ in results])[-self.error_window:]
        if response.text:
            self.texts.append(response.text)
        if results:
            self.errors = self.errors + 1 if all(err for _, err in results) else 0
        self.calls, self.outputs, self.texts = (self.calls[-self.window * 2:], self.outputs[-self.window * 2:],
                                                self.texts[-self.window:])

    def signals(self) -> list[str]:
        s = []
        recent = self.calls[-self.window:]
        if recent:
            sig, n = Counter(recent).most_common(1)[0]
            if n >= self.repeat_calls:
                s.append(f"repeated the same tool call {n}x: {sig[:160]}")
        if len(recent) >= 6 and len(set(recent[-6:])) == 2 and recent[-1] != recent[-2]:
            s.append("oscillating between two actions")
        outs = self.outputs[-self.window:]
        if len(outs) >= 4 and len(set(outs[-4:])) == 1:
            s.append("the same call returned the same result 4 times (no new information)")
        rec = recurring_error(self.errlines, self.repeat_error)
        if rec:
            s.append(f"the same error keeps coming back ({rec[1]} of the last {len(self.errlines)} results): {rec[0]}")
        if self.errors >= self.error_streak:
            s.append(f"{self.errors} consecutive turns of tool errors")
        if len(self.texts) >= 3:
            a, b, c = (_words(t) for t in self.texts[-3:])
            if a and b and c and len(a & c) / max(1, len(a | c)) > self.similar_text \
                    and len(b & c) / max(1, len(b | c)) > self.similar_text:
                s.append("the last replies say the same thing")
        if self.texts and degenerate(self.texts[-1]):
            s.append("degenerate output: a phrase repeating over and over")
        return s

    def check(self, turn: int, task: str) -> tuple[str, list[str], object] | None:
        """Returns (action, signals, decision) or None when nothing looks wrong."""
        if turn - self.last_intervention_turn < self.cooldown:
            return None
        sig = self.signals()
        if not sig:
            return None
        history = f" previous interventions: {', '.join(self.interventions)}" if self.interventions else ""
        prior = {"continue": 1.0, "reorient": 1.0, "reset": 1.0 + 0.5 * self.interventions.count("reorient"),
                 "stop": 1.0 + 0.6 * max(0, len(self.interventions) - 1)}
        opts = [replace(o, prior=prior[o.id]) for o in ACTIONS]
        d = self.jev.choose("The agent may be stuck in a loop. What should happen?",
                            f"{'; '.join(sig)}.{history} task: {task[:300]}", opts)
        action = d.best
        if action == "stop" and d.probs["stop"] < self.stop_confidence \
                and len(self.interventions) < self.max_interventions:
            action = max((o for o in d.probs if o != "stop"), key=d.probs.get)
        elif len(self.interventions) >= self.max_interventions and action != "continue":
            action = "stop"
        if action != "continue":
            self.interventions.append(action)
            self.last_intervention_turn = turn
            self.calls.clear()
            self.outputs.clear()
            self.errlines.clear()
            self.errors = 0
        return action, sig, d

    @staticmethod
    def reorientation(task: str, sig: list[str], steps: list[dict]) -> str:
        tried = []
        for s in steps[-12:]:
            k = f"{s['tool']} {json.dumps(s['input'])[:120]}"
            if k not in tried:
                tried.append(k)
        return ("[rameness loop guard] You are going in circles: " + "; ".join(sig) + ".\n"
                f"Your task: {task}\n"
                "Already tried (do NOT repeat these):\n" + "\n".join(f"- {t}" for t in tried[-8:]) + "\n"
                "Step back: state in one sentence what is blocking you, then take a *different* action. If the same "
                "error keeps coming back, stop varying the same idea: reduce the failing part to the smallest case "
                "that still fails, or rebuild that part a different way. "
                "If you have enough to finish, finish and report. If you are blocked on something outside "
                "your control, say so plainly.")

    @staticmethod
    def reset_messages(task: str, sig: list[str], steps: list[dict]) -> list[dict]:
        notes = []
        for s in steps[-15:]:
            status = "ok" if s.get("ok", True) else "error"
            notes.append(f"- {s['tool']} {json.dumps(s['input'])[:140]} -> {status}: {s.get('out', '')[:160]}")
        return [{"role": "user", "content":
                 f"{task}\n\n[rameness loop guard] Your previous attempt got stuck ({'; '.join(sig)}), so the "
                 "context was reset. Notes from that attempt:\n" + "\n".join(notes) +
                 "\nUse these notes, avoid what failed, and take a different approach."}]
