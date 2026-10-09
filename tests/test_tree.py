"""The SOP tree stays efficient as it grows: new SOPs are placed by walking the tree, crowded categories split
into subcategories inside themselves, tiny ones fold back, and moved SOPs keep working under their old ids."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from rameness import tree
from rameness.jev import Jev, LexicalJev
from rameness.learning import register_sop
from rameness.llm import FakeProvider
from rameness.publish import recategorize
from rameness.sops import Executor, Library

ECHO = "import json, sys\na = json.load(sys.stdin)\nprint(json.dumps({'n': a['n']}))\n"
TESTS = [{"input": {"n": 1}, "expect": {"n": 1}}, {"input": {"n": 2}, "expect": {"n": 2}}]


def write_sop(root, sid, desc):
    d = root.joinpath(*sid.split("."))
    d.mkdir(parents=True)
    (d / "run.py").write_text(ECHO)
    (d / "sop.json").write_text(json.dumps({"id": sid, "description": desc, "status": "validated", "tests": TESTS,
                                            "inputs": {"type": "object", "properties": {"n": {"type": "integer"}}}}))


def node(root, cid, desc):
    d = root.joinpath(*cid.split("."))
    d.mkdir(parents=True, exist_ok=True)
    (d / "_node.json").write_text(json.dumps({"description": desc}))


class Base(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp()) / "sops"
        self.root.mkdir()
        self.jev = Jev(LexicalJev())

    def lib(self):
        lib = Library([(self.root, "private")])
        return lib, Executor(lib, ["compute"], cwd=self.root.parent)


SERVER = ["start_server", "restart_server", "stop_server", "check_server", "serve_folder"]
BROWSER = ["load_page", "page_console_errors", "page_screenshot", "click_through", "page_probe"]


class Split(Base):
    def crowd(self):
        node(self.root, "http", "Web servers and browsers")
        for n in SERVER:
            write_sop(self.root, f"http.{n}", f"{n.replace('_', ' ')} for a local web server")
        for n in BROWSER:
            write_sop(self.root, f"http.{n}", f"{n.replace('_', ' ')} in a headless browser")
        lib, ex = self.lib()
        lib.stats["http.load_page"] = {"uses": 3, "ok": 3}
        return lib, ex

    def groups(self, members_server=SERVER, members_browser=BROWSER):
        return FakeProvider(json_script=[{"groups": [
            {"name": "server", "description": "Start, stop and check local web servers", "keywords": ["server"],
             "members": [f"http.{n}" for n in members_server]},
            {"name": "browser", "description": "Load and check pages in a headless browser", "keywords": ["browser"],
             "members": [f"http.{n}" for n in members_browser]}]}])

    def test_a_crowded_category_splits_into_subcategories_inside_itself(self):
        lib, ex = self.crowd()
        self.jev.yes = lambda *a, **k: 0.9                        # JEV confirms every member
        out = tree.rebalance(lib, ex, "http", self.groups(), self.jev)
        self.assertEqual(len(out["split"]), 10)
        self.assertEqual(sorted(c for c in lib.root.children["http"].children), ["browser", "server"])
        self.assertTrue((self.root / "http" / "browser" / "_node.json").exists())
        self.assertIn("load_page", lib.root.children["http"].children["browser"].children)
        moved = lib.get("http.load_page")                          # the old id still works
        self.assertEqual(moved.id, "http.browser.load_page")
        self.assertEqual(ex.run("http.load_page", {"n": 5}), {"n": 5})
        self.assertEqual(lib.stats["http.browser.load_page"], {"uses": 3, "ok": 3})   # its record moved with it
        self.assertEqual(ex.test("http.browser.load_page"), [])

    def test_members_jev_does_not_confirm_stay_where_they_are(self):
        lib, ex = self.crowd()
        self.jev.yes = lambda q, state, *a, **k: 0.9 if "server" in state else 0.1
        tree.rebalance(lib, ex, "http", self.groups(), self.jev)
        self.assertIn("load_page", lib.root.children["http"].children)   # browser group not confirmed: stays
        self.assertIn("server", lib.root.children["http"].children)

    def test_a_group_too_small_to_last_is_not_made(self):
        lib, ex = self.crowd()
        self.jev.yes = lambda *a, **k: 0.9
        out = tree.rebalance(lib, ex, "http", self.groups(members_browser=BROWSER[:2]), self.jev)
        self.assertEqual(out["folded"], [])                       # no split-then-undo
        self.assertNotIn("browser", lib.root.children["http"].children)
        self.assertIn("server", lib.root.children["http"].children)

    def test_a_small_category_does_not_split(self):
        node(self.root, "http", "Web")
        for n in SERVER:
            write_sop(self.root, f"http.{n}", n)
        lib, ex = self.lib()
        llm = FakeProvider()                                        # would raise if asked
        self.assertEqual(tree.rebalance(lib, ex, "http", llm, self.jev)["split"], [])
        self.assertEqual(llm.schemas, [])

    def test_a_tiny_subcategory_folds_back(self):
        node(self.root, "http", "Web")
        node(self.root, "http.server", "Servers")
        write_sop(self.root, "http.server.start_server", "start a server")
        write_sop(self.root, "http.server.stop_server", "stop a server")
        write_sop(self.root, "http.load_page", "load a page")
        lib, ex = self.lib()
        out = tree.rebalance(lib, ex, "http", None, self.jev)
        self.assertEqual(len(out["folded"]), 2)
        self.assertNotIn("server", lib.root.children["http"].children)
        self.assertEqual(lib.get("http.server.start_server").id, "http.start_server")


class Placement(Base):
    def test_a_deeper_path_is_kept_not_flattened(self):
        node(self.root, "code", "Source code: syntax checks, linting, formatting")
        node(self.root, "code.lint", "Lint source code with linters such as eslint or ruff")
        write_sop(self.root, "code.lint.ruff_check", "lint python with ruff")
        write_sop(self.root, "learned.eslint_check", "lint javascript source code with eslint")
        lib, ex = self.lib()
        sop = recategorize(lib, lib.get("learned.eslint_check"), "code.lint")
        self.assertEqual(sop.id, "code.lint.eslint_check")
        self.assertTrue((self.root / "code" / "lint" / "eslint_check" / "sop.json").exists())

    def test_placement_walks_down_into_the_subcategory_that_fits(self):
        node(self.root, "code", "Source code: syntax checks, linting, formatting")
        node(self.root, "code.lint", "lint linting linter source code eslint ruff")
        node(self.root, "data", "Data files: csv, json, spreadsheets")
        write_sop(self.root, "code.lint.ruff_check", "lint python with ruff")
        write_sop(self.root, "data.csv_rows", "count csv rows")
        write_sop(self.root, "code.lint.eslint_check", "lint linting javascript source code with eslint linter")
        lib, _ = self.lib()
        self.assertEqual(tree.place(self.jev, lib, lib.get("code.lint.eslint_check")), "code.lint")

    def test_registering_into_a_full_category_splits_it(self):
        node(self.root, "http", "Web servers and browsers")
        for n in SERVER + BROWSER[:3]:
            write_sop(self.root, f"http.{n}", f"{n.replace('_', ' ')}")
        lib, ex = self.lib()
        self.jev.yes = lambda *a, **k: 0.9
        groups = {"groups": [{"name": "server", "description": "local web servers",
                              "members": [f"http.{n}" for n in SERVER]},
                             {"name": "browser", "description": "headless browser pages",
                              "members": [f"http.{n}" for n in BROWSER[:3]] + ["http.page_probe"]}]}
        llm = FakeProvider(json_script=[groups])                  # nothing runs inputs: no security model call
        with patch("rameness.tree.place", return_value="http"):   # filed into http (placement is tested above)
            sop, failures = register_sop(lib, ex, {"id": "http.page_probe", "description": "probe a page",
                                                   "script": ECHO, "tests": TESTS,
                                                   "inputs": {"type": "object", "properties": {"n": {"type": "integer"}}}},
                                         llm=llm, jev=self.jev)
        self.assertEqual(failures, [])
        self.assertEqual(sop.id, "http.browser.page_probe")       # filed, the category split, it moved with it
        self.assertEqual(sorted(lib.root.children["http"].children), ["browser", "server"])


if __name__ == "__main__":
    unittest.main()
