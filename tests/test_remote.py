import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path

from rameness import config
from rameness.harness import Harness
from rameness.llm import FakeProvider
from rameness.publish import build_index
from rameness.sops import BUILTIN_ROOT

SLUG = ("import json, re, sys\na = json.load(sys.stdin)\n"
        "print(json.dumps({'slug': re.sub(r'[^a-z0-9]+', '-', a['text'].lower()).strip('-')}))\n")


def sop(root, sid, desc, keywords, script, perms=("compute",), tests=None, node=None):
    d = root.joinpath(*sid.split("."))
    d.mkdir(parents=True)
    (d / "sop.json").write_text(json.dumps({
        "id": sid, "description": desc, "keywords": keywords, "permissions": list(perms),
        "inputs": {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]},
        "tests": tests if tests is not None else [{"input": {"text": "Hello World"}, "expect": {"slug": "hello-world"}}]}))
    (d / "run.py").write_text(script)
    if node:
        (d.parent / "_node.json").write_text(json.dumps(node))


class RemotePullTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        os.environ["RAMENESS_HOME"] = str(self.tmp / "home")
        self.reg = self.tmp / "registry" / "sops"
        sop(self.reg, "text.slugify", "Convert a title or sentence into a URL slug", ["slug", "slugify", "url", "title"],
            SLUG, node={"description": "Text transformations: slugs, case, wrapping", "keywords": ["text", "string"]})
        sop(self.reg, "media.thumbnail", "Resize an image into a thumbnail", ["image", "resize", "thumbnail", "png"],
            "print('{}')", tests=[], node={"description": "Images, audio and video processing", "keywords": ["image", "video"]})
        sop(self.reg, "finance.ledger_export", "Export ledger entries", ["ledger", "accounting", "export"],
            "print('{}')", tests=[], node={"description": "Accounting and ledgers", "keywords": ["ledger", "invoice"],
                                           "requires": [{"name": "ledger_system", "hints": ["xero", "quickbooks"]}]})
        sop(self.reg, "net.fetch_json", "Fetch a URL and slugify the title field", ["slug", "url", "fetch"],
            SLUG, perms=("network",), node={"description": "Network helpers", "keywords": ["http", "url", "slug"]})
        build_index(self.reg)
        (self.tmp / "proj" / ".rameness").mkdir(parents=True)

    def harness(self, **reg):
        cfg = config.load(self.tmp / "proj", {"provider": "fake"})
        cfg["jev"]["backend"] = "lexical"
        cfg["permissions"]["mode"] = "auto"
        cfg["learning"]["enabled"] = False
        cfg["registry"].update({"remote_index": (self.reg / "index.json").as_uri(), **reg})
        return Harness(cfg, llm=FakeProvider())

    def test_index_is_sharded(self):
        root = json.loads((self.reg / "index.json").read_text())
        self.assertEqual({e["type"] for e in root["entries"]}, {"node"})          # categories only, no SOPs
        self.assertTrue((self.reg / "text" / "_index.json").exists())
        sub = json.loads((self.reg / "text" / "_index.json").read_text())
        self.assertEqual(sub["entries"][0]["id"], "text.slugify")
        self.assertTrue(sub["entries"][0]["files"][0]["sha256"])

    def test_pulls_only_the_needed_sop_and_only_explored_branches(self):
        h = self.harness()
        plan = h.plan("slugify this blog post title into a url slug")
        pulled = [x["id"] for x in plan.pulls if x["result"] == "pulled"]
        self.assertEqual(pulled, ["text.slugify"], plan.describe())
        fetched = h.router.remote.fetched
        self.assertIn("<root>", fetched)
        self.assertIn("text", fetched)
        self.assertNotIn("media", fetched)                      # unrelated branch never downloaded
        self.assertNotIn("finance", fetched)                    # requirement unmet: deferred, not opened
        installed = [p for p in (self.tmp / "home" / "public").rglob("sop.json")]
        self.assertEqual([p.parent.name for p in installed], ["slugify"])          # nothing else mirrored
        self.assertIn("text.slugify", [s.id for s, _ in plan.activation.selected])
        self.assertEqual(h.executor.run("text.slugify", {"text": "Ramen Is Good"}), {"slug": "ramen-is-good"})

    def test_extra_permissions_need_a_user(self):
        h = self.harness()
        plan = h.plan("fetch the url and slugify the title field from it")
        net = [x for x in plan.pulls if x["id"] == "net.fetch_json"]
        self.assertTrue(net and net[0]["result"] == "skipped" and "network" in net[0]["reason"], plan.pulls)
        self.assertFalse((self.tmp / "home" / "public" / "remote" / "sops" / "net").exists())

    def test_tampered_file_is_rejected(self):
        (self.reg / "text" / "slugify" / "run.py").write_text("import os; os.system('echo pwned')\n")
        h = self.harness()
        plan = h.plan("slugify this blog post title into a url slug")
        rec = [x for x in plan.pulls if x["id"] == "text.slugify"][0]
        self.assertEqual(rec["result"], "rejected")
        self.assertIn("hash mismatch", rec["reason"])
        self.assertNotIn("text.slugify", h.lib.sops)

    def test_failing_tests_remove_the_sop(self):
        shutil.rmtree(self.reg / "text")
        sop(self.reg, "text.slugify", "Convert a title or sentence into a URL slug", ["slug", "slugify", "url", "title"],
            "import json; print(json.dumps({'slug': 'wrong'}))",
            node={"description": "Text transformations: slugs, case, wrapping", "keywords": ["text", "string"]})
        build_index(self.reg)
        h = self.harness()
        rec = [x for x in h.plan("slugify this blog post title into a url slug").pulls if x["id"] == "text.slugify"][0]
        self.assertEqual(rec["result"], "rejected")
        self.assertNotIn("text.slugify", h.lib.sops)

    def test_local_coverage_skips_the_registry(self):
        (self.tmp / "proj" / "sales.csv").write_text("a,b\n1,2\n")
        h = self.harness()
        h.plan("what uncommitted changes do I have in git status")
        self.assertEqual(h.router.remote.fetched, [])

    def test_offline_registry_is_harmless(self):
        h = self.harness(remote_index=(self.tmp / "nowhere" / "index.json").as_uri())
        plan = h.plan("slugify this blog post title into a url slug")
        self.assertEqual(plan.pulls, [])

    def test_off_switch(self):
        h = self.harness(auto_pull="off")
        self.assertIsNone(h.router.remote)


if __name__ == "__main__":
    unittest.main()
