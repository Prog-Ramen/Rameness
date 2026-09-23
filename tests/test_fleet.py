import json
import os
import subprocess
import tempfile
import time
import unittest
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


if __name__ == "__main__":
    unittest.main()
