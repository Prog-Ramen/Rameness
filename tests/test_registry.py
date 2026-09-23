import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from rameness import publish, registry
import re

from rameness.jev import Backend, CascadeJev, Jev, LexicalJev
from rameness.publish import SHARE_Q
from rameness.org import Org
from rameness.registry import Registry, RegistryError
from rameness.sops import Library

class Judge(Backend):
    """Stands in for a model-backed JEV: flags details with numbers or org words, and calls an SOP
    shareable only when it reads as generic and no specific details were found."""
    name = "judge"

    def activate(self, question, query, options, context=""):
        return [0.9 if re.search(r"\d|internal|team|company", o.text) else 0.1 for o in options]

    def choose(self, question, query, options, context=""):
        if question == SHARE_Q:
            if "generic" not in query.lower():
                return [0.05, 0.15, 0.8]                      # private
            return [0.9, 0.05, 0.05] if "details found: none" in query else [0.05, 0.8, 0.15]   # or generalize
        return LexicalJev().choose(question, query, options, context)


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
        self.jev = Jev(CascadeJev(LexicalJev(), Judge()))

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
        rule = self.sop("pricing.discount", "Apply the 18% partner discount - a generic pricing helper",
                        "DISC = 0.18\n", ["pricing", "generic"])
        self.assertEqual(publish.classify(self.jev, gen, self.org)["visibility"], "shareable")
        r = publish.classify(self.jev, per, self.org)
        self.assertEqual(r["visibility"], "private")
        self.assertIn("scrubber", r["reason"])                      # private term: no model needed
        self.assertEqual(json.loads((per.path / "sop.json").read_text())["visibility"], "private")
        r = publish.classify(self.jev, rule, self.org)                # business constant: JEV says generalize
        self.assertEqual(r["visibility"], "private")
        self.assertIn("18%", r["specific"])
        self.assertIn("generalize", r["reason"])

    def test_keyword_only_jev_cannot_mark_shareable(self):
        gen = self.sop("text.upper", "Convert text to upper case: a generic reusable text utility",
                       GENERIC, ["text", "convert", "format", "utility", "generic"])
        r = publish.classify(Jev(LexicalJev()), gen, self.org)
        self.assertTrue(r["unclassified"])
        self.assertEqual(r["visibility"], "private")
        reg = self.reg()
        reg.jev = Jev(LexicalJev())
        with self.assertRaisesRegex(RegistryError, "could not be classified"):
            reg.propose(gen)

    def test_propose_goes_only_to_staging_sanitized(self):
        gen = self.sop("text.upper", "Convert text to upper case: a generic reusable text utility",
                       GENERIC, ["text", "convert", "format", "utility", "generic"])
        r = self.reg().propose(gen)
        self.assertRegex(r["branch"], r"^sop/[\w.-]+/text\.upper-")        # sop/<contributor>/<id>-<time>
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
        with self.assertRaisesRegex(RegistryError, "not shareable"):
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


if __name__ == "__main__":
    unittest.main()
