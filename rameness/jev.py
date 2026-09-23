"""The JEV decision engine.

Every adaptive choice in the harness goes through one of two calls:

* ``activate`` - multi-label: an independent probability per option
  ("which of these SOP categories will the task touch?").
* ``choose``   - multiple choice: a distribution that sums to 1
  ("route this task: direct / answer / agent / clarify").

Backends are swappable so an open-source JEV model can be dropped in:

* ``LexicalJev``  - zero-cost, offline token-overlap scorer (default first pass)
* ``LLMJev``      - asks a small LLM for calibrated probabilities as JSON
* ``HttpJev``     - POSTs to a served JEV model (see ``HttpJev`` for the contract)
* ``CascadeJev``  - lexical first, escalates only options that land in the
                    uncertain band to the stronger backend

Every decision is logged to ``decisions.jsonl`` together with its later
outcome, which is the training data for fine-tuning a JEV.
"""

from __future__ import annotations

import json
import math
import re
import time
import urllib.request
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class Option:
    id: str
    text: str
    prior: float = 1.0          # multiplicative prior, e.g. from SOP success rate


@dataclass
class Decision:
    id: str
    question: str
    probs: dict[str, float]
    backend: str

    def top(self, n: int = 1) -> list[tuple[str, float]]:
        return sorted(self.probs.items(), key=lambda kv: -kv[1])[:n]

    @property
    def best(self) -> str:
        return self.top(1)[0][0]


# --------------------------------------------------------------------------- text utils

_STOP = set("""a an the and or of to in on for with by from at as is are was were be been it this that
these those i you we they me my our your please can could would should will just into about using use
then than so do does did not no yes if any all some""".split())


def _stem(w: str) -> str:
    for suf in ("ing", "ies", "ed", "es", "s"):
        if len(w) > len(suf) + 2 and w.endswith(suf):
            return w[: -len(suf)] + ("y" if suf == "ies" else "")
    return w


def _related(a: str, b: str) -> bool:
    """deploy~deployment, summarize~summarise~summary: shared stem of >= 5 chars."""
    if len(a) < 4 or len(b) < 4:
        return False
    if a.startswith(b) or b.startswith(a):
        return True
    k = 0
    for x, y in zip(a, b):
        if x != y:
            break
        k += 1
    return k >= 5


def tokens(text: str) -> list[str]:
    words = re.findall(r"[a-zA-Z][a-zA-Z0-9]+", text.lower().replace("_", " "))
    return [_stem(w) for w in words if w not in _STOP]


# --------------------------------------------------------------------------- backends

class Backend(ABC):
    name = "base"

    @abstractmethod
    def activate(self, question: str, query: str, options: list[Option], context: str = "") -> list[float]:
        """Independent probability per option."""

    def choose(self, question: str, query: str, options: list[Option], context: str = "") -> list[float]:
        raw = self.activate(question, query, options, context)
        s = sum(raw)
        return [1 / len(raw)] * len(raw) if s <= 0 else [r / s for r in raw]


class LexicalJev(Backend):
    """IDF-weighted token overlap mapped to a probability.

    Deliberately simple: it is the cheap first pass that settles the obvious
    cases so the expensive backend only sees ambiguous ones.
    """

    name = "lexical"

    def __init__(self, sharpness: float = 1.2, floor: float = 0.02):
        self.sharpness = sharpness
        self.floor = floor

    def activate(self, question, query, options, context=""):
        qset = set(tokens(query)) | set(tokens(context)[:200])
        docs = [set(tokens(o.text)) for o in options]
        n = len(docs)
        df: dict[str, int] = {}
        for d in docs:
            for t in d:
                df[t] = df.get(t, 0) + 1
        out = []
        for d in docs:
            if not d:
                out.append(self.floor)
                continue
            # tokens shared by every option don't discriminate between them
            idf = {t: math.log(1 + n / df[t]) + (0.3 if n == 1 else 0.0) for t in d}
            exact = sum(idf[t] for t in d & qset)
            partial = 0.6 * sum(idf[t] for t in d - qset if any(_related(t, x) for x in qset))
            # long keyword lists shouldn't win by volume: dampen by option size
            hit = (exact + partial) / (1 + math.log(1 + len(d)) / 3)
            p = 1 - math.exp(-hit / self.sharpness)
            out.append(max(self.floor, min(0.99, p)))
        return out


class LLMJev(Backend):
    """Probabilities from a (small) LLM. ``llm`` must expose ``complete_json(system, prompt)``."""

    name = "llm"

    SYSTEM = ("You are JEV, a decision model inside an agent harness. You output calibrated "
              "probabilities only, as JSON. Never explain.")

    def __init__(self, llm, timeout: float = 20.0):
        self.llm = llm
        self.timeout = timeout           # a decision that takes longer than this falls back to the cheap pass

    def _ask(self, mode, question, query, options, context):
        opts = "\n".join(f"- {o.id}: {o.text[:300]}" for o in options)
        rule = ("Each probability is independent (multi-label), 0..1."
                if mode == "activate" else "Probabilities form a distribution summing to 1.")
        prompt = (f"Decision: {question}\nTask: {query}\n"
                  + (f"Context:\n{context[:3000]}\n" if context else "")
                  + f"Options:\n{opts}\n{rule}\n"
                  'Reply with JSON only: {"probs": {"<option id>": <probability>, ...}}')
        data = self.llm.complete_json(self.SYSTEM, prompt, max_tokens=600, timeout=self.timeout)
        probs = data.get("probs", data) if isinstance(data, dict) else {}
        return [float(probs.get(o.id, 0.0)) for o in options]

    def activate(self, question, query, options, context=""):
        return self._ask("activate", question, query, options, context)

    def choose(self, question, query, options, context=""):
        raw = self._ask("choose", question, query, options, context)
        s = sum(raw)
        return [1 / len(raw)] * len(raw) if s <= 0 else [r / s for r in raw]


class HttpJev(Backend):
    """Client for a served JEV model.

    Request  (POST ``url``)::

        {"mode": "activate"|"choose", "question": str, "query": str,
         "context": str, "options": [{"id": str, "text": str}]}

    Response::

        {"probs": {"<id>": float, ...}}
    """

    name = "http"

    def __init__(self, url: str, timeout: float = 10.0):
        self.url = url
        self.timeout = timeout

    def _post(self, mode, question, query, options, context):
        body = json.dumps({"mode": mode, "question": question, "query": query, "context": context,
                           "options": [{"id": o.id, "text": o.text} for o in options]}).encode()
        req = urllib.request.Request(self.url, data=body, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=self.timeout) as r:
            probs = json.loads(r.read())["probs"]
        return [float(probs.get(o.id, 0.0)) for o in options]

    def activate(self, question, query, options, context=""):
        return self._post("activate", question, query, options, context)

    def choose(self, question, query, options, context=""):
        return self._post("choose", question, query, options, context)


class CascadeJev(Backend):
    """Cheap backend first; escalate to the strong one only when unsure."""

    name = "cascade"

    def __init__(self, cheap: Backend, strong: Backend | None, band=(0.3, 0.7)):
        self.cheap, self.strong, self.band = cheap, strong, band
        self.escalations = 0

    def _unsure(self, probs):
        lo, hi = self.band
        return any(lo <= p <= hi for p in probs)

    def activate(self, question, query, options, context=""):
        p = self.cheap.activate(question, query, options, context)
        if self.strong is None or not self._unsure(p):
            return p
        idx = [i for i, x in enumerate(p) if self.band[0] <= x <= self.band[1]]
        try:
            sub = self.strong.activate(question, query, [options[i] for i in idx], context)
        except Exception:
            return p
        self.escalations += 1
        for i, v in zip(idx, sub):
            p[i] = v
        return p

    def choose(self, question, query, options, context=""):
        p = self.cheap.choose(question, query, options, context)
        if self.strong is None or max(p) >= self.band[1]:
            return p
        try:
            self.escalations += 1
            return self.strong.choose(question, query, options, context)
        except Exception:
            return p


# --------------------------------------------------------------------------- comfort gate

SIGNIFICANCE = [
    Option("routine", "routine reversible cheap internal assignment scheduling which model environment runtime slot "
                      "retry wait nudge research draft read only local branch worktree small fix test"),
    Option("significant", "irreversible destructive delete remove drop overwrite production deploy release merge main "
                          "publish push costly expensive paid money budget credentials secrets security legal privacy "
                          "customer data external email send abandon cancel scope priority preference"),
]


@dataclass
class Comfort:
    """JEV's check on itself before a decision takes effect."""
    significance: float        # P(the decision is significant enough to belong to the user)
    confidence: float          # top option probability
    margin: float              # gap between the top two options
    needs_user: bool
    reason: str


# --------------------------------------------------------------------------- engine

@dataclass
class Jev:
    backend: Backend
    log_path: Path | None = None
    calls: int = 0
    history: list[Decision] = field(default_factory=list)
    tuning_path: Path | None = None       # jev_tuning.json: learned weights + cue overrides (see improve.py)
    _tuning: dict = field(default_factory=dict)
    _tuning_mtime: float = 0.0

    def _tune(self, question: str, options: list[Option]) -> list[Option]:
        p = self.tuning_path
        if p and p.exists() and p.stat().st_mtime != self._tuning_mtime:
            try:
                self._tuning = json.loads(p.read_text())
                self._tuning_mtime = p.stat().st_mtime
            except (json.JSONDecodeError, OSError):
                pass
        if not self._tuning:
            return options
        w, cues = self._tuning.get("weights", {}), self._tuning.get("cues", {})
        return [Option(o.id, f"{o.text} {cues.get(f'{question}::{o.id}', '')}".strip(),
                       o.prior * float(w.get(f"{question}::{o.id}", 1.0))) for o in options]

    def _record(self, question, query, options, probs, kind) -> Decision:
        self.calls += 1
        d = Decision(uuid.uuid4().hex[:12], question, {o.id: round(p, 4) for o, p in zip(options, probs)},
                     self.backend.name)
        self.history.append(d)
        if self.log_path:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            with self.log_path.open("a") as f:
                f.write(json.dumps({"id": d.id, "t": time.time(), "kind": kind, "question": question,
                                    "query": query[:2000], "options": [o.id for o in options],
                                    "option_texts": {o.id: o.text[:300] for o in options},
                                    "probs": d.probs, "backend": d.backend}) + "\n")
        return d

    def activate(self, question: str, query: str, options: list[Option], context: str = "") -> Decision:
        if not options:
            return Decision("-", question, {}, self.backend.name)
        options = self._tune(question, options)
        raw = self.backend.activate(question, query, options, context)
        probs = [min(0.999, max(0.0, p * o.prior)) for p, o in zip(raw, options)]
        return self._record(question, query, options, probs, "activate")

    def choose(self, question: str, query: str, options: list[Option], context: str = "") -> Decision:
        options = self._tune(question, options)
        raw = self.backend.choose(question, query, options, context)
        raw = [p * o.prior for p, o in zip(raw, options)]
        s = sum(raw) or 1.0
        return self._record(question, query, options, [p / s for p in raw], "choose")

    def yes(self, question: str, query: str, yes_cues: str, no_cues: str = "", context: str = "") -> float:
        """Probability of 'yes' for a binary question."""
        d = self.choose(question, query, [Option("yes", yes_cues), Option("no", no_cues or "none unspecified")], context)
        return d.probs["yes"]

    def comfort(self, question: str, query: str, d: Decision, sig_threshold: float = 0.55,
                margin_floor: float = 0.06, uncertain_sig: float = 0.3) -> Comfort:
        """Is this a decision JEV is comfortable making, or one the user should make?

        Deferred when the decision looks significant (irreversible, costly, external, a
        preference), or when JEV is unsure (near-tie) about something that isn't trivially routine.
        """
        top = d.top(2)
        conf = top[0][1]
        margin = conf - (top[1][1] if len(top) > 1 else 0.0)
        opts_text = " ".join(d.probs)
        sd = self.choose("Is this decision significant enough that the user should make it?",
                         f"{question} {query[:600]} options: {opts_text} leaning: {top[0][0]}", SIGNIFICANCE)
        sig = sd.probs["significant"]
        if sig >= sig_threshold:
            return Comfort(sig, conf, margin, True, "significant")
        if margin < margin_floor and sig >= uncertain_sig:
            return Comfort(sig, conf, margin, True, "uncertain")
        return Comfort(sig, conf, margin, False, "routine" if margin >= margin_floor else "low-stakes guess")

    def feedback(self, decision_id: str, outcome: dict) -> None:
        """Attach an observed outcome to a logged decision (training signal)."""
        if self.log_path:
            with self.log_path.open("a") as f:
                f.write(json.dumps({"feedback": decision_id, "t": time.time(), **outcome}) + "\n")


def build(cfg: dict, llm=None, log_path: Path | None = None) -> Jev:
    jc = cfg["jev"]
    kind = jc["backend"]
    if kind == "lexical" or (kind in ("llm", "cascade") and llm is None):
        backend: Backend = LexicalJev()
    elif kind == "cascade-http":
        backend = CascadeJev(LexicalJev(), HttpJev(jc["url"]), tuple(jc["uncertain_band"]))
    elif kind == "llm":
        backend = LLMJev(llm, jc.get("llm_timeout", 20))
    elif kind == "http":
        backend = HttpJev(jc["url"])
    else:
        backend = CascadeJev(LexicalJev(), LLMJev(llm, jc.get("llm_timeout", 20)), tuple(jc["uncertain_band"]))
    return Jev(backend, log_path, tuning_path=log_path.parent / "jev_tuning.json" if log_path else None)
