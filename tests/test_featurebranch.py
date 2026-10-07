"""Feature cycles on git branches: the project directory only ever holds verified work."""
import subprocess
import tempfile
import unittest
from pathlib import Path

from rameness.featurebranch import FeatureBranches, available
from rameness.harness import Harness
from rameness.llm import FakeProvider, Response, ToolCall

from test_rameness import Env


def git(root, *args):
    return subprocess.run(["git", *args], cwd=root, capture_output=True, text=True).stdout.strip()


@unittest.skipUnless(available(), "git is not installed")
class FeatureBranchFolders(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp()) / "game"
        (self.root / "node_modules" / "lib").mkdir(parents=True)
        (self.root / "index.html").write_text("core\n")
        self.fb = FeatureBranches(self.root)
        self.assertEqual(self.fb.commit_core(), "main")

    def test_verified_cycle_is_merged_and_heavy_folders_are_linked_not_committed(self):
        folder = self.fb.start(1)
        self.assertNotEqual(folder, self.root)
        self.assertTrue((folder / "node_modules").is_symlink())
        (folder / "index.html").write_text("core + feature\n")
        self.assertEqual((self.root / "index.html").read_text(), "core\n")      # untouched while the cycle runs
        self.assertIn("merged rameness/cycle-1 into main", self.fb.merge())
        self.assertEqual((self.root / "index.html").read_text(), "core + feature\n")
        self.assertFalse(folder.exists())
        self.assertNotIn("node_modules", git(self.root, "ls-files"))

    def test_unfinished_cycle_leaves_the_project_as_verified(self):
        folder = self.fb.start(2)
        (folder / "index.html").write_text("half-done\n")
        self.assertIn("unfinished", self.fb.abandon())
        self.assertEqual((self.root / "index.html").read_text(), "core\n")
        self.assertEqual(git(self.root, "show", "rameness/cycle-2:index.html"), "half-done")

    def test_paths_and_commands_naming_the_project_go_to_the_cycle_copy(self):
        folder = self.fb.start(1)
        self.assertEqual(self.fb.redirect_path(self.root / "js" / "a.js"), folder / "js" / "a.js")
        self.assertEqual(self.fb.redirect_path(Path("/etc/hosts")), Path("/etc/hosts"))
        cmd = f"cd {self.root} && node --test {self.root}/test"
        self.assertEqual(self.fb.redirect_command(cmd), f"cd {folder} && node --test {folder}/test")
        self.assertEqual(self.fb.redirect_command(f"ls {self.root}-backup"), f"ls {self.root}-backup")


@unittest.skipUnless(available(), "git is not installed")
class FeatureBranchRuns(Env):
    def run_open_ended(self, cycle_script):
        cfg = self.cfg()
        cfg["task_scope"] = {"mode": "open_ended", "feature_cycles": 1, "branches": True}
        cfg["learning"]["enabled"] = False
        cfg["progress_review"] = {"every": 0}
        core = [Response("", [ToolCall("1", "write_file", {"path": str(self.cwd / "game.txt"), "content": "core"})],
                         "tool_use"),
                Response("Core done.", [], "end_turn")]
        cfg["max_turns"] = len(core) + len(cycle_script)
        llm = FakeProvider(core + cycle_script)
        result = Harness(cfg, llm=llm).run("build a game", allow_direct=False)
        return result, llm

    def test_a_verified_cycle_is_merged_into_the_project(self):
        cycle = [Response("", [ToolCall("2", "write_file", {"path": str(self.cwd / "game.txt"), "content": "core+fx"})],
                          "tool_use"),
                 Response("Feature done and checked.", [], "end_turn")]
        result, llm = self.run_open_ended(cycle)
        self.assertTrue(result.metrics["success"])
        self.assertIn("own copy: git branch rameness/cycle-1", str(llm.seen[-1]["messages"]))
        self.assertEqual((self.cwd / "game.txt").read_text(), "core+fx")
        self.assertIn("merged rameness/cycle-1", result.text)

    def test_a_cycle_cut_off_mid_change_leaves_the_verified_project(self):
        cycle = [Response("", [ToolCall("2", "write_file", {"path": str(self.cwd / "game.txt"), "content": "broken"})],
                          "tool_use")]                     # max_turns ends the run before the cycle finishes
        result, _ = self.run_open_ended(cycle)
        self.assertEqual((self.cwd / "game.txt").read_text(), "core")
        self.assertIn("unfinished", result.text)
        self.assertEqual(git(self.cwd, "show", "rameness/cycle-1:game.txt"), "broken")


if __name__ == "__main__":
    unittest.main()


class ThinkingAndToolLog(Env):
    def test_a_whole_file_write_is_never_followed_by_minimal_thinking(self):
        from rameness.jev import Decision
        from rameness.thinking import Thinking

        class AlwaysMinimal:
            def choose(self, q, state, opts, context=""):
                return Decision("x", q, {o.id: (0.9 if o.id == "minimal" else 0.025) for o in opts}, "t")
        t = Thinking(AlwaysMinimal(), {"minimal": 128, "brief": 512, "normal": 2048, "deep": 4096, "maximum": 8192})
        wrote = [{"tool": "write_file", "input": {"path": "a.js"}, "ok": True}]
        ran = [{"tool": "bash", "input": {"command": "ls"}, "ok": True}]
        self.assertEqual(t.decide(3, "task", wrote, "", "high")[:2], ("normal", 2048))
        self.assertEqual(t.decide(3, "task", ran, "", "high")[:2], ("minimal", 128))

    def test_full_tool_inputs_go_to_their_own_log(self):
        import json
        cfg = self.cfg()
        cfg["learning"]["enabled"] = False
        cfg["event_log"] = str(self.tmp / "events.jsonl")
        big = "x" * 10000
        Harness(cfg, llm=FakeProvider([Response("", [ToolCall("1", "write_file", {"path": "big.txt", "content": big})],
                                                "tool_use"), Response("done", [], "end_turn")])).run(
            "write big.txt", allow_direct=False)
        calls = [json.loads(l) for l in (self.tmp / "tool_calls.jsonl").read_text().splitlines()]
        self.assertEqual(calls[0]["input"]["content"], big)                   # whole, not truncated
        events = (self.tmp / "events.jsonl").read_text()
        self.assertLess(len(events), len(big))                                # the event log stays small


class GroupReviewAndDebug(Env):
    def run_script(self, script):
        cfg = self.cfg()
        cfg["learning"]["enabled"] = False
        cfg["task_scope"]["mode"] = "off"
        cfg["progress_review"] = {"every": 0}
        llm = FakeProvider(script + [Response("done", [], "end_turn")])
        Harness(cfg, llm=llm).run("build it", allow_direct=False)
        return [str(m.get("content")) for m in llm.seen[-1]["messages"] if m.get("role") == "user"]

    @staticmethod
    def call(i, tool, **a):
        return Response("", [ToolCall(str(i), tool, a)], "tool_use")

    def test_a_drafting_burst_is_reviewed_together_once_it_ends(self):
        notes = self.run_script([self.call(1, "write_file", path="a.js", content="export const A = 1;\n"),
                                 self.call(2, "write_file", path="b.js", content="export const B = 2;\n"),
                                 self.call(3, "bash", command="echo run")])
        review = [n for n in notes if "review them together" in n]
        self.assertEqual(len(review), 1)
        self.assertIn("a.js, b.js", review[0])

    def test_a_single_file_needs_no_group_review(self):
        notes = self.run_script([self.call(1, "write_file", path="a.js", content="export const A = 1;\n"),
                                 self.call(2, "bash", command="echo run")])
        self.assertFalse(any("review them together" in n for n in notes))

    def test_a_long_investigation_gets_one_grouped_probe_request(self):
        probes = [self.call(i, "bash", command=f"echo probe {i}") for i in range(7)]
        notes = self.run_script(probes)
        self.assertEqual(sum("Debug as a group now" in n for n in notes), 1)

    def test_changing_the_work_resets_the_investigation_count(self):
        script = []
        for i in range(4):
            script += [self.call(10 * i, "bash", command="echo probe"), self.call(10 * i + 1, "bash", command="echo probe"),
                       self.call(10 * i + 2, "write_file", path="game.js", content=f"// v{i}\n")]
        notes = self.run_script(script)
        self.assertFalse(any("Debug as a group now" in n for n in notes))
        # edits to test or probe files are investigation, not changes to the work
        notes = self.run_script([self.call(i, "bash", command="echo x") if i % 2 else
                                 self.call(i, "write_file", path=f"test/probe{i}.mjs", content="1") for i in range(13)])
        self.assertTrue(any("Debug as a group now" in n for n in notes))


class TruncatedToolCalls(Env):
    def test_a_tool_call_missing_its_arguments_is_reported_not_fatal(self):
        cfg = self.cfg()
        cfg["learning"]["enabled"] = False
        cfg["task_scope"]["mode"] = "off"
        llm = FakeProvider([Response("", [ToolCall("1", "edit_file", {"old": "x"})], "tool_use"),
                            Response("", [ToolCall("2", "write_file", {})], "tool_use"),
                            Response("done", [], "end_turn")])
        result = Harness(cfg, llm=llm).run("fix the file", allow_direct=False)
        errors = [str(m.get("content")) for m in llm.seen[-1]["messages"] if m.get("role") == "tool"]
        self.assertIn("edit_file is missing required argument(s): path", errors[0])
        self.assertIn("write_file is missing required argument(s): path, content", errors[1])
        self.assertTrue(result.metrics["turns"] >= 3)
