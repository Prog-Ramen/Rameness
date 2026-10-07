import json
import os
import tempfile
import unittest
import subprocess
from pathlib import Path
from unittest.mock import patch

from rameness import config
from rameness.context import ArtifactStore, ContextManager
from rameness.harness import Harness
from rameness.jev import Jev, LexicalJev, Option
from rameness.learning import Learner, RunStore, mine_repeats, register_sop
from rameness.llm import AnthropicProvider, FakeProvider, OpenAICompatProvider, Response, ToolCall
from rameness.org import Org
from rameness.publish import publish, scrub
from rameness.sops import Executor, Library, SOPError, activate
from rameness.tools import Approver


def write_sop(root: Path, sop_id: str, spec: dict, script: str | None = None, node: dict | None = None):
    d = root.joinpath(*sop_id.split("."))
    d.mkdir(parents=True, exist_ok=True)
    (d / "sop.json").write_text(json.dumps({"id": sop_id, **spec}))
    if script:
        (d / "run.py").write_text(script)
    return d


class Env(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.cwd = self.tmp / "proj"
        self.home = self.tmp / "home"
        (self.cwd / ".rameness" / "sops").mkdir(parents=True)
        os.environ["RAMENESS_HOME"] = str(self.home)

    def cfg(self, **over):
        c = config.load(self.cwd, {"provider": "fake"})
        c["jev"]["backend"] = "lexical"
        c["permissions"]["mode"] = "auto"
        c["registry"]["auto_propose"] = False       # unit tests do not publish to the real registry
        for k, v in over.items():
            c[k] = v
        return c

    def lib(self):
        return Library.default(self.cwd, self.home)


class TestJev(unittest.TestCase):
    def test_lexical_ranks_relevant_option_first(self):
        j = Jev(LexicalJev())
        d = j.activate("which?", "download the csv file from the url",
                       [Option("http", "http fetch download url api"), Option("git", "git commit branch")])
        self.assertGreater(d.probs["http"], 0.5)
        self.assertLess(d.probs["git"], 0.2)

    def test_choose_is_a_distribution_and_logs(self):
        with tempfile.TemporaryDirectory() as t:
            log = Path(t) / "d.jsonl"
            j = Jev(LexicalJev(), log)
            d = j.choose("route?", "explain what a mutex is", [Option("a", "explain what"), Option("b", "fix bug")])
            self.assertAlmostEqual(sum(d.probs.values()), 1.0, places=3)
            j.feedback(d.id, {"ok": True})
            lines = log.read_text().splitlines()
            self.assertEqual(len(lines), 2)


class TestTraversal(Env):
    def setUp(self):
        super().setUp()
        root = self.cwd / ".rameness" / "sops"
        (root / "customer_data").mkdir()
        (root / "customer_data" / "_node.json").write_text(json.dumps({
            "description": "retrieve customer records data", "keywords": ["customer", "customers", "clients"],
            "requires": [{"name": "data_source", "hints": ["postgres", "salesforce"], "question": "Which data source?"}]}))
        write_sop(root, "customer_data.postgres", {"description": "retrieve customer records from postgres",
                                                   "keywords": ["postgres", "customer", "sql", "retrieve"]})
        write_sop(root, "customer_data.salesforce", {"description": "retrieve customer records from salesforce",
                                                     "keywords": ["salesforce", "crm", "retrieve"]})

    def test_defers_when_discriminator_missing(self):
        a = activate(self.lib(), Jev(LexicalJev()), "retrieve customer data and generate a report")
        self.assertIn("customer_data", [n.id for n, _, _ in a.deferred])
        self.assertEqual([r.name for r in a.missing], ["data_source"])
        self.assertFalse(any(s.id.startswith("customer_data") for s, _ in a.selected))

    def test_org_default_resolves_and_traverses(self):
        a = activate(self.lib(), Jev(LexicalJev()), "retrieve customer data and generate a report",
                     context="Defaults: data_source=postgres", defaults={"data_source": "postgres"})
        self.assertFalse(a.deferred)
        self.assertEqual(a.selected[0][0].id, "customer_data.postgres")

    def test_router_clarifies_then_resumes(self):
        h = Harness(self.cfg(), llm=FakeProvider())
        r = h.run("retrieve customer data")
        self.assertEqual(r.route, "clarify")
        self.assertIn("Which data source?", r.text)
        plan = h.plan("retrieve customer data", resolved={"data_source": "salesforce"})
        self.assertNotEqual(plan.route, "clarify")

    def test_private_overrides_public(self):
        write_sop(self.cwd / ".rameness" / "sops", "http.get", {"description": "acme proxy fetch"})
        self.assertEqual(self.lib().get("http.get").scope, "private")


class TestExecutor(Env):
    def test_script_composite_and_permissions(self):
        root = self.cwd / ".rameness" / "sops"
        write_sop(root, "math.double", {"description": "double", "inputs": {"type": "object", "properties": {
            "x": {"type": "integer"}}, "required": ["x"]}, "tests": [{"input": {"x": 2}, "expect": {"y": 4}}]},
                  "import json,sys; a=json.load(sys.stdin); print(json.dumps({'y': a['x']*2}))")
        write_sop(root, "math.quad", {"kind": "composite", "description": "x4",
                                      "inputs": {"type": "object", "properties": {"x": {"type": "integer"}}},
                                      "steps": [{"sop": "math.double", "args": {"x": "${input.x}"}},
                                                {"sop": "math.double", "args": {"x": "${steps.0.y}"}}],
                                      "outputs": {"return": {"result": "${steps.1.y}"}}})
        write_sop(root, "net.thing", {"description": "needs net", "permissions": ["network"]},
                  "print('{}')")
        lib = self.lib()
        ex = Executor(lib, ["fs:read"], cwd=self.cwd)
        self.assertEqual(ex.run("math.double", {"x": 3}), {"y": 6})
        self.assertEqual(ex.run("math.quad", {"x": 3}), {"result": 12})
        self.assertEqual(ex.test("math.double"), [])
        with self.assertRaises(SOPError):
            ex.run("math.double", {})
        with self.assertRaises(SOPError):
            ex.run("net.thing", {})
        # composite sub-steps and failed validation are not counted as top-level uses
        self.assertEqual(lib.stats["math.double"]["uses"], 1)
        self.assertEqual(lib.stats["math.quad"]["uses"], 1)

    def test_builtin_sops_pass_their_tests(self):
        lib = self.lib()
        ex = Executor(lib, [], cwd=self.cwd)
        for sid, s in lib.sops.items():
            if s.tests and s.scope == "public":
                self.assertEqual(ex.test(sid), [], sid)


class TestContext(Env):
    def test_long_lines_big_files_and_broad_output_stay_small(self):
        from rameness.tools import Toolbox, READ_WHOLE_CHARS
        tb = Toolbox(self.cwd, approve=lambda *a: True)
        # a minified line (the AutoRound run's bash grep returned one 600k-character line of three.min.js)
        (self.cwd / "vendor.min.js").write_text("var a=1;" * 80000 + "\n")
        out = tb.bash({"command": "grep -n var vendor.min.js"})
        self.assertLess(len(out), 3000)
        self.assertIn("line cut", out)
        # a big source file read without a range: outline + opening lines, not the whole file
        body = "\n".join(f"function f{i}() {{\n  return {i};\n}}" + "\n// pad" * 40 for i in range(400))
        self.assertGreater(len(body), READ_WHOLE_CHARS)
        (self.cwd / "big.js").write_text(body)
        shown = tb.read_file({"path": "big.js"})
        self.assertIn("== outline ==", shown)
        self.assertIn("function f199()", shown)
        self.assertIn("(200 more definitions)", shown)
        self.assertLess(len(shown), len(body) // 3)
        # with a range it reads exactly that range
        self.assertIn("function f0()", tb.read_file({"path": "big.js", "offset": 1, "limit": 3}))
        # broad command output: a short preview with a hint to narrow the command
        cm = ContextManager(ArtifactStore(self.tmp / "art3"), Jev(LexicalJev()), budget_tokens=200000)
        preview = cm.ingest("bash", "match line\n" * 20000)
        self.assertIn("narrow the command", preview)
        self.assertLess(len(preview), 12000)

    def test_compaction_leaves_room_for_the_requested_reply(self):
        # The AutoRound Minecraft run (2026-10-07): 262,144-token window, 32,000-token replies. The server refused
        # every prompt above 230,144 tokens, while compaction waited until 242,144; three requests failed first.
        cm = ContextManager(ArtifactStore(self.tmp / "art"), Jev(LexicalJev()), budget_tokens=262144)
        self.assertLessEqual(cm.limit() + cm.reply_tokens, 262144)
        self.assertTrue(cm.needs_compaction("", [], reported=230145))
        self.assertEqual(cm.reply_room("", [], reported=100000), 32000)
        # a window too small for the whole reserve: the request is cut to what still fits
        small = ContextManager(ArtifactStore(self.tmp / "art2"), Jev(LexicalJev()), budget_tokens=40000)
        room = small.reply_room("", [], reported=small.limit())
        self.assertLessEqual(small.limit() + room, 40000)
        self.assertGreaterEqual(room, 1024)

    def test_offload_recall_and_compaction(self):
        store = ArtifactStore(self.tmp / "art")
        cm = ContextManager(store, Jev(LexicalJev()), budget_tokens=2000, offload_chars=500, keep_recent_turns=1)
        big = "\n".join(f"line {i} value" for i in range(1000))
        shown = cm.ingest("bash", big)
        self.assertLess(len(shown), 1000)
        ref = shown.split("ref=")[1].split(".")[0]
        self.assertIn("line 500 value", store.recall(ref, grep="line 500 "))
        msgs = [{"role": "user", "content": "task"}]
        for i in range(6):
            msgs.append({"role": "assistant", "content": "", "tool_calls": [ToolCall(f"t{i}", "bash", {"command": "x"})]})
            msgs.append({"role": "tool", "tool_call_id": f"t{i}", "name": "bash", "content": "y" * 3000})
        self.assertTrue(cm.needs_compaction("", msgs))
        out = cm.compact("", msgs, "task")
        self.assertTrue(any(m.get("evicted") for m in out))
        self.assertFalse(out[-1].get("evicted"))                      # recent turn kept
        calls = {c.id for m in out if m["role"] == "assistant" for c in m.get("tool_calls") or []}
        self.assertTrue(all(m["tool_call_id"] in calls for m in out if m["role"] == "tool"))  # pairs intact
        ev = next(m for m in out if m.get("evicted"))
        self.assertEqual(store.get(ev["content"].split("ref=")[1].split(";")[0]), "y" * 3000)


class TestLearning(Env):
    def test_harness_learns_validates_and_automatically_proposes(self):
        public = self.tmp / "registry.git"
        subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(public)], check=True)
        cfg = self.cfg()
        cfg["registry"].update(public=str(public), remote_index=None, auto_propose=True)
        spec = {"id": "text.word_count", "description": "Generic reusable utility to count words in text",
                "permissions": ["compute"], "keywords": ["text", "generic", "utility"],
                "inputs": {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]},
                "script": "import json,sys; a=json.load(sys.stdin); print(json.dumps({'words': len(a['text'].split())}))",
                "tests": [{"input": {"text": "a b c"}, "expect": {"words": 3}}]}
        llm = FakeProvider([
            Response("", [ToolCall("1", "bash", {"command": "echo checking"})], "tool_use"),
            Response("", [ToolCall("2", "bash", {"command": "echo checked"})], "tool_use"),
            Response("Done and verified.", [], "end_turn")], json_script=[
                {"procedures": [{"name": "word_count", "description": "count words", "steps": [0],
                                 "params": ["text"], "p_reusable": 0.9}]}, spec])
        h = Harness(cfg, llm=llm)
        with patch("rameness.registry.Registry._gh_pr", return_value="https://github.com/example/sops/pull/2") as pr:
            result = h.run("implement a reusable word counting utility", allow_direct=False)
        self.assertTrue(result.metrics["success"])
        self.assertTrue(any(r.get("sop") == "text.word_count" for r in result.learned), result.learned)
        self.assertEqual(result.proposals, [{"id": "text.word_count", "status": "proposed",
                                            "pr": "https://github.com/example/sops/pull/2"}])
        self.assertEqual(pr.call_count, 1)
        self.assertEqual(json.loads((self.cwd / ".rameness/sop-proposals.json").read_text()), result.proposals)

    def test_agent_saved_sop_is_proposed_even_without_trace_learning(self):
        public = self.tmp / "registry.git"
        subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(public)], check=True)
        cfg = self.cfg()
        cfg["registry"].update(public=str(public), remote_index=None, auto_propose=True)
        cfg["learning"]["enabled"] = False
        spec = {"id": "text.upper", "description": "Generic reusable text conversion utility",
                "inputs": {"type": "object", "properties": {"s": {"type": "string"}}, "required": ["s"]},
                "script": "import json,sys; a=json.load(sys.stdin); print(json.dumps({'s': a['s'].upper()}))",
                "permissions": ["compute"], "tests": [{"input": {"s": "a"}, "expect": {"s": "A"}}]}
        llm = FakeProvider([Response("", [ToolCall("1", "sop_save", spec)], "tool_use"),
                            Response("done", [], "end_turn")])
        h = Harness(cfg, llm=llm)
        with patch("rameness.registry.Registry._gh_pr", return_value="https://github.com/example/sops/pull/2"):
            result = h.run("implement a text conversion utility", allow_direct=False)
        self.assertEqual(result.learned, [])
        self.assertEqual(result.proposals[0]["status"], "proposed")

    def test_proposal_failure_does_not_fail_completed_task(self):
        cfg = self.cfg()
        cfg["registry"].update(auto_propose=True, remote_index=None)
        h = Harness(cfg, llm=FakeProvider([Response("", [ToolCall("1", "bash", {"command": "echo checked"})],
                                                       "tool_use"), Response("done", [], "end_turn")]))
        with patch("rameness.registry.Registry.auto_propose", side_effect=RuntimeError("registry unavailable")):
            result = h.run("implement the checked change", allow_direct=False)
        self.assertTrue(result.metrics["success"])
        self.assertEqual(result.proposals[0]["status"], "failed")
        self.assertIn("registry unavailable", result.proposals[0]["error"])

    def test_auto_proposals_are_disabled_by_config_and_readonly_mode(self):
        for enabled, mode in [(False, "auto"), (True, "readonly")]:
            with self.subTest(enabled=enabled, mode=mode):
                cfg = self.cfg()
                cfg["registry"].update(auto_propose=enabled, remote_index=None)
                cfg["permissions"]["mode"] = mode
                cfg["learning"]["enabled"] = False
                h = Harness(cfg, llm=FakeProvider([Response("Done and verified.", [], "end_turn")]))
                with patch("rameness.registry.Registry.auto_propose") as propose:
                    result = h.run("implement the feature", allow_direct=False)
                self.assertTrue(result.metrics["success"])
                self.assertEqual(result.proposals, [])
                propose.assert_not_called()

    def test_repeated_shell_sequence_becomes_candidate_sop(self):
        lib = self.lib()
        ex = Executor(lib, ["exec"], cwd=self.cwd)
        runs = RunStore(self.cwd / ".rameness" / "runs")
        learner = Learner(lib, ex, Jev(LexicalJev()), runs, None, min_repeats=2, threshold=0.5)
        steps = [{"tool": "bash", "input": {"command": "echo build"}, "ok": True},
                 {"tool": "bash", "input": {"command": "echo package"}, "ok": True}]
        self.assertEqual(learner.observe("build it", steps, True), [])
        out = learner.observe("build the thing again", steps, True)
        created = [o for o in out if "sop" in o]
        self.assertEqual(len(created), 1)
        sop = lib.get(created[0]["sop"])
        self.assertEqual(sop.status, "candidate")                 # never auto-validated without tests
        self.assertEqual(ex.run(sop.id, {}), {"output": "build\npackage"})
        # third time: duplicate of the SOP it just made
        out3 = learner.observe("build again", steps, True)
        self.assertFalse([o for o in out3 if "sop" in o])

    def test_llm_generated_sop_validated_by_tests(self):
        lib = self.lib()
        ex = Executor(lib, ["compute"], cwd=self.cwd)
        spec = {"id": "text.word_count", "description": "count words in text", "permissions": ["compute"],
                "inputs": {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]},
                "script": "import json,sys; a=json.load(sys.stdin); print(json.dumps({'words': len(a['text'].split())}))",
                "tests": [{"input": {"text": "a b c"}, "expect": {"words": 3}}]}
        llm = FakeProvider(json_script=[
            {"procedures": [{"name": "word_count", "description": "count words", "steps": [0], "params": ["text"],
                             "p_reusable": 0.9}]},
            spec])
        learner = Learner(lib, ex, Jev(LexicalJev()), RunStore(self.cwd / "runs"), llm, threshold=0.6)
        steps = [{"tool": "bash", "input": {"command": "wc -w notes.txt"}, "ok": True},
                 {"tool": "bash", "input": {"command": "cat notes.txt"}, "ok": True}]
        out = learner.observe("count the words in notes.txt", steps, True)
        self.assertEqual(out[0]["status"], "validated", out)
        self.assertEqual(ex.run("text.word_count", {"text": "x y"}), {"words": 2})

    def test_register_rejects_broken_script(self):
        lib = self.lib()
        with self.assertRaises(Exception):
            register_sop(lib, Executor(lib, []), {"id": "x.y", "description": "d", "script": "def (:"})

    def test_non_object_script_outputs_fail_tests_and_still_get_classified(self):
        lib = self.lib()
        for output in ("[]", "1", "true", "null", '"text"'):
            with self.subTest(output=output):
                sop, failures = register_sop(lib, Executor(lib, []), {
                    "id": "internal.routes", "description": "Internal company-specific routing policy",
                    "script": f"print({output!r})", "permissions": [],
                    "tests": [{"input": {}, "expect": {"routes": []}}]}, jev=Jev(LexicalJev()))
                self.assertEqual(sop.status, "candidate")
                self.assertTrue(failures)
                self.assertIn("must output a JSON object", failures[0])
                self.assertEqual(sop.visibility, "private")
                self.assertTrue(sop.classified)

    def test_generated_duplicate_is_checked_before_registration(self):
        lib = self.lib()
        llm = FakeProvider(json_script=[
            {"procedures": [{"name": "different_trace_name", "description": "audit records", "steps": [0],
                             "p_reusable": 0.9}]},
            {"id": "data.repeated_audit", "description": "Audit JSONL text", "script": "print('{}')",
             "tests": [{"input": {}, "expect": {"valid": True}}]}])
        learner = Learner(lib, Executor(lib, []), Jev(LexicalJev()), RunStore(self.tmp / "runs"), llm)
        with patch.object(learner, "_duplicate", return_value=None), \
                patch.object(learner, "_duplicate_generated", return_value="data.jsonl_audit") as check:
            rows = learner.observe("audit records", [
                {"tool": "bash", "input": {"command": "echo inspect"}},
                {"tool": "bash", "input": {"command": "echo verified"}}], True)
        self.assertEqual(rows[0]["skipped"], "generated procedure duplicates data.jsonl_audit")
        check.assert_called_once()
        self.assertNotIn("data.repeated_audit", lib.sops)

    def test_mine_repeats_shape_vs_exact(self):
        runs = [{"task": f"t{i}", "success": True, "steps": [
            {"tool": "bash", "input": {"command": f"curl -s https://api.x.io/items/{i} | jq .name"}, "ok": True}]}
            for i in range(3)]
        c = mine_repeats(runs, 2)
        self.assertEqual(c[0].count, 3)
        self.assertFalse(c[0].exact_repeat)


class TestHarness(Env):
    def test_direct_route_uses_no_model(self):
        (self.cwd / "sales.csv").write_text("region,amount\neu,10\nus,30\n")
        llm = FakeProvider()
        h = Harness(self.cfg(), llm=llm)
        r = h.run("summarize sales.csv")
        self.assertEqual(r.route, "direct", r.plan.describe())
        self.assertEqual(llm.usage["calls"], 0)
        self.assertIn('"rows": 2', r.text)

    def test_agent_loop_exposes_selected_sops_and_records_trace(self):
        (self.cwd / "a.json").write_text('{"user": {"name": "ada"}}')
        script = [
            Response("", [ToolCall("1", "sop_data__json_extract", {"paths": ["user.name"], "file": "a.json"})], "tool_use"),
            Response("", [ToolCall("2", "bash", {"command": "echo hi"})], "tool_use"),
            Response("The name is ada.", [], "end_turn"),
        ]
        llm = FakeProvider(script)
        h = Harness(self.cfg(), llm=llm)
        r = h.run("fix the loader so it can extract the user name json field from a.json", allow_direct=False)
        self.assertEqual(r.route, "agent")
        self.assertIn("sop_data__json_extract", llm.seen[0]["tools"])
        self.assertNotIn("sop_git__status_summary", llm.seen[0]["tools"])      # irrelevant SOPs not sent
        tool_msgs = [m for m in h.messages if m["role"] == "tool"]
        self.assertIn('"ada"', tool_msgs[0]["content"])
        self.assertEqual(r.text, "The name is ada.")
        self.assertTrue(r.metrics["success"])
        self.assertEqual(len(list((self.cwd / ".rameness" / "runs").glob("*.json"))), 1)

    def test_reply_cut_off_at_the_output_limit_continues(self):
        script = [
            Response("<think>long plan that never finishes", [], "max_tokens"),
            Response("", [ToolCall("1", "bash", {"command": "echo hi"})], "tool_use"),
            Response("Done.", [], "end_turn"),
        ]
        llm = FakeProvider(script)
        h = Harness({**self.cfg(), "loop_guard": False}, llm=llm)
        r = h.run("write a script that prints hi", allow_direct=False)
        self.assertTrue(r.metrics["success"])
        self.assertEqual(len(llm.seen), 3)                          # truncation did not end the run
        self.assertIn("cut off at the output-token limit", str(llm.seen[1]["messages"][-1]["content"]))

    def test_sop_search_and_save_extend_toolset(self):
        script = [
            Response("", [ToolCall("1", "sop_search", {"query": "git status changes"})], "tool_use"),
            Response("", [ToolCall("2", "sop_save", {
                "id": "text.upper", "description": "uppercase text",
                "inputs": {"type": "object", "properties": {"s": {"type": "string"}}, "required": ["s"]},
                "script": "import json,sys; a=json.load(sys.stdin); print(json.dumps({'s': a['s'].upper()}))",
                "permissions": ["compute"], "tests": [{"input": {"s": "a"}, "expect": {"s": "A"}}]})], "tool_use"),
            Response("", [ToolCall("3", "sop_text__upper", {"s": "hey"})], "tool_use"),
            Response("done", [], "end_turn"),
        ]
        llm = FakeProvider(script)
        h = Harness(self.cfg(), llm=llm)
        h.run("design a new approach to refactor this module", allow_direct=False)
        self.assertIn("sop_git__status_summary", llm.seen[1]["tools"])
        self.assertIn("sop_text__upper", llm.seen[2]["tools"])
        self.assertIn('"HEY"', [m for m in h.messages if m["role"] == "tool"][-1]["content"])
        self.assertEqual(h.lib.get("text.upper").status, "validated")

    def test_readonly_mode_denies_bash(self):
        c = self.cfg()
        c["permissions"]["mode"] = "readonly"
        llm = FakeProvider([Response("", [ToolCall("1", "bash", {"command": "touch x"})], "tool_use"),
                            Response("ok", [], "end_turn")])
        h = Harness(c, llm=llm)
        h.run("implement the feature", allow_direct=False)
        self.assertFalse((self.cwd / "x").exists())


class TestWireFormats(unittest.TestCase):
    msgs = [{"role": "user", "content": "hi"},
            {"role": "assistant", "content": "", "tool_calls": [ToolCall("a", "bash", {"command": "ls"}),
                                                                ToolCall("b", "bash", {"command": "pwd"})]},
            {"role": "tool", "tool_call_id": "a", "name": "bash", "content": "x"},
            {"role": "tool", "tool_call_id": "b", "name": "bash", "content": "y", "is_error": True}]

    def test_anthropic_batches_tool_results(self):
        w = AnthropicProvider.to_wire(self.msgs)
        self.assertEqual([m["role"] for m in w], ["user", "assistant", "user"])
        self.assertEqual(len(w[2]["content"]), 2)
        self.assertTrue(w[2]["content"][1]["is_error"])

    def test_reasoning_round_trips_in_the_servers_own_field(self):
        # Current vLLM returns the thinking as `reasoning` and reads back only that field; llama.cpp, TabbyAPI and
        # ninfer use `reasoning_content`. Reading only reasoning_content lost every vLLM run's thinking (2026-10-07).
        from openai.types.chat import ChatCompletion
        for field in ("reasoning", "reasoning_content"):
            reply = ChatCompletion.model_validate({
                "id": "x", "object": "chat.completion", "created": 0, "model": "m",
                "choices": [{"index": 0, "finish_reason": "stop",
                             "message": {"role": "assistant", "content": "done", field: "I weighed two options."}}],
                "usage": {"prompt_tokens": 5, "completion_tokens": 3, "total_tokens": 8}})
            p = OpenAICompatProvider(base_url="http://127.0.0.1:9/v1", model="m")
            sent = []

            class Completions:
                def create(self, **kw):
                    sent.append(kw)
                    return reply

            class Client:
                chat = type("Chat", (), {"completions": Completions()})()

                def with_options(self, **kw):
                    return self

            p.client = Client()
            p.context_window = lambda: None
            r = p.chat("sys", [{"role": "user", "content": "go"}], [])
            self.assertEqual(r.reasoning, "I weighed two options.")
            p.chat("sys", [{"role": "user", "content": "go"},
                           {"role": "assistant", "content": "done", "reasoning": r.reasoning},
                           {"role": "user", "content": "next"}], [])
            back = sent[-1]["messages"][2]
            self.assertEqual(back.get(field), "I weighed two options.", field)

    def test_openai_format(self):
        w = OpenAICompatProvider.to_wire("sys", self.msgs)
        self.assertEqual([m["role"] for m in w], ["system", "user", "assistant", "tool", "tool"])
        self.assertEqual(json.loads(w[2]["tool_calls"][0]["function"]["arguments"]), {"command": "ls"})

    def test_images_ride_with_tool_results_in_each_wire_format(self):
        img = {"media_type": "image/png", "data": "aGk="}
        msgs = [{"role": "user", "content": "look"},
                {"role": "assistant", "content": "", "tool_calls": [ToolCall("a", "read_file", {"path": "s.png"}),
                                                                    ToolCall("b", "bash", {"command": "ls"})]},
                {"role": "tool", "tool_call_id": "a", "name": "read_file", "content": "[image s.png]", "images": [img]},
                {"role": "tool", "tool_call_id": "b", "name": "bash", "content": "x"},
                {"role": "assistant", "content": "seen", "tool_calls": []}]
        a = AnthropicProvider.to_wire(msgs)
        block = a[2]["content"][0]
        self.assertEqual(block["content"][1]["source"]["data"], "aGk=")
        o = OpenAICompatProvider.to_wire("sys", msgs)
        self.assertEqual([m["role"] for m in o], ["system", "user", "assistant", "tool", "tool", "user", "assistant"])
        self.assertEqual(o[5]["content"][1]["image_url"]["url"], "data:image/png;base64,aGk=")

    def test_text_tool_calls_both_forms_typed_and_round_trip(self):
        tools = [{"name": "write_file", "description": "", "input_schema": {"type": "object", "properties": {
                     "path": {"type": "string"}, "content": {"type": "string"}}}},
                 {"name": "bash", "description": "", "input_schema": {"type": "object", "properties": {
                     "command": {"type": "string"}, "timeout": {"type": "integer"}}}}]
        p = OpenAICompatProvider("m", tool_mode="prompt")
        code = 'const s = "a\\\\n\\"b\\"";\n  if (x) {\n    y();\n  }'
        text = ("Writing it.\n" + OpenAICompatProvider.format_call("write_file", {"path": "a.js", "content": code})
                + '\n<tool_call>{"name": "bash", "arguments": {"command": "node a.js"}}</tool_call>'
                + "\n<tool_call>\n<function=bash>\n<parameter=command>\nls\n</parameter>\n"
                  "<parameter=timeout>\n30\n</parameter>\n</function>\n</tool_call>")
        rest, calls = p._parse_prompted(text, tools)
        self.assertEqual(rest, "Writing it.")
        self.assertEqual([(c.name, c.input) for c in calls],
                         [("write_file", {"path": "a.js", "content": code}), ("bash", {"command": "node a.js"}),
                          ("bash", {"command": "ls", "timeout": 30})])
        # the history shows the model its calls in the same form, and keeps its reasoning
        msgs = [{"role": "assistant", "content": "", "reasoning": "plan", "tool_calls": calls[:1]},
                {"role": "tool", "tool_call_id": calls[0].id, "name": "write_file", "content": "ok"}]
        w = OpenAICompatProvider.to_wire("sys", msgs, prompted=True)
        self.assertEqual(w[1]["reasoning_content"], "plan")
        self.assertEqual(p._parse_prompted(w[1]["content"], tools)[1][0].input, calls[0].input)
        self.assertTrue(w[2]["content"].startswith('<tool_result name="write_file">'))


class TestPublish(Env):
    def test_scrub_blocks_private_content_and_publish_strips_origin(self):
        root = self.cwd / ".rameness" / "sops"
        write_sop(root, "acme.fetch", {"description": "fetch", "origin": {"task": "x"}},
                  "URL='https://db.acme.internal/x'\napi_key = 'abcd1234efgh5678'\n")
        write_sop(root, "gen.clean", {"description": "normalize names", "origin": {"task": "acme stuff"}},
                  "import json,sys; print(json.dumps({'ok': True}))")
        lib = self.lib()
        org = Org(name="Acme", private_terms=["acme.internal"])
        f = scrub(lib.get("acme.fetch"), org)
        self.assertTrue(any("secret" in x for x in f))
        self.assertTrue(any("private-host" in x for x in f))
        with self.assertRaises(PermissionError):
            publish(lib.get("acme.fetch"), self.tmp / "pkg", org)
        self.assertEqual(scrub(lib.get("gen.clean"), org), [])
        out = publish(lib.get("gen.clean"), self.tmp / "pkg", org)
        d = json.loads((out / "sop.json").read_text())
        self.assertNotIn("origin", d)
        self.assertEqual(d["scope"], "public")


class TestModelPolicy(unittest.TestCase):
    def test_family_settings_merge_over_the_config(self):
        from rameness.config import apply_model_policy
        cfg = {"model": "occamy-1.0", "todo_reminder_turns": 10, "progress_review": {"every": 10, "min_confidence": 0.5},
               "model_policies": {"qwen3": {"todo_reminder_turns": 12},
                                  "occamy": {"todo_reminder_turns": 6, "progress_review": {"every": 8}}}}
        self.assertEqual(apply_model_policy(cfg), "occamy")
        self.assertEqual(cfg["todo_reminder_turns"], 6)
        self.assertEqual(cfg["progress_review"], {"every": 8, "min_confidence": 0.5})
        self.assertIsNone(apply_model_policy({"model": "claude-opus-5-5", "model_policies": {"occamy": {"x": 1}}}))


if __name__ == "__main__":
    unittest.main()
