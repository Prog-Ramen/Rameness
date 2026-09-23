import json
import os
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

from rameness.fleet.cycles import TAXONOMY, parse_options
from rameness.fleet.manager import FLEET_DEFAULTS, Fleet
from rameness.harness import Harness
from rameness import config
from rameness.jev import Jev, LexicalJev
from rameness.llm import FakeProvider, Response, ToolCall
from rameness.loopguard import LoopGuard, degenerate

# one fake "CLI agent": research tasks print options JSON, everything else appends a line to app.txt
AGENT = r'''
case "$0" in
  *"Test type:"*"Follow-up"*) echo "extended" >> tests.txt; printf 'More tests.\n```json\n{"summary": "edge cases now covered", "tests_run": 14, "tests_failed": 1, "tests_written": ["test_empty_input", "test_unicode", "test_timeout", "test_happy"], "covered": ["boundaries", "error paths", "invalid input"], "not_covered": [], "findings": [{"title": "Crash on empty input", "severity": "high", "details": "empty string raises"}, {"title": "Button slightly misaligned", "severity": "low", "details": "2px"}]}\n```\n';;
  *"Test type:"*) echo "basic" >> tests.txt; printf 'Tested.\n```json\n{"summary": "happy path only", "tests_run": 2, "tests_failed": 0, "tests_written": ["test_happy"], "covered": ["happy path"], "not_covered": ["edge cases", "error paths", "invalid input"], "findings": []}\n```\n';;
  *"Propose 3-6"*) printf 'Looked around.\n```json\n{"options": [{"title": "Add input validation", "why": "bad input crashes", "impact": "high", "effort": "small", "risk": "low"}, {"title": "Rewrite in Rust", "why": "speed", "impact": "low", "effort": "large", "risk": "high"}]}\n```\n';;
  *) echo "change" >> app.txt; echo "done and verified";;
esac
'''


def git(cwd, *args):
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", *args], cwd=cwd, check=True, capture_output=True)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        os.environ["RAMENESS_HOME"] = str(self.tmp / "home")
        self.repo = self.tmp / "repo"
        self.repo.mkdir()
        git(self.repo, "init", "-q", "-b", "main")
        (self.repo / "app.txt").write_text("v0\n")
        git(self.repo, "add", ".")
        git(self.repo, "commit", "-qm", "init")
        (self.repo / ".rameness").mkdir()
        (self.repo / ".rameness" / "config.json").write_text(json.dumps({"jev": {"backend": "lexical"}}))

    def fleet(self, **over):
        cfg = json.loads(json.dumps(FLEET_DEFAULTS))
        cfg.update(backend="subprocess", discover=False, decision_policy={"*": "jev"},
                   slots=[{"id": "cli:fake", "kind": "cli", "model": "fake", "capacity": 4,
                           "argv": ["bash", "-c", AGENT, "{task}"]}])
        cfg.update(over)
        return Fleet(self.repo, cfg, planner=False, probe=False)

    def run_until(self, f, pred, timeout=40):
        end = time.time() + timeout
        while time.time() < end:
            f.tick()
            if pred():
                return True
            time.sleep(0.15)
        return False


class TestCycles(Base):
    def test_draft_then_two_cycles_then_single_merge(self):
        f = self.fleet()
        r = f.ask("build app A", cycles=2)
        pid = r["program"]
        self.assertTrue(self.run_until(f, lambda: f.programs.get(pid)["status"] == "done"),
                        f.programs.get(pid))
        p = f.programs.get(pid)
        self.assertEqual(len(p["history"]), 2)
        for h in p["history"]:
            self.assertIn(h["category"], {c["id"] for c in TAXONOMY})
            self.assertEqual([o["title"] for o in h["chosen"]], ["Add input validation"])   # JEV skipped the risky one
        self.assertEqual((self.repo / "app.txt").read_text(), "v0\n")         # nothing merged yet
        merges = [e for e in f.store.escalations() if e["kind"] == "merge"]
        self.assertEqual(len(merges), 1)                                       # one review for the whole program
        f.answer(merges[0]["id"], "merge")
        self.assertEqual((self.repo / "app.txt").read_text(), "v0\nchange\nchange\nchange\n")   # draft + 2 cycles
        lead = f.tree()["children"][0]
        self.assertEqual(lead["id"], p["lead"])
        self.assertEqual(len(lead["children"]), 5)                             # draft + 2 x (research, improve)

    def test_programs_usable_from_other_threads(self):
        import threading
        f = self.fleet()
        out = {}
        t = threading.Thread(target=lambda: out.update(r=f.ask("x", cycles=1, on="HEAD")))
        t.start(); t.join()
        self.assertEqual(out["r"]["route"], "program")

    def test_categories_restrict_the_choice(self):
        f = self.fleet()
        pid = f.ask("build app A", cycles=2, categories=["security", "ui"])["program"]
        self.assertTrue(self.run_until(f, lambda: f.programs.get(pid)["status"] == "done"))
        self.assertEqual({h["category"] for h in f.programs.get(pid)["history"]}, {"security", "ui"})   # both covered

    def test_godmode_needs_permission_and_stops(self):
        f = self.fleet()
        with self.assertRaises(ValueError):
            f.set_autonomy("godmode")
        with self.assertRaises(ValueError):
            f.ask("x", cycles="godmode")
        f = self.fleet(allow_godmode=True, godmode={"max_cycles": 2})
        f.set_autonomy("godmode")
        pid = f.ask("build app A", cycles="godmode")["program"]
        self.assertTrue(self.run_until(f, lambda: f.programs.get(pid)["status"] == "done", 60))
        p = f.programs.get(pid)
        self.assertLessEqual(len(p["history"]), 2)
        self.assertFalse(f.store.escalations())                     # nobody was asked anything
        self.assertIn("app.txt", "app.txt")
        self.assertIn("change", (self.repo / "app.txt").read_text())  # JEV merged by itself


class TestTestingCycles(Base):
    def test_testing_cycles_coverage_gate_fix_and_extension(self):
        f = self.fleet()
        pid = f.ask("the signup app", cycles=1, categories=["testing"], on="HEAD")["program"]
        p = f.programs.get(pid)
        self.assertEqual(p["phase"], "choose")                       # existing work: no draft
        self.assertTrue(self.run_until(f, lambda: f.programs.get(pid)["status"] != "running"
                                       or bool(f.store.escalations()), 60), f.programs.get(pid))
        p = f.programs.get(pid)
        h = p["history"][0]
        self.assertEqual(h["mode"], "test")
        self.assertIn(h["category"], {c["id"] for c in TAXONOMY if c["group"] == "Testing"})
        # JEV judged the first, happy-path-only tests inadequate and sent the tester back once
        cov = [d for d in f.store.decisions() if d["question"].startswith("Are these the right kinds of tests")]
        self.assertEqual([d["chosen"] for d in cov], ["adequate", "gaps"])     # newest first
        self.assertEqual(h["coverage"], "adequate")
        # the high-severity finding was fixed; the cosmetic one left
        self.assertEqual([o["title"] for o in h["chosen"]], ["Crash on empty input"])
        self.assertEqual(h["findings"], {"high": 1, "low": 1})
        # requested count done: whether to test more is the director's call in balanced mode
        ext = [e for e in f.store.escalations() if "Is further work of this kind needed" in e["question"]]
        self.assertEqual(len(ext), 1)
        f.answer(ext[0]["id"], "enough")
        self.assertTrue(self.run_until(f, lambda: f.programs.get(pid)["status"] == "done"))

    def test_autopilot_extends_testing_on_its_own_within_cap(self):
        f = self.fleet(decision_policy={"*": "auto"}, autopilot_extra_cycles=1)
        f.set_autonomy("autopilot")
        pid = f.ask("the signup app", cycles=1, categories=["user_testing"], on="HEAD")["program"]
        self.assertTrue(self.run_until(f, lambda: f.programs.get(pid)["status"] == "done", 90))
        p = f.programs.get(pid)
        self.assertLessEqual(p["total"], 2)
        self.assertFalse([e for e in f.store.escalations() if e["kind"] == "decision"])


class TestAutonomy(Base):
    def test_restrictive_sends_work_decisions_through_the_manager(self):
        f = self.fleet(decision_policy={"*": "auto"})
        f.set_autonomy("restrictive")
        r = f.ask("fix the thing in app.txt")
        self.assertEqual(r["route"], "awaiting-director")
        e = f.store.escalations()[0]
        self.assertTrue(e["question"].startswith("Manager:"))
        f.answer(e["id"], "delegate")
        f.tick()
        # the next work decision (associate vs lead) also goes to the director, relayed by the manager...
        open_q = [x["question"] for x in f.store.escalations()]
        self.assertTrue(any("lead manage" in q for q in open_q), open_q)
        # ...while nothing infra-level (environment / model) was deferred
        self.assertFalse(any("environment" in q or "runtime" in q for q in open_q))

    def test_autopilot_decides_everything(self):
        f = self.fleet(decision_policy={"*": "auto"})
        f.set_autonomy("autopilot")
        r = f.ask("delete the production database and email every customer")    # significant, still no asking
        aid = r["agents"][0]
        self.assertTrue(self.run_until(f, lambda: f.get(aid)["status"] == "done"))
        self.assertFalse([e for e in f.store.escalations() if e["kind"] == "decision"])
        self.assertTrue(any("would have asked" in (d["reason"] or "") for d in f.store.decisions()))


class TestLoopGuard(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        os.environ["RAMENESS_HOME"] = str(self.tmp / "home")
        (self.tmp / ".rameness").mkdir()

    def harness(self, script):
        cfg = config.load(self.tmp, {"provider": "fake"})
        cfg["jev"]["backend"] = "lexical"
        cfg["permissions"]["mode"] = "auto"
        cfg["learning"]["enabled"] = False
        return Harness(cfg, llm=FakeProvider(script))

    def test_repeated_calls_trigger_reorientation_then_stop(self):
        same = lambda i: Response("", [ToolCall(str(i), "bash", {"command": "cat missing.txt"})], "tool_use")
        h = self.harness([same(i) for i in range(30)])
        seen = []
        h.on_decision = lambda kind, d, detail: seen.append(detail["action"])
        r = h.run("read the config and summarise it", allow_direct=False)
        self.assertIn("reorient", seen)
        self.assertEqual(seen[-1], "stop")
        self.assertFalse(r.metrics["success"])
        self.assertIn("loop guard", r.text)
        self.assertLess(r.metrics["turns"], 30)
        self.assertTrue(any("[rameness loop guard]" in str(m.get("content")) for m in h.messages))

    def test_degenerate_reply_is_reset_not_accepted(self):
        blab = "I will now check the file again and " * 40
        h = self.harness([Response(blab, [], "max_tokens"), Response("All done.", [], "end_turn")])
        r = h.run("read the config and summarise it", allow_direct=False)
        self.assertEqual(r.text, "All done.")
        self.assertTrue(r.metrics["success"])
        self.assertTrue(degenerate(blab))
        self.assertFalse(degenerate("a normal answer that says a few different things about the code"))

    def test_parse_options_fallbacks(self):
        self.assertEqual(parse_options("- one idea\n- two idea")[1]["title"], "two idea")
        self.assertEqual(parse_options("- one idea\n- two idea")[1]["id"], "2. two idea")
        self.assertEqual(parse_options('```json\n{"options": [{"title": "t", "impact": "high"}]}\n```')[0]["impact"], "high")


if __name__ == "__main__":
    unittest.main()
