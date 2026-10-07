"""Progress review: JEV periodically checks that the work is still heading the right way.

The loop guard only reacts to hard signals (the same call again, identical outputs, error
streaks). An agent can pass all of those and still be going the wrong way: building something
other than what was asked, polishing one part while the rest of the task is untouched, rewriting
the same files without ever running them, or carrying on after the task is already done.

Every ``every`` turns the review asks JEV one typed question about the run so far. Following the
context-scoping rule, JEV sees only this decision's material: the task, the agent's latest stated
intent, and a compact digest of the recent steps. It never sees the transcript.

* ``on_track``  - leave it alone
* ``drifting``  - recent work doesn't serve the task: restate the task and what is still missing
* ``stalled``   - activity without progress: stop, diagnose, change approach
* ``finish``    - the task looks done: verify it once and wrap up

An intervention needs a confident verdict (``min_confidence``); a near-tie leaves the agent alone.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, replace

from .jev import Jev, Option
from .loopguard import recurring_error

VERDICTS = [
    Option("on_track", "progress advancing toward task new files created features added tests passing verified "
                       "building implementing next part",
           desc="Recent steps clearly advance the task: new parts get built and checked. Let the agent continue.",
           prior=1.3),
    Option("drifting", "off task unrelated different goal scope creep side quest wrong project unasked extra "
                       "polishing irrelevant",
           desc="Recent steps work on something the task did not ask for, or ignore the main parts of the task."),
    Option("stalled", "no progress rewriting same file again errors failing not verified never run undoing "
                      "churn guessing same error recurring again and again",
           desc="Recent steps keep churning (rewriting the same files, failing, never running or checking the "
                "result) without the task moving forward."),
    Option("finish", "done complete finished all parts implemented verified works summary ready",
           desc="Everything the task asked for appears to be built and checked; the agent should verify once "
                "and finish."),
]

NOTES = {
    "drifting": ("[rameness progress review] Your recent steps are drifting from the task.\nTask: {task}\n"
                 "Recent steps:\n{digest}\nState in one sentence which parts of the task are still missing, then "
                 "work on those. Drop anything the task did not ask for."),
    "stalled": ("[rameness progress review] The last steps are not moving the task forward.\nTask: {task}\n"
                "Recent steps:\n{digest}\nStop repeating the same kind of change. Run or test what exists, read the "
                "actual error or output, say what is blocking you, and take a different approach."),
    "finish": ("[rameness progress review] The task looks complete.\nTask: {task}\nVerify it once (run it or its "
               "tests). If it works, stop and give a brief summary of what you built. If it doesn't, fix what fails."),
}


def _target(step: dict) -> str:
    i = step.get("input") or {}
    for k in ("path", "file_path", "command", "query", "pattern", "ref"):
        if k in i:
            return str(i[k]).replace("\n", " ")[:90]
    return json.dumps(i)[:90]


def digest(steps: list[dict], n: int = 14) -> str:
    """One line per recent step: tool, target, ok/error and the start of the output on errors."""
    lines = []
    for s in steps[-n:]:
        line = f"- {s['tool']} {_target(s)} -> {'ok' if s.get('ok', True) else 'error'}"
        if not s.get("ok", True):
            line += ": " + " ".join(str(s.get("out", "")).split())[:100]
        lines.append(line)
    return "\n".join(lines)


def facts(steps: list[dict], since: int) -> str:
    """Cheap counts over the window since the last review."""
    win = steps[since:]
    writes = [_target(s) for s in win if s["tool"] in ("write_file", "edit_file", "edit_lines")]
    runs = [s for s in win if s["tool"] == "bash"]
    errors = sum(1 for s in win if not s.get("ok", True))
    earlier = {_target(s) for s in steps[:since] if s["tool"] in ("write_file", "edit_file", "edit_lines")}
    return (f"{len(win)} steps since the last review: {len(writes)} file writes/edits "
            f"({len(set(writes) - earlier)} new files, {len(set(writes) & earlier)} rewritten), "
            f"{len(runs)} commands run, {errors} errors; {len({_target(s) for s in steps if s['tool'] in ('write_file', 'edit_file')})} "
            f"files touched in total over {len(steps)} steps."
            + (f" Recurring error: the same error appeared in {rec[1]} of the last {min(len(steps), 12)} results: "
               f"{rec[0]}" if (rec := recurring_error([s.get("errs") or [] for s in steps[-12:]], 4)) else ""))


@dataclass
class ProgressReview:
    jev: Jev
    every: int = 10                # review after this many agent turns
    min_confidence: float = 0.5    # act only on a verdict at least this probable
    last_turn: int = 0
    last_step: int = 0
    reviews: list[dict] = field(default_factory=list)

    def due(self, turn: int, steps: list[dict]) -> bool:
        return self.every > 0 and turn + 1 - self.last_turn >= self.every and len(steps) > self.last_step

    def review(self, turn: int, task: str, intent: str, steps: list[dict]) -> tuple[str, object, str | None]:
        """Returns (verdict, decision, message to inject or None)."""
        state = (f"task: {task[:600]}\nagent's latest note: {' '.join(intent.split())[:300] or '(none)'}\n"
                 f"{facts(steps, self.last_step)}\nrecent steps:\n{digest(steps)}")
        # after a 'finish' nudge the agent should stop soon; asking again leans harder on finish
        nudged = sum(1 for r in self.reviews if r["verdict"] == "finish")
        opts = [replace(o, prior=o.prior * (1 + 0.3 * nudged)) if o.id == "finish" else o for o in VERDICTS]
        d = self.jev.choose("Is the agent's recent work heading the right way to complete the task?", state, opts)
        verdict, p = d.best, d.probs[d.best]
        acted = verdict != "on_track" and p >= self.min_confidence
        self.reviews.append({"turn": turn, "verdict": verdict, "p": round(p, 3), "acted": acted})
        msg = NOTES[verdict].format(task=task[:1200], digest=digest(steps, 8)) if acted else None
        self.last_turn, self.last_step = turn + 1, len(steps)
        return verdict, d, msg
