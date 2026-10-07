"""A labelled benchmark of real harness decisions, for choosing and checking JEV's decision model.

Each case is one ``choose`` the harness really makes (same question, same options) with the
answer a careful human would give. ``rameness jev bench`` runs it against the configured
backend, so a new Laya checkpoint, a TypeSafe model or a change to the option sentences can be
measured before it is trusted. The cases are deliberately clear-cut: a model that misses them
will miss the ambiguous ones too.
"""

from __future__ import annotations

from dataclasses import replace

from .fleet.cycles import CATEGORY_TAG_Q, taxonomy_options
from .fleet.manager import FAILURE, INTAKE, QUESTION, STALL
from .jev import SIGNIFICANCE, Jev, Option
from .loopguard import ACTIONS
from .router import EFFORT, ROUTES, SCOPE_Q, SCOPES

SETS: dict[str, tuple[str, list[Option]]] = {
    "route": ("How should this task be executed?", ROUTES),
    "effort": ("How much reasoning does this task need?", EFFORT),
    "significance": ("Is this decision significant enough that the user should make it?", SIGNIFICANCE),
    "intake": ("How should the manager handle this request?", INTAKE),
    "failure": ("An agent failed. What should the manager do?", FAILURE),
    "stall": ("This agent has produced no output for a while. What now?", STALL),
    "question": ("Can the manager answer this agent's question, or must the director decide?", QUESTION),
    "loop": ("The agent may be stuck in a loop. What should happen?", ACTIONS),
    "category": (CATEGORY_TAG_Q, taxonomy_options()),        # 36 options: tests the option budget
    "scope": (SCOPE_Q, SCOPES),
}

CASES: list[tuple[str, str, str]] = [
    # (set, state, gold option)
    ("scope", "Build a Minecraft clone that runs in the browser, with no build step.", "open_ended"),
    ("scope", "make me a personal finance dashboard web app", "open_ended"),
    ("scope", "create a landing page for my bakery", "open_ended"),
    ("scope", "the login page returns 500 when the email has a plus sign; fix it and add a test", "specific"),
    ("scope", "convert this CSV of orders to JSON grouped by customer, with totals", "specific"),
    ("scope", "write a script that renames every .jpeg in photos/ to .jpg", "specific"),
    ("route", "what does HTTP status 418 mean?", "answer"),
    ("route", "explain the difference between a mutex and a semaphore", "answer"),
    ("route", "should I use postgres or sqlite for a small desktop app?", "answer"),
    ("route", "fix the failing unit test in the parser and run the suite", "agent"),
    ("route", "add a --verbose flag to the CLI and document it", "agent"),
    ("route", "refactor the payment module to remove the global state", "agent"),
    ("route", "run the csv summary SOP on sales.csv", "direct"),
    ("route", "show the git status of this repository", "direct"),
    ("effort", "list the files in the src directory", "low"),
    ("effort", "rename the variable tmp to buffer in utils.py", "low"),
    ("effort", "write a small script that converts a csv file to json", "medium"),
    ("effort", "add a unit test for the date parser", "medium"),
    ("effort", "design the sharding architecture for our event store", "high"),
    ("effort", "debug an intermittent race condition in the job scheduler", "high"),
    ("significance", "Which environment should this agent's tools run in? options: local ssh-gpu leaning: local", "routine"),
    ("significance", "Which model should run this task? options: haiku sonnet leaning: haiku", "routine"),
    ("significance", "Retry the flaky test run? options: retry abandon leaning: retry", "routine"),
    ("significance", "Merge this branch into main and deploy to production? options: merge keep leaning: merge", "significant"),
    ("significance", "Delete the old customer records table? options: delete keep leaning: delete", "significant"),
    ("significance", "Send the invoice email to the client? options: send hold leaning: send", "significant"),
    ("intake", "fix the typo in the README title", "delegate"),
    ("intake", "rename the function get_user to fetch_user", "delegate"),
    ("intake", "build a full-stack habit tracker app with a React frontend, an API backend and a database", "decompose"),
    ("intake", "migrate the whole monolith to microservices across all modules", "decompose"),
    ("intake", "investigate why our API latency doubled last week and report the causes", "research"),
    ("intake", "compare three vector databases and recommend one", "research"),
    ("intake", "make it better", "clarify"),
    ("failure", "task: run tests\nError: connection timed out while downloading dependencies", "retry"),
    ("failure", "task: fix the parser\nError: 12 tests still failing after 40 turns, the agent gave up", "stronger"),
    ("failure", "task: deploy the staging stack\nError: permission denied: missing AWS credentials", "escalate"),
    ("failure", "task: port the tool to Windows\nError: the project was cancelled, the feature is obsolete", "abandon"),
    ("stall", "task: build the docker image\nStep 14/32: RUN npm install ... downloading packages", "wait"),
    ("stall", "task: write the migration\nWaiting for your input: which database should I target? >", "nudge"),
    ("stall", "task: run the server\nprocess not responding for 20 minutes, no CPU usage, deadlock suspected", "restart"),
    ("question", "Which test framework does this repo use, pytest or unittest?", "answer"),
    ("question", "Where should I put the new helper module?", "answer"),
    ("question", "May I delete the production database backups to free space?", "escalate"),
    ("question", "Should we spend budget on a paid API plan for this feature?", "escalate"),
    ("loop", "same tool call repeated 6 times with identical error: file not found", "reorient"),
    ("loop", "output repeats the phrase 'the the the' over and over, garbled text", "reset"),
    ("loop", "installing dependencies, each step prints new progress", "continue"),
    ("category", "write unit tests for the invoice calculator", "unit_tests"),
    ("category", "the checkout page takes 9 seconds to load, make it faster", "performance"),
    ("category", "add a GitHub Actions pipeline that deploys on merge", "devops"),
    ("category", "translate the app into German and French", "i18n"),
    ("category", "write the README and API reference docs", "documentation"),
    ("category", "check the login form for SQL injection and XSS", "security_testing"),
    ("category", "add a database migration for the new orders table", "data"),
]


# The same decisions in casual wording that avoids the options' keyword cues: what a model has
# to get right when a user doesn't phrase things the way the cues expect.
PARAPHRASED: list[tuple[str, str, str]] = [
    ("route", "hey, quick one: is 0.0.0.0 the same thing as localhost?", "answer"),
    ("route", "the build is borked since this morning, sort it out", "agent"),
    ("route", "gimme the numbers from sales.csv using the usual procedure", "direct"),
    ("effort", "what's in src/?", "low"),
    ("effort", "orders sometimes vanish under heavy load and nobody knows why", "high"),
    ("significance", "Wipe last year's billing tables? options: wipe keep leaning: wipe", "significant"),
    ("significance", "Put this on the cheaper laptop runner? options: laptop cluster leaning: laptop", "routine"),
    ("intake", "the login button text says 'Sing in', please sort that", "delegate"),
    ("intake", "we need a whole online store: catalogue, cart, checkout, admin panel", "decompose"),
    ("intake", "why did signups drop in March? dig in and tell me what you find", "research"),
    ("failure", "task: sync repos\nError: 503 from the server, it came back a minute later", "retry"),
    ("failure", "task: set up billing\nError: needs the Stripe account owner to grant access", "escalate"),
    ("stall", "task: set up the dev env\nCollecting torch (2.1 GB) ... 61%", "wait"),
    ("question", "Is it fine to push straight to main and tell the customers it's live?", "escalate"),
    ("question", "Tabs or spaces in this codebase?", "answer"),
    ("loop", "it has opened config.yaml, closed it, and opened it again eight times", "reorient"),
    ("category", "the page is sluggish when lots of people are on it", "performance"),
    ("category", "make sure people using screen readers can use the signup page", "accessibility_testing"),
]


def run(jev: Jev, keywords_only: bool = False, sets: list[str] | None = None, paraphrased: bool = False) -> dict:
    """Accuracy and mean probability on the gold option, overall and per decision set."""
    per: dict[str, list[tuple[bool, float]]] = {}
    misses = []
    for name, state, gold in (PARAPHRASED if paraphrased else CASES):
        if sets and name not in sets:
            continue
        question, opts = SETS[name]
        if keywords_only:                       # the keyword cues even for a sentences-configured model
            opts = [replace(o, desc="") for o in opts]
        d = jev.choose(question, state, opts)
        ok = d.best == gold
        per.setdefault(name, []).append((ok, d.probs[gold]))
        if not ok:
            misses.append({"set": name, "state": state, "gold": gold, "got": d.best, "p_gold": d.probs[gold]})

    def agg(rows):
        return {"n": len(rows), "accuracy": round(sum(ok for ok, _ in rows) / len(rows), 3),
                "p_gold": round(sum(p for _, p in rows) / len(rows), 3)}
    allrows = [r for rows in per.values() for r in rows]
    return {"backend": jev.backend.name, **agg(allrows), "sets": {k: agg(v) for k, v in per.items()}, "misses": misses}
