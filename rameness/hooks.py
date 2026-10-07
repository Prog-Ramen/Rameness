"""Lifecycle hooks: standard procedures (SOPs) that Rameness runs itself at fixed points of the work.

The split Rameness is built on:
    the LLM   thinks and generates (plans, code, the checks that define "working")
    JEV       decides (route, effort, when to intervene, what to evict, whether to roll back)
    SOPs      carry out standard procedures, the same way every time

Most SOPs are tools the model may call. Hook SOPs are the ones nobody should have to remember to call:
Rameness runs them on an event and puts their report where the model will see it, so the model spends its
turns on thinking instead of on mechanics.

The events are the same for every kind of task (code, research, analysis, media):

    event          payload                                 where a non-empty report goes
    on_start       {root}                                  the system prompt
    after_output   {paths}  files the work produced or     appended to the tool result that produced them
                            changed (file tools, commands)
    before_finish  {root}                                  a note to the model before it may stop
    on_end         {root}                                  the run log

Which procedures fit depends on the task, so that is JEV's call: the config lists candidate SOPs per event,
and at the start of a task JEV picks the ones that apply (a syntax check for a coding task, a link check for a
research report...). Config:  "hooks": {event: [sop ids]},  "hook_args": {sop id: {...}},
"hook_selection": "jev" | "all",  "hook_threshold".
A hook SOP returns {"report": "..."} (empty: nothing to say) plus any data. Configured hooks run
pre-approved; a failing hook never fails the step it is attached to.
"""

from __future__ import annotations

import os
from pathlib import Path

EVENTS = ("on_start", "after_output", "before_finish", "on_end")


class Hooks:
    def __init__(self, executor, cfg: dict, root: Path, on_event=None):
        self.executor = executor
        self.table = {e: list((cfg.get("hooks") or {}).get(e) or []) for e in EVENTS}
        self.args = cfg.get("hook_args") or {}
        self.mode = cfg.get("hook_selection", "jev")
        self.threshold = cfg.get("hook_threshold", 0.25)
        self.selected: set[str] | None = None    # None until select(): then only these run
        self.root = root
        self.on_event = on_event            # callable(kind, **data): the harness event log

    def candidates(self) -> list[str]:
        return list(dict.fromkeys(s for ids in self.table.values() for s in ids))

    def select(self, jev, task: str) -> set[str]:
        """JEV decides which candidate procedures fit this task."""
        from .jev import Option
        ids = [i for i in self.candidates() if self._exists(i)]
        if self.mode == "all" or not ids:
            self.selected = set(ids)
            return self.selected
        opts = [Option(i, self._sop(i).text, desc=self._sop(i).desc) for i in ids]
        d = jev.activate("Which of these standard procedures should run automatically while doing this task?",
                         task, opts)
        self.selected = {i for i in ids if d.probs.get(i, 0) >= self.threshold}
        if self.on_event:
            self.on_event("hooks_selected", selected=sorted(self.selected), probs=d.probs)
        return self.selected

    def _sop(self, sop_id):
        return self.executor.lib.get(sop_id)

    def _exists(self, sop_id) -> bool:
        try:
            self._sop(sop_id)
            return True
        except Exception:
            return False

    def active(self, event: str) -> bool:
        return any(self.selected is None or s in self.selected for s in self.table.get(event) or [])

    def fire(self, event: str, **payload) -> str:
        """Run the event's SOPs; returns their combined non-empty reports."""
        reports = []
        for sop_id in self.table.get(event) or []:
            if self.selected is not None and sop_id not in self.selected:
                continue
            args = {**payload, **self.args.get(sop_id, {})}
            try:
                out = self.executor.run(sop_id, args, preapproved=True)
            except Exception as e:            # a broken hook must not break the work it watches
                if self.on_event:
                    self.on_event("hook_error", event=event, sop=sop_id, error=str(e)[:500])
                continue
            rep = (out.get("report") or "").strip() if isinstance(out, dict) else ""
            if self.on_event:
                self.on_event("hook", event=event, sop=sop_id, reported=bool(rep), report=rep[:2000])
            if rep:
                reports.append(rep)
        return "\n".join(reports)


# --------------------------------------------------------------------------- what did a shell command change

def snapshot(root: Path, skip: set[str], cap: int = 5000) -> dict[str, float]:
    """mtimes of the files under ``root`` (hidden and dependency directories skipped)."""
    seen: dict[str, float] = {}
    for dirpath, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d not in skip and not d.startswith(".")]
        for f in files:
            p = os.path.join(dirpath, f)
            try:
                seen[p] = os.stat(p).st_mtime
            except OSError:
                pass
            if len(seen) >= cap:
                return seen
    return seen


def changed(before: dict[str, float], after: dict[str, float], limit: int = 20) -> list[str]:
    return [p for p, t in after.items() if before.get(p) != t][:limit]
