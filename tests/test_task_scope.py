"""task_scope: JEV sorts tasks into specific (build every requirement, then finish) and open-ended (a tested core,
then feature cycles), and the harness runs the cycles before it lets an open-ended task finish."""
import unittest

from rameness.harness import FEATURE_CYCLE_NOTE, Harness
from rameness.jev import Jev, LexicalJev
from rameness.llm import FakeProvider, Response, ToolCall
from rameness.org import Org
from rameness.router import SCOPES, Router

from test_rameness import Env


def finished_work():
    """A model that runs one check and then says it is done, every time it is asked to continue."""
    return [Response("", [ToolCall("1", "bash", {"command": "echo checked"})], "tool_use"),
            Response("Done and verified.", [], "end_turn")] * 12


class TaskScope(Env):
    def run_task(self, task, **scope):
        cfg = self.cfg()
        cfg["task_scope"] = {**cfg["task_scope"], **scope}
        cfg["learning"]["enabled"] = False
        llm = FakeProvider(finished_work())
        result = Harness(cfg, llm=llm).run(task, allow_direct=False)
        cycles = [m["content"] for m in llm.seen[-1]["messages"]
                  if m.get("role") == "user" and "Feature cycle" in str(m.get("content"))]
        return result, llm.seen[0]["system"], cycles

    def test_open_ended_task_runs_its_feature_cycles_before_finishing(self):
        result, system, cycles = self.run_task("build a game", mode="open_ended", feature_cycles=2)
        self.assertTrue(result.metrics["success"])
        self.assertEqual(result.plan.scope, "open_ended")
        self.assertIn("feature\n  cycles (2 in all)", system)
        self.assertEqual(cycles, [FEATURE_CYCLE_NOTE.format(k=1, n=2), FEATURE_CYCLE_NOTE.format(k=2, n=2)])

    def test_specific_task_builds_to_its_requirements_and_finishes(self):
        result, system, cycles = self.run_task("fix the failing parser test", mode="specific")
        self.assertTrue(result.metrics["success"])
        self.assertIn("This task has specific requirements", system)
        self.assertNotIn("This task is open-ended", system)
        self.assertEqual(cycles, [])

    def test_off_and_zero_cycles_add_nothing(self):
        for scope in ({"mode": "off"}, {"mode": "open_ended", "feature_cycles": 0}):
            with self.subTest(**scope):
                _, system, cycles = self.run_task("build a game", **scope)
                self.assertNotIn("This task is open-ended", system)
                self.assertNotIn("This task has specific requirements", system)
                self.assertEqual(cycles, [])

    def test_jev_decides_the_scope(self):
        cfg = self.cfg()
        router = Router(self.lib(), Jev(LexicalJev()), Org(self.cwd), cfg, self.cwd)
        self.assertEqual({o.id for o in SCOPES}, {"specific", "open_ended"})
        plan = router.plan("build a 2D platformer game in the browser", allow_direct=False)
        self.assertEqual(plan.scope, "open_ended")
        self.assertIn("open_ended", plan.scope_probs)


if __name__ == "__main__":
    unittest.main()
