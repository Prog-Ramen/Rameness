"""End-to-end: SOP sharing through the real CLI - classify, propose (private intake), secret
refusal, pre-push hook, release, encrypted relay, lazy remote pulls, scaffolding."""

import json
import subprocess
import sys

from .helpers import E2E, git

UPPER = "import json, sys\na = json.load(sys.stdin)\nprint(json.dumps({'upper': a['text'].upper()}))\n"


class RegistryE2E(E2E):
    def setUp(self):
        super().setUp()
        self.intake = self.bare("intake")
        self.public = self.bare("public")
        self.cli("init")
        self.config({"jev": {"backend": "lexical"},
                     "registry": {"staging": str(self.intake), "public": str(self.public), "release": "pr",
                                  "remote_index": None}})

    def sop(self, sid, desc, script, keywords, tests=None):
        d = self.proj / ".rameness" / "sops"
        for part in sid.split("."):
            d = d / part
        d.mkdir(parents=True)
        (d / "sop.json").write_text(json.dumps({
            "id": sid, "description": desc, "keywords": keywords, "status": "validated", "permissions": ["compute"],
            "inputs": {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]},
            "tests": tests if tests is not None else [{"input": {"text": "a"}, "expect": {"upper": "A"}}]}))
        (d / "run.py").write_text(script)


class TestProposeRelease(RegistryE2E):
    def test_classify_propose_signoff_release(self):
        self.sop("text.upper", "Convert text to upper case", UPPER, ["text", "upper"])
        out = self.cli("sop", "classify", "text.upper").stdout
        self.assertIn("ambiguous", out)                               # keyword JEV never says shareable
        p = self.cli("sop", "propose", "text.upper", check=False)
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("needs your sign-off", p.stderr)
        p = self.cli("sop", "propose", "text.upper", "--sign-off")
        self.assertIn("proposed text.upper to the private staging repo", p.stdout)
        self.assertIn("sop/", p.stdout)
        branches = git(self.tmp, "--git-dir", str(self.intake), "branch").stdout
        self.assertIn("sop/", branches)
        self.assertEqual(git(self.tmp, "--git-dir", str(self.public), "branch").stdout, "")   # nothing public
        # a reviewer merges in the intake repo
        rev = self.tmp / "reviewer"
        subprocess.run(["git", "clone", "-q", str(self.intake), str(rev)], check=True)
        br = branches.strip().split()[-1]
        git(rev, "checkout", "-q", "-b", "main", f"origin/{br}")
        git(rev, "push", "-q", "origin", "main")
        # the public registry already has a main branch (like RamenSOPs): releases arrive as PRs
        seed = self.tmp / "public-seed"
        subprocess.run(["git", "clone", "-q", str(self.public), str(seed)], check=True, capture_output=True)
        (seed / "README.md").write_text("# registry\n")
        git(seed, "add", "-A")
        git(seed, "commit", "-qm", "readme")
        git(seed, "push", "-q", "origin", "HEAD:main")
        rel = json.loads(self.cli("sop", "release").stdout)
        self.assertEqual(rel["released"], ["text.upper"])
        files = git(self.tmp, "--git-dir", str(self.public), "ls-tree", "-r", "--name-only", rel["branch"]).stdout
        self.assertIn("sops/text/upper/run.py", files)
        self.assertIn("sops/text/_index.json", files)                 # sharded index
        self.assertTrue(rel["branch"].startswith("release/"))
        self.assertIn("pending review", self.cli("sop", "release").stdout)       # no duplicate release PR

    def test_first_release_into_an_empty_registry_becomes_main(self):
        self.sop("text.upper", "Convert text to upper case", UPPER, ["text", "upper"])
        branch = self.cli("sop", "propose", "text.upper", "--sign-off").stdout.split("branch ")[1].split()[0]
        rev = self.tmp / "reviewer"
        subprocess.run(["git", "clone", "-q", str(self.intake), str(rev)], check=True, capture_output=True)
        git(rev, "checkout", "-q", "-b", "main", f"origin/{branch}")
        git(rev, "push", "-q", "origin", "main")
        rel = json.loads(self.cli("sop", "release").stdout)
        self.assertEqual((rel["branch"], rel["released"]), ("main", ["text.upper"]))
        self.assertIn("first release", rel["note"])
        self.assertIn("up to date", self.cli("sop", "release").stdout)

    def test_secrets_are_refused_and_pushes_blocked(self):
        self.sop("net.leak", "Fetch data", "TOKEN = 'ghp_abcdefghijklmnopqrstuvwxyz0123456789'\n", ["http"])
        p = self.cli("sop", "propose", "net.leak", "--sign-off", "--override-personal", check=False)
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("secrets found", p.stderr)
        # scrub-tree is also the pre-push hook: a manual push of a secret from a managed clone fails
        self.sop("text.upper", "Convert text to upper case", UPPER, ["text"])
        self.cli("sop", "propose", "text.upper", "--sign-off")
        clone = self.home / "registry" / "staging"
        (clone / "leak.env").write_text("AWS_KEY=AKIAABCDEFGHIJKLMNOP\n")
        git(clone, "add", "-A")
        git(clone, "commit", "-qm", "oops")
        push = git(clone, "push", "origin", "HEAD", check=False)
        self.assertNotEqual(push.returncode, 0)
        self.assertIn("BLOCKED", push.stderr)


class TestRelay(RegistryE2E):
    def test_submit_seals_and_relay_forwards_privately(self):
        keys = self.tmp / "public-checkout"
        keys.mkdir()
        out = self.cli("sop", "relay-init", str(keys)).stdout
        self.assertIn("INTAKE_PRIVATE_KEY", out)
        priv = out.split("Add ")[1].split(" as the")[0]
        self.assertTrue((keys / ".github" / "workflows" / "intake-relay.yml").exists())
        cfg = json.loads((self.proj / ".rameness" / "config.json").read_text())
        cfg["registry"]["relay_key"] = str(keys / ".github" / "intake_public_key.pem")
        self.config(cfg)
        self.sop("text.upper", "Convert text to upper case", UPPER, ["text"])
        r = self.cli("sop", "submit", "text.upper", "--sign-off").stdout
        self.assertIn("encrypted relay", r)                           # gh disabled: the body is saved to a file
        body_file = next(self.home.rglob("SOP-submission-*.txt"))
        body = body_file.read_text()
        self.assertTrue(body.startswith("RAMENESS-SOP-SUBMISSION v1"))
        self.assertNotIn("upper", body.lower())                       # nothing readable in the public issue
        code = (f"import sys; from pathlib import Path; from rameness.relay import receive;"
                f"print(receive(Path({str(body_file)!r}).read_text(), 'outsider', 'RamenSOPs#3', "
                f"Path({priv!r}).read_bytes(), {str(self.intake)!r}, Path({str(self.tmp / 'relay')!r}))['branch'])")
        branch = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=self.env,
                                check=True).stdout.strip()
        self.assertTrue(branch.startswith("sop/outsider/text.upper-"))
        files = git(self.tmp, "--git-dir", str(self.intake), "ls-tree", "-r", "--name-only", branch).stdout
        self.assertIn("sops/text/upper/run.py", files)

    def test_scaffold_intake_repo(self):
        d = self.tmp / "intake-checkout"
        self.cli("sop", "registry-init", str(d))
        for f in (".github/CODEOWNERS", ".github/pull_request_template.md", ".github/workflows/secret-scan.yml",
                  ".github/workflows/release-to-public.yml", "README.md", "sops/index.json"):
            self.assertTrue((d / f).exists(), f)
        self.assertIn("@Prog-Ramen/sop-reviewers", (d / ".github/CODEOWNERS").read_text())


class TestRemotePull(RegistryE2E):
    def test_plan_pulls_only_what_the_task_needs(self):
        reg = self.tmp / "registry" / "sops"
        code = f"""
import json
from pathlib import Path
from rameness.publish import build_index
reg = Path({str(reg)!r})
def sop(sid, desc, kw, node, expect="hello-world"):
    d = reg.joinpath(*sid.split(".")); d.mkdir(parents=True)
    (d / "sop.json").write_text(json.dumps({{"id": sid, "description": desc, "keywords": kw, "permissions": ["compute"],
        "inputs": {{"type": "object", "properties": {{"text": {{"type": "string"}}}}, "required": ["text"]}},
        "tests": [{{"input": {{"text": "Hello World"}}, "expect": {{"slug": expect}}}}]}}))
    (d / "run.py").write_text("import json,re,sys\\na=json.load(sys.stdin)\\nprint(json.dumps({{'slug': re.sub('[^a-z0-9]+','-',a['text'].lower()).strip('-')}}))\\n")
    (d.parent / "_node.json").write_text(json.dumps(node))
sop("text.slugify", "Convert a title into a URL slug", ["slug", "slugify", "url", "title"],
    {{"description": "Text transformations", "keywords": ["text"]}})
sop("media.thumbnail", "Resize an image into a thumbnail", ["image", "resize"],
    {{"description": "Images and video", "keywords": ["image"]}}, expect="a-broken-expectation")
build_index(reg)
"""
        subprocess.run([sys.executable, "-c", code], env=self.env, check=True)
        cfg = json.loads((self.proj / ".rameness" / "config.json").read_text())
        cfg["registry"]["remote_index"] = (reg / "index.json").as_uri()
        self.config(cfg)
        plan = self.cli("plan", "slugify this blog post title into a url slug").stdout
        self.assertIn("pulled", plan)
        self.assertIn("text.slugify", plan)
        installed = sorted(p.parent.name for p in (self.home / "public").rglob("sop.json"))
        self.assertEqual(installed, ["slugify"])                          # nothing else mirrored
        self.assertIn("text.slugify", self.cli("sop", "list").stdout)
        pulled = self.cli("sop", "pull", "media.thumbnail", check=False)
        self.assertNotEqual(pulled.returncode, 0)                          # its test fails -> removed again
        self.assertIn("tests failed", pulled.stderr)
