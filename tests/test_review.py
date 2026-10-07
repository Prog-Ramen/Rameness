import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from rameness.review import markdown, review, used_permissions

UPPER = ("import json, sys\na = json.load(sys.stdin)\n"
         "print(json.dumps({'upper': a['text'].upper()}))\n")
TESTS = [{"input": {"text": "ab"}, "expect": {"upper": "AB"}}]


def sh(cwd, *args):
    return subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", *args], cwd=cwd,
                          capture_output=True, text=True, check=True).stdout


class ReviewTest(unittest.TestCase):
    def setUp(self):
        self.repo = Path(tempfile.mkdtemp())
        sh(self.repo, "init", "-q", "-b", "main")
        self.sop("text.lower", UPPER.replace("upper", "lower"), [], [{"input": {"text": "AB"}, "expect": {"lower": "ab"}}])
        sh(self.repo, "add", "-A")
        sh(self.repo, "commit", "-qm", "base")
        sh(self.repo, "checkout", "-q", "-b", "pr")

    def sop(self, sid, script, permissions, tests=TESTS, kind="script", **extra):
        d = self.repo / "sops" / Path(*sid.split("."))
        d.mkdir(parents=True, exist_ok=True)
        (d / "sop.json").write_text(json.dumps({"id": sid, "description": f"{sid} utility", "kind": kind,
                                                "permissions": permissions, "tests": tests, **extra}))
        (d / "run.py").write_text(script)
        return d

    def check(self, **kw):
        sh(self.repo, "add", "-A")
        sh(self.repo, "commit", "-qm", "pr")
        return review(self.repo, base="main", **kw)

    def test_new_low_risk_sop_is_auto_merge_eligible(self):
        self.sop("text.upper", UPPER, ["compute"])
        r = self.check()
        self.assertTrue(r["ok"], r)
        self.assertTrue(r["auto_merge"], r)
        self.assertEqual(r["sops"][0]["tests"], "pass")
        self.assertIn("eligible for auto-merge", markdown(r))

    def test_failing_tests_fail_and_static_skips_them(self):
        self.sop("text.upper", UPPER, ["compute"], tests=[{"input": {"text": "ab"}, "expect": {"upper": "nope"}}])
        self.assertFalse(self.check()["ok"])
        r = review(self.repo, base="main", static=True)
        self.assertTrue(r["ok"])
        self.assertIsNone(r["sops"][0]["tests"])

    def test_undeclared_permissions_fail(self):
        self.sop("net.leak", "import urllib.request\nurllib.request.urlopen('https://x.example')\n" + UPPER, ["compute"])
        r = self.check(static=True)
        self.assertFalse(r["ok"])
        self.assertTrue(any("does not declare" in f and "network" in f for f in r["fail"]), r["fail"])

    def test_powerful_skill_and_changed_sops_need_a_human(self):
        self.sop("net.get", "import urllib.request\n" + UPPER, ["network"])
        self.sop("text.lower", UPPER.replace("upper", "lower") + "# tweak\n", [],
                 [{"input": {"text": "AB"}, "expect": {"lower": "ab"}}])
        self.sop("howto.deploy", "", [], tests=[], kind="skill", instructions="Deploy the app.")
        r = self.check(static=True)
        self.assertTrue(r["ok"], r["fail"])
        self.assertFalse(r["auto_merge"])
        human = " ".join(r["human"])
        self.assertIn("needs ['network']", human)
        self.assertIn("changes an existing SOP", human)
        self.assertIn("skill SOP", human)

    def test_scope_generated_index_and_secrets_fail(self):
        self.sop("net.fetch", UPPER + "TOKEN = 'ghp_abcdefghijklmnopqrstuvwxyz0123456789'\n", ["compute"])
        (self.repo / "sops" / "index.json").write_text("{}")
        wf = self.repo / ".github" / "workflows"
        wf.mkdir(parents=True)
        (wf / "x.yml").write_text("on: push\n")
        fail = " ".join(self.check(static=True)["fail"])
        self.assertIn("outside sops/", fail)
        self.assertIn("generated index", fail)
        self.assertIn("key-like", fail)

    def test_permission_detection(self):
        d = self.sop("fs.save", "from pathlib import Path\nPath('x').write_text('y')  # a > b\nif 2 > 1: pass\n", [])
        self.assertEqual(used_permissions(d), {"fs:write"})
        (d / "run.py").write_text("import json\nprint(1 > 0)\n# import requests\n")
        self.assertEqual(used_permissions(d), set())
        (d / "run.sh").write_text("#!/bin/sh\ncurl -s https://x.example > out.txt 2>/dev/null\n")
        self.assertEqual(used_permissions(d), {"exec", "network", "fs:write"})


if __name__ == "__main__":
    unittest.main()
