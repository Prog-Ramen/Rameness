"""Per-turn thinking budgets, decided by JEV.

Claude decides for itself how long to think on each turn (adaptive thinking), so Claude Code only sets one
effort level for the session. Local reasoning models don't: they think until they are done or hit a cap, so a
flat budget spends as much thought on "run the check again" as on "why is nothing rendering?". Thinking tokens
are the slowest part of a local model's turn, so that is most of the time spent.

Before each model call JEV picks how hard the next step needs thinking about, from what just happened:

    minimal  purely mechanical: re-run the same check, list files, read a file just referenced
    brief    a routine next step: carry on with the plan after a step that worked
    normal   build the next piece
    deep     understand a new failure, or make a non-trivial design choice
    maximum  plan the whole task, break out of a recurring failure, rethink the approach

The level becomes a token budget for servers that take one (llama-server's reasoning_budget_tokens) and an
effort level for models that pace themselves (Claude). Config "thinking": {"mode": "jev" | "fixed", ...}.
"""

from __future__ import annotations

from dataclasses import dataclass, replace

from .jev import Jev, Option
from .loopguard import recurring_error

LEVELS = [
    Option("minimal", "mechanical passed list files read file referenced trivial obvious nothing to decide",
           desc="Purely mechanical: re-run the same check, list files, or read a file that was just referenced.",
           prior=0.9),
    Option("brief", "routine continue next step passed succeeded ok straightforward same plan small edit",
           desc="A routine next step: continue the plan after a step that worked.", prior=1.0),
    Option("normal", "implement build write next feature part module add extend moderate",
           desc="Build the next piece of the work.", prior=1.0),
    Option("deep", "failed new error failure unexpected wrong broken debug investigate why design decide choose",
           desc="Understand a new failure, or make a non-trivial design choice.", prior=1.0),
    Option("maximum", "plan whole task nothing done yet architecture same error occurred times stuck repeated rethink approach redesign",
           desc="Plan the whole task, break out of a recurring failure, or rethink the approach.", prior=0.9),
]
LEVEL_ORDER = [o.id for o in LEVELS]
EFFORT = {"minimal": "low", "brief": "low", "normal": "medium", "deep": "high"}
_RANK = ["low", "medium", "high", "xhigh", "max"]


def _at_least_high(task_effort: str) -> str:
    """maximum thinking: the task's own effort level, but never below high."""
    return task_effort if task_effort in _RANK and _RANK.index(task_effort) >= _RANK.index("high") else "high"


@dataclass
class Thinking:
    jev: Jev
    budgets: dict                    # level -> thinking tokens
    mode: str = "jev"                # jev | fixed

    @classmethod
    def from_cfg(cls, jev: Jev, cfg: dict) -> "Thinking":
        t = cfg.get("thinking") or {}
        defaults = {"minimal": 512, "brief": 2048, "normal": 4096, "deep": 8192, "maximum": 16384}
        return cls(jev, {k: t.get(k, v) for k, v in defaults.items()}, t.get("mode", "jev"))

    def state(self, turn: int, task: str, steps: list[dict], last_text: str) -> str:
        """Only what this decision needs, with the deciding fact said outright: where the work is, and whether the
        last step passed, failed for the first time, or failed the same way again."""
        if turn == 0:
            return f"Nothing has been done yet: the agent is about to plan the whole task.\nTask: {task[:300]}"
        rec = recurring_error([s.get("errs") or [] for s in steps[-12:]], 3)
        last = steps[-1] if steps else None
        if rec:
            head = f"The same error has now occurred {rec[1]} times; the agent is stuck on it: {rec[0][:120]}"
        elif last and (not last.get("ok", True) or last.get("errs")):
            head = "The last step failed with a new error: " + "; ".join(last.get("errs") or ["(tool error)"])[:160]
        elif last and last["tool"] == "bash":
            head = "The last check or command passed."
        elif last:
            head = f"The last step ({last['tool']}) succeeded."
        else:
            head = "The agent has not run anything yet."
        lines = []
        for s in steps[-4:]:
            target = str(s["input"].get("path") or s["input"].get("command") or "")[:80].replace("\n", " ")
            status = "ok" if s.get("ok", True) and not s.get("errs") else "error"
            lines.append(f"- {s['tool']} {target} -> {status}")
        return (f"{head}\nTurn {turn}. Task: {task[:200]}\nLast steps:\n" + "\n".join(lines or ["(none)"])
                + (f"\nThe agent last said: {' '.join(last_text.split())[:160]}" if last_text else ""))

    def decide(self, turn: int, task: str, steps: list[dict], last_text: str, task_effort: str) -> tuple[str, int, str, object]:
        """(level, token budget, effort, decision)."""
        if self.mode != "jev":
            return "maximum", self.budgets["maximum"], _at_least_high(task_effort), None
        opts = LEVELS
        if steps and (not steps[-1].get("ok", True) or steps[-1].get("errs")):
            # something just failed: lean towards thinking about it
            opts = [replace(o, prior=o.prior * 1.3) if o.id in ("deep", "maximum") else o for o in LEVELS]
        d = self.jev.choose("How much should the agent think before its next step?",
                            self.state(turn, task, steps, last_text), opts)
        level = d.best
        if steps and steps[-1]["tool"] == "write_file" and LEVEL_ORDER.index(level) < LEVEL_ORDER.index("normal"):
            # a whole file was just written: the next step is often the next whole file (drafting), and JEV reads a
            # successful write as mechanical progress; Minecraft runs drafted modules on the minimal budget
            level = "normal"
        return level, self.budgets[level], EFFORT.get(level) or _at_least_high(task_effort), d
