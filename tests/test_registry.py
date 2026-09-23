import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from rameness import publish, registry
from rameness.jev import Jev, LexicalJev
from rameness.org import Org
from rameness.registry import Registry, RegistryError
from rameness.sops import Library

GENERIC = ("import json, sys\na = json.load(sys.stdin)\n"
           "print(json.dumps({'upper': a['text'].upper()}))\n")


def sh(cwd, *args):
    return subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", *args], cwd=cwd,
                          capture_output=True, text=True)


class RegistryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        os.environ["RAMENESS_HOME"] = str(self.tmp / "home")
        self.staging = self.tmp / "staging.git"
        self.public = self.tmp / "public.git"
        for r in (self.staging, self.public):
            subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(r)], check=True)
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
        cfg = {"registry": {"staging": str(self.staging), "public": str(self.public), "release": "pr", **over}}
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

    def test_propose_goes_only_to_staging_sanitized(self):
        gen = self.sop("text.upper", "Convert text to upper case: a generic reusable text utility",
                       GENERIC, ["text", "convert", "format", "utility", "generic"])
        r = self.reg().propose(gen)
        self.assertTrue(r["branch"].startswith("sop/text.upper-"))
        self.assertIn(r["branch"], sh(self.tmp, "--git-dir", str(self.staging), "branch").stdout)
        self.assertEqual(sh(self.tmp, "--git-dir", str(self.public), "branch").stdout, "")   # public untouched
        blob = sh(self.tmp, "--git-dir", str(self.staging), "show", f"{r['branch']}:sops/text/upper/sop.json").stdout
        d = json.loads(blob)
        self.assertNotIn("origin", d)                                # the task that produced it stays private
        self.assertNotIn("classified", d)
        self.assertEqual(d["scope"], "public")

    def test_secrets_and_personal_are_refused(self):
        leak = self.sop("net.fetch", "fetch a url - generic http utility",
                        "TOKEN = 'ghp_abcdefghijklmnopqrstuvwxyz0123456789'\n", ["http", "generic"])
        with self.assertRaisesRegex(RegistryError, "secrets"):
            self.reg().propose(leak, override_personal=True)       # no override for secrets
        per = self.sop("team.report", "Build our team's weekly company report for the internal account",
                       "print('report')", ["company", "team", "internal", "account"])
        with self.assertRaisesRegex(RegistryError, "personal"):
            self.reg().propose(per)
        self.reg().propose(per, override_personal=True)             # explicit override; scans still ran
        cand = self.sop("x.draft", "generic util", GENERIC, ["generic"], status="candidate")
        with self.assertRaisesRegex(RegistryError, "tests pass"):
            self.reg().propose(cand)

    def test_public_staging_repo_is_refused(self):
        gen = self.sop("text.upper", "Convert text to upper case: a generic reusable text utility",
                       GENERIC, ["text", "convert", "format", "utility", "generic"])
        with mock.patch.object(registry, "visibility", return_value=(False, "gh: public")):
            with self.assertRaisesRegex(RegistryError, "not verifiably private"):
                self.reg().propose(gen)

    def test_pre_push_hook_blocks_secrets(self):
        gen = self.sop("text.upper", "Convert text to upper case: a generic reusable text utility",
                       GENERIC, ["text", "convert", "format", "utility", "generic"])
        reg = self.reg()
        reg.propose(gen)
        clone = self.tmp / "home" / "registry" / "staging"
        (clone / "oops.env").write_text("AWS_KEY=AKIAABCDEFGHIJKLMNOP\n")
        sh(clone, "add", "-A")
        sh(clone, "commit", "-qm", "oops")
        p = sh(clone, "push", "origin", "HEAD")
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("BLOCKED", p.stderr)

    def test_release_after_merge_publishes_via_pr_branch(self):
        gen = self.sop("text.upper", "Convert text to upper case: a generic reusable text utility",
                       GENERIC, ["text", "convert", "format", "utility", "generic"])
        reg = self.reg()
        self.assertEqual(reg.release()["released"], [])            # nothing merged yet
        r = reg.propose(gen)
        rev = self.tmp / "reviewer"                                 # a reviewer merges the PR in staging
        subprocess.run(["git", "clone", "-q", str(self.staging), str(rev)], check=True)
        sh(rev, "checkout", "-q", "-b", "main", f"origin/{r['branch']}")
        self.assertEqual(sh(rev, "push", "-q", "origin", "main").returncode, 0)
        out = reg.release()
        self.assertEqual(out["released"], ["text.upper"])
        self.assertTrue(out["branch"].startswith("release/"))
        files = sh(self.tmp, "--git-dir", str(self.public), "ls-tree", "-r", "--name-only", out["branch"]).stdout
        self.assertIn("sops/text/upper/run.py", files)
        self.assertIn("sops/index.json", files)
        self.assertEqual(reg.release()["released"], [])             # idempotent


class SeedTest(unittest.TestCase):
    def test_seed_empty_public_registry_then_install(self):
        from rameness.publish import install
        from rameness.sops import BUILTIN_ROOT, Executor
        tmp = Path(tempfile.mkdtemp())
        home = tmp / "home"
        pub = tmp / "public.git"
        subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(pub)], check=True)
        out = registry.seed_public(str(pub), home, Org(), [BUILTIN_ROOT])
        self.assertTrue(out["pushed"], out)
        self.assertIn("http.get", out["sops"])
        main = sh(tmp, "--git-dir", str(pub), "ls-tree", "-r", "--name-only", "main").stdout
        self.assertIn("README.md", main)
        self.assertNotIn("sops/", main)                              # SOPs arrive via the review branch
        seed = sh(tmp, "--git-dir", str(pub), "ls-tree", "-r", "--name-only", out["branch"]).stdout
        for f in ("sops/index.json", "sops/http/_node.json", "sops/http/get/run.py", ".github/workflows/secret-scan.yml"):
            self.assertIn(f, seed)
        # a user installs the registry (after the seed PR merges) and gets the same ids
        merged = tmp / "merged"
        subprocess.run(["git", "clone", "-q", "-b", out["branch"], str(pub), str(merged)], check=True)
        install(str(merged), home, "RamenSOPs")
        proj = tmp / "proj"
        proj.mkdir()
        lib = Library([(p, sc) for p, sc in Library.default(proj, home).roots if "RamenSOPs" in str(p)])
        self.assertIn("http.get", lib.sops)
        self.assertEqual(lib.root.children["http"].description, Library([(BUILTIN_ROOT, "public")]).root.children["http"].description)
        self.assertEqual(Executor(lib, [], cwd=proj).test("data.json_extract"), [])


if __name__ == "__main__":
    unittest.main()
