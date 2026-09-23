"""Model-driven improvement of JEV decisions.

Signals (all appended to ``decisions.jsonl`` next to the decisions themselves):

* explicit feedback - the director marks a decision right, or picks the option that should have won
* outcomes          - the fleet records whether the agent a decision assigned succeeded

Improvement paths, from cheapest to richest:

1. ``calibrate()`` - deterministic, idempotent: per (question, option) weights
   ``w = exp(lr * (wins - misses))`` clipped to [0.5, 2], rebuilt from all feedback.
2. ``propose(llm)`` - a model reads the misdecisions for a question and proposes
   extra cue text per option. Proposals are reviewed and applied by a human.
3. ``export()``     - a labelled JSONL training set for fine-tuning an open-source JEV.

Both 1 and 2 land in ``jev_tuning.json``, which the running JEV hot-reloads.
"""

from __future__ import annotations

import json
import math
import time
import uuid
from collections import defaultdict
from pathlib import Path


class Tuner:
    def __init__(self, state: Path):
        self.state = state
        self.log = state / "decisions.jsonl"
        self.tuning_path = state / "jev_tuning.json"
        self.proposals_path = state / "jev_proposals.json"

    # ---- data

    def load(self) -> tuple[dict, dict]:
        decisions, fb = {}, defaultdict(dict)
        if not self.log.exists():
            return decisions, fb
        for line in self.log.read_text().splitlines():
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            if "feedback" in r:
                fb[r["feedback"]].update({k: v for k, v in r.items() if k in ("label", "correct", "outcome")})
            elif "id" in r and r.get("probs"):
                decisions[r["id"]] = r
        return decisions, fb

    @staticmethod
    def chosen(rec: dict) -> str:
        return max(rec["probs"].items(), key=lambda kv: kv[1])[0]

    def feedback(self, did: str, label: str | None = None, correct: bool | None = None,
                 outcome: str | None = None) -> None:
        self.state.mkdir(parents=True, exist_ok=True)
        rec = {"feedback": did, "t": time.time()}
        if label is not None:
            rec["label"] = label
        if correct is not None:
            rec["correct"] = correct
        if outcome is not None:
            rec["outcome"] = outcome
        with self.log.open("a") as f:
            f.write(json.dumps(rec) + "\n")

    def label_of(self, rec: dict, fb: dict) -> str | None:
        if fb.get("label"):
            return fb["label"]
        if fb.get("correct") is True:
            return self.chosen(rec)
        return None

    # ---- views

    def stats(self) -> list[dict]:
        decisions, fb = self.load()
        by_q: dict[str, dict] = {}
        for did, rec in decisions.items():
            q = by_q.setdefault(rec["question"], {"question": rec["question"], "n": 0, "labeled": 0, "agree": 0,
                                                  "ok": 0, "fail": 0, "options": defaultdict(int)})
            q["n"] += 1
            q["options"][self.chosen(rec)] += 1
            f = fb.get(did, {})
            lab = self.label_of(rec, f)
            if lab:
                q["labeled"] += 1
                q["agree"] += lab == self.chosen(rec)
            if f.get("outcome") == "ok":
                q["ok"] += 1
            elif f.get("outcome") == "fail":
                q["fail"] += 1
        out = []
        for q in by_q.values():
            q["accuracy"] = round(q["agree"] / q["labeled"], 3) if q["labeled"] else None
            q["options"] = dict(q["options"])
            out.append(q)
        return sorted(out, key=lambda q: -q["n"])

    def tuning(self) -> dict:
        return json.loads(self.tuning_path.read_text()) if self.tuning_path.exists() else {"weights": {}, "cues": {}}

    def _save_tuning(self, t: dict) -> None:
        self.tuning_path.write_text(json.dumps(t, indent=1, sort_keys=True))

    # ---- 1. calibration

    def calibrate(self, lr: float = 0.15) -> dict:
        decisions, fb = self.load()
        score: dict[str, float] = defaultdict(float)
        for did, f in fb.items():
            rec = decisions.get(did)
            if not rec:
                continue
            q, chosen = rec["question"], self.chosen(rec)
            lab = self.label_of(rec, f)
            if lab:
                score[f"{q}::{lab}"] += 1
                if lab != chosen:
                    score[f"{q}::{chosen}"] -= 1
            if f.get("outcome") == "ok":
                score[f"{q}::{chosen}"] += 0.3
            elif f.get("outcome") == "fail":
                score[f"{q}::{chosen}"] -= 0.3
        t = self.tuning()
        t["weights"] = {k: round(min(2.0, max(0.5, math.exp(lr * v))), 4) for k, v in score.items() if v}
        t["calibrated_at"] = time.time()
        self._save_tuning(t)
        return t["weights"]

    # ---- 2. model-proposed cue improvements

    def proposals(self) -> list[dict]:
        return json.loads(self.proposals_path.read_text()) if self.proposals_path.exists() else []

    def propose(self, llm, question: str | None = None, max_examples: int = 12) -> list[dict]:
        decisions, fb = self.load()
        misses: dict[str, list] = defaultdict(list)
        texts: dict[str, dict] = {}
        for did, f in fb.items():
            rec = decisions.get(did)
            if not rec or (question and rec["question"] != question):
                continue
            lab, ch = self.label_of(rec, f), self.chosen(rec)
            if (lab and lab != ch) or f.get("outcome") == "fail":
                misses[rec["question"]].append({"query": rec["query"][:400], "chosen": ch,
                                                "should_be": lab or "(chosen option failed)"})
                texts[rec["question"]] = rec.get("option_texts", {o: "" for o in rec["options"]})
        new = []
        for q, ex in misses.items():
            prompt = (f"Decision question: {q}\nOptions and their current cue text:\n"
                      + "\n".join(f"- {k}: {v}" for k, v in texts[q].items())
                      + "\n\nMisdecisions:\n" + "\n".join(json.dumps(e) for e in ex[:max_examples])
                      + '\n\nPropose short additional cue words for options so these cases would be decided '
                        'correctly without breaking typical cases. Reply {"cues": {"<option id>": "words ..."}, '
                        '"rationale": "one paragraph"}')
            try:
                d = llm.complete_json("You tune a decision model's option descriptions. JSON only.", prompt,
                                      max_tokens=1500)
            except Exception as e:
                d = {"cues": {}, "rationale": f"model call failed: {e}"}
            new.append({"id": "p-" + uuid.uuid4().hex[:6], "t": time.time(), "question": q, "examples": len(ex),
                        "cues": {k: v for k, v in (d.get("cues") or {}).items() if k in texts[q]},
                        "rationale": d.get("rationale", ""), "status": "pending"})
        allp = self.proposals() + new
        self.proposals_path.write_text(json.dumps(allp, indent=1))
        return new

    def resolve(self, pid: str, apply: bool) -> dict:
        allp = self.proposals()
        p = next(x for x in allp if x["id"] == pid)
        p["status"] = "applied" if apply else "rejected"
        if apply:
            t = self.tuning()
            for opt, words in p["cues"].items():
                k = f"{p['question']}::{opt}"
                t.setdefault("cues", {})[k] = f"{t['cues'].get(k, '')} {words}".strip()
            self._save_tuning(t)
        self.proposals_path.write_text(json.dumps(allp, indent=1))
        return p

    # ---- 3. training data

    def export(self, path: Path) -> int:
        decisions, fb = self.load()
        n = 0
        with path.open("w") as f:
            for did, rec in decisions.items():
                lab = self.label_of(rec, fb.get(did, {}))
                outcome = fb.get(did, {}).get("outcome")
                if not lab and not outcome:
                    continue
                f.write(json.dumps({"question": rec["question"], "query": rec["query"],
                                    "options": [{"id": o, "text": rec.get("option_texts", {}).get(o, "")}
                                                for o in rec["options"]],
                                    "probs": rec["probs"], "label": lab, "outcome": outcome}) + "\n")
                n += 1
        return n
