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


    def test_distinct_edits_with_the_same_reply_are_not_a_loop(self):
        g = LoopGuard(Jev(LexicalJev()))
        for i in range(6):
            r = Response("", [ToolCall(str(i), "edit_file", {"path": "a.js", "old": f"x{i}", "new": f"y{i}"})],
                         "tool_use")
            g.observe(r, [("edited a.js", False)])
            self.assertIsNone(g.check(i, "build the game"))

    def test_rerunning_a_test_between_edits_is_not_a_loop(self):
        g = LoopGuard(Jev(LexicalJev()))
        for i in range(5):
            g.observe(Response("", [ToolCall(f"e{i}", "edit_file", {"path": "a.js", "old": f"x{i}", "new": f"y{i}"})],
                               "tool_use"), [("edited a.js", False)])
            g.observe(Response("", [ToolCall(f"t{i}", "bash", {"command": "node test.js"})], "tool_use"),
                      [(f"{i} failing", False)])
        self.assertFalse([s for s in g.signals() if "repeated the same tool call" in s])

    def test_the_same_error_from_different_commands_is_a_loop(self):
        g = LoopGuard(Jev(LexicalJev()))
        for i in range(6):
            g.observe(Response("", [ToolCall(str(i), "bash", {"command": f"node probe{i}.js"})], "tool_use"),
                      [(f"exit=0\nlogs: {i}\n[ERR] Unexpected token ')' at line {700 + i}", False)])
        sig = [s for s in g.signals() if "keeps coming back" in s]
        self.assertTrue(sig)
        self.assertIn("Unexpected token ')' at line #", sig[0])

    def test_passing_test_summaries_are_not_errors(self):
        from rameness.loopguard import error_lines
        self.assertEqual(error_lines("12 passed, 0 failed\nerrors: 0\nno errors found"), [])
        self.assertEqual(error_lines("TypeError: x is not a function"), ["TypeError: x is not a function"])

    def test_the_same_call_with_the_same_result_is_a_loop(self):
        g = LoopGuard(Jev(LexicalJev()), repeat_calls=99)
        for i in range(4):
            g.observe(Response("", [ToolCall(str(i), "bash", {"command": "cat log"})], "tool_use"), [("same", False)])
        self.assertIn("the same call returned the same result 4 times (no new information)", g.signals())


class _Verdict(LexicalJev):
    """Answers the progress-review question with a fixed verdict; everything else lexically."""
    def __init__(self, verdict: str, p: float = 0.9):
        super().__init__()
        self.verdict, self.p = verdict, p

    def choose(self, question, query, options, context=""):
        if not question.startswith("Is the agent's recent work"):
            return super().choose(question, query, options, context)
        rest = (1 - self.p) / (len(options) - 1)
        # undo the option priors so the verdict comes out at probability p
        return [(self.p if o.id == self.verdict else rest) / o.prior for o in options]


class TestProgressReview(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        os.environ["RAMENESS_HOME"] = str(self.tmp / "home")
        (self.tmp / ".rameness").mkdir()

    def run_with(self, verdict, p=0.9, every=3, turns=7):
        cfg = config.load(self.tmp, {"provider": "fake"})
        cfg["permissions"]["mode"] = "auto"
        cfg["learning"]["enabled"] = False
        cfg["progress_review"] = {"every": every, "min_confidence": 0.5}
        script = [Response("", [ToolCall(str(i), "write_file", {"path": f"f{i}.txt", "content": f"part {i}"})],
                           "tool_use") for i in range(turns)] + [Response("done", [], "end_turn")]
        h = Harness(cfg, llm=FakeProvider(script))
        h.jev = Jev(_Verdict(verdict, p))
        seen = []
        h.on_decision = lambda kind, d, detail: seen.append((kind, detail))
        r = h.run("write the parts of the report", allow_direct=False)
        notes = [m["content"] for m in h.messages if "[rameness progress review]" in str(m.get("content"))]
        return r, notes, seen

    def test_drifting_verdict_injects_a_refocus_note(self):
        r, notes, seen = self.run_with("drifting")
        self.assertEqual([v["turn"] for v in r.metrics["progress_reviews"]], [2, 5])
        self.assertTrue(all(v["acted"] for v in r.metrics["progress_reviews"]))
        self.assertEqual(len(notes), 2)
        self.assertIn("write the parts of the report", notes[0])
        self.assertIn("write_file f2.txt -> ok", notes[0])
        self.assertIn(("progress", {"action": "drifting", "acted": True}), seen)
        self.assertTrue(r.metrics["success"])

    def test_on_track_and_unsure_verdicts_leave_the_agent_alone(self):
        for verdict, p in (("on_track", 0.9), ("stalled", 0.4)):
            r, notes, _ = self.run_with(verdict, p)
            self.assertEqual(notes, [])
            self.assertEqual(len(r.metrics["progress_reviews"]), 2)
            self.assertFalse(any(v["acted"] for v in r.metrics["progress_reviews"]))

    def test_event_log_records_turns_tools_and_reviews(self):
        log = self.tmp / "events.jsonl"
        os.environ["RAMENESS_EVENT_LOG"] = str(log)
        try:
            self.run_with("on_track")
        finally:
            del os.environ["RAMENESS_EVENT_LOG"]
        ev = [json.loads(l) for l in log.read_text().splitlines()]
        kinds = [e["kind"] for e in ev]
        self.assertEqual((kinds[0], kinds[-1]), ("plan", "end"))
        self.assertEqual(kinds.count("tool"), 7)
        self.assertEqual(kinds.count("progress_review"), 2)
        tool = next(e for e in ev if e["kind"] == "tool")
        self.assertEqual((tool["tool"], tool["ok"]), ("write_file", True))
        self.assertTrue(all("t" in e for e in ev))

    def test_every_zero_disables_the_review(self):
        r, notes, _ = self.run_with("drifting", every=0)
        self.assertEqual((notes, r.metrics["progress_reviews"]), ([], []))

    def test_facts_count_new_and_rewritten_files(self):
        from rameness.progress import digest, facts
        steps = [{"tool": "write_file", "input": {"path": "a.js"}, "ok": True},
                 {"tool": "write_file", "input": {"path": "a.js"}, "ok": True},
                 {"tool": "write_file", "input": {"path": "b.js"}, "ok": True},
                 {"tool": "bash", "input": {"command": "node a.js"}, "ok": False, "out": "SyntaxError: x"}]
        self.assertIn("2 file writes/edits (1 new files, 1 rewritten), 1 commands run, 1 errors", facts(steps, 1))
        self.assertIn("- bash node a.js -> error: SyntaxError: x", digest(steps))
        rec = [{"tool": "bash", "input": {"command": f"node t{i}.js"}, "ok": True, "out": "",
                "errs": ["[ERR] Unexpected token ')'"]} for i in range(5)]
        self.assertIn("Recurring error: the same error appeared in 5 of the last 5 results", facts(rec, 0))


class TestFinishing(unittest.TestCase):
    """The agent is sent back when it stops with open todos or untested code changes, and sees whole files."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        os.environ["RAMENESS_HOME"] = str(self.tmp / "home")
        (self.tmp / ".rameness").mkdir()

    def harness(self, script):
        cfg = config.load(self.tmp, {"provider": "fake"})
        cfg["jev"]["backend"] = "lexical"
        cfg["permissions"]["mode"] = "auto"
        cfg["learning"]["enabled"] = False
        cfg["progress_review"] = {"every": 0}
        cfg["task_scope"]["mode"] = "off"            # these count finish-logic turns; feature cycles: test_task_scope
        return Harness(cfg, llm=FakeProvider(script))

    @staticmethod
    def call(i, name, **args):
        return Response("", [ToolCall(str(i), name, args)], "tool_use")

    def test_open_todos_send_the_agent_back_until_they_are_done(self):
        todos = lambda *st: [{"content": f"step {i}", "status": s} for i, s in enumerate(st)]
        h = self.harness([self.call(0, "todo_write", todos=todos("completed", "in_progress")),
                          Response("all done", [], "end_turn"),                 # step 1 still open: sent back
                          self.call(1, "todo_write", todos=todos("completed", "completed")),
                          Response("all done", [], "end_turn"),                 # one self-review against the task
                          Response("reviewed: both steps checked", [], "end_turn")])
        r = h.run("do the two steps", allow_direct=False)
        notes = [m["content"] for m in h.messages if "todo list still has open items" in str(m.get("content"))]
        self.assertEqual(len(notes), 1)
        self.assertIn("- step 1", notes[0])
        self.assertEqual(sum("review the work against the task" in str(m.get("content")) for m in h.messages), 1)
        self.assertTrue(r.metrics["success"])
        self.assertEqual(r.metrics["turns"], 5)

    def test_code_changed_after_the_last_run_must_be_tested(self):
        h = self.harness([self.call(0, "write_file", path="app.py", content="print('hi')\n"),
                          Response("done", [], "end_turn"),                     # never ran it: sent back
                          self.call(1, "bash", command="python3 app.py"),
                          Response("done, it prints hi", [], "end_turn")])
        r = h.run("write app.py that prints hi", allow_direct=False)
        self.assertTrue(any("changed code after your last run" in str(m.get("content")) for m in h.messages))
        self.assertIn("hi", next(m["content"] for m in h.messages if m.get("name") == "bash"))
        self.assertEqual(r.metrics["turns"], 4)

    def test_finish_checks_are_bounded(self):
        h = self.harness([self.call(0, "todo_write", todos=[{"content": "never done", "status": "pending"}])]
                         + [Response("stopping", [], "end_turn")] * 5)
        r = h.run("impossible task", allow_direct=False)
        self.assertEqual(r.metrics["turns"], 5)                  # 1 + 3 checks + the stop that is accepted
        self.assertEqual(sum("open items" in str(m.get("content")) for m in h.messages), 3)

    def test_self_review_happens_once_and_can_reopen_work(self):
        done = [{"content": "build it", "status": "completed"}]
        h = self.harness([self.call(0, "todo_write", todos=done),
                          Response("done", [], "end_turn"),                     # -> self-review
                          self.call(1, "todo_write", todos=done + [{"content": "test controls", "status": "pending"}]),
                          Response("stopping", [], "end_turn"),                 # -> open item
                          self.call(2, "todo_write", todos=done + [{"content": "test controls", "status": "completed"}]),
                          Response("done", [], "end_turn")])                    # no second review: accepted
        r = h.run("build it", allow_direct=False)
        self.assertEqual(sum("review the work against the task" in str(m.get("content")) for m in h.messages), 1)
        self.assertEqual(r.metrics["turns"], 6)

    def test_a_stale_todo_list_gets_a_reminder(self):
        todo = [{"content": "build the thing", "status": "in_progress"}]
        script = [self.call(0, "todo_write", todos=todo)] + \
                 [self.call(i, "write_file", path=f"n{i}.md", content="x") for i in range(1, 5)] + \
                 [self.call(5, "todo_write", todos=[{**todo[0], "status": "completed"}]),
                  Response("done", [], "end_turn"), Response("reviewed", [], "end_turn")]
        h = self.harness(script)
        h.cfg["todo_reminder_turns"] = 3
        h.run("build the thing", allow_direct=False)
        notes = [m["content"] for m in h.messages if "not updated your todo list" in str(m.get("content"))]
        self.assertEqual(len(notes), 1)
        self.assertIn("[>] build the thing", notes[0])

    def test_notes_survive_a_context_reset(self):
        h = self.harness([])
        call = lambda name, **a: h._call(name, a, {})
        self.assertEqual(call("note", kind="fact", text="The renderer needs yaw and pitch from the camera.")[0], "noted fact 1")
        call("note", kind="issue", text="Terrain is invisible at spawn.")
        call("note", kind="decision", text="Use one index.html with ES modules.")
        call("todo_write", todos=[{"content": "render terrain", "status": "in_progress"},
                                  {"content": "scaffold", "status": "completed"}])
        self.assertIn("resolved", call("note", resolve=2)[0])
        self.assertTrue(call("note", kind="opinion", text="x")[1])
        state = h.state_summary()
        self.assertIn("Plan: 1/2 done. Open: render terrain", state)
        self.assertIn("[1] The renderer needs yaw", state)
        self.assertNotIn("Terrain is invisible", state)                  # resolved issues drop out
        h.messages = [{"role": "user", "content": "task"}]
        h._reinject_state("context reset")
        self.assertIn("[rameness state, kept across the context reset]", h.messages[-1]["content"])
        self.assertTrue((h.state / "ledger.json").exists())

    def test_delegate_runs_a_sub_agent_when_jev_says_spawn(self):
        from rameness.jev import Decision
        def build(choice):
            script = [self.call(0, "delegate", task="make part.txt containing hi", context="plain text file"),
                      self.call(1, "write_file", path="part.txt", content="hi"),     # the sub-agent's turn
                      Response("made part.txt", [], "end_turn"),                    # the sub-agent reports
                      Response("all done", [], "end_turn")]                         # the main agent finishes
            h = self.harness(script if choice == "spawn" else [script[0], script[3]])
            h.cfg["delegation"] = True
            real = h.jev.choose
            h.jev.choose = lambda q, query, opts, context="": (
                Decision("x", q, {o.id: (0.9 if o.id == choice else 0.1) for o in opts}, "t")
                if q.startswith("Should this sub-task run") else real(q, query, opts, context))
            h.run("build the thing", allow_direct=False)
            return h, next(m["content"] for m in h.messages if m.get("name") == "delegate")
        h, out = build("spawn")
        self.assertIn("Sub-agent report", out)
        self.assertIn("made part.txt", out)
        self.assertIn("Files it changed: part.txt", out)
        self.assertEqual((self.tmp / "part.txt").read_text(), "hi")
        h, out = build("inline")
        self.assertTrue(out.startswith("Not delegated"))

    def test_notes_and_docs_do_not_need_a_test_run(self):
        h = self.harness([self.call(0, "write_file", path="NOTES.md", content="# notes\n"),
                          Response("done", [], "end_turn")])
        self.assertEqual(h.run("write notes", allow_direct=False).metrics["turns"], 2)

    def test_read_file_output_is_shown_whole(self):
        big = "\n".join(f"const line{i} = {i};" for i in range(2500))      # ~60k chars, over offload_chars
        (self.tmp / "big.js").write_text(big)
        h = self.harness([self.call(0, "read_file", path="big.js", limit=5000), Response("read it", [], "end_turn")])
        h.run("read big.js", allow_direct=False)
        out = next(m["content"] for m in h.messages if m.get("name") == "read_file")
        self.assertIn("line2499", out)
        self.assertNotIn("ref=", out)


class TestClaudeCodeParity(unittest.TestCase):
    """Limits and tools that match Claude Code (decisioning aside)."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        os.environ["RAMENESS_HOME"] = str(self.tmp / "home")
        (self.tmp / ".rameness").mkdir()

    def test_compaction_follows_the_model_window_and_the_reported_size(self):
        from rameness.context import ArtifactStore, ContextManager
        cm = ContextManager(ArtifactStore(self.tmp / "a"), Jev(LexicalJev()), 184000)
        small = [{"role": "user", "content": "x" * 4000}]
        self.assertFalse(cm.needs_compaction("", small, reported=140000))       # 140k of 184k: keep going
        self.assertTrue(cm.needs_compaction("", small, reported=150000))        # 150k + a 32k reply + margin: compact
        self.assertTrue(cm.needs_compaction("", small, reported=172000))

    def test_compaction_reaches_its_target_even_when_the_context_is_mostly_the_models_own_turns(self):
        from rameness.context import ArtifactStore, ContextManager, estimate_tokens
        from rameness.llm import ToolCall
        cm = ContextManager(ArtifactStore(self.tmp / "a"), Jev(LexicalJev()), 100000)
        self.assertEqual(cm.limit(), 75000)                                 # room kept for the reply (capped at 25%)
        msgs = [{"role": "user", "content": "build the game"}]
        for i in range(30):                                                 # reasoning + big file writes, small results
            msgs.append({"role": "assistant", "content": "writing part", "reasoning": "r" * 4000,
                         "tool_calls": [ToolCall(str(i), "write_file", {"path": f"f{i}.js", "content": "x" * 8000})]})
            msgs.append({"role": "tool", "tool_call_id": str(i), "name": "write_file", "content": "wrote"})
        cm.calibrate("", msgs, int(estimate_tokens(msgs) * 1.35))              # the server counts more than chars/4
        self.assertTrue(cm.needs_compaction("", msgs))
        out = cm.compact("", msgs, "build the game")
        self.assertLessEqual(cm.size("", out), int(cm.limit() * 0.65))
        self.assertEqual(out[0]["content"], "build the game")               # the task stays
        self.assertEqual(out[-2]["reasoning"], "r" * 4000)                  # the recent turns stay whole
        old = next(m for m in out if m["role"] == "assistant")
        self.assertIn("in the file, not repeated here", old["tool_calls"][0].input["content"])

    def test_context_overflow_is_recovered_not_fatal(self):
        from rameness.harness import _overflow
        self.assertTrue(_overflow(Exception("request (100037 tokens) exceeds the available context size (100000 tokens)")))
        self.assertFalse(_overflow(Exception("connection refused")))

    def test_llama_server_window_comes_from_props(self):
        import http.server, threading
        class H(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                body = json.dumps({"default_generation_settings": {"n_ctx": 184000}}).encode()
                self.send_response(200); self.send_header("Content-Length", str(len(body))); self.end_headers()
                self.wfile.write(body)
            def log_message(self, *a): pass
        srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            from rameness.llm import OpenAICompatProvider
            p = OpenAICompatProvider("m", base_url=f"http://127.0.0.1:{srv.server_address[1]}/v1")
            self.assertEqual(p.context_window(), 184000)
        finally:
            srv.shutdown()

    def test_reasoning_is_kept_and_sent_back(self):
        from rameness.llm import OpenAICompatProvider
        wire = OpenAICompatProvider.to_wire("sys", [
            {"role": "user", "content": "go"},
            {"role": "assistant", "content": "", "reasoning": "the bug is in grad3",
             "tool_calls": [ToolCall("1", "bash", {"command": "ls"})]},
            {"role": "tool", "tool_call_id": "1", "name": "bash", "content": "ok"}])
        self.assertEqual(wire[2]["reasoning_content"], "the bug is in grad3")
        self.assertNotIn("reasoning_content", wire[1])

    def test_high_effort_when_it_has_more_than_ten_percent(self):
        cfg = config.load(self.tmp, {"provider": "fake"})
        cfg["jev"]["backend"] = "lexical"
        h = Harness(cfg, llm=FakeProvider([]))
        from rameness.jev import Decision
        real = h.jev.choose
        def fake_choose(q, query, opts, context=""):
            if q.startswith("How much reasoning"):
                return Decision("x", q, {"low": 0.5, "medium": 0.39, "high": p_high}, "test")
            return real(q, query, opts, context)
        h.router.jev.choose = fake_choose
        for p_high, want in ((0.11, "high"), (0.09, "low")):
            self.assertEqual(h.plan("refactor the parser").effort, want)

    def hooked(self, **cfg_over):
        cfg = config.load(self.tmp, {"provider": "fake"})
        cfg["jev"]["backend"] = "lexical"
        cfg["permissions"]["mode"] = "auto"
        cfg["hook_selection"] = "all"          # the lexical test backend does not choose; JEV does in real runs
        cfg.update(cfg_over)
        h = Harness(cfg, llm=FakeProvider([]))
        call = lambda name, **a: h._call(name, a, {})
        call.harness = h
        return call

    def test_syntax_errors_come_back_with_the_write(self):
        import shutil
        call = self.hooked()
        out, err = call("write_file", path="a.py", content="def f(:\n    pass\n")
        self.assertIn("[diagnostics]", out)
        self.assertIn("a.py:1: SyntaxError", out)
        self.assertNotIn("Traceback", out)
        out, _ = call("edit_file", path="a.py", old="def f(:", new="def f():")
        self.assertNotIn("[diagnostics]", out)                  # fixed: nothing to report
        out, _ = call("write_file", path="d.json", content='{"a": 1,}')
        self.assertIn("d.json", out)
        self.assertIn("line 1 column", out)
        if shutil.which("node"):
            page = ('<html>\n<script type="x-shader/x-fragment">void main(){}</script>\n'
                    '<script type="module">\nconst a = f(1, 2));\n</script>\n</html>\n')
            out, _ = call("write_file", path="index.html", content=page)
            self.assertIn("index.html:4 (inline script 2)", out)
            self.assertIn("f(1, 2));", out)
        off = self.hooked(hooks={"after_output": []})
        self.assertNotIn("[diagnostics]", off("write_file", path="b.py", content="def g(:\n")[0])

    def test_jev_chooses_which_procedures_fit_the_task(self):
        call = self.hooked(hook_selection="jev")
        from rameness.jev import Decision
        h = call.harness
        pick = {"code.syntax_check": 0.05}
        h.jev.activate = lambda q, query, opts, context="": Decision("x", q, {o.id: pick.get(o.id, 0) for o in opts}, "t")
        h.hooks.select(h.jev, "write a two-page history of the printing press")
        self.assertEqual(h.hooks.selected, set())                       # not a coding task: no syntax check
        self.assertNotIn("[diagnostics]", call("write_file", path="x.py", content="def f(:\n")[0])
        pick["code.syntax_check"] = 0.4
        h.hooks.select(h.jev, "build a browser game")
        self.assertIn("[diagnostics]", call("write_file", path="y.py", content="def f(:\n")[0])

    def test_an_edit_that_breaks_a_working_file_is_undone(self):
        call = self.hooked()
        call("write_file", path="m.py", content="def f():\n    return 1\n")
        out, err = call("edit_file", path="m.py", old="return 1", new="return (1")
        self.assertTrue(err)
        self.assertIn("edit rejected and undone", out)
        self.assertIn("m.py:", out)
        self.assertEqual((self.tmp / "m.py").read_text(), "def f():\n    return 1\n")     # unchanged
        self.assertFalse(call("edit_file", path="m.py", old="return 1", new="return 2")[1])  # a good edit goes through
        # a file that was already broken is not guarded: the edit lands and the problem is reported
        (self.tmp / "b.py").write_text("def g(:\n    x = 1\n")
        out, err = call("edit_file", path="b.py", old="x = 1", new="x = 2")
        self.assertFalse(err)
        self.assertIn("x = 2", (self.tmp / "b.py").read_text())
        self.assertIn("[diagnostics]", out)
        self.assertFalse(self.hooked(edit_guard=False)("edit_file", path="m.py", old="return 2", new="return (2")[1])

    def test_native_checkers_and_shell_written_files(self):
        import shutil
        call = self.hooked()
        out, _ = call("write_file", path="s.sh", content="if [ -f x ]; then echo hi\n")
        self.assertIn("s.sh", out)
        self.assertIn("syntax error", out)
        if shutil.which("cc"):
            # a file written by a shell command gets the same check as write_file
            out, _ = call("bash", command="printf 'int main(void){ return 0 }\\n' > c.c && echo made")
            self.assertIn("made", out)
            self.assertIn("c.c:1:", out)
        self.assertNotIn("[diagnostics]", call("bash", command="echo nothing-written")[0])
        # any language via hook_args; a missing toolchain is skipped silently
        call = self.hooked(hook_args={"code.syntax_check": {"commands": {".foo": "grep -q ok {f}",
                                                                          ".zz": "no-such-checker-xyz {f}"}}})
        self.assertIn("[diagnostics]", call("write_file", path="a.foo", content="bad")[0])
        self.assertNotIn("[diagnostics]", call("write_file", path="b.foo", content="ok")[0])
        self.assertNotIn("[diagnostics]", call("write_file", path="c.zz", content="x")[0])

    def test_model_family_sampling_reaches_the_request(self):
        from types import SimpleNamespace as NS
        from rameness.llm import OpenAICompatProvider, sampling_for
        self.assertEqual(sampling_for("occamy-1.0", "auto")["temperature"], 0.6)
        self.assertEqual(sampling_for("Qwen3.8-27B", "auto")["top_k"], 20)
        self.assertEqual(sampling_for("some-other-model", "auto"), {})
        self.assertEqual(sampling_for("occamy-1.0", "server"), {})
        self.assertEqual(sampling_for("occamy-1.0", {"temperature": 0.2}), {"temperature": 0.2})
        p = OpenAICompatProvider("occamy-1.0", base_url="http://127.0.0.1:9/v1")
        p.sampling = sampling_for("occamy-1.0", "auto")
        p._props = {"n_ctx": 200000}          # pretend it is llama-server (it answers /props)
        sent = {}
        msg = NS(content="ok", tool_calls=None, reasoning_content=None, model_extra={})
        p.client = NS(chat=NS(completions=NS(create=lambda **kw: sent.update(kw) or NS(
            choices=[NS(message=msg, finish_reason="stop")], usage=NS(prompt_tokens=1, completion_tokens=1)))))
        p.chat("sys", [{"role": "user", "content": "hi"}], [], thinking=512)
        self.assertEqual((sent["temperature"], sent["presence_penalty"]), (0.6, 0.0))
        self.assertEqual(sent["extra_body"]["top_k"], 20)
        self.assertEqual(sent["extra_body"]["reasoning_budget_tokens"], 512)
        self.assertTrue(sent["extra_body"]["chat_template_kwargs"]["preserve_thinking"])

    def test_thinking_budget_goes_in_each_servers_own_field(self):
        # Before 2026-10-07 only a server answering llama.cpp's /props got a budget (and a known window).
        from types import SimpleNamespace as NS
        from rameness.llm import OpenAICompatProvider
        cases = [({"owned_by": "vllm", "max_model_len": 262144}, "thinking_token_budget", 262144),
                 ({"owned_by": "ninfer", "max_model_len": 131072}, "thinking_budget", 131072),
                 ({"owned_by": "tabbyAPI"}, "reasoning_budget_tokens", None),
                 ({"owned_by": "openai"}, None, None)]
        for card, field, window in cases:
            p = OpenAICompatProvider("m", base_url="http://127.0.0.1:9/v1")
            p._props, p._model_card = {}, {"id": "m", **card}
            sent = {}
            msg = NS(content="ok", tool_calls=None, reasoning_content=None, model_extra={})
            p.client = NS(chat=NS(completions=NS(create=lambda **kw: sent.update(kw) or NS(
                choices=[NS(message=msg, finish_reason="stop")], usage=NS(prompt_tokens=1, completion_tokens=1)))))
            p.chat("sys", [{"role": "user", "content": "hi"}], [], thinking=4096)
            extra = sent.get("extra_body") or {}
            if field:
                self.assertEqual(extra.get(field), 4096, card)
            for other in {"thinking_token_budget", "thinking_budget", "reasoning_budget_tokens"} - {field}:
                self.assertNotIn(other, extra, card)
            self.assertEqual(p.context_window(), window, card)

    def test_json_replies_are_enforced_by_schema_where_the_server_supports_it(self):
        from types import SimpleNamespace as NS
        from openai import BadRequestError
        from rameness.llm import OpenAICompatProvider
        schema = {"type": "object", "required": ["ok"], "properties": {"ok": {"type": "boolean"}}}
        for owner, sent_format in (("vllm", True), ("openai", False)):
            p = OpenAICompatProvider("m", base_url="http://127.0.0.1:9/v1")
            p._props, p._model_card = {}, {"id": "m", "owned_by": owner}
            sent = []
            msg = NS(content='{"ok": true}', tool_calls=None, reasoning_content=None, model_extra={})
            p.client = NS(chat=NS(completions=NS(create=lambda **kw: sent.append(kw) or NS(
                choices=[NS(message=msg, finish_reason="stop")], usage=NS(prompt_tokens=1, completion_tokens=1)))))
            self.assertEqual(p.complete_json("sys", "go", schema=schema), {"ok": True})
            self.assertEqual("response_format" in sent[-1], sent_format, owner)
            if sent_format:
                self.assertEqual(sent[-1]["response_format"]["json_schema"]["schema"], schema)
        # a server that refuses the schema: dropped once, the request retried without it
        p = OpenAICompatProvider("m", base_url="http://127.0.0.1:9/v1")
        p._props, p._model_card = {}, {"id": "m", "owned_by": "vllm"}
        calls = []

        def create(**kw):
            calls.append(kw)
            if "response_format" in kw:
                err = BadRequestError.__new__(BadRequestError)      # what the server's 400 becomes
                Exception.__init__(err, "response_format is not supported")
                raise err
            return NS(choices=[NS(message=msg, finish_reason="stop")], usage=NS(prompt_tokens=1, completion_tokens=1))

        p.client = NS(chat=NS(completions=NS(create=create)))
        self.assertEqual(p.complete_json("sys", "go", schema=schema), {"ok": True})
        self.assertEqual(["response_format" in c for c in calls], [True, False])
        self.assertTrue(p._format_rejected)

    def test_json_whose_strings_hold_code_fences_parses_whole(self):
        from rameness.llm import parse_json
        spec = {"id": "x.y", "script": "def f():\n    \"\"\"Example:\n```json\n{\"a\": 1}\n```\"\"\"\n"}
        self.assertEqual(parse_json(json.dumps(spec)), spec)
        self.assertEqual(parse_json('Here it is:\n```json\n{"a": 1}\n```'), {"a": 1})   # prose replies still work

    def test_the_run_review_asks_for_its_schema(self):
        from rameness import schemas
        from rameness.learning import Learner, RunStore
        from rameness.sops import Executor, Library
        lib = Library([])
        llm = FakeProvider(json_script=[{"procedures": []}])
        Learner(lib, Executor(lib, []), Jev(LexicalJev()), RunStore(self.tmp / "runs"), llm).review_runs(
            [{"task": "t", "steps": [{"tool": "bash", "input": {"command": "ls"}}]}])
        self.assertEqual(llm.schemas, [schemas.REVIEW])

    def test_line_anchors_edit_by_handle_and_refuse_stale_ones(self):
        from rameness.tools import Approver, Toolbox, anchor
        (self.tmp / "a.py").write_text("x = 1\ny = 2\nz = 3\n")
        tb = Toolbox(self.tmp, Approver("auto"), line_anchors=True)
        out = tb.call("read_file", {"path": "a.py"})[0]
        self.assertIn(f"2:{anchor('y = 2')}|y = 2", out)
        a2 = f"2:{anchor('y = 2')}"
        self.assertIn("replaced lines 2-2", tb.call("edit_lines", {"path": "a.py", "start": a2, "end": a2,
                                                                   "new": "y = 20\nw = 4"})[0])
        self.assertEqual((self.tmp / "a.py").read_text(), "x = 1\ny = 20\nw = 4\nz = 3\n")
        out = tb.call("edit_lines", {"path": "a.py", "start": a2, "end": a2, "new": "q"})[0]     # stale anchor
        self.assertIn("no longer matches", out)
        self.assertIn("bad anchor", tb.call("edit_lines", {"path": "a.py", "start": "2", "end": "2", "new": ""})[0])
        # anchors pasted with their line text, or as a block of lines, as models often do
        a3, a4 = f"3:{anchor('w = 4')}", f"4:{anchor('z = 3')}"
        out = tb.call("edit_lines", {"path": "a.py", "start": f"{a3}|w = 4\n{a4}|z = 3", "end": f"{a3}|w = 4\n{a4}|z = 3",
                                     "new": "v = 5"})[0]
        self.assertIn("replaced lines 3-4", out)
        self.assertEqual((self.tmp / "a.py").read_text(), "x = 1\ny = 20\nv = 5\n")

    def test_a_stop_right_after_announcing_a_step_is_sent_back_to_take_it(self):
        cfg = config.load(self.tmp, {"provider": "fake"})
        cfg["jev"]["backend"] = "lexical"; cfg["permissions"]["mode"] = "auto"; cfg["learning"]["enabled"] = False
        cfg["hook_selection"] = "all"
        h = Harness(cfg, llm=FakeProvider([
            Response("", [ToolCall("1", "bash", {"command": "echo hi"})], "tool_use"),
            Response("Found it: the loop is off by one. Let me fix the condition:", [], "end_turn"),
            Response("", [ToolCall("2", "write_file", {"path": "x.txt", "content": "fixed"})], "tool_use"),
            Response("Done: x.txt holds the fix, verified above.", [], "end_turn")]))
        h.run("fix the bug", allow_direct=False)
        nudges = [m["content"] for m in h.messages if m["role"] == "user" and "did not do it" in str(m["content"])]
        self.assertEqual(len(nudges), 1)
        self.assertEqual((self.tmp / "x.txt").read_text(), "fixed")

    def test_images_are_shown_only_to_models_that_see_and_old_ones_are_pruned(self):
        import base64
        from rameness.tools import Approver, Toolbox
        png = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==")
        (self.tmp / "s.png").write_bytes(png)
        blind = Toolbox(self.tmp, Approver("auto"))
        self.assertIn("cannot see images", blind.call("read_file", {"path": "s.png"})[0])
        self.assertEqual(blind.take_images(), [])
        tb = Toolbox(self.tmp, Approver("auto"), vision=True)
        self.assertIn("shown below", tb.call("read_file", {"path": "s.png"})[0])
        self.assertEqual(tb.take_images()[0]["data"], base64.b64encode(png).decode())
        # in the loop: the image rides on its tool result; only the latest vision_keep stay in view
        read = lambda i: Response("", [ToolCall(str(i), "read_file", {"path": "s.png"})], "tool_use")
        cfg = config.load(self.tmp, {"provider": "fake"})
        cfg["jev"]["backend"] = "lexical"; cfg["permissions"]["mode"] = "auto"; cfg["learning"]["enabled"] = False
        cfg["hook_selection"] = "all"; cfg["vision"] = True; cfg["vision_keep"] = 1; cfg["dedup_reads"] = False
        cfg["vision_describe_first"] = cfg["vision_final_look"] = True        # experimental, off by default
        todo = Response("", [ToolCall("t", "todo_write", {"todos": [{"content": "look", "status": "completed"}]})], "tool_use")
        done = lambda: Response("done", [], "end_turn")
        h = Harness(cfg, llm=FakeProvider([todo, read(0), read(1), done(), done()]))
        h.run("look at s.png", allow_direct=False)
        shown = [m for m in h.messages if m.get("name") == "read_file"]
        self.assertEqual([bool(m.get("images")) for m in shown], [False, True])
        self.assertIn("no longer shown", shown[0]["content"])
        self.assertIn("You can see images", h._environment("."))
        self.assertIn("First describe what the image actually shows", shown[1]["content"])
        review = [m["content"] for m in h.messages if "review the work against the task" in str(m.get("content"))]
        self.assertTrue(review and "look at the final result the way its user would see it" in review[0])
        # both off: no describe prompt, no final look
        cfg["vision_describe_first"] = cfg["vision_final_look"] = False
        h2 = Harness(cfg, llm=FakeProvider([todo, read(0), read(1), done(), done()]))
        h2.run("look at s.png", allow_direct=False)
        self.assertFalse(any("First describe" in str(m.get("content")) for m in h2.messages))
        self.assertFalse(any("look at the final result" in str(m.get("content")) for m in h2.messages))

    def test_a_kill_pattern_that_matches_the_agent_itself_is_refused(self):
        import subprocess, sys
        from rameness.tools import Approver, Toolbox
        # a child whose command line mentions "static web server", as the agent's does when the task says so
        code = ("import sys; sys.path.insert(0, %r); from rameness.tools import Approver, Toolbox; from pathlib import Path; "
                "tb = Toolbox(Path('.'), Approver('auto')); "
                "print(tb.call('bash', {'command': 'pkill -f serve 2>/dev/null; echo done'})[0]); "
                "print(tb.call('bash', {'command': 'pkill -f ' + 'no-such-' + 'proc 2>/dev/null; echo ok'})[0])"
                % str(Path(__file__).resolve().parents[1]))
        out = subprocess.run([sys.executable, "-c", code, "open through a static web server"],
                             capture_output=True, text=True, timeout=60).stdout
        self.assertIn("would also kill this agent itself", out)
        self.assertNotIn("refused", out.split("would also kill")[-1])     # a pattern that doesn't hit the agent runs

    def test_fuzzy_edit_applies_a_whitespace_only_mismatch_once(self):
        from rameness.tools import Approver, Toolbox
        (self.tmp / "a.py").write_text("def f():\n    if x:\n        return 1\n    return 2\n")
        old, new = "if x:\n    return 1", "if x:\n    return 10\nlog()"            # wrong indentation, as models do
        off = Toolbox(self.tmp, Approver("auto"))
        self.assertIn("old string not found", off.call("edit_file", {"path": "a.py", "old": old, "new": new})[0])
        on = Toolbox(self.tmp, Approver("auto"), edit_fuzzy=True)
        self.assertIn("apart from whitespace", on.call("edit_file", {"path": "a.py", "old": old, "new": new})[0])
        self.assertEqual((self.tmp / "a.py").read_text(),
                         "def f():\n    if x:\n        return 10\n    log()\n    return 2\n")
        (self.tmp / "b.py").write_text("x = 1\ny = 2\nx = 1\n")                    # ambiguous: two matches
        self.assertIn("old string not found", on.call("edit_file", {"path": "b.py", "old": "  x = 1", "new": "z"})[0])

    def test_a_failed_edit_shows_where_the_file_is_closest(self):
        from rameness.tools import Approver, Toolbox, anchor
        (self.tmp / "a.js").write_text("function f() {\n    return 1;\n}\n\nfunction g() {}\n")
        tb = Toolbox(self.tmp, Approver("auto"), line_anchors=True)
        out = tb.call("edit_file", {"path": "a.js", "old": "function f() {\n  return 1;\n}", "new": "x"})[0]
        self.assertIn("old string not found; the closest match (identical apart from whitespace) is lines 1-3", out)
        self.assertIn(f"2:{anchor('    return 1;')}|    return 1;", out)
        out = tb.call("edit_file", {"path": "a.js", "old": "class Unrelated extends Nothing", "new": "x"})[0]
        self.assertIn("nothing similar", out)

    def test_unchanged_rereads_return_a_stub_while_still_in_context(self):
        (self.tmp / "big.py").write_text("x = 1\n" * 50)
        read = lambda i: Response("", [ToolCall(str(i), "read_file", {"path": "big.py"})], "tool_use")
        cfg = config.load(self.tmp, {"provider": "fake"})
        cfg["jev"]["backend"] = "lexical"; cfg["permissions"]["mode"] = "auto"; cfg["learning"]["enabled"] = False
        cfg["dedup_reads"] = True; cfg["hook_selection"] = "all"
        h = Harness(cfg, llm=FakeProvider([read(0), read(1),
                                           Response("", [ToolCall("w", "write_file", {"path": "big.py", "content": "y = 2\n"})], "tool_use"),
                                           read(2), Response("done", [], "end_turn")]))
        h.run("read big.py twice", allow_direct=False)
        outs = [m["content"] for m in h.messages if m.get("name") == "read_file"]
        self.assertIn("x = 1", outs[0])
        self.assertTrue(outs[1].startswith("[unchanged] big.py has not changed since you read it at turn 0"))
        self.assertIn("y = 2", outs[2])                           # the file changed: read in full again

    def test_glob_web_fetch_and_bash_timeout_cap(self):
        import http.server, threading
        from rameness.tools import Approver, Toolbox, BASH_MAX_TIMEOUT
        (self.tmp / "src").mkdir()
        (self.tmp / "src" / "a.js").write_text("1")
        (self.tmp / "b.py").write_text("2")
        tb = Toolbox(self.tmp, Approver("auto"))
        self.assertEqual(tb.call("glob", {"pattern": "**/*.js"}), ("src/a.js", False))
        class H(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                body = b"<html><head><title>t</title><script>x()</script></head><body><h1>Docs</h1><p>Use it.</p></body></html>"
                self.send_response(200); self.send_header("Content-Type", "text/html"); self.end_headers()
                self.wfile.write(body)
            def log_message(self, *a): pass
        srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            out, err = tb.call("web_fetch", {"url": f"http://127.0.0.1:{srv.server_address[1]}/doc"})
        finally:
            srv.shutdown()
        self.assertFalse(err)
        self.assertIn("# Docs", out)
        self.assertIn("Use it.", out)
        self.assertNotIn("x()", out)
        self.assertEqual(BASH_MAX_TIMEOUT, 600)



class TestDiscover(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def run_log(self, name, cmds):
        d = self.tmp / name
        d.mkdir()
        ev = []
        for i, (tool, cmd, out) in enumerate(cmds):
            ev.append({"t": i, "kind": "llm", "turn": i, "text": "let me start the server" if "server" in cmd else "",
                       "tools": [tool]})
            ev.append({"t": i, "kind": "tool", "turn": i, "tool": tool, "input": json.dumps({"command": cmd, "path": cmd}),
                       "ok": True, "output": out, "seconds": 1})
        (d / "events.jsonl").write_text("\n".join(json.dumps(e) for e in ev))

    def test_finds_the_costly_repeated_procedure_across_runs(self):
        from rameness import discover as dsc
        for r, port in (("r1", 8000), ("r2", 8123), ("r3", 9001)):
            self.run_log(r, [("write_file", "index.html", "wrote"),
                             ("bash", f"python3 -m http.server {port} > /tmp/s.log 2>&1 &", "exit=0"),
                             ("bash", f"pkill -f http.server; python3 -m http.server {port} &", "EADDRINUSE"),
                             ("bash", f"nohup python3 -m http.server {port + 1} >/dev/null 2>&1 &", "exit=0"),
                             ("bash", "git status", "clean")])
        runs = dsc.load_paths([str(self.tmp)])
        rows = dsc.discover(runs, None, top=5)
        top = rows[0]
        self.assertIn("prog:python3:http.server", top["label"])
        self.assertEqual(top["runs"], 3)
        self.assertGreaterEqual(top["steps"], 6)
        self.assertFalse(any("write_file" in " ".join(r["label"]) for r in rows))   # generative work is not an SOP
        self.assertIn("Candidate SOPs from 3 runs", dsc.report(rows, 3, 15))



class TestThinking(unittest.TestCase):
    """JEV picks one of five thinking levels per turn; the level's budget reaches the model call."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        os.environ["RAMENESS_HOME"] = str(self.tmp / "home")
        (self.tmp / ".rameness").mkdir()

    def test_five_levels_and_their_budgets(self):
        from rameness.thinking import LEVELS, Thinking
        self.assertEqual([o.id for o in LEVELS], ["minimal", "brief", "normal", "deep", "maximum"])
        t = Thinking.from_cfg(Jev(LexicalJev()), {"thinking": {"maximum": 5000}})
        self.assertEqual(t.budgets, {"minimal": 512, "brief": 2048, "normal": 4096, "deep": 8192, "maximum": 5000})
        self.assertEqual(Thinking.from_cfg(None, {"thinking": {"mode": "fixed"}}).decide(3, "t", [], "", "high")[:3],
                         ("maximum", 16384, "high"))

    def test_each_turn_gets_the_budget_jev_chose(self):
        from rameness.jev import Decision
        seen = []

        class Rec(FakeProvider):
            def chat(self, system, messages, tools, effort="high", max_tokens=32000, fast=False, thinking=None):
                seen.append((effort, thinking))
                return super().chat(system, messages, tools, effort, max_tokens, fast, thinking)

        cfg = config.load(self.tmp, {"provider": "fake"})
        cfg["jev"]["backend"] = "lexical"
        cfg["permissions"]["mode"] = "auto"
        cfg["learning"]["enabled"] = False
        h = Harness(cfg, llm=Rec([Response("", [ToolCall("1", "bash", {"command": "false"})], "tool_use"),
                                  Response("done", [], "end_turn")]))
        real = h.jev.choose
        picks = iter(["maximum", "deep"])
        def choose(q, query, opts, context=""):
            if q.startswith("How much should the agent think"):
                level = next(picks)
                return Decision("x", q, {o.id: (0.9 if o.id == level else 0.025) for o in opts}, "t")
            return real(q, query, opts, context)
        h.jev.choose = choose
        h.run("look into it", allow_direct=False)
        self.assertEqual(seen[:2], [("high", 16384), ("high", 8192)])     # maximum: never below high


if __name__ == "__main__":
    unittest.main()
