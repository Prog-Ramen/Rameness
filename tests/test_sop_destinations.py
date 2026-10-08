"""Where a generated SOP goes: its category, built-in (Rameness repo) or registry (RamenSOPs), and
the packages and programs it may need."""
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from rameness import deps, publish
from rameness.jev import Jev, LexicalJev
from rameness.org import Org
from rameness.registry import Registry
from rameness.learning import CloseSOPExists, Learner, RunStore, extend_sop, register_sop
from rameness.llm import FakeProvider
from rameness.sops import BUILTIN_ROOT, Executor, Library

GENERIC = 'import json, sys\nargs = json.load(sys.stdin)\nprint(json.dumps({"out": str(args.get("x", "")).upper()}))\n'


def git(cwd, *a):
    return subprocess.run(["git", *a], cwd=cwd, check=True, capture_output=True, text=True).stdout


class Requirements(unittest.TestCase):
    def setUp(self):
        self.d = Path(tempfile.mkdtemp())

    def test_lists_packages_and_programs_beyond_the_base(self):
        (self.d / "run.py").write_text(
            "import json, os, subprocess\nimport yaml\nfrom PIL import Image\nimport helper\n"
            "subprocess.run(['jq', '.a'], check=True)\nsubprocess.run(['git', 'status'])\n"
            "os.system('curl -s https://example.com | grep x')\n")
        (self.d / "helper.py").write_text("X = 1\n")                 # a local module is not a package
        req = deps.requirements(self.d)
        self.assertEqual(req["python"], ["pillow", "pyyaml"])        # install names, not import names
        self.assertEqual(req["commands"], ["curl", "jq"])           # git and grep come with the base
        self.assertEqual(deps.describe(req), "May need Python packages: pillow, pyyaml; programs: curl, jq.")

    def test_shell_scripts_and_nothing_extra(self):
        (self.d / "run.sh").write_text("#!/bin/sh\nset -e\nOUT=x ffmpeg -i in.mp4 out.webm && ls | wc -l\n")
        self.assertEqual(deps.requirements(self.d), {"python": [], "commands": ["ffmpeg"]})
        (self.d / "run.sh").unlink()
        (self.d / "run.py").write_text(GENERIC)
        self.assertEqual(deps.describe(deps.requirements(self.d)), "")

    def test_builtin_requirements_are_shown_to_the_agent(self):
        lib = Library([(BUILTIN_ROOT, "public")])
        self.assertIn("May need", lib.get("code.syntax_check").tool_schema()["description"])
        self.assertNotIn("May need", lib.get("data.json_extract").tool_schema()["description"])


class Destinations(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        os.environ["RAMENESS_HOME"] = str(self.tmp / "home")
        self.jev = Jev(LexicalJev())
        self.org = Org(name="Acme")
        self.root = self.tmp / "proj" / ".rameness" / "sops"

    def sop(self, sid, desc, keywords, origin=None):
        d = self.root.joinpath(*sid.split("."))
        d.mkdir(parents=True)
        (d / "sop.json").write_text(json.dumps({"id": sid, "description": desc, "keywords": keywords,
                                                "status": "validated", "origin": origin or {}}))
        (d / "run.py").write_text(GENERIC)
        return Library([(BUILTIN_ROOT, "public"), (self.root, "private")]).get(sid)

    def test_jev_files_the_sop_under_an_existing_category(self):
        s = self.sop("learned.csv_columns", "Summarise the columns of a CSV data file: counts, types, nulls",
                     ["csv", "data", "columns", "summary"])
        lib = Library([(BUILTIN_ROOT, "public"), (self.root, "private")])
        cat = publish.categorize(self.jev, lib, s)
        self.assertEqual(cat, "data")
        moved = publish.recategorize(lib, s, cat)
        self.assertEqual(moved.id, "data.csv_columns")
        self.assertTrue((self.root / "data" / "csv_columns" / "sop.json").exists())
        self.assertFalse((self.root / "learned" / "csv_columns").exists())

    def test_builtin_needs_broad_use_and_jev(self):
        s = self.sop("data.read_json", "Read a JSON file and check its format: a common basic file utility",
                     ["json", "file", "read", "check", "format"])
        few = publish.destination(self.jev, s, runs_seen=4, runs_total=5)
        self.assertEqual(few["destination"], "registry")              # too little evidence
        self.assertIn("only 5 runs", few["reason"])
        rare = publish.destination(self.jev, s, runs_seen=3, runs_total=30)
        self.assertEqual(rare["destination"], "registry")             # used in 10% of runs
        common = publish.destination(self.jev, s, runs_seen=24, runs_total=30)
        self.assertEqual(common["destination"], "builtin", common)

    def test_builtin_proposal_goes_to_the_rameness_repo_as_a_draft_for_developers(self):
        # a stand-in for the Rameness repo, whose own tests carry a fake secret outside builtin_sops/
        src, bare = self.tmp / "rameness-src", self.tmp / "rameness.git"
        (src / "rameness" / "builtin_sops" / "data").mkdir(parents=True)
        (src / "rameness" / "builtin_sops" / "data" / "_node.json").write_text('{"description": "data"}')
        (src / "tests").mkdir()
        (src / "tests" / "fixture.py").write_text("TOKEN = 'ghp_abcdefghijklmnopqrstuvwxyz0123456789'\n")
        git(self.tmp, "init", "-q", "-b", "main", str(src))
        git(src, "add", "-A")
        git(src, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "base")
        git(self.tmp, "clone", "-q", "--bare", str(src), str(bare))
        s = self.sop("data.read_json", "Read a JSON file and check its format: a common basic file utility",
                     ["json", "file", "read", "check", "format"], origin={"runs_seen": 24, "runs_total": 30})
        reg = Registry({"registry": {"public": str(self.tmp / "unused.git"), "builtin": str(bare),
                                     "min_tokens_saved": 0}},
                       self.tmp / "home", self.org, self.jev)
        r = reg.propose(s)
        self.assertEqual(r["destination"], "builtin")
        files = git(self.tmp, "--git-dir", str(bare), "ls-tree", "-r", "--name-only", r["branch"])
        self.assertIn("rameness/builtin_sops/data/read_json/run.py", files)
        self.assertIn("Needs verification by a Rameness developer", r["body"])
        self.assertIn("Needs nothing beyond Python", r["body"])
        d = json.loads(git(self.tmp, "--git-dir", str(bare), "show",
                           f"{r['branch']}:rameness/builtin_sops/data/read_json/sop.json"))
        self.assertNotIn("destination", d)
        self.assertNotIn("origin", d)

    def test_registry_is_the_default_without_a_builtin_target(self):
        s = self.sop("data.read_json", "Read a JSON file", ["json"], origin={"runs_seen": 30, "runs_total": 30})
        reg = Registry({"registry": {"public": str(self.tmp / "p.git")}}, self.tmp / "home", self.org, self.jev)
        self.assertEqual(reg.destination(s)["destination"], "registry")


JQ_SCRIPT = ("import json, subprocess, sys\nargs = json.load(sys.stdin)\n"
             "out = subprocess.run(['jq', 'keys | length'], input=json.dumps(args['obj']), capture_output=True, text=True)\n"
             "print(json.dumps({'count': int(out.stdout.strip() or 0)}))\n")
STD_SCRIPT = "import json, sys\nargs = json.load(sys.stdin)\nprint(json.dumps({'count': len(args['obj'])}))\n"
WRONG_SCRIPT = "import json, sys\nprint(json.dumps({'count': -1}))\n"


class Simplify(unittest.TestCase):
    """Occam's razor: an SOP keeps an extra package or program only if nothing standard does the job."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.root = self.tmp / ".rameness" / "sops"
        self.root.mkdir(parents=True)
        self.lib = Library([(self.root, "private")])
        self.ex = Executor(self.lib, [], cwd=self.tmp)
        self.spec = {"id": "data.count_keys", "description": "Count the keys of a JSON object", "script": JQ_SCRIPT,
                     "inputs": {"type": "object", "properties": {"obj": {"type": "object"}}},
                     "tests": [{"input": {"obj": {"a": 1, "b": 2}}, "expect": {"count": 2}}]}

    def test_a_standard_rewrite_that_passes_replaces_the_extra_program(self):
        sop, failures = register_sop(self.lib, self.ex, self.spec, llm=FakeProvider(json_script=[{"script": STD_SCRIPT}]))
        self.assertEqual(failures, [])
        self.assertEqual(sop.status, "validated")
        self.assertEqual(sop.requirements, {})                      # jq is gone
        self.assertEqual(sop.origin["simplified"]["removed"], {"commands": ["jq"]})
        self.assertIn("len(args['obj'])", (sop.path / "run.py").read_text())

    def test_a_rewrite_that_fails_the_tests_is_rejected(self):
        sop, _ = register_sop(self.lib, self.ex, self.spec, llm=FakeProvider(json_script=[{"script": WRONG_SCRIPT}]))
        self.assertEqual(sop.requirements, {"commands": ["jq"]})     # the original stays
        self.assertIn("jq", (sop.path / "run.py").read_text())

    def test_the_model_can_say_the_extra_is_unavoidable(self):
        sop, _ = register_sop(self.lib, self.ex, self.spec,
                              llm=FakeProvider(json_script=[{"keep": True, "why": "needs jq's streaming parser"}]))
        self.assertEqual(sop.requirements, {"commands": ["jq"]})

COUNT_TESTS = [{"input": {"obj": {"a": 1, "b": 2}}, "expect": {"count": 2}}]
EXTENDED = ("import json, sys\nargs = json.load(sys.stdin)\nobj = args['obj']\n"
            "for key in (args.get('path') or '').split('.'):\n    obj = obj[key] if key else obj\n"
            "print(json.dumps({'count': len(obj)}))\n")
NEW_TESTS = [{"input": {"obj": {"x": {"a": 1, "b": 2, "c": 3}}, "path": "x"}, "expect": {"count": 3}}]
NESTED_SPEC = {"id": "data.count_nested_keys", "description": "Count the keys of an object at a path inside JSON",
               "script": "...", "inputs": {"type": "object", "properties": {"obj": {}, "path": {"type": "string"}}}}


class Extend(unittest.TestCase):
    """A close existing SOP is extended (backward compatible, tests decide) instead of adding a near-duplicate."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.private = self.tmp / ".rameness" / "sops"
        self.public = self.tmp / "public"

    def make(self, root, scope):
        d = root / "data" / "count_keys"
        d.mkdir(parents=True)
        (d / "run.py").write_text(STD_SCRIPT)
        (d / "sop.json").write_text(json.dumps({"id": "data.count_keys", "description": "Count the keys of a JSON object",
                                                "inputs": {"type": "object", "properties": {"obj": {"type": "object"}},
                                                           "required": ["obj"]},
                                                "tests": COUNT_TESTS, "version": "1.0.0"}))
        lib = Library([(self.public, "public"), (self.private, "private")])
        return lib, Executor(lib, [], cwd=self.tmp)

    def reply(self, script=EXTENDED, tests=NEW_TESTS, required=None):
        props = {"obj": {"type": "object"}, "path": {"type": "string"}}
        return FakeProvider(json_script=[{"script": script, "tests": tests, "keywords": ["path", "nested"],
                                          "inputs": {"type": "object", "properties": props,
                                                     "required": required or ["obj"]}}])

    def test_a_private_sop_is_extended_in_place(self):
        lib, ex = self.make(self.private, "private")
        sop = extend_sop(lib, ex, lib.get("data.count_keys"), NESTED_SPEC, self.reply())
        self.assertIsNotNone(sop)
        self.assertEqual(sop.version, "1.1.0")
        self.assertEqual(len(sop.tests), 2)                          # the old test kept, the new one added
        self.assertEqual(ex.test("data.count_keys"), [])
        self.assertEqual(sop.origin["extended"][0]["from"], "1.0.0")
        self.assertEqual(sorted(p.name for p in (self.private / "data").iterdir()), ["count_keys"])  # no new SOP

    def test_an_extension_that_breaks_an_old_test_is_rejected(self):
        lib, ex = self.make(self.private, "private")
        before = (self.private / "data" / "count_keys" / "run.py").read_text()
        self.assertIsNone(extend_sop(lib, ex, lib.get("data.count_keys"), NESTED_SPEC, self.reply(script=WRONG_SCRIPT)))
        self.assertEqual((self.private / "data" / "count_keys" / "run.py").read_text(), before)
        self.assertEqual(lib.get("data.count_keys").version, "1.0.0")

    def test_a_new_required_input_is_rejected(self):
        lib, ex = self.make(self.private, "private")
        self.assertIsNone(extend_sop(lib, ex, lib.get("data.count_keys"), NESTED_SPEC,
                                     self.reply(required=["obj", "path"])))     # old callers would break

    def test_a_public_sop_gets_a_private_override_with_the_same_id(self):
        lib, ex = self.make(self.public, "public")
        sop = extend_sop(lib, ex, lib.get("data.count_keys"), NESTED_SPEC, self.reply())
        self.assertEqual(sop.scope, "private")                       # the private layer wins
        self.assertEqual(sop.origin["overrides"], "registry")        # not under rameness/builtin_sops
        reg = Registry({"registry": {"public": "x", "builtin": "y"}}, self.tmp / "home", Org(), Jev(LexicalJev()))
        self.assertEqual(reg.destination(sop)["destination"], "registry")   # the update goes where the original is
        self.assertEqual((self.public / "data" / "count_keys" / "run.py").read_text(), STD_SCRIPT)  # original untouched
        self.assertTrue((self.private / "data" / "count_keys" / "run.py").exists())

    def test_the_learner_extends_a_close_match_and_otherwise_creates_a_new_sop(self):
        lib, ex = self.make(self.private, "private")
        spec = {**NESTED_SPEC, "script": EXTENDED, "tests": NEW_TESTS}
        learner = Learner(lib, ex, Jev(LexicalJev()), RunStore(self.tmp / "runs"), self.reply())
        learner.extension_target = lambda spec: lib.get("data.count_keys")
        sop, failures, extended = learner.extend_or_register(spec)
        self.assertTrue(extended)
        self.assertEqual((sop.id, sop.version), ("data.count_keys", "1.1.0"))
        learner.llm = self.reply(script=WRONG_SCRIPT)               # this extension fails the old test...
        sop, failures, extended = learner.extend_or_register({**spec, "id": "data.count_deep"})
        self.assertFalse(extended)                                    # ...so a new SOP is created instead
        self.assertEqual(sop.id, "data.count_deep")

    def test_end_of_run_learning_drops_a_failing_near_duplicate_when_extension_fails(self):
        lib, ex = self.make(self.private, "private")
        learner = Learner(lib, ex, Jev(LexicalJev()), RunStore(self.tmp / "runs"), self.reply(script=WRONG_SCRIPT))
        learner.extension_target = lambda spec: lib.get("data.count_keys")
        with self.assertRaises(CloseSOPExists) as e:
            learner.extend_or_register({**NESTED_SPEC, "id": "data.count_deep", "script": WRONG_SCRIPT,
                                        "tests": COUNT_TESTS}, new_if_close=False)
        self.assertEqual(str(e.exception), "data.count_keys")
        self.assertEqual(sorted(p.name for p in (self.private / "data").iterdir()), ["count_keys"])


class TrustedSops(unittest.TestCase):
    """The agent sees only SOPs that pass their tests, and their interface says so, so it need not re-check them."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        root = self.tmp / "sops"
        for name, status in (("counted", "validated"), ("draft", "candidate")):
            d = root / "data" / name
            d.mkdir(parents=True)
            (d / "run.py").write_text(STD_SCRIPT)
            (d / "sop.json").write_text(json.dumps({"id": f"data.{name}", "description": "Count the keys of a JSON object",
                                                    "status": status, "tests": COUNT_TESTS}))
        self.lib = Library([(root, "private")])

    def test_candidates_are_not_offered(self):
        found = [s.id for s, _ in self.lib.search(Jev(LexicalJev()), "count the keys of a JSON object")]
        self.assertIn("data.counted", found)
        self.assertNotIn("data.draft", found)

    def test_a_tested_sop_says_its_output_is_verified(self):
        desc = self.lib.get("data.counted").tool_schema(uses=4)["description"]
        self.assertIn("tests pass, used successfully 4 times", desc)
        self.assertNotIn("Tested", self.lib.get("data.draft").tool_schema()["description"])


class PrivateFolders(unittest.TestCase):
    """The user picks private SOP folders per use case or session; private SOPs and their tests never reach git."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.cwd, self.home = self.tmp / "project", self.tmp / "home"
        self.cwd.mkdir()

    def spec(self, sid):
        return {"id": sid, "description": "Count the keys of a JSON object", "script": STD_SCRIPT, "tests": COUNT_TESTS}

    def test_session_folders_are_searched_and_receive_new_sops(self):
        work, research = self.tmp / "work-sops", self.tmp / "research-sops"
        lib = Library.default(self.cwd, self.home, [str(work), str(research)], str(research))
        self.assertEqual(lib.private_root, research.resolve())
        register_sop(lib, Executor(lib, [], cwd=self.cwd), self.spec("data.count_keys"))
        self.assertTrue((research / "data" / "count_keys" / "sop.json").exists())
        self.assertFalse((self.cwd / ".rameness" / "sops" / "data").exists())
        other = Library.default(self.cwd, self.home, [str(work)])          # another session does not see it
        self.assertNotIn("data.count_keys", other.sops)

    def test_relative_folders_resolve_against_the_project(self):
        lib = Library.default(self.cwd, self.home, ["~/x", "team-sops"])
        self.assertIn(((self.cwd / "team-sops").resolve(), "private"), lib.roots)
        self.assertIn((Path("~/x").expanduser().resolve(), "private"), lib.roots)

    def test_a_private_folder_inside_a_git_repo_is_ignored_by_git(self):
        subprocess.run(["git", "init", "-q", str(self.cwd)], check=True)
        lib = Library.default(self.cwd, self.home)
        register_sop(lib, Executor(lib, [], cwd=self.cwd), self.spec("data.count_keys"))
        status = subprocess.run(["git", "-C", str(self.cwd), "status", "--porcelain", "--untracked-files=all"],
                                capture_output=True, text=True, check=True).stdout
        self.assertNotIn("count_keys", status)                            # code and tests both stay local

    def test_a_folder_outside_git_gets_no_gitignore(self):
        lib = Library.default(self.cwd, self.home)
        register_sop(lib, Executor(lib, [], cwd=self.cwd), self.spec("data.count_keys"))
        self.assertFalse((self.cwd / ".rameness" / "sops" / ".gitignore").exists())


READ_PAGE = ("import json, sys\nargs = json.load(sys.stdin)\ntext = open(args['path']).read()\n"
             "print(json.dumps({'chars': len(text), 'has_script': '<script' in text}))\n")
PNG_SIZE = ("import json, struct, sys\nargs = json.load(sys.stdin)\ndata = open(args['path'], 'rb').read()\n"
            "w, h = struct.unpack('>II', data[16:24])\nprint(json.dumps({'width': w, 'height': h}))\n")
MAKE_PNG = ("import struct, zlib\n"
            "def chunk(t, d): return struct.pack('>I', len(d)) + t + d + struct.pack('>I', zlib.crc32(t + d))\n"
            "raw = b''.join(b'\\x00' + b'\\xff\\x00\\x00' * 3 for _ in range(2))\n"
            "open('img.png', 'wb').write(b'\\x89PNG\\r\\n\\x1a\\n' + chunk(b'IHDR', struct.pack('>IIBBBBB', 3, 2, 8, 2, 0, 0, 0))"
            " + chunk(b'IDAT', zlib.compress(raw)) + chunk(b'IEND', b''))\n")


class FinishingSops(unittest.TestCase):
    """The model finishes an SOP with tests that run: fixtures for files, setup code for binary data, and repair
    rounds that see what the tests actually returned."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.root = self.tmp / ".rameness" / "sops"
        self.root.mkdir(parents=True)
        self.lib = Library([(self.root, "private")])
        self.ex = Executor(self.lib, [], cwd=self.tmp)

    def test_fixture_files_and_setup_code_make_tests_runnable(self):
        sop, failures = register_sop(self.lib, self.ex, {
            "id": "web.read_page", "description": "Read an HTML page", "script": READ_PAGE,
            "tests": [{"input": {"path": "page.html"}, "files": {"page.html": "<html><script>x</script></html>"},
                       "expect": {"chars": 31, "has_script": True}}]})
        self.assertEqual((failures, sop.status), ([], "validated"))
        sop, failures = register_sop(self.lib, self.ex, {
            "id": "data.png_size", "description": "Size of a PNG", "script": PNG_SIZE,
            "tests": [{"input": {"path": "img.png"}, "setup": MAKE_PNG, "expect": {"width": 3, "height": 2}}]})
        self.assertEqual((failures, sop.status), ([], "validated"))     # a real PNG, built by code, not by hand

    def test_a_wrong_expectation_is_repaired_after_seeing_the_real_output(self):
        spec = {"id": "data.png_size", "description": "Size of a PNG", "script": PNG_SIZE,
                "tests": [{"input": {"path": "img.png"}, "setup": MAKE_PNG, "expect": {"width": 4, "height": 4}}]}
        fixed = {"script": PNG_SIZE, "explanation": "the image built by setup is 3x2",
                 "tests": [{"input": {"path": "img.png"}, "setup": MAKE_PNG, "expect": {"width": 3, "height": 2}}]}
        llm = FakeProvider([], json_script=[fixed])
        sop, failures = register_sop(self.lib, self.ex, spec, llm=llm)
        self.assertEqual((failures, sop.status), ([], "validated"))
        self.assertEqual(sop.origin["repairs"], ["the image built by setup is 3x2"])
        self.assertEqual(llm.schemas[-1]["required"], ["script", "tests"])

    def test_tests_never_run_in_the_project_directory(self):
        (self.ex.cwd / "report.json").write_text("the real report")
        writes = "import json,sys; a=json.load(sys.stdin); open('report.json','w').write('sample'); print('{}')"
        register_sop(self.lib, self.ex, {"id": "x.writer", "description": "writes a report", "script": writes,
                                         "tests": [{"input": {}, "expect_keys": []}]})
        self.assertEqual((self.ex.cwd / "report.json").read_text(), "the real report")

    def test_a_script_that_does_not_compile_is_fixed_from_the_compiler_error(self):
        broken = "import json, sys\nprint(json.dumps({'ok': 'yes\n'}))\n"     # a newline inside the literal
        fixed = {"script": "import json, sys\nprint(json.dumps({'ok': 'yes'}))\n"}
        sop, failures = register_sop(self.lib, self.ex, {"id": "x.ok", "description": "ok", "script": broken,
                                                         "tests": [{"input": {}, "expect": {"ok": "yes"}}]},
                                     llm=FakeProvider([], json_script=[fixed]))
        self.assertEqual((failures, sop.status), ([], "validated"))

    def test_a_repair_that_guts_the_tests_is_refused(self):
        spec = {"id": "data.png_size", "description": "Size of a PNG", "script": PNG_SIZE,
                "tests": [{"input": {"path": "img.png"}, "setup": MAKE_PNG, "expect": {"width": 4, "height": 4}}]}
        gutted = {"script": PNG_SIZE, "tests": [{"input": {"path": "img.png"}, "setup": MAKE_PNG}]}
        sop, failures = register_sop(self.lib, self.ex, spec, llm=FakeProvider([], json_script=[gutted, gutted]))
        self.assertTrue(failures)
        self.assertEqual(sop.status, "candidate")
        self.assertEqual(sop.tests[0]["expect"], {"width": 4, "height": 4})   # the original test stays

if __name__ == "__main__":
    unittest.main()
