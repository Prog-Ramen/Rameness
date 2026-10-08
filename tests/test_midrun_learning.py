"""Learning at the end of a run: the model reviews its last five runs for repeated multi-step tasks, the savings are
measured from what those turns cost, and Kev and the tests decide. Procedures repeated in several projects go to the
user's own library."""
import json
import os
import tempfile
import unittest
from pathlib import Path

from rameness import config
from rameness.harness import Harness
from rameness.learning import Candidate, Learner, RunStore
from rameness.jev import Jev, LexicalJev
from rameness.llm import FakeProvider, Response, ToolCall
from rameness.sops import Executor, Library


def step(cmd):
    return {"tool": "bash", "input": {"command": cmd}, "ok": True, "out": ""}


class Membership(LexicalJev):
    """Stands in for Kev on "is this step part of the procedure?": the keyword fallback cannot judge that."""

    def activate(self, question, query, options, context=""):
        if question.startswith("Is this step part of"):
            return [0.9 if ("ls " in o.text or "wc -l" in o.text) else 0.1 for o in options]
        return super().activate(question, query, options, context)


SUMMARY = ("import json, os, sys\nargs = json.load(sys.stdin)\nd = args['dir']\n"
           "names = sorted(os.listdir(d))\n"
           "lines = sum(sum(1 for _ in open(os.path.join(d, n))) for n in names if n.endswith('.txt'))\n"
           "print(json.dumps({'files': len(names), 'lines': lines}))\n")


class WorthIt(unittest.TestCase):
    """An SOP is made only if it saves real model output: per use, and over its uses more than it costs."""

    def cand(self):
        occs = [[dict(step(f"ls {d}"), turn=2 * t), dict(step(f"wc -l {d}/notes.txt"), turn=2 * t + 1)]
                for t, d in enumerate(("alpha", "beta", "gamma"))]
        return Candidate("summarize a folder", "", occs[0], count=3, occurrences=occs)

    def test_savings_are_measured_from_the_turns_that_produced_the_steps(self):
        from rameness.learning import estimate_savings
        costs = {t: {"tokens": 900, "seconds": 9.0} for t in range(6)}
        s = estimate_savings(self.cand(), costs)
        self.assertTrue(s["measured"])
        self.assertEqual(s["tokens"], 2 * 900 - 300)             # two turns of output, minus reading + calling the SOP
        self.assertAlmostEqual(s["seconds"], 9.0)                 # two turns' time minus one turn

    def test_a_short_procedure_is_not_worth_an_sop(self):
        tmp = Path(tempfile.mkdtemp())
        lib = Library([(tmp / "sops", "private")])
        learner = Learner(lib, Executor(lib, [], cwd=tmp), Jev(LexicalJev()), RunStore(tmp / "runs"),
                          FakeProvider([], json_script=[{"id": "x.y"}]))
        c = self.cand()
        c.saves = {"tokens": 200, "seconds": 2.0, "measured": True}
        c.score = 0.9
        out = learner._learn("summarise the folders", [c])
        self.assertEqual(out[0]["skipped"], "too small to be worth an SOP")
        self.assertFalse(list((tmp / "sops").rglob("sop.json")))   # nothing generated, no tokens spent


class EndOfRunReview(unittest.TestCase):
    """At the end of a run the model reviews its last runs for repeated multi-step tasks; Kev and tests decide."""

    def test_the_model_points_at_repetitions_across_runs(self):
        tmp = Path(tempfile.mkdtemp())
        lib = Library([(tmp / "sops", "private")])
        runs = RunStore(tmp / "runs")
        for lang, chk in (("python", "python3 -m py_compile app.py"), ("rust", "rustfmt --check main.rs")):
            runs.save(f"fix the {lang} app", [step("ls"), step(chk), step("cat report.txt")], True, {"project": "/p"})
        review = {"procedures": [
            {"name": "syntax_check_and_report", "description": "Syntax-check a source file and show the report",
             "params": ["file"], "occurrences": [{"run": "R1", "steps": [1, 2]}, {"run": "R2", "steps": [1, 2]}]},
            {"name": "just_list", "description": "List files", "occurrences": [{"run": "R1", "steps": [0]},
                                                                              {"run": "R2", "steps": [0]}]}]}
        learner = Learner(lib, Executor(lib, [], cwd=tmp), Jev(LexicalJev()), runs, FakeProvider([], json_script=[review]))
        cands = learner.review_runs(runs.all())
        self.assertEqual([c.name for c in cands], ["syntax_check_and_report"])   # a single command is left out
        self.assertEqual((cands[0].count, len(cands[0].steps)), (2, 2))         # python and rust: one procedure
        self.assertEqual(cands[0].params, ["file"])

    def test_savings_are_measured_from_the_runs_turn_costs(self):
        tmp = Path(tempfile.mkdtemp())
        runs = RunStore(tmp / "runs")
        for chk in ("python3 -m py_compile app.py", "rustfmt --check main.rs"):
            steps = [dict(step("ls"), turn=0), dict(step(chk), turn=1), dict(step("cat report.txt"), turn=2)]
            runs.save("fix the app", steps, True, {"turn_costs": {"0": {"tokens": 100, "seconds": 1.0},
                                                                "1": {"tokens": 800, "seconds": 8.0},
                                                                "2": {"tokens": 700, "seconds": 7.0}}})
        review = {"procedures": [{"name": "check_and_report", "description": "Check a file and show the report",
                                  "occurrences": [{"run": "R1", "steps": [1, 2]}, {"run": "R2", "steps": [1, 2]}]}]}
        lib = Library([(tmp / "sops", "private")])
        learner = Learner(lib, Executor(lib, [], cwd=tmp), Jev(LexicalJev()), runs,
                          FakeProvider([], json_script=[review]))
        c = learner.review_runs(runs.all())[0]
        self.assertTrue(c.saves["measured"])
        self.assertEqual(c.saves["cost_tokens"], 1500)                  # the two turns that produced the steps
        self.assertEqual(c.saves["tokens"], 1500 - 300)                  # less reading + calling the SOP


class MeasuredUse(unittest.TestCase):
    """Each SOP call is measured: the procedure's generation cost (baseline) less what the call cost."""

    def test_a_call_records_tokens_and_seconds_saved(self):
        tmp = Path(tempfile.mkdtemp())
        cwd = tmp / "proj"
        sop_dir = cwd / ".rameness" / "sops" / "data" / "count"
        sop_dir.mkdir(parents=True)
        code = "import json, sys\nargs = json.load(sys.stdin)\nprint(json.dumps({'n': len(args['items'])}))\n" * 5
        (sop_dir / "run.py").write_text(code)
        (sop_dir / "sop.json").write_text(json.dumps({"id": "data.count", "description": "Count items",
                                                      "inputs": {"type": "object", "properties": {"items": {}}}}))
        os.environ["RAMENESS_HOME"] = str(tmp / "home")
        c = config.load(cwd, {"provider": "fake"})
        c["jev"]["backend"] = "lexical"
        c["permissions"]["mode"] = "auto"
        h = Harness(c, llm=FakeProvider([]))
        sop = h.lib.get("data.count")
        use = h._measure_sop_use(sop, 3, {"tokens": 20, "seconds": 0.5}, 1, 0.1)
        self.assertEqual(use["baseline"], "code size")
        self.assertEqual(use["baseline_tokens"], len(code) // 4)       # what the model would write instead
        self.assertEqual(use["call_tokens"], 20)                       # identifying the SOP and calling it
        self.assertEqual(use["tokens_saved"], len(code) // 4 - 20)
        self.assertAlmostEqual(use["seconds_saved"], (len(code) / 4) / 40 - 0.5, places=1)  # at this turn's 40 tok/s
        self.assertEqual(h.lib.stats["data.count"]["tokens_saved"], use["tokens_saved"])
        # a learned SOP's baseline is the measured cost of the repetitions it replaced
        sop.origin = {"saves_per_use": {"cost_tokens": 900, "cost_seconds": 9.0}}
        use = h._measure_sop_use(sop, 4, {"tokens": 40, "seconds": 1.0}, 2, 0.2)     # two calls shared the turn
        self.assertEqual((use["baseline"], use["call_tokens"], use["tokens_saved"]), ("measured", 20, 880))
        self.assertAlmostEqual(use["seconds_saved"], 9.0 - 0.5)

class AcrossProjects(unittest.TestCase):
    def test_repeats_from_two_projects_go_to_the_users_library(self):
        tmp = Path(tempfile.mkdtemp())
        user, proj = tmp / "home" / "sops", tmp / "proj" / ".rameness" / "sops"
        user.mkdir(parents=True)
        proj.mkdir(parents=True)
        lib = Library([(user, "private"), (proj, "private")])
        runs = RunStore(tmp / "home" / "runs")
        for p in ("/work/a", "/work/b"):
            runs.save("check the app", [step("ls"), step("python3 -m py_compile app.py"), step("cat log.txt")], True,
                      {"project": p})
        review = {"procedures": [{"name": "compile_and_report", "description": "Compile and show the log",
                                  "occurrences": [{"run": "R1", "steps": [1, 2]}, {"run": "R2", "steps": [1, 2]}]}]}
        learner = Learner(lib, Executor(lib, [], cwd=tmp), Jev(LexicalJev()), runs,
                          FakeProvider([], json_script=[review]))
        multi = learner.review_runs(runs.all())[0]
        self.assertEqual(multi.projects, {"/work/a", "/work/b"})
        self.assertTrue(multi.exact_repeat)                         # the same commands both times
        self.assertEqual(learner._root_for(multi), user)
        multi.projects = {"/work/a"}
        self.assertIsNone(learner._root_for(multi))                 # one project: stays with that project

if __name__ == "__main__":
    unittest.main()
