"""The JEV decision engine.

Every adaptive choice in the harness goes through one of two calls:

* ``activate`` - multi-label: an independent probability per option
  ("which of these SOP categories will the task touch?").
* ``choose``   - multiple choice: a distribution that sums to 1
  ("route this task: direct / answer / agent / clarify").

Decisions are made by a typed decision model, never by an LLM: a Jev-compatible "System One"
model returns calibrated probabilities over the options in one forward pass, no generated text.

* ``SystemOneJev`` - **Laya** (open-source, self-hosted, the default) or TypeSafe AI's hosted Jev.
                     ``choose`` is a Choice question, ``activate`` a batch of Noul questions.
* ``LexicalJev``   - offline token-overlap scorer. Only a last resort when no decision model can
                     be reached (and for tests); the harness warns while it is in use.

Every decision is logged to ``decisions.jsonl`` together with its later
outcome, which is the training data for fine-tuning Laya on your own decisions.
"""

from __future__ import annotations

import json
import math
import os
import re
import time
import urllib.error
import urllib.request
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field, replace
from pathlib import Path


@dataclass
class Option:
    id: str
    text: str                   # keyword cues: what the offline lexical scorer matches against
    prior: float = 1.0          # multiplicative prior, e.g. from SOP success rate
    desc: str = ""              # one plain sentence saying what choosing this option means: what a
                                # decision model (Laya / TypeSafe Jev) reads as the option's criterion

    @property
    def criterion(self) -> str:
        return (self.desc or self.text)[:500] or self.id

    def criterion_for(self, style: str) -> str:
        """``keywords`` (the default: measured as accurate or better with Laya and Kev by
        ``rameness jev bench``) or ``sentences``."""
        return (self.text[:500] or self.id) if style == "keywords" else self.criterion


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


# Characters of state a model can read after the question and options take their share
# (~4 chars per token). Laya: 512-token window on the base checkpoint, 1024 on typed-decisions /
# multilingual (laya-serve doesn't expose multilingual's 8k mode). Kev and TypeSafe read far more,
# but a decision should still only see what it needs (see ``fit`` and the call sites).
STATE_BUDGET = {"laya": 1200, "laya-1024": 3000, "kev": 8000, "typesafe": 8000}


def state_budget(preset: str, model: str | None) -> int:
    if preset == "laya":
        return STATE_BUDGET["laya-1024"] if model and any(k in model for k in ("typed", "multilingual")) \
            else STATE_BUDGET["laya"]
    return STATE_BUDGET.get(preset, 3000)


def fit(text: str, limit: int, head: float = 0.35) -> str:
    """Trim to ``limit`` characters keeping the start (what the thing is) and the end (where
    errors, results and conclusions are), with a marker where the middle was cut."""
    if len(text) <= limit:
        return text
    h = int(limit * head)
    return text[:h] + "\n[...]\n" + text[-(limit - h - 7):]


class SystemOneJev(Backend):
    """Client for the Jev "System One" API: typed decisions, never text.

    Two servers speak it:

    * **Laya** (``laya``, the default): Convai Innovations' open-source (Apache-2.0) Jev-compatible
      decision model, self-hosted: ``pip install "laya[serve]" && laya-serve`` serves it on
      ``http://127.0.0.1:8000/v1/systemone``. ~35 ms per decision on a T4, no per-call cost.
    * **TypeSafe AI's Jev** (``typesafe``): the hosted original, ``TYPESAFE_API_KEY``.

    ``choose`` asks one **Choice** question whose criteria are the options and uses the
    per-option probabilities. ``activate`` asks one **Noul** question per option (does this
    option apply?), batched into one request, and uses each Noul probability. The task goes in
    ``state``, with the harness context alongside it.

    When the server can't be reached (not running, no key, 429/529 after retries) the call falls
    back to ``fallback`` so the harness keeps working; ``failures`` counts those calls.
    """

    PRESETS = {
        "kev": {"url": "http://127.0.0.1:8008/v1/systemone", "model": "kev", "key_env": "KEV_API_KEY"},
        "laya": {"url": "http://127.0.0.1:8000/v1/systemone", "model": "typed-decisions", "key_env": "LAYA_API_KEY"},
        "typesafe": {"url": "https://api.typesafe.ai/v1/systemone", "model": "jev-latest", "key_env": "TYPESAFE_API_KEY"},
    }
    BATCH = 32                       # Noul questions per request

    def __init__(self, preset: str = "laya", url: str | None = None, model: str | None = None,
                 api_key: str | None = None, timeout: float = 10.0, retries: int = 2,
                 fallback: Backend | None = None, option_text: str = "keywords",
                 max_state_chars: int | None = None):
        ps = self.PRESETS[preset]
        self.name = preset
        self.url, self.model = url or ps["url"], model or ps["model"]
        self.api_key = api_key or os.environ.get(ps["key_env"])
        self.timeout, self.retries, self.option_text = timeout, retries, option_text
        self.max_state_chars = max_state_chars or state_budget(preset, self.model)
        self.fallback = fallback or LexicalJev()
        self.failures, self.last_error = 0, ""
        if preset == "typesafe" and not self.api_key:
            self.last_error = "TYPESAFE_API_KEY is not set"

    def _state(self, query: str, context: str) -> str:
        """The decision's own summary (plus the call's specific context, if it passed one),
        fitted to the model's window: models truncate silently, so we trim deliberately."""
        text = f"{query}\n\n{context}" if context else query
        return fit(text, self.max_state_chars)

    def _ask(self, state, questions: dict) -> dict:
        if self.name == "typesafe" and not self.api_key:
            raise RuntimeError(self.last_error)
        body = json.dumps({**({"model": self.model} if self.model else {}), "state": state,
                           "questions": questions}).encode()
        headers = {"Content-Type": "application/json", **({"Authorization": f"Bearer {self.api_key}"} if self.api_key else {})}
        req = urllib.request.Request(self.url, data=body, headers=headers)
        for attempt in range(self.retries + 1):
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as r:
                    return json.loads(r.read())["answers"]
            except urllib.error.HTTPError as e:
                if e.code not in (429, 529) or attempt == self.retries:
                    raise
                time.sleep(0.5 * 2 ** attempt)     # overloaded / rate limited: back off and retry
        raise RuntimeError("unreachable")

    def _fall(self, e: Exception, mode: str, *args) -> list[float]:
        self.failures += 1
        self.last_error = f"{type(e).__name__}: {e}"
        return getattr(self.fallback, mode)(*args)

    @staticmethod
    def _choice_probs(ans: dict, options: list[Option]) -> list[float]:
        """Per-option probabilities, keyed by option id or by option index (servers differ);
        with only a choice + confidence, the rest of the mass is spread over the other options."""
        probs = ans.get("probabilities") or {}
        if probs and all(o.id in probs for o in options):
            return [float(probs[o.id]) for o in options]
        if probs and all(str(i) in probs for i in range(len(options))):
            return [float(probs[str(i)]) for i in range(len(options))]
        conf = float(ans.get("confidence", 1.0))
        rest = (1 - conf) / max(1, len(options) - 1)
        return [conf if o.id == ans.get("choice") else rest for o in options]

    def activate(self, question, query, options, context=""):
        out: list[float] = []
        try:
            for i in range(0, len(options), self.BATCH):
                chunk = options[i:i + self.BATCH]
                qs = {f"o{j}": {"type": "noul",
                                "instructions": f"{question} Does this option apply: {o.id}?",
                                "criteria": {"true": f"It applies: {o.criterion_for(self.option_text)}",
                                             "false": "It does not apply."}}
                      for j, o in enumerate(chunk)}
                ans = self._ask(self._state(query, context), qs)
                out += [float(ans[f"o{j}"]["noul"]) for j in range(len(chunk))]
            return out
        except Exception as e:
            return self._fall(e, "activate", question, query, options, context)

    def choose(self, question, query, options, context=""):
        try:
            ans = self._ask(self._state(query, context), {"decision": {
                "type": "choice", "instructions": question,
                "criteria": {o.id: o.criterion_for(self.option_text) for o in options}}})["decision"]
            raw = self._choice_probs(ans, options)
            s = sum(raw)
            return [1 / len(raw)] * len(raw) if s <= 0 else [r / s for r in raw]
        except Exception as e:
            return self._fall(e, "choose", question, query, options, context)


def laya_running(url: str = SystemOneJev.PRESETS["laya"]["url"], timeout: float = 1.0) -> bool:
    """Is a Jev-compatible server (Laya) answering at ``url``? Asks it one real Noul question:
    other servers on the same port (vLLM also defaults to :8000) don't speak /v1/systemone."""
    body = json.dumps({"state": "ping",                   # no model name: servers reject names they don't serve
                       "questions": {"up": {"type": "noul", "instructions": "Is this a ping?"}}}).encode()
    try:
        req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return "noul" in json.loads(r.read())["answers"]["up"]
    except Exception:
        return False


class LayaLocalJev(SystemOneJev):
    """Laya loaded in-process with the ``laya`` Python package: no server, and the token budgets
    are ours to set per call (``agent.predict(..., max_len=, head_max_len=)``).

    ``max_len`` is the whole window (question + options + state); ``head_max_len`` is the part
    reserved for the question and its options. ``None`` keeps each checkpoint's trained sizes
    (English 512/192, typed-decisions and multilingual 1024/256), which measured best: widening
    typed-decisions to 2048/512 dropped the 36-option category decision from 0.71 to 0.43
    (``rameness jev bench``). The model loads once per process, on the first decision.
    """

    TRAINED = {"english": (512, 192), "typed-decisions": (1024, 256), "multilingual": (1024, 256)}

    _agents: dict = {}               # (checkpoint, device) -> loaded agent, shared within the process

    def __init__(self, checkpoint: str | None = "typed-decisions", max_len: int | None = None,
                 head_max_len: int | None = None, device: str | None = None, fallback: Backend | None = None,
                 option_text: str = "keywords", max_state_chars: int | None = None):
        ml, hl = self.TRAINED.get(checkpoint or "english", (1024, 256))
        room = (max_len or ml) - (head_max_len or hl)                 # tokens left for the state
        super().__init__("laya", url="in-process", model=checkpoint or "english", fallback=fallback,
                         option_text=option_text, max_state_chars=max_state_chars or room * 4)
        self.name = "laya-local"
        self.checkpoint, self.device = checkpoint, device
        self.max_len, self.head_max_len = max_len, head_max_len

    def _agent(self):
        key = (self.checkpoint, self.device)
        if key not in self._agents:
            import laya                                    # optional dependency: pip install laya
            sub = None if self.checkpoint in (None, "", "laya", "english") else self.checkpoint
            self._agents[key] = laya.load("convaiinnovations/laya", subfolder=sub, device=self.device)
        return self._agents[key]

    def _ask(self, state, questions: dict) -> dict:
        return self._agent().predict(state, questions, max_len=self.max_len,
                                     head_max_len=self.head_max_len)["answers"]


def laya_importable() -> bool:
    import importlib.util
    return importlib.util.find_spec("laya") is not None


# --------------------------------------------------------------------------- comfort gate

SIGNIFICANCE = [
    Option("routine", "routine reversible cheap internal assignment scheduling which model environment runtime slot "
                      "retry wait nudge research draft read only local branch worktree small fix test",
           desc="A routine, reversible, low-cost choice (scheduling, which model or environment, retries, drafts) that is safe to decide automatically."),
    Option("significant", "irreversible destructive delete remove drop overwrite production deploy release merge main "
                          "publish push costly expensive paid money budget credentials secrets security legal privacy "
                          "customer data external email send abandon cancel scope priority preference",
           desc="A significant choice the user should make: irreversible or destructive, costly, external (publishing, sending, merging to main, production), touching secrets or private data, or a matter of preference, priority or scope."),
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
        return [replace(o, text=f"{o.text} {cues.get(f'{question}::{o.id}', '')}".strip(),
                        prior=o.prior * float(w.get(f"{question}::{o.id}", 1.0))) for o in options]

    def _record(self, question, query, options, probs, kind, context="") -> Decision:
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
                                    "option_descs": {o.id: o.criterion for o in options},
                                    "context": context[:2000],
                                    "probs": d.probs, "backend": d.backend}) + "\n")
        return d

    def _telemetry(self, question: str, kind: str, d: Decision, n_opts: int, t0: float, fails0: int, state: str) -> None:
        from . import telemetry
        if not telemetry.url():
            return
        top = d.top(2)
        b = self.backend
        lab = {"backend": b.name, "model": getattr(b, "model", b.name), "kind": kind, "question": question[:80]}
        telemetry.emit([
            ("rameness_jev_decision_seconds", round(time.time() - t0, 4), lab),
            ("rameness_jev_decision_confidence", top[0][1] if top else None, lab),
            ("rameness_jev_decision_margin", (top[0][1] - top[1][1]) if len(top) > 1 else None, lab),
            ("rameness_jev_decision_options", n_opts, lab),
            ("rameness_jev_decision_state_chars", len(state), lab),
            ("rameness_jev_decision_fallback", int(getattr(b, "failures", 0) > fails0), lab),
        ])

    def activate(self, question: str, query: str, options: list[Option], context: str = "") -> Decision:
        if not options:
            return Decision("-", question, {}, self.backend.name)
        t0, f0 = time.time(), getattr(self.backend, "failures", 0)
        options = self._tune(question, options)
        raw = self.backend.activate(question, query, options, context)
        probs = [min(0.999, max(0.0, p * o.prior)) for p, o in zip(raw, options)]
        d = self._record(question, query, options, probs, "activate", context)
        self._telemetry(question, "activate", d, len(options), t0, f0, query + context)
        return d

    def choose(self, question: str, query: str, options: list[Option], context: str = "") -> Decision:
        t0, f0 = time.time(), getattr(self.backend, "failures", 0)
        options = self._tune(question, options)
        raw = self.backend.choose(question, query, options, context)
        raw = [p * o.prior for p, o in zip(raw, options)]
        s = sum(raw) or 1.0
        d = self._record(question, query, options, [p / s for p in raw], "choose", context)
        self._telemetry(question, "choose", d, len(options), t0, f0, query + context)
        return d

    def yes(self, question: str, query: str, yes_cues: str, no_cues: str = "", context: str = "",
            yes_desc: str = "Yes.", no_desc: str = "No.") -> float:
        """Probability of 'yes' for a binary question."""
        d = self.choose(question, query, [Option("yes", yes_cues, desc=yes_desc),
                                          Option("no", no_cues or "none unspecified", desc=no_desc)], context)
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
                         f"{question} {fit(query, 400)} options: {opts_text} leaning: {top[0][0]}", SIGNIFICANCE)
        sig = sd.probs["significant"]
        if sig >= sig_threshold:
            return Comfort(sig, conf, margin, True, "significant")
        # a dead 50/50 on significance means "no evidence either way", not "risky": don't bother the user
        if margin < margin_floor and sig >= uncertain_sig and abs(sig - 0.5) > 0.02:
            return Comfort(sig, conf, margin, True, "uncertain")
        return Comfort(sig, conf, margin, False, "routine" if margin >= margin_floor else "low-stakes guess")

    def feedback(self, decision_id: str, outcome: dict) -> None:
        """Attach an observed outcome to a logged decision (training signal)."""
        if self.log_path:
            with self.log_path.open("a") as f:
                f.write(json.dumps({"feedback": decision_id, "t": time.time(), **outcome}) + "\n")


LOCAL_ORDER = ["kev", "laya", "laya-local"]   # servers first (one shared copy), in-process last


def resolve_backend(jc: dict) -> str:
    """``auto``: the first local decision model whose server answers (``local_order``), else
    TypeSafe's hosted Jev if ``TYPESAFE_API_KEY`` is set, else the offline lexical scorer
    (a degraded mode: the harness warns about it)."""
    kind = jc.get("backend", "auto")
    if kind != "auto":
        return kind
    for local in jc.get("local_order") or LOCAL_ORDER:
        if local == "laya-local":
            if laya_importable():
                return local
        elif laya_running(jc.get(f"{local}_url") or SystemOneJev.PRESETS[local]["url"]):
            return local
    return "typesafe" if os.environ.get("TYPESAFE_API_KEY") else "lexical"


def build(cfg: dict, llm=None, log_path: Path | None = None) -> Jev:
    """``llm`` is accepted for call-site compatibility and ignored: decisions never use an LLM."""
    jc = cfg["jev"]
    kind = resolve_backend(jc)
    style = jc.get("option_text", "keywords")
    budget = jc.get("max_state_chars")
    if kind == "laya-local":
        fallback = SystemOneJev("typesafe", url=jc.get("typesafe_url"), model=jc.get("typesafe_model"),
                                option_text=style, max_state_chars=budget) \
            if os.environ.get("TYPESAFE_API_KEY") else LexicalJev()
        backend: Backend = LayaLocalJev(jc.get("laya_model") or "typed-decisions", jc.get("laya_max_len"),
                                        jc.get("laya_head_max_len"), jc.get("laya_device"),
                                        fallback=fallback, option_text=style, max_state_chars=budget)
    elif kind in ("laya", "kev"):       # a local model; TypeSafe covers an outage when a key is set
        fallback = SystemOneJev("typesafe", url=jc.get("typesafe_url"), model=jc.get("typesafe_model"),
                                option_text=style, max_state_chars=budget) \
            if os.environ.get("TYPESAFE_API_KEY") else LexicalJev()
        backend = SystemOneJev(kind, url=jc.get(f"{kind}_url"), model=jc.get(f"{kind}_model"),
                               fallback=fallback, option_text=style, max_state_chars=budget)
    elif kind == "typesafe":
        backend = SystemOneJev("typesafe", url=jc.get("typesafe_url"), model=jc.get("typesafe_model"),
                               option_text=style, max_state_chars=budget)
    elif kind == "lexical":
        backend = LexicalJev()
    else:
        raise ValueError(f"unknown jev.backend {kind!r}: use auto, laya-local, laya, kev, typesafe or lexical")
    return Jev(backend, log_path, tuning_path=log_path.parent / "jev_tuning.json" if log_path else None)
