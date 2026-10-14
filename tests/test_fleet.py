import json
import os
import subprocess
import tempfile
import time
import unittest
from dataclasses import replace
from pathlib import Path

from rameness.fleet import envs, sessions
from rameness.fleet.manager import FLEET_DEFAULTS, Fleet
from rameness.fleet.store import Store
from rameness.improve import Tuner
from rameness.jev import Jev, LexicalJev, Option
from rameness.llm import FakeProvider


def git(cwd, *args):
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", *args], cwd=cwd, check=True,
                   capture_output=True)


class FleetEnv(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        os.environ["RAMENESS_HOME"] = str(self.tmp / "home")
        self.repo = self.tmp / "repo"
        self.repo.mkdir()
        git(self.repo, "init", "-q", "-b", "main")
        (self.repo / "app.py").write_text("print('v1')\n")
        git(self.repo, "add", ".")
        git(self.repo, "commit", "-qm", "init")
        (self.repo / ".rameness").mkdir()
        (self.repo / ".rameness" / "config.json").write_text(json.dumps({"jev": {"backend": "lexical"}}))

    def fleet(self, **over):
        cfg = json.loads(json.dumps(FLEET_DEFAULTS))
        # a "CLI agent" that edits the file: stands in for claude/codex so no model is needed
        cfg.update(backend="subprocess", discover=False, slots=[
            {"id": "cli:editor", "kind": "cli", "model": "editor", "capacity": 2, "traits": "coding edits",
             "argv": ["bash", "-c", "echo \"print('v2')\" > app.py; echo \"edited: $0\"", "{task}"]}],
            decision_policy={"*": "jev"})
        cfg.update(over)
        return Fleet(self.repo, cfg, planner=False, probe=False)

    def run_until(self, f, pred, timeout=15):
        end = time.time() + timeout
        while time.time() < end:
            f.tick()
            if pred():
                return True
            time.sleep(0.2)
        return False


class TestFleetLifecycle(FleetEnv):
    def test_ask_spawn_run_review_merge(self):
        f = self.fleet()
        r = f.ask("fix the version string printed by app.py")
        self.assertEqual(r["route"], "delegate")
        aid = r["agents"][0]
        self.assertTrue(self.run_until(f, lambda: f.get(aid)["status"] == "done"), f.store.events())
        a = f.get(aid)
        self.assertEqual(a["runtime"], "cli:editor")
        self.assertEqual(a["env"], "local")
        self.assertTrue(a["branch"].startswith("rameness/"))
        self.assertIn("edited:", a["result"])
        self.assertEqual((self.repo / "app.py").read_text(), "print('v1')\n")      # isolated in its worktree
        esc = [e for e in f.store.escalations() if e["kind"] == "merge"]
        self.assertEqual(len(esc), 1)                                              # review mode: director merges
        f.answer(esc[0]["id"], "merge")
        self.assertEqual((self.repo / "app.py").read_text(), "print('v2')\n")
        # every choice is on record for the shadow view
        qs = {d["question"] for d in f.store.decisions()}
        self.assertIn("Which model or agent runtime should run this task?", qs)
        self.assertIn("Which environment should this agent's tools run in?", qs)
        # outcome feedback flowed to the tuner
        _, fb = Tuner(f.state).load()
        self.assertTrue(any(v.get("outcome") == "ok" for v in fb.values()))

    def test_self_reported_done_is_finalized_and_merged(self):
        """rameness workers set their own status; the manager must still commit and merge."""
        f = self.fleet(mode="local", slots=[{"id": "cli:quiet", "kind": "cli", "model": "quiet", "capacity": 1,
                                             "argv": ["bash", "-c", "sleep 30"]}])
        a = f.spawn("fix it", slot="cli:quiet")
        self.assertTrue(self.run_until(f, lambda: f.get(a["id"])["status"] == "running"))
        wt = Path(f.get(a["id"])["worktree"])
        (wt / "app.py").write_text("print('fixed')\n")
        (wt / "__pycache__").mkdir()
        (wt / "__pycache__" / "x.pyc").write_text("junk")
        f.store.update_agent(a["id"], result="fixed")
        f.store.set_status(a["id"], "done")            # what worker.py does
        f.tick()
        self.assertEqual((self.repo / "app.py").read_text(), "print('fixed')\n")
        self.assertFalse((self.repo / "__pycache__").exists())
        self.assertTrue(f.get(a["id"])["meta"]["finalized"])
        f.retire(a["id"])

    def test_crud_pause_resume_reassign_retire_delete(self):
        f = self.fleet(slots=[{"id": "cli:slow", "kind": "cli", "model": "slow", "capacity": 2,
                               "argv": ["bash", "-c", "sleep 30"]},
                              {"id": "cli:other", "kind": "cli", "model": "other", "capacity": 1,
                               "argv": ["bash", "-c", "sleep 30"]}])
        a = f.spawn("long task", slot="cli:slow")
        self.assertTrue(self.run_until(f, lambda: f.get(a["id"])["status"] == "running"))
        f.pause(a["id"])
        self.assertEqual(f.get(a["id"])["status"], "paused")
        f.resume(a["id"])
        self.assertEqual(f.get(a["id"])["status"], "queued")
        f.reassign(a["id"], slot="cli:other")
        self.assertTrue(self.run_until(f, lambda: f.get(a["id"])["status"] == "running"))
        self.assertEqual(f.get(a["id"])["slot"], "cli:other")
        f.retire(a["id"])
        self.assertEqual(f.get(a["id"])["status"], "retired")
        f.retire(a["id"], delete=True)
        self.assertIsNone(f.store.agent(a["id"]))

    def test_capacity_is_a_hard_limit(self):
        f = self.fleet(slots=[{"id": "cli:one", "kind": "cli", "model": "one", "capacity": 1,
                               "argv": ["bash", "-c", "sleep 30"]}])
        a, b = f.spawn("task a"), f.spawn("task b")
        f.tick()
        states = sorted([f.get(a["id"])["status"], f.get(b["id"])["status"]])
        self.assertEqual(states, ["queued", "running"])
        f.retire(a["id"]); f.retire(b["id"])

    def test_dependencies_and_hierarchy(self):
        f = self.fleet()
        lead = f.spawn("build the feature", role="lead")
        f.tick()                                        # lead starts, plans (no planner: one child)
        kids = f.store.children(lead["id"])
        self.assertEqual(len(kids), 1)
        self.assertEqual(f.get(lead["id"])["status"], "running")
        second = f.spawn("follow-up", parent=lead["id"], depends=[kids[0]["id"]])
        f.tick()
        self.assertEqual(f.get(second["id"])["status"], "queued")      # waits for its dependency
        self.assertTrue(self.run_until(f, lambda: f.get(lead["id"])["status"] == "done", 25))
        tree = f.tree()
        self.assertEqual(tree["children"][0]["id"], lead["id"])
        self.assertEqual(len(tree["children"][0]["children"]), 2)

    def test_fork_group_picks_one_winner(self):
        f = self.fleet()
        a = f.spawn("optimize the hot loop")
        forks = f.fork(a["id"], ["approach one", "approach two"])
        self.assertEqual(len(forks), 2)
        group = [a["id"]] + [x["id"] for x in forks]
        self.assertTrue(self.run_until(f, lambda: all(f.get(i)["status"] in ("done", "retired") for i in group)
                                       and sum(f.get(i)["meta"].get("integrated", False) for i in group) == 1, 25))
        self.assertEqual(sum(1 for i in group if f.get(i)["meta"].get("fork_lost")), 2)

    def test_a_follow_up_returns_to_the_runtime_that_did_the_work_with_a_fresh_log(self):
        f = self.fleet()
        f.slots.append(replace(f.slots[0], id="cli:other", model="other"))
        aid = f.spawn("fix the version string printed by app.py", role_explicit=True)["id"]
        self.assertTrue(self.run_until(f, lambda: f.get(aid)["status"] == "done"), f.store.events())
        first = f.get(aid)
        self.assertEqual(f.prompt(aid, "also print the date"), "reopened")
        self.assertTrue(f.get(aid)["meta"]["pinned_slot"])
        self.assertTrue(self.run_until(f, lambda: f.get(aid)["status"] == "done" and f.get(aid)["meta"].get("finalized")))
        again = f.get(aid)
        self.assertEqual(again["slot"], first["slot"])
        self.assertIn("also print the date", again["result"])
        self.assertEqual(again["result"].count("edited:"), 1)            # this run's output only
        self.assertTrue(Path(again["handle"]["log"]).with_name(f"{aid}.prev.log").exists())
        f.retire(aid)                                                      # closing a finished agent keeps its outcome
        self.assertEqual(f.get(aid)["status"], "done")
        self.assertTrue(f.get(aid)["meta"]["closed"])
        self.assertIsNone(f.get(aid)["worktree"])

    def test_with_no_runtime_left_the_director_is_asked_once_and_nothing_is_re_decided(self):
        f = self.fleet()
        aid = f.spawn("fix the version string printed by app.py", role_explicit=True,
                      meta={"exclude_slots": ["cli:editor"], "fork_decided": True})["id"]
        f.tick()
        made = len(f.store.decisions(agent=aid))
        for _ in range(5):
            f.tick()
        esc = [e for e in f.store.escalations() if e["agent"] == aid]
        self.assertEqual(len(esc), 1)
        self.assertIn("no runtime is left", esc[0]["question"])
        self.assertEqual(len(f.store.decisions(agent=aid)), made)     # waiting is not a decision
        self.assertFalse([d for d in f.store.decisions(agent=aid) if d["question"].startswith("Which environment")])
        f.answer(esc[0]["id"], "retry")                                  # clears the exclusions
        self.assertTrue(self.run_until(f, lambda: f.get(aid)["status"] == "done"), f.store.events())

    def test_a_director_retry_after_failure_runs_and_reports_again(self):
        f = self.fleet(max_attempts=0)
        f.slots[0].argv = ["bash", "-c", "test -e .ok || { echo nope; exit 1; }; echo \"print('v2')\" > app.py", "{task}"]
        aid = f.spawn("fix the version string printed by app.py", role_explicit=True, meta={"fork_decided": True})["id"]
        self.assertTrue(self.run_until(f, lambda: any(e["agent"] == aid and e["kind"] == "failure"
                                                      for e in f.store.escalations())), f.store.events())
        f.slots[0].argv = ["bash", "-c", "echo \"print('v2')\" > app.py; echo fixed", "{task}"]
        f.answer([e for e in f.store.escalations() if e["agent"] == aid][0]["id"], "retry")
        self.assertTrue(self.run_until(f, lambda: any(e["kind"] == "merge" for e in f.store.escalations())),
                        f.store.events())                                # finished for real: offered for merge

    def test_a_follow_up_after_a_merge_builds_on_the_merged_work(self):
        f = self.fleet()
        f.slots[0].argv = ["bash", "-c", "git rev-parse --git-dir >/dev/null || exit 3; echo \"$0\" | grep -o '[a-z]* note' | tail -1 >> notes.txt",
                           "{task}"]
        aid = f.spawn("first note", role_explicit=True, meta={"fork_decided": True})["id"]
        merge = lambda: [e for e in f.store.escalations() if e["kind"] == "merge"]
        self.assertTrue(self.run_until(f, lambda: merge()), f.store.events())
        f.answer(merge()[0]["id"], "merge")
        self.assertIsNone(f.get(aid)["worktree"])
        f.prompt(aid, "second note")
        self.assertTrue(self.run_until(f, lambda: merge()), f.store.events())
        self.assertEqual(f.get(aid)["status"], "done")
        self.assertTrue(f.get(aid)["worktree"])                          # isolated again, in a fresh worktree
        self.assertNotIn("second note", (self.repo / "notes.txt").read_text())   # nothing lands before the merge
        f.answer(merge()[0]["id"], "merge")
        notes = (self.repo / "notes.txt").read_text()
        self.assertIn("first note", notes)
        self.assertIn("second note", notes)

    def test_a_conversational_message_is_answered_by_the_manager_without_an_agent(self):
        from rameness.llm import Response

        class Planner:
            slot_id = "local"
            seen = []

            def chat(self, system, messages, tools, **kw):
                self.seen.append(messages)
                return Response("Doing well. Two agents are idle; ask me for work any time.", [], "end_turn")
        f = self.fleet()
        f.planner = Planner()
        r = f.ask("hey, how are you?")
        self.assertEqual(r["route"], "answer")
        self.assertTrue(self.run_until(f, lambda: any(e["type"] == "reply" for e in f.store.events(agent="manager"))))
        self.assertEqual([a for a in f.store.agents() if a["role"] != "manager"], [])     # nobody was started
        self.assertIn("Doing well", [e for e in f.store.events(agent="manager") if e["type"] == "reply"][0]["message"])
        self.assertEqual(Planner.seen[0][-1], {"role": "user", "content": "hey, how are you?"})

    def test_without_a_model_of_its_own_the_manager_delegates_a_conversational_message(self):
        f = self.fleet()
        r = f.ask("hey, how are you?")
        self.assertEqual(r["route"], "delegate")


class TestComfortGate(FleetEnv):
    def test_director_policy_defers_then_applies_answer(self):
        f = self.fleet(decision_policy={"*": "director"})
        r = f.ask("fix the version string printed by app.py")
        self.assertEqual(r["route"], "awaiting-director")
        esc = f.store.escalations()
        self.assertEqual(esc[0]["kind"], "decision")
        self.assertIn(esc[0]["payload"]["recommended"], esc[0]["options"])
        self.assertTrue(any(d["gate"] == "deferred" for d in f.store.decisions()))
        f.answer(esc[0]["id"], "research")               # director overrides JEV's lean
        d = [d for d in f.store.decisions() if d["id"] == esc[0]["payload"]["decision"]][0]
        self.assertEqual((d["gate"], d["chosen"]), ("director", "research"))
        _, fb = Tuner(f.state).load()
        self.assertEqual(fb[d["id"]]["label"], "research")        # the override is a training label
        # the resumed intake now waits on the next deferred decision (role), not the same one again
        self.assertFalse([e for e in f.store.escalations() if e["id"] == esc[0]["id"]])

    def test_significance_gate(self):
        j = Jev(LexicalJev())
        d = j.choose("Should the branch be merged?", "merge into main and deploy to production and delete the old db",
                     [Option("merge", "merge deploy"), Option("wait", "wait")])
        self.assertTrue(j.comfort("Should the branch be merged?", "merge into main and deploy to production", d).needs_user)
        d2 = j.choose("Which environment?", "run unit tests", [Option("local", "local tests"), Option("gpu", "gpu cuda")])
        self.assertFalse(j.comfort("Which environment?", "run unit tests locally", d2).needs_user)


class TestTuner(unittest.TestCase):
    def test_calibrate_and_proposals(self):
        state = Path(tempfile.mkdtemp())
        j = Jev(LexicalJev(), state / "decisions.jsonl", tuning_path=state / "jev_tuning.json")
        opts = [Option("a", "alpha"), Option("b", "beta")]
        ds = [j.choose("Q?", "alpha thing", opts) for _ in range(3)]
        t = Tuner(state)
        for d in ds:
            t.feedback(d.id, label="b")
        w = t.calibrate()
        self.assertGreater(w["Q?::b"], 1)
        self.assertLess(w["Q?::a"], 1)
        after = j.choose("Q?", "alpha thing", opts)          # hot-reloaded weights shift the decision
        self.assertGreater(after.probs["b"], ds[0].probs["b"])
        llm = FakeProvider(json_script=[{"cues": {"b": "alpha thing"}, "rationale": "b handles alpha"}])
        props = t.propose(llm)
        self.assertEqual(props[0]["cues"], {"b": "alpha thing"})
        t.resolve(props[0]["id"], apply=True)
        self.assertIn("alpha thing", t.tuning()["cues"]["Q?::b"])
        out = state / "train.jsonl"
        self.assertEqual(t.export(out), 3)


class TestEnvsAndSessions(unittest.TestCase):
    def test_exec_prefix_environment_file_ops(self):
        # an "exec" environment with an identity prefix exercises the remote code path locally
        e = envs.Environment("fake-remote", "exec", tempfile.mkdtemp(), prefix=["env"])
        e.write_text("sub/x.txt", "hello")
        self.assertEqual(e.read_text("sub/x.txt"), "hello")
        self.assertEqual(e.run("cat sub/x.txt").out, "hello")
        self.assertTrue(e.probe()["reachable"])

    def test_remote_toolbox_and_sop(self):
        from rameness.sops import Executor, Library
        from rameness.tools import Approver, Toolbox
        wd = tempfile.mkdtemp()
        e = envs.Environment("fake-remote", "exec", wd, prefix=["env"])
        tb = Toolbox(Path(wd), Approver("auto"), e)
        tb.call("write_file", {"path": "a.txt", "content": "one two\n"})
        self.assertIn("one two", tb.call("read_file", {"path": "a.txt"})[0])
        tb.call("edit_file", {"path": "a.txt", "old": "two", "new": "three"})
        self.assertIn("a.txt:1:one three", tb.call("grep", {"pattern": "three"})[0])
        self.assertIn("exit=0", tb.call("bash", {"command": "ls"})[0])
        lib = Library.default(Path(wd), Path(wd) / "home")
        ex = Executor(lib, ["fs:read"], cwd=Path(wd), env=e)
        self.assertEqual(ex.run("fs.find_files", {"pattern": "*.txt"})["files"], ["a.txt"])

    def test_store_reopens(self):
        p = Path(tempfile.mkdtemp()) / "f.db"
        Store(p).create_agent(id="x", role="associate", title="t", task="t")
        self.assertEqual(Store(p).agent("x")["title"], "t")


@unittest.skipUnless(sessions.TmuxBackend.available(), "tmux not installed")
class TmuxSessions(unittest.TestCase):
    def test_a_rerun_replaces_the_agents_window(self):
        class Tmux(sessions.TmuxBackend):
            SESSION = f"rameness-test-{os.getpid()}"
        b = Tmux(Path(tempfile.mkdtemp()))
        try:
            for _ in range(2):
                h = b.start("a-1", ["true"], tempfile.gettempdir())
            names = b._tmux("list-windows", "-t", Tmux.SESSION, "-F", "#{window_name}").stdout.split()
            self.assertEqual(names.count("a-1"), 1)
            b.stop(h)
            self.assertFalse(b.alive(h))
        finally:
            b._tmux("kill-session", "-t", Tmux.SESSION)

class ReportsAndTitles(unittest.TestCase):
    def test_a_cli_agents_report_is_its_final_answer_not_its_transcript(self):
        from rameness.fleet.slots import cli_report
        out = ("model: gpt\n--------\nuser\nCreate hello.txt\ncodex\nI'll create it.\nexec\nprintf hi > hello.txt\n"
               "codex\nCreated hello.txt.\ntokens used\n3,791\nCreated hello.txt.\n")
        self.assertEqual(cli_report("cli:codex", out), "Created hello.txt.")
        self.assertEqual(cli_report("cli:codex", "error: unexpected argument"), "error: unexpected argument")
        self.assertEqual(cli_report("cli:claude", "Done: added the test."), "Done: added the test.")

    def test_titles_end_on_a_word(self):
        from rameness.fleet.manager import short_title
        self.assertEqual(short_title("Fix the typo"), "Fix the typo")
        self.assertEqual(short_title("Create a file hello.txt containing the single line: hello from rameness"),
                         "Create a file hello.txt containing the single line: hello…")
        self.assertEqual(short_title("Build the API\n\nwith tests and docs"), "Build the API")


if __name__ == "__main__":
    unittest.main()
