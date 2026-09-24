"""End-to-end against a real llama-server. Set RAMENESS_E2E_URL=http://host:port to run."""

import json
import os
import re
import subprocess
import sys
import unittest
import urllib.request

from .helpers import E2E, LIVE_URL, ROOT

CALC_BUG = {"calc.py": "def add(a, b):\n    return a - b\n\n\ndef mul(a, b):\n    return a * b\n",
            "test_calc.py": "import unittest\nfrom calc import add, mul\n\n\nclass T(unittest.TestCase):\n"
                            "    def test_add(self):\n        self.assertEqual(add(2, 3), 5)\n\n"
                            "    def test_mul(self):\n        self.assertEqual(mul(2, 3), 6)\n"}


def model_id():
    with urllib.request.urlopen(LIVE_URL.rstrip("/") + "/v1/models", timeout=10) as r:
        return json.loads(r.read())["data"][0]["id"]


@unittest.skipUnless(LIVE_URL, "set RAMENESS_E2E_URL to a llama-server to run live tests")
class LiveE2E(E2E):
    def setUp(self):
        super().setUp()
        self.base = LIVE_URL.rstrip("/") + "/v1"
        self.model = model_id()

    def unit_tests_pass(self, where):
        return subprocess.run([sys.executable, "-m", "unittest", "-q"], cwd=where, capture_output=True).returncode == 0


class TestLiveAgent(LiveE2E):
    def test_real_model_fixes_a_bug_through_the_cli(self):
        self.repo(files=CALC_BUG)
        self.cli("init")
        self.config({"jev": {"backend": "lexical"}, "learning": {"enabled": False}})
        self.assertFalse(self.unit_tests_pass(self.proj))
        r = self.cli("-y", "--base-url", self.base, "--model", self.model, "run",
                     "The unit tests fail. Fix the bug in calc.py and run python3 -m unittest to verify.", timeout=900)
        self.assertTrue(self.unit_tests_pass(self.proj), r.stdout[-1500:] + r.stderr[-1500:])
        m = re.search(r"llm_calls=(\d+)", r.stderr)
        self.assertTrue(m and int(m.group(1)) >= 1)

    def test_model_backed_jev_cascade_escalates_without_silent_fallbacks(self):
        self.repo(files=CALC_BUG)
        self.cli("init")
        self.config({"jev": {"backend": "cascade", "llm_timeout": 240}, "learning": {"enabled": False}})
        code = f"""
import json, sys
from rameness import config
from rameness.harness import Harness
cfg = config.load(overrides={{"provider": "openai", "base_url": {self.base!r}, "model": {self.model!r}}})
cfg.update(api_key_env=None, fast_model={self.model!r})
cfg["jev"].update(backend="cascade", llm_timeout=240)
h = Harness(cfg)
plan = h.plan("could you maybe look into whatever is going on with the numbers thing")
b = h.jev.backend
print(json.dumps({{"escalations": b.escalations, "failures": b.failures, "route": plan.route}}))
"""
        out = subprocess.run([sys.executable, "-c", code], cwd=self.proj, env=self.env, capture_output=True,
                             text=True, timeout=900)
        self.assertEqual(out.returncode, 0, out.stderr[-2000:])
        res = json.loads(out.stdout.strip().splitlines()[-1])
        self.assertGreaterEqual(res["escalations"], 1)
        self.assertEqual(res["failures"], [])          # the reasoning-model bug would show up here

    def test_prompted_tool_protocol_with_a_real_model(self):
        from rameness.llm import OpenAICompatProvider
        p = OpenAICompatProvider(self.model, self.model, self.base, None, tool_mode="prompt", timeout=600)
        r = p.chat("You are a helpful agent.", [{"role": "user", "content": "What is 1234*5678? Use the calc tool."}],
                   [{"name": "calc", "description": "evaluate an arithmetic expression",
                     "input_schema": {"type": "object", "properties": {"expr": {"type": "string"}}, "required": ["expr"]}}],
                   max_tokens=4000)
        self.assertEqual([c.name for c in r.tool_calls], ["calc"], r.text[:500])


class TestLiveFleet(LiveE2E):
    def test_fleet_worker_on_llama_server_fixes_and_merges(self):
        self.repo(files=CALC_BUG)
        self.config({"jev": {"backend": "lexical"}, "learning": {"enabled": False}},
                    {"backend": "tmux", "discover": False, "manager_slot": "none", "mode": "local",
                     "decision_policy": {"*": "jev"},
                     "slots": [{"id": "occamy", "kind": "llama-server", "url": LIVE_URL, "traits": "local coding"}]})
        base = self.serve()
        st = self.api(base, "/api/state")
        slot = [s for s in st["slots"] if s["id"] == "occamy"][0]
        self.assertTrue(slot["available"])
        self.assertGreaterEqual(slot["capacity"], 1)                 # read from llama-server /props
        a = self.api(base, "/api/agents", {"task": "The unit tests fail. Fix the bug in calc.py and verify "
                                                   "with python3 -m unittest.", "slot": "occamy"})
        aid = a["id"]
        self.until(lambda: self.api(base, f"/api/agents/{aid}")["status"] in ("done", "failed"), 900, 3, "live agent")
        a = self.api(base, f"/api/agents/{aid}")
        self.assertEqual(a["status"], "done", a["log"][-2000:])
        self.until(lambda: self.unit_tests_pass(self.proj), 60, 2, "merged fix on main")


class TestLiveShareability(LiveE2E):
    def test_no_leaks_on_subtle_business_rules(self):
        out = subprocess.run([sys.executable, "-m", "evals.shareability", "--llm", self.base, "--model", self.model,
                              "--only", "metrics.churn,pricing.discount,deploy.release_train,text.slugify,data.json_diff"],
                             cwd=ROOT, env=self.env, capture_output=True, text=True, timeout=1800)
        self.assertIn("leaks (specific marked shareable): 0/", out.stdout, out.stdout + out.stderr[-1000:])
