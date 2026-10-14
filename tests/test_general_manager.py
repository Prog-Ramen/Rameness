import json
import os
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

from rameness.fleet.manager import FLEET_DEFAULTS, Fleet
from rameness.fleet.slots import Slot, add_dir_args
from rameness.llm import Response


def git(cwd, *args):
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *args], cwd=cwd, check=True,
                   capture_output=True)


class GeneralManager(unittest.TestCase):
    """One manager for every request: no project list and no lookup. Each chat's agents start in a folder of the
    chat's own and find the project a task is about themselves; chats keep their conversations apart."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        os.environ["RAMENESS_HOME"] = str(self.tmp / "rhome")
        self.home = self.tmp / "home"
        (self.home / ".rameness").mkdir(parents=True)
        (self.home / ".rameness" / "config.json").write_text(json.dumps({"jev": {"backend": "lexical"}}))
        self.project = self.tmp / "projects" / "printer"
        self.project.mkdir(parents=True)
        (self.project / "app.py").write_text("print('v1')\n")
        git(self.project, "init", "-q", "-b", "main")
        git(self.project, "add", ".")
        git(self.project, "commit", "-qm", "init")

    def fleet(self, argv=None, **over):
        cfg = json.loads(json.dumps(FLEET_DEFAULTS))
        # the stand-in agent records where it started, then (as a real agent would) finds the project and edits it
        argv = argv or ["bash", "-c", f"pwd > started_in; echo \"print('v2')\" > {self.project}/app.py; echo done: $0",
                        "{task}"]
        cfg.update(backend="subprocess", discover=False, general=True, decision_policy={"*": "jev"},
                   slots=[{"id": "cli:editor", "kind": "cli", "model": "editor", "capacity": 2, "traits": "coding edits",
                           "argv": argv}])
        cfg.update(over)
        return Fleet(self.home, cfg, planner=False, probe=False)

    def run_until(self, f, pred, timeout=15):
        end = time.time() + timeout
        while time.time() < end:
            f.tick()
            if pred():
                return True
            time.sleep(0.2)
        return False

    def test_agents_start_in_their_chats_folder_and_find_the_project_themselves(self):
        f = self.fleet()
        r = f.ask("fix the version string that app.py prints in the printer project", chat="c1")
        aid = r["agents"][0]
        self.assertTrue(self.run_until(f, lambda: f.get(aid)["status"] == "done"), f.store.events())
        folder = self.home / "work" / "c1"
        self.assertEqual((folder / "started_in").read_text().strip(), str(folder))
        self.assertEqual((self.project / "app.py").read_text(), "print('v2')\n")
        self.assertFalse([d for d in f.store.decisions_since(0) if "project" in d["question"].lower()])  # no lookup
        other = f.ask("fix the version string that app.py prints in the printer project", chat="c2")
        self.assertEqual(f.get(other["agents"][0])["meta"]["workdir"], str(self.home / "work" / "c2"))

    def test_cli_agents_may_work_beyond_their_folder(self):
        claude = Slot("cli:claude", "cli", "claude", argv=["claude", "-p", "{task}"])
        self.assertEqual(add_dir_args(claude, ["~"]), ["--add-dir", os.path.expanduser("~")])
        self.assertEqual(add_dir_args(Slot("cli:pi", "cli", "pi"), ["~"]), [])         # no sandbox to widen
        self.assertEqual(add_dir_args(Slot("q", "vllm", "qwen"), ["~"]), [])           # not a CLI agent
        f = self.fleet(argv=["bash", "-c", "echo \"$@\" > argv; echo ok", "x", "{task}"], agent_dirs=[str(self.project)])
        f.slots[0].model = "claude"
        aid = f.ask("print the arguments", chat="a")["agents"][0]
        self.assertTrue(self.run_until(f, lambda: f.get(aid)["status"] == "done"), f.store.events())
        self.assertIn(f"--add-dir {self.project}", (self.home / "work" / "a" / "argv").read_text())

    def test_direct_mode_one_agent_takes_every_message_of_a_chat(self):
        f = self.fleet(work_mode="direct")
        first = f.ask("fix the version string that app.py prints in the printer project", chat="d")
        self.assertEqual(first["route"], "direct")
        self.assertTrue(self.run_until(f, lambda: f.get(first["agents"][0])["status"] == "done"))
        second = f.ask("also print the date", chat="d")
        self.assertEqual(second["agents"], first["agents"])
        self.assertFalse([d for d in f.store.decisions() if d["question"].startswith("How should the manager")])
        self.assertEqual(f.get(first["agents"][0])["role"], "associate")
        other = f.ask("fix the version string that app.py prints", chat="e")
        self.assertNotEqual(other["agents"], first["agents"])                     # another chat, another agent

    def test_the_managers_replies_keep_to_their_chat(self):
        seen = []

        class Planner:
            slot_id = "local"

            def chat(self, system, messages, tools, **kw):
                seen.append((system, messages))
                return Response(f"reply {len(seen)}", [], "end_turn")
        f = self.fleet()
        f.planner = Planner()
        f.ask("hey, how are you?", chat="a")
        self.assertTrue(self.run_until(f, lambda: len(seen) == 1))
        f.ask("hello, thanks!", chat="b")
        self.assertTrue(self.run_until(f, lambda: len(seen) == 2))
        self.assertEqual(seen[1][1], [{"role": "user", "content": "hello, thanks!"}])  # chat a is not in chat b
        self.assertIn("on this machine", seen[1][0])
        self.assertTrue(self.run_until(f, lambda: any(e["type"] == "reply" for e in f.store.events(chat="b"))))
        self.assertEqual([e["message"] for e in f.store.events(chat="b") if e["type"] == "reply"], ["reply 2"])
        self.assertEqual([a for a in f.store.agents() if a["role"] != "manager"], [])


    def test_a_follow_up_about_finished_work_sees_its_report(self):
        seen = []

        class Planner:
            slot_id = "local"

            def chat(self, system, messages, tools, **kw):
                seen.append(messages)
                return Response("Here is the full report.", [], "end_turn")
        f = self.fleet(argv=["bash", "-c", "echo 'Report: the printer prints v2 now.'", "{task}"])
        aid = f.ask("fix the version string that app.py prints in the printer project", chat="r")["agents"][0]
        self.assertTrue(self.run_until(f, lambda: f.get(aid)["status"] == "done"))
        f.planner = Planner()
        f.store.event("manager", "intake", "answer: read me the full report", chat="r")
        f._reply("read me the full report", "r")
        said = "\n".join(m["content"] for m in seen[0] if m["role"] == "assistant")
        self.assertIn("finished]", said)
        self.assertIn("Report: the printer prints v2 now.", said)

if __name__ == "__main__":
    unittest.main()
