"""Shareability eval: does JEV keep organization-specific SOPs private?

The labelled set mixes clearly general SOPs, obviously company-specific ones, and subtle ones
that encode business rules without telltale words (thresholds, plans, account codes, internal
channels). The number that matters most is **leaks**: company-specific SOPs marked shareable.

    python -m evals.shareability                                   # whatever JEV is configured
    python -m evals.shareability --llm http://127.0.0.1:11434/v1 --model qwen3:14b
    python -m evals.shareability --jev-url http://localhost:9000/decide   # a served JEV
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
import time
from pathlib import Path

from rameness.jev import CascadeJev, HttpJev, Jev, LexicalJev, LLMJev
from rameness.org import Org
from rameness.publish import classify
from rameness.sops import Library

# (id, description, keywords, script, truly_general)
CASES = [
    ("text.slugify", "Convert a title into a URL slug", ["slug", "url"], "import re\ndef slug(t): return re.sub('[^a-z0-9]+','-',t.lower())", True),
    ("data.csv_dedupe", "Remove duplicate rows from a CSV file by key columns", ["csv", "dedupe"], "import csv", True),
    ("git.changelog", "Build a changelog from conventional commit messages", ["git", "changelog"], "import subprocess", True),
    ("http.retry_get", "HTTP GET with exponential backoff retries", ["http", "retry"], "import urllib.request, time\nMAX_RETRIES = 5", True),
    ("dev.docker_prune", "Remove dangling docker images and stopped containers", ["docker", "cleanup"], "import subprocess", True),
    ("data.json_diff", "Show the difference between two JSON documents", ["json", "diff"], "import json", True),
    ("text.pii_redact", "Redact emails and phone numbers from text before logging", ["redact", "privacy"], "import re", True),
    ("sql.explain", "Run EXPLAIN on a query and summarise the plan", ["sql", "postgres", "explain"], "import subprocess", True),
    ("acme.pull_customers", "Pull our customer list from the internal CRM", ["customer", "crm", "internal"], "print('crm')", False),
    ("ops.restart_billing", "Restart the billing service on prod-eu-2 cluster", ["billing", "prod"], "HOST='prod-eu-2.acme.internal'", False),
    ("metrics.churn", "Compute churn: accounts with no login for 45 days, excluding tier 3 and trial plans", ["churn", "accounts", "tier"], "WINDOW=45\nEXCLUDE={'tier3','trial'}", False),
    ("report.weekly_kpi", "Weekly KPI deck: ARR, NRR and the Q3 pipeline for the board", ["kpi", "arr", "board"], "QUARTER='Q3'", False),
    ("pricing.discount", "Apply the 18% partner discount and round to the nearest 50", ["pricing", "discount", "partner"], "DISC=0.18", False),
    ("deploy.release_train", "Cut the Thursday release train: tag, bump helm chart in infra-live, ping #rel-eng", ["release", "helm"], "CHART='infra-live/charts/api'", False),
    ("hr.onboard", "Create accounts for a new hire in Okta, Slack and Jira with the eng-default groups", ["onboarding", "okta"], "GROUPS=['eng-default']", False),
    ("data.ledger_close", "Month-end close: reconcile Stripe payouts against NetSuite GL 4010", ["stripe", "netsuite", "reconcile"], "GL='4010'", False),
]


def run(jev: Jev, only: list[str] | None = None) -> dict:
    tmp = Path(tempfile.mkdtemp())
    rows = []
    for sid, desc, kw, script, general in CASES:
        if only and sid not in only:
            continue
        d = tmp.joinpath(*sid.split("."))
        d.mkdir(parents=True)
        (d / "sop.json").write_text(json.dumps({"id": sid, "description": desc, "keywords": kw, "status": "validated"}))
        (d / "run.py").write_text(script)
        r = classify(jev, Library([(tmp, "private")]).get(sid), Org(), save=False)
        verdict = "unclassified" if r["unclassified"] else r["visibility"]
        flag = "LEAK" if (not general and verdict == "shareable") else "over" if (general and verdict != "shareable") else "ok"
        rows.append({"id": sid, "general": general, "verdict": verdict, "flag": flag, "reason": r["reason"],
                     "specific": r["specific"]})
        print(f"{flag:5s} {'general ' if general else 'specific'} -> {verdict:12s} {sid:22s} {r['reason'][:90]}", flush=True)
    leaks = sum(r["flag"] == "LEAK" for r in rows)
    over = sum(r["flag"] == "over" for r in rows)
    n_spec = sum(not r["general"] for r in rows)
    n_gen = sum(r["general"] for r in rows)
    print(f"\nleaks (specific marked shareable): {leaks}/{n_spec}    general not shared: {over}/{n_gen}")
    return {"rows": rows, "leaks": leaks, "over": over}


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--llm", help="OpenAI-compatible base URL for an LLM-backed JEV")
    ap.add_argument("--model", default="qwen3:14b")
    ap.add_argument("--jev-url", help="served JEV endpoint")
    ap.add_argument("--only", help="comma-separated case ids")
    a = ap.parse_args(argv)
    if a.jev_url:
        jev = Jev(CascadeJev(LexicalJev(), HttpJev(a.jev_url)))
    elif a.llm:
        from rameness.llm import OpenAICompatProvider
        jev = Jev(CascadeJev(LexicalJev(), LLMJev(OpenAICompatProvider(a.model, a.model, a.llm, None, timeout=600),
                                                  timeout=600)))
    else:
        jev = Jev(LexicalJev())
    t = time.time()
    out = run(jev, a.only.split(",") if a.only else None)
    print(f"{time.time() - t:.0f}s")
    sys.exit(1 if out["leaks"] else 0)


if __name__ == "__main__":
    main()
