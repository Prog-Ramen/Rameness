"""An SOP gets from written to validated without help: tests completed from what the script really returns,
tests that check real values, and a security review whose findings go back to the model to fix."""
import json
import tempfile
import unittest
from pathlib import Path

from rameness import sopsafety
from rameness.jev import Jev, LexicalJev
from rameness.learning import complete_tests, finish_sop, register_sop
from rameness.llm import FakeProvider
from rameness.sops import Executor, Library

COUNT = "import json, sys\na = json.load(sys.stdin)\nprint(json.dumps({'count': len(a['obj'])}))\n"
INJECTABLE = ("import json, subprocess, sys\na = json.load(sys.stdin)\n"
              "out = subprocess.run(['sh', '-c', 'echo ' + a['text']], capture_output=True, text=True).stdout\n"
              "print(json.dumps({'echo': out.strip()}))\n")
SAFE_ECHO = ("import json, sys\na = json.load(sys.stdin)\nprint(json.dumps({'echo': a['text']}))\n")


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.root = self.tmp / "sops"
        self.root.mkdir()
        self.lib = Library([(self.root, "private")])
        self.ex = Executor(self.lib, ["compute", "exec"], cwd=self.tmp)
        self.jev = Jev(LexicalJev())


class StaticRisks(Base):
    def test_shell_strings_built_from_input_are_found(self):
        risks = sopsafety.static_risks(INJECTABLE)
        self.assertEqual([(r["line"], r["kind"], r["severity"]) for r in risks], [(3, "shell injection", "high")])

    def test_each_risky_construct_is_found_and_constants_are_not(self):
        src = ("import os, subprocess\nx = input()\n"
               "subprocess.run(x, shell=True)\nsubprocess.run('ls -l', shell=True)\n"
               "os.system('echo ' + x)\nos.system('date')\neval(x)\neval('1+1')\n__import__(x)\n"
               "subprocess.run(['bash', '-c', 'echo hi'])\nsubprocess.run(['echo', x])\n")
        self.assertEqual([r["line"] for r in sopsafety.static_risks(src)], [3, 5, 7, 9])


class TestsThatCheckSomething(Base):
    def spec(self, tests):
        return {"id": "data.count_keys", "description": "Count the keys of a JSON object", "script": COUNT,
                "inputs": {"type": "object", "properties": {"obj": {"type": "object"}}}, "tests": tests}

    def test_a_test_that_asserts_nothing_fails(self):
        sop, failures = register_sop(self.lib, self.ex, self.spec([{"input": {"obj": {"a": 1}}},
                                                                    {"input": {"obj": {}}, "expect": {"count": 0}}]))
        self.assertEqual(sop.status, "candidate")
        self.assertIn("test 0: asserts nothing (no expect, expect_keys or expect_error)", failures)

    def test_the_model_writes_assertions_from_the_real_outputs(self):
        reply = {"tests": [{"index": 0, "correct": True, "expect": {"count": 1}},
                           {"index": 1, "correct": True, "expect": {"count": 0}}]}
        llm = FakeProvider(json_script=[reply, {"risks": []}])
        sop, failures = register_sop(self.lib, self.ex, self.spec([{"input": {"obj": {"a": 1}}}, {"input": {"obj": {}}}]),
                                     llm=llm, jev=self.jev)
        self.assertEqual((failures, sop.status), ([], "validated"))
        self.assertEqual([t["expect"] for t in sop.tests], [{"count": 1}, {"count": 0}])
        self.assertEqual(sop.origin["security"]["verdict"], "safe")

    def test_a_wrong_result_becomes_a_failing_test_for_repair(self):
        wrong = COUNT.replace("len(a['obj'])", "len(a['obj']) + 1")
        reply = {"tests": [{"index": 0, "correct": False, "expect": {"count": 1}, "why": "one key"},
                           {"index": 1, "correct": False, "expect": {"count": 0}, "why": "empty"}]}
        fixed = {"script": COUNT, "tests": [{"input": {"obj": {"a": 1}}, "expect": {"count": 1}},
                                            {"input": {"obj": {}}, "expect": {"count": 0}}], "explanation": "off by one"}
        llm = FakeProvider(json_script=[reply, fixed, {"risks": []}])
        spec = dict(self.spec([{"input": {"obj": {"a": 1}}}, {"input": {"obj": {}}}]), script=wrong)
        sop, failures = register_sop(self.lib, self.ex, spec, llm=llm, jev=self.jev)
        self.assertEqual((failures, sop.status), ([], "validated"))
        self.assertNotIn("+ 1", (sop.path / "run.py").read_text())

    def test_the_model_adds_cases_until_two_check_concrete_values(self):
        first = {"tests": [{"index": 0, "correct": True, "expect": {"count": 2}}],
                 "add": [{"input": {"obj": {}}}]}
        second = {"tests": [{"index": 1, "correct": True, "expect": {"count": 0}}]}
        llm = FakeProvider(json_script=[first, second])
        register_sop(self.lib, self.ex, self.spec([{"input": {"obj": {"a": 1, "b": 2}}}]))
        done = complete_tests(self.lib, self.ex, "data.count_keys", llm)
        self.assertEqual(done["added"], 1)
        self.assertEqual([t.get("expect") for t in self.lib.get("data.count_keys").tests], [{"count": 2}, {"count": 0}])


class SameRulesAsTheRegistry(Base):
    def test_inputs_must_be_given_with_their_declared_types(self):
        from rameness.learning import test_quality
        spec = {"id": "data.count_keys", "description": "Count the keys of a JSON object", "script": COUNT,
                "inputs": {"type": "object", "properties": {"obj": {"type": "object"}, "n": {"type": "integer"}},
                           "required": ["obj"]},
                "tests": [{"input": {"obj": {"a": 1}, "n": "2"}, "expect": {"count": 1}},
                          {"input": {"n": True}, "expect_error": True}]}
        sop, _ = register_sop(self.lib, self.ex, spec)
        self.assertEqual(test_quality(sop), ["fewer than two tests check concrete expected values",
                                             "test 0: input n should be integer",
                                             "test 1: missing required input obj", "test 1: input n should be integer"])

    def test_the_permissions_the_code_uses_are_declared(self):
        writer = "import json, sys\na = json.load(sys.stdin)\nopen(a['path'], 'w').write('x')\nprint(json.dumps({'ok': True}))\n"
        spec = {"id": "fs.touch", "description": "Write a marker file", "script": writer, "permissions": [],
                "inputs": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]},
                "tests": [{"input": {"path": "a.txt"}, "expect": {"ok": True}},
                          {"input": {"path": "b.txt"}, "expect": {"ok": True}}]}
        sop, _ = register_sop(self.lib, self.ex, spec)
        self.assertIn("fs:write", sop.permissions)


class SecurityReview(Base):
    def spec(self, script):
        return {"id": "text.echo", "description": "Echo text back", "script": script, "permissions": ["exec"],
                "inputs": {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]},
                "tests": [{"input": {"text": "hi"}, "expect": {"echo": "hi"}},
                          {"input": {"text": "a b"}, "expect": {"echo": "a b"}}]}

    def test_an_injection_risk_is_fixed_by_the_model_and_reviewed_again(self):
        fixed = {"script": SAFE_ECHO, "tests": self.spec(SAFE_ECHO)["tests"], "explanation": "no shell"}
        llm = FakeProvider(json_script=[
            {"risks": [{"line": 3, "kind": "shell injection", "detail": "text goes into sh -c", "severity": "high"}]},
            fixed, {"risks": []}])
        sop, failures = register_sop(self.lib, self.ex, self.spec(INJECTABLE), llm=llm, jev=self.jev)
        self.assertEqual(failures, [])
        self.assertEqual(sop.origin["security"]["verdict"], "safe")
        self.assertNotIn("sh', '-c'", (sop.path / "run.py").read_text())

    def test_without_a_model_a_script_that_runs_inputs_stays_unreviewed(self):
        register_sop(self.lib, self.ex, self.spec(INJECTABLE))
        result = sopsafety.review(self.lib.get("text.echo"), None, self.jev)   # code can't tell command from data
        self.assertEqual(result["verdict"], "unreviewed")

    def test_a_command_the_caller_means_to_run_is_fine(self):
        runner = ("import json, subprocess, sys\na = json.load(sys.stdin)\n"
                  "r = subprocess.run(['sh', '-c', a['command']], capture_output=True, text=True)\n"
                  "print(json.dumps({'exit': r.returncode}))\n")
        spec = {"id": "dev.run_command", "description": "Run the test command the caller gives and report its exit code",
                "script": runner, "permissions": ["compute"],
                "inputs": {"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]},
                "tests": [{"input": {"command": "true"}, "expect": {"exit": 0}},
                          {"input": {"command": "false"}, "expect": {"exit": 1}}]}
        llm = FakeProvider(json_script=[{"risks": []}])           # the model: a command input, run on purpose
        sop, failures = register_sop(self.lib, self.ex, spec, llm=llm, jev=self.jev)
        self.assertEqual((failures, sop.origin["security"]["verdict"]), ([], "safe"))
        self.assertIn("exec", sop.permissions)                     # declared, so it asks like the agent's bash

    def test_sharing_requires_a_safe_review_of_the_current_code(self):
        from rameness.org import Org
        from rameness.registry import Registry, RegistryError
        register_sop(self.lib, self.ex, self.spec(INJECTABLE))
        reg = Registry({"registry": {"public": str(self.tmp / "public.git"), "min_tokens_saved": 0}},
                       self.tmp / "home", Org(), self.jev)
        with self.assertRaisesRegex(RegistryError, "security review"):
            reg.propose(self.lib.get("text.echo"))


if __name__ == "__main__":
    unittest.main()
