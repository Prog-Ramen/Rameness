import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from rameness import publish
from rameness.jev import Jev, LexicalJev
from rameness.org import Org
from rameness.registry import Registry, RegistryError, fingerprint
from rameness.sops import Executor, Library

GENERIC = ("import json, sys\na = json.load(sys.stdin)\n"
           "print(json.dumps({'upper': a['text'].upper()}))\n")


def sh(cwd, *args):
    return subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", *args], cwd=cwd,
                          capture_output=True, text=True)


class RegistryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        os.environ["RAMENESS_HOME"] = str(self.tmp / "home")
        self.public = self.tmp / "public.git"
        subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(self.public)], check=True)
        self.proj = self.tmp / "proj"
        self.root = self.proj / ".rameness" / "sops"
        self.org = Org(name="Acme", private_terms=["analytics.acme.internal"])
        self.jev = Jev(LexicalJev())

    def sop(self, sid, desc, script, keywords, status="validated"):
        d = self.root.joinpath(*sid.split("."))
        d.mkdir(parents=True)
        (d / "sop.json").write_text(json.dumps({"id": sid, "description": desc, "keywords": keywords,
                                                "status": status, "origin": {"task": "internal ticket 42"}}))
        (d / "run.py").write_text(script)
        return Library([(self.root, "private")]).get(sid)

    def reg(self, **over):
        cfg = {"registry": {"public": str(self.public), "min_tokens_saved": 0, **over}}   # tiny example SOPs
        return Registry(cfg, self.tmp / "home", self.org, self.jev)

    def test_classification(self):
        gen = self.sop("text.upper", "Convert text to upper case: a generic reusable text utility",
                       GENERIC, ["text", "convert", "format", "utility", "generic"])
        per = self.sop("acme.customers", "Pull our customer records from the company analytics database",
                       "print('psql analytics.acme.internal')", ["customer", "company", "internal"])
        self.assertEqual(publish.classify(self.jev, gen, self.org)["visibility"], "shareable")
        r = publish.classify(self.jev, per, self.org)
        self.assertEqual(r["visibility"], "private")
        self.assertIn("scrubber", r["reason"])                      # private term found before JEV even weighs in
        self.assertEqual(json.loads((per.path / "sop.json").read_text())["visibility"], "private")

    def test_benign_findings_are_judged_by_jev(self):
        doc = self.sop("text.email", "Validate an email address format: a generic reusable text utility",
                       GENERIC + "# example: user@example.com, placeholder test fixture\n",
                       ["text", "email", "validate", "utility", "generic"])
        r = publish.classify(self.jev, doc, self.org)
        self.assertEqual(r["visibility"], "shareable")
        self.assertIn("judged benign", r["reason"])
        leak = self.sop("ops.backup", "Back up a directory: a generic reusable file utility",
                        GENERIC + "# runs as jane.doe@initech.com on the production database server\n",
                        ["file", "backup", "utility", "generic"])
        self.assertEqual(publish.classify(self.jev, leak, self.org)["visibility"], "private")
        with self.assertRaisesRegex(RegistryError, "JEV judged these findings private"):
            self.reg().propose(leak, override_personal=True)

    def test_propose_opens_public_branch_sanitized(self):
        gen = self.sop("text.upper", "Convert text to upper case: a generic reusable text utility",
                       GENERIC, ["text", "convert", "format", "utility", "generic"])
        r = self.reg().propose(gen)
        self.assertTrue(r["branch"].startswith("sop/text.upper-"))
        self.assertIn(r["branch"], sh(self.tmp, "--git-dir", str(self.public), "branch").stdout)
        files = sh(self.tmp, "--git-dir", str(self.public), "ls-tree", "-r", "--name-only", r["branch"]).stdout
        self.assertIn("sops/text/upper/run.py", files)
        self.assertNotIn("sops/index.json", files)                  # generated on merge, never in a PR
        blob = sh(self.tmp, "--git-dir", str(self.public), "show", f"{r['branch']}:sops/text/upper/sop.json").stdout
        d = json.loads(blob)
        self.assertNotIn("origin", d)                                # the task that produced it stays private
        self.assertNotIn("classified", d)
        self.assertEqual(d["scope"], "public")

    def test_push_goes_to_fork_when_configured(self):
        gen = self.sop("text.upper", "Convert text to upper case: a generic reusable text utility",
                       GENERIC, ["text", "convert", "format", "utility", "generic"])
        fork = self.tmp / "fork.git"
        subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(fork)], check=True)
        r = self.reg(fork=str(fork)).propose(gen)
        self.assertEqual(r["pushed"], str(fork))
        self.assertIn(r["branch"], sh(self.tmp, "--git-dir", str(fork), "branch").stdout)
        self.assertEqual(sh(self.tmp, "--git-dir", str(self.public), "branch").stdout, "")

    def test_secrets_and_personal_are_refused(self):
        leak = self.sop("net.fetch", "fetch a url - generic http utility",
                        "TOKEN = 'ghp_abcdefghijklmnopqrstuvwxyz0123456789'\n", ["http", "generic"])
        with self.assertRaisesRegex(RegistryError, "secrets"):
            self.reg().propose(leak, override_personal=True)       # no override for secrets
        term = self.sop("net.ping", "ping a host - generic network utility",
                        "print('analytics.acme.internal')\n", ["network", "generic"])
        with self.assertRaisesRegex(RegistryError, "private term"):
            self.reg().propose(term, override_personal=True)       # nor for the org's private terms
        per = self.sop("team.report", "Build our team's weekly company report for the internal account",
                       "print('report')", ["company", "team", "internal", "account"])
        with self.assertRaisesRegex(RegistryError, "personal"):
            self.reg().propose(per)
        self.reg().propose(per, override_personal=True)             # explicit override; scans still ran
        cand = self.sop("x.draft", "generic util", GENERIC, ["generic"], status="candidate")
        with self.assertRaisesRegex(RegistryError, "tests pass"):
            self.reg().propose(cand)

    def test_pre_push_hook_blocks_secrets(self):
        gen = self.sop("text.upper", "Convert text to upper case: a generic reusable text utility",
                       GENERIC, ["text", "convert", "format", "utility", "generic"])
        reg = self.reg()
        reg.propose(gen)
        clone = self.tmp / "home" / "registry" / "public"
        (clone / "oops.env").write_text("AWS_KEY=AKIAABCDEFGHIJKLMNOP\n")
        sh(clone, "add", "-A")
        sh(clone, "commit", "-qm", "oops")
        p = sh(clone, "push", "origin", "HEAD")
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("BLOCKED", p.stderr)

    def test_existing_fixture_secrets_in_the_target_repo_do_not_block_a_proposal(self):
        seed = self.tmp / "seed"
        subprocess.run(["git", "clone", "-q", str(self.public), str(seed)], check=True, capture_output=True)
        (seed / "tests").mkdir()
        (seed / "tests" / "test_ci.py").write_text('TOKEN = "ghp_abcdefghijklmnopqrstuvwxyz123456"\n')
        sh(seed, "add", "-A")
        sh(seed, "commit", "-qm", "CI tests with a fake token")
        sh(seed, "push", "-q", "origin", "HEAD:main")
        gen = self.sop("text.upper", "Convert text to upper case: a generic reusable text utility",
                       GENERIC, ["text", "convert", "format", "utility", "generic"])
        self.assertIn(self.reg().propose(gen)["status"], ("proposed", "pushed_local"))

    def test_a_proposal_is_placed_in_the_registry_tree_not_the_private_one(self):
        seed = self.tmp / "seed"
        subprocess.run(["git", "clone", "-q", str(self.public), str(seed)], check=True, capture_output=True)
        for cid, desc in (("text", "Text transformations"), ("text.case", "upper lower title case conversion of text")):
            d = seed / "sops" / Path(*cid.split("."))
            d.mkdir(parents=True)
            (d / "_node.json").write_text(json.dumps({"description": desc}))
        lower = seed / "sops" / "text" / "case" / "lower"
        lower.mkdir()
        (lower / "sop.json").write_text(json.dumps({"id": "text.case.lower", "description": "lower case text"}))
        (lower / "run.py").write_text("print('{}')\n")
        sh(seed, "add", "-A")
        sh(seed, "commit", "-qm", "a registry with a case subcategory")
        sh(seed, "push", "-q", "origin", "HEAD:main")
        gen = self.sop("text.upper", "Convert text to upper case: a generic reusable text utility",
                       GENERIC, ["text", "convert", "format", "utility", "generic"])
        with patch("rameness.tree.place", return_value="text.case"):      # JEV's walk (tested in test_tree)
            entry = self.reg().propose(gen)
        self.assertEqual(entry["registry_id"], "text.case.upper")
        pushed = sh(self.public, "show", f"{entry['head']}:sops/text/case/upper/sop.json")
        self.assertEqual(json.loads(pushed.stdout)["id"], "text.case.upper")

    def seed_crowded_registry(self, n=8):
        seed = self.tmp / "seed"
        subprocess.run(["git", "clone", "-q", str(self.public), str(seed)], check=True, capture_output=True)
        (seed / "sops" / "text").mkdir(parents=True)
        (seed / "sops" / "text" / "_node.json").write_text(json.dumps({"description": "Text transformations"}))
        names = [f"case_{i}" for i in range(4)] + [f"slug_{i}" for i in range(n - 4)]
        for name in names:
            d = seed / "sops" / "text" / name
            d.mkdir()
            (d / "sop.json").write_text(json.dumps({"id": f"text.{name}", "description": name.replace("_", " ")}))
            (d / "run.py").write_text("print('{}')\n")
        sh(seed, "add", "-A")
        sh(seed, "commit", "-qm", "a full text category")
        sh(seed, "push", "-q", "origin", "HEAD:main")
        return names

    def test_a_proposal_that_crowds_a_category_carries_the_reorganization(self):
        names = self.seed_crowded_registry()
        gen = self.sop("text.upper", "Convert text to upper case: a generic reusable text utility",
                       GENERIC, ["text", "convert", "format", "utility", "generic"])
        groups = {"groups": [
            {"name": "case", "description": "change letter case",
             "members": [f"text.{n}" for n in names if n.startswith("case")] + ["text.upper"]},
            {"name": "slug", "description": "make url slugs", "members": [f"text.{n}" for n in names if n.startswith("slug")]}]}
        reg = Registry({"registry": {"public": str(self.public), "min_tokens_saved": 0}}, self.tmp / "home", self.org,
                       self.jev, llm=Mock(complete_json=Mock(return_value=groups)))
        with patch("rameness.tree.place", return_value="text"), \
                patch.object(self.jev, "yes", return_value=0.9):
            entry = reg.propose(gen)
        self.assertEqual(entry["registry_id"], "text.case.upper")          # it moved with the split
        self.assertTrue(entry["reorganized"])
        files = sh(self.public, "ls-tree", "-r", "--name-only", entry["head"]).stdout.split()
        self.assertIn("sops/text/case/upper/sop.json", files)            # the new SOP...
        self.assertIn("sops/text/slug/slug_0/sop.json", files)            # ...and the reorganization, one PR
        self.assertIn("sops/_aliases.json", files)
        self.assertNotIn("sops/text/case_0/sop.json", files)

    def test_a_crowded_category_without_a_grouping_is_not_proposed(self):
        self.seed_crowded_registry()
        gen = self.sop("text.upper", "Convert text to upper case: a generic reusable text utility",
                       GENERIC, ["text", "convert", "format", "utility", "generic"])
        reg = Registry({"registry": {"public": str(self.public), "min_tokens_saved": 0}}, self.tmp / "home", self.org,
                       self.jev, llm=Mock(complete_json=Mock(return_value={"groups": []})))
        with patch("rameness.tree.place", return_value="text"), self.assertRaisesRegex(RegistryError, "no grouping"):
            reg.propose(gen)

    def validated(self):
        from rameness.learning import register_sop
        lib = Library([(self.root, "private")])
        executor = Executor(lib, ["compute"], cwd=self.proj)
        sop, failures = register_sop(lib, executor, {
            "id": "text.upper", "description": "Convert text to upper case: a generic reusable text utility",
            "script": GENERIC, "permissions": ["compute"], "keywords": ["text", "utility", "generic"],
            "inputs": {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]},
            "tests": [{"input": {"text": "ab"}, "expect": {"upper": "AB"}},
                      {"input": {"text": ""}, "expect": {"upper": ""}}]},
            root=self.root, jev=self.jev, org=self.org)
        self.assertEqual(failures, [])
        self.assertEqual(sop.visibility, "shareable")
        return lib, executor, sop

    def test_auto_pipeline_validates_pushes_and_deduplicates(self):
        lib, executor, sop = self.validated()
        reg = self.reg()
        with patch.object(reg, "_gh_pr", return_value="https://github.com/example/sops/pull/2") as pr:
            rows = reg.auto_propose(lib, executor)
            self.assertEqual(rows[0]["status"], "proposed", rows)
            self.assertEqual(reg.auto_propose(lib, executor), [])
            self.assertEqual(reg.propose(sop)["pr"], rows[0]["pr"])
            self.assertEqual(pr.call_count, 1)
        self.assertEqual(len(reg.proposals()), 1)
        self.assertIn(reg.proposals()[0]["branch"], sh(self.tmp, "--git-dir", str(self.public), "branch").stdout)

    def test_pr_failure_retries_same_pushed_branch_after_restart(self):
        lib, executor, sop = self.validated()
        reg = self.reg()
        with patch.object(reg, "_gh_pr", side_effect=RegistryError("GitHub unavailable")):
            rows = reg.auto_propose(lib, executor)
        self.assertEqual(rows[0]["status"], "failed")
        self.assertIn("GitHub unavailable", rows[0]["error"])
        pending = reg.proposals()[0]
        self.assertEqual(pending["status"], "pushed")
        restarted = self.reg()
        with patch.object(restarted, "_gh_pr", return_value="https://github.com/example/sops/pull/2"), \
                patch.object(restarted, "_push", side_effect=AssertionError("must not push again")):
            self.assertEqual(restarted.auto_propose(lib, executor)[0]["status"], "proposed")
        self.assertEqual(restarted.proposals()[0]["branch"], pending["branch"])
        self.assertEqual(len(restarted.proposals()), 1)

    def test_auto_pipeline_skips_private_and_candidates_and_rechecks_tests(self):
        lib, executor, sop = self.validated()
        reg = self.reg()
        with patch.object(reg, "propose") as propose:
            sop.visibility = "private"
            self.assertEqual(reg.auto_propose(lib, executor), [])
            sop.visibility, sop.status = "shareable", "candidate"
            self.assertEqual(reg.auto_propose(lib, executor), [])
            sop.status = "validated"
            (sop.path / "run.py").write_text("raise RuntimeError('broken')\n")
            rows = reg.auto_propose(lib, executor)
            self.assertEqual(rows[0]["status"], "failed")
            self.assertIn("tests failed", rows[0]["error"])
            propose.assert_not_called()

    def test_auto_pipeline_rechecks_privacy_before_push(self):
        lib, executor, sop = self.validated()
        with (sop.path / "run.py").open("a") as f:
            f.write("\n# analytics.acme.internal\n")
        reg = self.reg()
        with patch.object(reg, "_push") as push:
            rows = reg.auto_propose(lib, executor)
        self.assertEqual(rows[0]["status"], "failed")
        self.assertIn("private terms", rows[0]["error"])
        push.assert_not_called()

    def test_every_kind_of_test_ships_with_the_proposal_and_meets_ramensops_rules(self):
        from rameness.registry import publishable_tests
        lib, executor, sop = self.validated()
        sop.tests += [{"input": {"text": "x"}, "expect_keys": ["upper"]},
                      {"input": {"text": "y"}, "files": {"in.txt": "y"}, "expect": {"upper": "Y"}}]
        self.assertEqual(publishable_tests(sop), [])
        sop.tests = [sop.tests[0], dict(sop.tests[0])]                # the same case twice
        self.assertIn("tests must exercise distinct inputs", publishable_tests(sop))
        sop.tests = [{"input": {"text": "a"}, "expect": {"upper": "A"}},
                     {"input": {"text": "b"}, "files": {"../x": ""}, "expect": {"upper": "B"}}]
        self.assertIn("test 1: fixture files must use relative paths", publishable_tests(sop))

    def test_missing_tests_cannot_auto_publish(self):
        lib, executor, sop = self.validated()
        sop.tests = []
        rows = self.reg().auto_propose(lib, executor)
        self.assertEqual(rows[0]["status"], "failed")
        self.assertIn("no tests", rows[0]["error"])

    def test_shape_only_and_error_only_tests_cannot_auto_publish(self):
        lib, executor, sop = self.validated()
        for tests in ([{"input": {"text": "ab"}, "expect_keys": ["upper"]}],
                      [{"input": {}, "expect_error": True}]):
            with self.subTest(tests=tests):
                sop.tests = tests
                reg = self.reg()
                with patch.object(reg, "propose") as propose:
                    rows = reg.auto_propose(lib, executor)
                self.assertEqual(rows[0]["status"], "failed")
                self.assertIn("expected output values", rows[0]["error"])
                propose.assert_not_called()

    def test_content_changes_make_new_proposals_bookkeeping_does_not(self):
        lib, executor, sop = self.validated()
        before = fingerprint(sop)
        sop.origin = {"task": "a different internal task"}
        sop.classified = {"reason": "new classification"}
        sop.save()
        self.assertEqual(fingerprint(sop), before)
        reg = self.reg()
        reg.auto_propose(lib, executor)
        with (sop.path / "run.py").open("a") as f:
            f.write("\n# Generic utility documentation\n")
        self.assertNotEqual(fingerprint(sop), before)
        reg.auto_propose(lib, executor)
        self.assertEqual(len(reg.proposals()), 2)

    def test_gh_create_error_is_not_reported_as_compare_link(self):
        reg = self.reg()
        with patch("rameness.registry.shutil.which", return_value="/usr/bin/gh"), \
                patch("rameness.registry.subprocess.run", side_effect=[
                    Mock(returncode=1, stdout="", stderr="not authenticated"),
                    Mock(returncode=0, stdout="[]", stderr="")]):
            with self.assertRaisesRegex(RegistryError, "not authenticated"):
                reg._gh_pr("https://github.com/example/sops.git", "branch", "main", "title", "body")

    def test_gh_create_recovers_existing_pr(self):
        reg = self.reg()
        with patch("rameness.registry.shutil.which", return_value="/usr/bin/gh"), \
                patch("rameness.registry.subprocess.run", side_effect=[
                    Mock(returncode=1, stdout="", stderr="already exists"),
                    Mock(returncode=0, stdout='[{"url":"https://github.com/example/sops/pull/2"}]', stderr="")]):
            self.assertEqual(reg._gh_pr("https://github.com/example/sops.git", "branch", "main", "title", "body"),
                             "https://github.com/example/sops/pull/2")


if __name__ == "__main__":
    unittest.main()
