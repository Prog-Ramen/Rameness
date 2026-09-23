"""Cycle mode: a draft (or existing work), then N cycles - or open-ended in godmode.

Two cycle shapes, picked by the category JEV chooses each cycle:

* refine (Discovery, Build, Quality, Interface, Non-functional, Delivery):
  research agent proposes options -> JEV selects -> improvement agent implements
* test (Testing: unit, simulated user testing, interface, accessibility, API contract,
  exploratory, regression, performance, security, acceptance, compatibility):
  tester agent tests and reports findings -> JEV selects which to fix -> fixer agent fixes
  and adds regression tests

After each cycle in godmode, JEV decides whether the work has converged (for testing:
whether further testing is needed) and which kind of cycle comes next. When a requested
count is reached, JEV decides whether *more* is warranted: autopilot may extend within
``autopilot_extra_cycles``; balanced / restrictive put the recommendation to the director.

Intermediate branches are not merged; the final branch goes through the normal
merge policy once, so a review-mode director approves the whole program at the end.
"""

from __future__ import annotations

import json
import re
import time
import uuid

from ..jev import Option
from ..llm import parse_json

# The taxonomy doubles as the set of "next step" categories JEV can choose for any task.
TAXONOMY: list[dict] = [
    # discovery
    {"id": "research", "group": "Discovery", "label": "Research & discovery",
     "cues": "research prior art competitors alternatives libraries best practices explore unknowns",
     "focus": "what comparable products and libraries do, which proven patterns and components apply, and what we are missing"},
    {"id": "requirements", "group": "Discovery", "label": "Requirements & scope",
     "cues": "requirements scope user stories acceptance criteria edge cases missing features expectations",
     "focus": "gaps between what the goal implies and what exists: user stories, acceptance criteria, edge cases"},
    {"id": "architecture", "group": "Discovery", "label": "Architecture & design",
     "cues": "architecture design structure modules boundaries data flow coupling tech stack",
     "focus": "structure, module boundaries, data flow and technology choices that limit the next steps"},
    # build
    {"id": "features", "group": "Build", "label": "Feature development",
     "cues": "feature functionality capability missing incomplete flow add build implement",
     "focus": "missing or incomplete functionality and end-to-end flows"},
    {"id": "integration", "group": "Build", "label": "Integrations",
     "cues": "integration api third party service webhook oauth provider sync import export",
     "focus": "connections to external services, APIs and data sources"},
    {"id": "data", "group": "Build", "label": "Data & schema",
     "cues": "data schema model database migration validation storage persistence",
     "focus": "data models, schema, validation, migrations and persistence"},
    # quality
    {"id": "correctness", "group": "Quality", "label": "Correctness & bugs",
     "cues": "bug wrong incorrect crash broken fix defect regression correctness",
     "focus": "bugs, wrong behaviour and crashes"},
    {"id": "reliability", "group": "Quality", "label": "Reliability & error handling",
     "cues": "reliability error handling retry timeout resilience recovery robust failure",
     "focus": "failure modes, error handling, retries, timeouts and recovery"},
    {"id": "refactor", "group": "Quality", "label": "Refactoring & maintainability",
     "cues": "refactor clean maintainability duplication complexity readability tech debt",
     "focus": "duplication, complexity and structure that make the code hard to change"},
    # testing (cycle shape: test -> triage findings -> fix)
    {"id": "unit_tests", "group": "Testing", "mode": "test", "label": "Unit & integration tests",
     "cues": "unit integration tests coverage automated test suite functions modules",
     "focus": "write and run automated unit and integration tests for the critical paths; keep the tests"},
    {"id": "user_testing", "group": "Testing", "mode": "test", "label": "User testing (simulated personas)",
     "cues": "user testing personas usability walkthrough journeys novice power user confusion",
     "focus": "act as three personas (a first-time user, a power user, a careless user); attempt the main "
              "journeys end to end and record every failure, confusion and friction point"},
    {"id": "interface_testing", "group": "Testing", "mode": "test", "label": "Interface testing",
     "cues": "interface testing ui screens pages cli commands endpoints states forms visual interaction",
     "focus": "exercise every screen, command or endpoint: empty, loading, error and edge states, inputs, "
              "navigation, responsive layouts (capture screenshots if a headless browser is available)"},
    {"id": "accessibility_testing", "group": "Testing", "mode": "test", "label": "Accessibility testing",
     "cues": "accessibility testing a11y keyboard screen reader contrast aria wcag audit",
     "focus": "keyboard-only use, focus order, labels/ARIA, contrast and screen-reader output"},
    {"id": "api_contract_testing", "group": "Testing", "mode": "test", "label": "API contract testing",
     "cues": "api contract testing schema endpoints status codes validation responses compatibility",
     "focus": "requests and responses against the documented contract: schemas, status codes, errors"},
    {"id": "exploratory_testing", "group": "Testing", "mode": "test", "label": "Exploratory & edge-case testing",
     "cues": "exploratory edge cases fuzz boundary invalid input adversarial unexpected weird",
     "focus": "boundaries, invalid and adversarial inputs, unusual sequences and concurrency"},
    {"id": "regression_testing", "group": "Testing", "mode": "test", "label": "Regression testing",
     "cues": "regression testing rerun previous fixes still work broke again verify stable",
     "focus": "re-run everything and confirm that earlier fixes and features still hold"},
    {"id": "performance_testing", "group": "Testing", "mode": "test", "label": "Performance & load testing",
     "cues": "performance load testing benchmark latency throughput stress memory",
     "focus": "measure latency, throughput and memory under realistic and peak load"},
    {"id": "security_testing", "group": "Testing", "mode": "test", "label": "Security testing",
     "cues": "security testing penetration authz bypass injection xss csrf secrets probe",
     "focus": "attack your own code: auth/authz bypass, injection, XSS/CSRF, secrets exposure"},
    {"id": "acceptance_testing", "group": "Testing", "mode": "test", "label": "Acceptance testing",
     "cues": "acceptance testing requirements goal criteria does it meet spec done definition",
     "focus": "check the work against the original goal and every stated or implied requirement"},
    {"id": "compatibility_testing", "group": "Testing", "mode": "test", "label": "Compatibility testing",
     "cues": "compatibility browsers platforms os versions devices environments",
     "focus": "supported platforms, browsers, runtimes and versions"},
    # interface
    {"id": "ui", "group": "Interface", "label": "UI & visual design",
     "cues": "ui visual design layout typography color spacing polish look aesthetic responsive",
     "focus": "visual hierarchy, layout, typography, spacing, colour, responsiveness and polish"},
    {"id": "ux", "group": "Interface", "label": "UX & flows",
     "cues": "ux usability flow friction onboarding navigation interaction feedback empty states",
     "focus": "friction in user flows, feedback, navigation, empty and error states"},
    {"id": "accessibility", "group": "Interface", "label": "Accessibility",
     "cues": "accessibility a11y screen reader contrast keyboard aria focus wcag",
     "focus": "keyboard use, screen readers, contrast and WCAG issues"},
    {"id": "api_design", "group": "Interface", "label": "API, CLI & developer interface",
     "cues": "api design endpoint cli sdk interface ergonomics naming contract developer",
     "focus": "the developer-facing surface: API/CLI/SDK ergonomics, naming, contracts and errors"},
    {"id": "content", "group": "Interface", "label": "Content, copy & formats",
     "cues": "copy content wording text format output report document microcopy tone",
     "focus": "wording, messages, output formats and the documents the work produces"},
    {"id": "i18n", "group": "Interface", "label": "Internationalization",
     "cues": "i18n internationalization localization translation locale language rtl",
     "focus": "hard-coded strings, locales, formats and translation readiness"},
    # non-functional
    {"id": "performance", "group": "Non-functional", "label": "Performance",
     "cues": "performance speed slow latency memory cpu optimize fast benchmark profile",
     "focus": "latency, throughput, memory and hot paths - measure before and after"},
    {"id": "security", "group": "Non-functional", "label": "Security",
     "cues": "security vulnerability auth authorization injection xss secrets hardening owasp",
     "focus": "authentication, authorization, injection, secrets handling and other OWASP-class issues"},
    {"id": "privacy", "group": "Non-functional", "label": "Privacy & compliance",
     "cues": "privacy compliance gdpr pii personal data retention consent audit legal",
     "focus": "personal data handling, retention, consent and compliance obligations"},
    {"id": "cost", "group": "Non-functional", "label": "Cost efficiency",
     "cues": "cost expensive tokens cloud bill resource usage efficiency cheaper",
     "focus": "what drives running cost and how to cut it without losing quality"},
    {"id": "scalability", "group": "Non-functional", "label": "Scalability",
     "cues": "scale scalability load concurrency throughput growth bottleneck",
     "focus": "bottlenecks under growth: load, concurrency and data volume"},
    {"id": "observability", "group": "Non-functional", "label": "Observability",
     "cues": "observability logging metrics tracing monitoring alerts debugging telemetry",
     "focus": "logging, metrics, tracing and alerting needed to operate and debug it"},
    # delivery
    {"id": "devops", "group": "Delivery", "label": "CI/CD & deployment",
     "cues": "deploy deployment ci cd pipeline docker infrastructure release environment",
     "focus": "build, CI, deployment and environment setup"},
    {"id": "packaging", "group": "Delivery", "label": "Packaging & release",
     "cues": "package release version installer distribution publish changelog",
     "focus": "packaging, versioning, installers and release process"},
    {"id": "documentation", "group": "Delivery", "label": "Documentation",
     "cues": "documentation docs readme guide tutorial reference examples",
     "focus": "README, guides, reference docs and examples"},
    {"id": "onboarding", "group": "Delivery", "label": "Setup & developer experience",
     "cues": "setup onboarding developer experience dx getting started install config",
     "focus": "how quickly a newcomer can install, configure and contribute"},
]
BY_ID = {c["id"]: c for c in TAXONOMY}
GROUPS = sorted({c["group"] for c in TAXONOMY})


def expand_categories(cats: list[str] | None) -> list[str]:
    """Accept category ids and group names ("testing", "interface")."""
    out = []
    for c in cats or []:
        g = [x["id"] for x in TAXONOMY if x["group"].lower().replace("-", "_") == c.lower().replace("-", "_")]
        out += g or [c]
    return list(dict.fromkeys(out))

CATEGORY_Q = "Which kind of refinement should the next cycle focus on?"
OPTIONS_Q = "Which proposed improvements should this cycle implement?"
FIXES_Q = "Which test findings should this cycle fix?"
ADEQUACY_Q = "Are these the right kinds of tests, and do they cover the edge cases?"
ADEQUACY = [
    Option("adequate", "covers boundaries invalid input unicode empty timeout failure modes concurrency personas "
                       "error handling thorough meaningful assertions many tests"),
    Option("gaps", "only happy path few tests missing shallow trivial wrong kind untested skipped superficial "
                   "single test basic smoke"),
]
CONVERGE_Q = "Has this work converged, or is another refinement cycle worth it?"
CATEGORY_TAG_Q = "Which category of work is this task?"
EXTEND_Q = "The requested cycles are done. Is further work of this kind needed?"
SEVERITY_IMPACT = {"critical": "high", "high": "high", "medium": "medium", "low": "low", "info": "low"}


CONVERGE_OPTIONS = [
    Option("continue", "high impact options remain new issues found significant improvements important gaps "
                       "critical high failures unfixed untested regressions retest needed"),
    Option("stop", "converged diminishing returns only low impact minor polish nothing new repeated suggestions "
                   "done all passed no findings stable"),
]


def taxonomy_options(ids: list[str] | None = None, recent: list[str] | None = None) -> list[Option]:
    recent = recent or []
    out = []
    for c in TAXONOMY:
        if ids and c["id"] not in ids:
            continue
        # steer toward variety: just-done kinds are damped; kinds the director asked for but that
        # haven't had a cycle yet are boosted
        prior = 0.35 if recent[-1:] == [c["id"]] else 0.65 if c["id"] in recent[-3:] else 1.0
        if ids and len(ids) > 1 and c["id"] not in recent:
            prior *= 1.6
        out.append(Option(c["id"], f"{c['label']} {c['cues']}", prior))
    return out


def parse_options(text: str, key: str = "options") -> list[dict]:
    """Options (or test findings) from a report: a JSON block, else bullet lines."""
    for m in reversed(list(re.finditer(r"```(?:json)?\s*(\{.*?\})\s*```", text or "", re.S))):
        try:
            d = json.loads(m.group(1))
            opts = d.get(key) or d.get("options") or d.get("findings") or []
            break
        except json.JSONDecodeError:
            continue
    else:
        try:
            d = parse_json(text)
            opts = d.get(key) or d.get("options") or d.get("findings") or []
        except Exception:
            opts = [{"title": l.strip("-*• ").strip()} for l in (text or "").splitlines()
                    if re.match(r"\s*([-*•]|\d+[.)])\s+\S", l)][:6]
    out = []
    for i, o in enumerate(opts[:8]):
        if isinstance(o, str):
            o = {"title": o}
        if not o.get("title"):
            continue
        sev = str(o.get("severity", "")).lower()
        slug = re.sub(r"[^\w\s.-]", "", str(o["title"]))[:48].strip() or f"option {i + 1}"
        out.append({"id": f"{i + 1}. {slug}", "title": str(o["title"])[:160],
                    "why": str(o.get("why") or o.get("details") or o.get("actual") or "")[:400],
                    "impact": SEVERITY_IMPACT.get(sev, o.get("impact", "medium")), "effort": o.get("effort", "medium"),
                    "risk": o.get("risk", "low"), **({"severity": sev} if sev else {})})
    return out


def option_prior(o: dict) -> float:
    return ({"high": 1.35, "medium": 1.0, "low": 0.7}.get(o["impact"], 1.0)
            * {"small": 1.15, "medium": 1.0, "large": 0.8}.get(o["effort"], 1.0)
            * {"low": 1.0, "medium": 0.9, "high": 0.7}.get(o["risk"], 1.0))


class Programs:
    """Drives every running cycle program forward, one phase per tick."""

    def __init__(self, fleet):
        self.f = fleet
        self.db.executescript("""
CREATE TABLE IF NOT EXISTS programs (id TEXT PRIMARY KEY, lead TEXT, goal TEXT, total INTEGER, done INTEGER,
  categories TEXT, phase TEXT, state TEXT, history TEXT, base_branch TEXT, status TEXT, created REAL, updated REAL);
""")

    @property
    def db(self):
        return self.f.store.db                     # per-thread connection (server threads + manager loop)

    # ---- storage

    def _row(self, r) -> dict:
        d = dict(r)
        for k in ("categories", "state", "history"):
            d[k] = json.loads(d[k] or ("[]" if k != "state" else "{}"))
        return d

    def get(self, pid: str) -> dict:
        return self._row(self.db.execute("SELECT * FROM programs WHERE id=?", (pid,)).fetchone())

    def all(self, status: str | None = None) -> list[dict]:
        q = "SELECT * FROM programs" + (" WHERE status=?" if status else "") + " ORDER BY created"
        return [self._row(r) for r in self.db.execute(q, (status,) if status else ())]

    def _save(self, p: dict) -> None:
        self.db.execute("UPDATE programs SET done=?, phase=?, state=?, history=?, base_branch=?, status=?, updated=? "
                        "WHERE id=?", (p["done"], p["phase"], json.dumps(p["state"]), json.dumps(p["history"]),
                                       p["base_branch"], p["status"], time.time(), p["id"]))

    # ---- create

    def create(self, goal: str, cycles: int | None, categories: list[str] | None = None,
               draft: bool = True, from_agent: str | None = None) -> dict:
        f = self.f
        if cycles is None and f.autonomy != "godmode":
            raise ValueError("open-ended cycling needs godmode (set autonomy to godmode; requires allow_godmode)")
        if cycles is not None:
            cycles = max(0, min(int(cycles), f.cfg["max_cycles"]))
        categories = expand_categories(categories)
        bad = [c for c in categories if c not in BY_ID]
        if bad:
            raise ValueError(f"unknown categories {bad}; choose ids {sorted(BY_ID)} or groups {GROUPS}")
        pid = "p-" + uuid.uuid4().hex[:6]
        label = f"{cycles} cycles" if cycles is not None else "godmode"
        lead = f.spawn(goal, role="lead", kind="deliver", title=f"{label} · {goal[:48]}",
                       meta={"program": pid, "program_lead": True, "role_decided": True})
        f.store.update_agent(lead["id"], status="running", runtime="manager", env="local")
        base = None
        state: dict = {"extensions": 0}
        phase = "choose"
        if from_agent:
            base = f.get(from_agent)["branch"]
        elif draft:
            d = f.spawn(goal, parent=lead["id"], kind="deliver", title=f"draft · {goal[:50]}", role_explicit=False,
                        meta={"program": pid, "size": "large"})
            state = {"draft": d["id"], "extensions": 0}
            phase = "draft"
        self.db.execute("INSERT INTO programs VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (pid, lead["id"], goal, cycles, 0, json.dumps(categories or []), phase, json.dumps(state),
                         "[]", base, "running", time.time(), time.time()))
        f.store.event(lead["id"], "program", f"{label}" + (f" focused on {categories}" if categories else ""))
        return self.get(pid)

    def stop(self, pid: str) -> None:
        p = self.get(pid)
        p["status"] = "stopping"
        self._save(p)

    # ---- the cycle state machine

    def tick(self) -> None:
        for p in self.all("running") + self.all("stopping"):
            try:
                self._step(p)
            except Exception as e:
                self.f.store.event(p["lead"], "error", f"program {p['id']}: {type(e).__name__}: {e}")

    def _agent_state(self, aid: str | None) -> str:
        a = self.f.store.agent(aid) if aid else None
        return a["status"] if a else "missing"

    def _step(self, p: dict) -> None:
        f, st = self.f, p["state"]
        if p["status"] == "stopping" and p["phase"] in ("choose", "research-wait"):
            return self._finish(p, "stopped by director")
        if p["phase"] == "draft":
            s = self._agent_state(st["draft"])
            if s == "done":
                p["base_branch"] = f.get(st["draft"])["branch"] or p["base_branch"]
                p["phase"] = "choose"
                f.store.event(p["lead"], "program", "draft finished; starting refinement cycles")
            elif s in ("failed", "retired", "missing"):
                return self._finish(p, "draft failed", ok=False)
            return self._save(p)

        if p["phase"] == "choose":
            n = p["done"] + 1
            if p["total"] is not None and p["done"] >= p["total"]:
                return self._maybe_extend(p)
            if p["total"] is None:
                if p["done"] >= (f.cfg["godmode"].get("max_cycles") or 10 ** 9):
                    return self._finish(p, "godmode safety cap reached")
                if p["done"] > 0:
                    last = "; ".join(f"#{h['n']} {h['category']}: {h.get('summary', '')[:160]}" for h in p["history"][-3:])
                    left = p["history"][-1].get("unchosen", [])
                    verdict, _ = f.decide(p["lead"], CONVERGE_Q, f"{p['goal'][:300]} recent: {last} "
                                          f"{self._outlook(p)} remaining ideas: "
                                          f"{'; '.join(o['title'] + ' impact ' + o['impact'] for o in left)[:600]}",
                                          CONVERGE_OPTIONS, "continue")
                    if verdict is None:
                        return
                    if verdict == "stop":
                        return self._finish(p, f"JEV judged the work converged after {p['done']} cycles")
            recent = [h["category"] for h in p["history"]]
            # previous *summaries* only: repeating category names here would make JEV pick them again
            query = (f"{p['goal'][:400]} cycle {n}. "
                     + " ".join(h.get("summary", "")[:100] for h in p["history"][-2:]))
            cat, _ = f.decide(p["lead"], CATEGORY_Q, query, taxonomy_options(p["categories"] or None, recent),
                              (p["categories"] or ["features"])[0])
            if cat is None:
                return
            c = BY_ID[cat]
            hist = "\n".join(f"- cycle {h['n']} ({h['category']}): {h.get('summary', '')[:200]}" for h in p["history"][-5:])
            if c.get("mode") == "test":
                r = f.spawn(
                    f"Testing cycle {n}{'/' + str(p['total']) if p['total'] else ''} for: {p['goal']}\n\n"
                    f"Test type: {c['label']} - {c['focus']}.\n"
                    f"Test the current state of the work in this checkout. Previous cycles:\n{hist or '- none'}\n\n"
                    "You may add automated tests (they will be kept) but do not fix product code - report instead. "
                    "End your reply with a JSON block:\n"
                    '```json\n{"summary": "...", "tests_run": 0, "tests_failed": 0, "findings": [{"title": "...", '
                    '"severity": "critical|high|medium|low", "details": "steps, expected, actual", '
                    '"effort": "small|medium|large"}], "tests_written": ["..."], "covered": ["areas / cases '
                    'exercised"], "not_covered": ["what you did not test"]}\n```\n'
                    "You choose and write the tests; pick the kinds that fit this test type and include edge cases, "
                    "error paths and invalid input, not just the happy path. An empty findings list means everything "
                    "you tried passed.",
                    parent=p["lead"], kind="deliver", title=f"cycle {n} · {c['label'].lower()}",
                    meta={"program": p["id"], "base_branch": p["base_branch"], "category": cat, "role_decided": True,
                          "fork_decided": True, "cycle_role": "tester"})
                p["state"] = {**p["state"], "n": n, "category": cat, "research": r["id"], "mode": "test"}
                p["phase"] = "research-wait"
                return self._save(p)
            r = f.spawn(
                f"Refinement cycle {n}{'/' + str(p['total']) if p['total'] else ''} for: {p['goal']}\n\n"
                f"Focus: {c['label']} - {c['focus']}.\n"
                f"The current state of the work is in this checkout. Previous cycles:\n{hist or '- none'}\n\n"
                "Study the current state. Do not modify files. Propose 3-6 concrete, high-value improvement options "
                "for this focus, most valuable first. End your reply with a JSON block:\n"
                '```json\n{"options": [{"title": "...", "why": "...", "impact": "high|medium|low", '
                '"effort": "small|medium|large", "risk": "low|medium|high"}]}\n```',
                parent=p["lead"], kind="research", title=f"cycle {n} · research {c['label'].lower()}",
                meta={"program": p["id"], "base_branch": p["base_branch"], "category": cat, "role_decided": True,
                      "fork_decided": True})
            p["state"] = {**p["state"], "n": n, "category": cat, "research": r["id"], "mode": "refine"}
            p["phase"] = "research-wait"
            return self._save(p)

        if p["phase"] == "research-wait":
            s = self._agent_state(st["research"])
            if s in ("failed", "retired", "missing"):
                return self._close_cycle(p, "research failed", [], [])
            if s != "done":
                return
            ra = f.get(st["research"])
            testing = st.get("mode") == "test"
            if testing and not st.get("coverage"):
                verdict = self._coverage(p, ra)
                if verdict is None:
                    return                              # the director is weighing in
                if verdict == "gaps" and not st.get("coverage_retry"):
                    st["coverage_retry"] = True
                    gaps = self._report(ra).get("not_covered") or []
                    f.prompt(ra["id"], sender="manager", text="Your manager reviewed your tests: they don't yet cover enough. Extend them to "
                                       "edge cases, error paths and invalid input" +
                                       (f" - in particular: {', '.join(map(str, gaps[:6]))}" if gaps else "") +
                                       ". Then reply with the full updated JSON report.")
                    f.store.event(p["lead"], "coverage", f"cycle {st['n']}: gaps - tester asked to extend coverage")
                    return self._save(p)
                st["coverage"] = verdict
                self._save(p)
            opts = parse_options(ra["result"] or "", "findings" if testing else "options")
            if testing and ra.get("branch"):
                p["base_branch"] = ra["branch"]        # keep the tests the tester added
            if not opts:
                return self._close_cycle(p, "all tests passed - no findings" if testing
                                         else "research produced no usable options", [], [])
            st["options"] = opts
            p["phase"] = "select"
            self._save(p)

        if p["phase"] == "select":
            opts = st["options"]
            testing = st.get("mode") == "test"
            chosen = f.decide_many(p["lead"], FIXES_Q if testing else OPTIONS_Q,
                                   f"{p['goal'][:300]} focus {BY_ID[st['category']]['label']}",
                                   [Option(o["id"], f"{o['title']} {o['why']} impact {o['impact']} effort {o['effort']} "
                                                    f"risk {o['risk']}", option_prior(o)) for o in opts],
                                   max_k=f.cfg["cycle_max_options"])
            if chosen is None:
                return
            picked = [o for o in opts if o["id"] in chosen]
            st["chosen"] = [o["id"] for o in picked]
            n = st["n"]
            if testing:
                body = (f"Testing cycle {n} for: {p['goal']}\n\nFix these findings from "
                        f"{BY_ID[st['category']]['label'].lower()}, and add a regression test for each where possible:\n"
                        + "\n".join(f"- [{o.get('severity', o['impact'])}] {o['title']}: {o['why']}" for o in picked)
                        + "\n\nRe-run the tests. Report what you fixed and anything still failing.")
                title = f"cycle {n} · fix {len(picked)} finding(s)"
            else:
                body = (f"Refinement cycle {n} for: {p['goal']}\n\nImplement these selected "
                        f"{BY_ID[st['category']]['label'].lower()} improvements on top of the existing work in this checkout:\n"
                        + "\n".join(f"- {o['title']}: {o['why']}" for o in picked)
                        + "\n\nVerify your changes (tests / build / run). Report what changed and anything you could not do.")
                title = f"cycle {n} · {BY_ID[st['category']]['label'].lower()}"
            imp = f.spawn(
                body, parent=p["lead"], kind="deliver", title=title,
                depends=[st["research"]],
                meta={"program": p["id"], "base_branch": p["base_branch"], "category": st["category"],
                      "role_decided": True, "fork_decided": True})
            st["improve"] = imp["id"]
            p["phase"] = "improve-wait"
            return self._save(p)

        if p["phase"] == "improve-wait":
            s = self._agent_state(st["improve"])
            if s in ("done", "failed", "retired", "missing"):
                a = f.store.agent(st["improve"]) or {}
                if s == "done" and a.get("branch"):
                    p["base_branch"] = a["branch"]
                summary = (a.get("result") or "").strip().splitlines()
                picked = [o for o in st["options"] if o["id"] in st.get("chosen", [])]
                rest = [o for o in st["options"] if o["id"] not in st.get("chosen", [])]
                return self._close_cycle(p, (summary[-1] if summary else "") if s == "done" else f"improvement {s}",
                                         picked, rest)

    def _close_cycle(self, p: dict, summary: str, chosen: list, unchosen: list) -> None:
        st = p["state"]
        dec = next((d for d in self.f.store.decisions(agent=p["lead"], limit=10)
                    if d["question"] in (OPTIONS_Q, FIXES_Q)), None)
        entry = {"n": st.get("n", p["done"] + 1), "category": st.get("category"), "mode": st.get("mode", "refine"),
                 "summary": summary[:500], "chosen": chosen, "unchosen": unchosen, "research": st.get("research"),
                 "improve": st.get("improve"), "branch": p["base_branch"], "decided_by": (dec or {}).get("gate"),
                 "t": time.time()}
        if st.get("mode") == "test":
            entry["coverage"] = st.get("coverage")
            sev = [o.get("severity", o["impact"]) for o in chosen + unchosen]
            entry["findings"] = {k: sev.count(k) for k in ("critical", "high", "medium", "low") if sev.count(k)}
            entry["unfixed"] = [o.get("severity", o["impact"]) for o in unchosen]
        p["history"].append(entry)
        p["done"] += 1
        p["phase"] = "choose"
        p["state"] = {"extensions": st.get("extensions", 0)}
        self.f.store.event(p["lead"], "cycle", f"cycle {p['done']} ({p['history'][-1]['category']}): {summary[:200]}")
        self._save(p)

    @staticmethod
    def _report(a: dict) -> dict:
        text = a.get("result") or ""
        for m in reversed(list(re.finditer(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S))):
            try:
                return json.loads(m.group(1))
            except json.JSONDecodeError:
                continue
        return {}

    def _coverage(self, p: dict, tester: dict) -> str | None:
        """JEV judges whether the model's tests are the right kind and cover edge cases."""
        r = self._report(tester)
        c = BY_ID[p["state"]["category"]]
        parts = [f"test type {c['label']}", f"{r.get('tests_run', '?')} tests run"]
        if r.get("tests_written"):
            parts.append("tests: " + ", ".join(map(str, r["tests_written"][:20])))
        if r.get("covered"):
            parts.append("covers " + ", ".join(map(str, r["covered"][:12])))
        if r.get("not_covered"):
            parts.append("missing " + ", ".join(map(str, r["not_covered"][:12])))
        if r.get("summary"):
            parts.append(str(r["summary"])[:300])
        q = ". ".join(parts)
        verdict, _ = self.f.decide(tester["id"], ADEQUACY_Q, q[:1500], ADEQUACY, "gaps")
        return verdict

    def _outlook(self, p: dict) -> str:
        """Plain-language state of the last cycles for convergence / extension decisions."""
        tests = [h for h in p["history"][-3:] if h.get("mode") == "test"]
        if not tests:
            return ""
        last = tests[-1]
        gaps = " coverage gaps edge cases untested" if last.get("coverage") == "gaps" else ""
        if not last.get("findings"):
            return "latest testing: all passed no findings stable" + (gaps or " coverage adequate")
        f = last["findings"]
        unfixed_bad = sum(1 for s in last.get("unfixed", []) if s in ("critical", "high"))
        return (f"latest testing found {', '.join(f'{v} {k}' for k, v in f.items())} findings"
                + (f"; {unfixed_bad} critical/high still unfixed" if unfixed_bad else "; severe ones fixed - retest to confirm fixes")
                + gaps)

    def _maybe_extend(self, p: dict) -> None:
        """Requested cycles are done: JEV decides whether more of this kind of work is needed."""
        f = self.f
        cap = f.cfg["autopilot_extra_cycles"]
        if p["state"].get("extensions", 0) >= cap and f.autonomy != "godmode":
            return self._finish(p, f"completed {p['done']} cycles")
        kinds = sorted({h["category"] for h in p["history"]})
        query = (f"{p['goal'][:300]} cycles done {p['done']} of kinds {kinds}. {self._outlook(p)}. "
                 f"last: {(p['history'][-1].get('summary', '') if p['history'] else '')[:200]}")
        opts = [Option("more", "critical high severity failures found unresolved untested areas "
                               "regressions flaky new issues important gaps remain retest confirm fixes"),
                Option("enough", "all passed no findings only low severity cosmetic coverage adequate "
                                 "stable converged diminishing returns")]
        if p["state"].get("extend_asked"):
            verdict, _ = f.decide(p["lead"], EXTEND_Q, query, opts, "enough", ask_director=True)
        else:
            # JEV's own view first; only a recommendation to do *more* is put to the director
            verdict, _ = f.decide(p["lead"], EXTEND_Q, query, opts, "enough", defer=False)
            if verdict == "more" and f.autonomy not in ("autopilot", "godmode"):
                p["state"]["extend_asked"] = True
                self._save(p)
                verdict, _ = f.decide(p["lead"], EXTEND_Q, query, opts, "enough", ask_director=True)
        if verdict is None:
            return
        if verdict == "more":
            p["total"] += 1
            p["state"]["extensions"] = p["state"].get("extensions", 0) + 1
            p["state"].pop("extend_asked", None)
            f.store.event(p["lead"], "program", f"extended to {p['total']} cycles ({self._outlook(p) or 'more needed'})")
            return self._save(p)
        return self._finish(p, f"completed {p['done']} cycles")

    def _finish(self, p: dict, why: str, ok: bool = True) -> None:
        f = self.f
        p["status"] = "done" if ok else "failed"
        p["phase"] = "finished"
        self._save(p)
        lead = f.get(p["lead"])
        report = f"{why}\n\n" + "\n".join(
            f"cycle {h['n']} · {h['category']}: " + ", ".join(o["title"] for o in h.get("chosen", [])) +
            (f" -> {h['summary'][:200]}" if h.get("summary") else "") for h in p["history"])
        f.store.update_agent(lead["id"], result=report, branch=p["base_branch"],
                             meta={**lead["meta"], "program_done": True, "finalized": True})
        f.store.set_status(lead["id"], "done" if ok else "failed", why)
        if ok and p["base_branch"]:
            lead = f.get(lead["id"])
            f._integrate({**lead, "meta": {**lead["meta"], "program": None}})
