import io
import json
import os
import subprocess
import tarfile
import tempfile
import unittest
from pathlib import Path

from rameness import relay
from rameness.jev import CascadeJev, Jev, LexicalJev
from rameness.org import Org
from rameness.registry import Registry, RegistryError
from rameness.sops import Library
from tests.test_registry import GENERIC, Judge, sh


class RelayTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        os.environ["RAMENESS_HOME"] = str(self.tmp / "home")
        self.pub, self.priv = relay.keygen(self.tmp / "keys")
        self.intake = self.tmp / "intake.git"
        subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(self.intake)], check=True)
        self.root = self.tmp / "proj" / ".rameness" / "sops"

    def sop(self, sid, desc, script, keywords):
        d = self.root.joinpath(*sid.split("."))
        d.mkdir(parents=True)
        (d / "sop.json").write_text(json.dumps({"id": sid, "description": desc, "keywords": keywords,
                                                "status": "validated", "origin": {"task": "ticket 42"}}))
        (d / "run.py").write_text(script)
        return Library([(self.root, "private")]).get(sid)

    def reg(self):
        cfg = {"registry": {"staging": None, "relay_repo": "Prog-Ramen/RamenSOPs", "relay_key": str(self.pub)}}
        return Registry(cfg, self.tmp / "home", Org(private_terms=["acme.internal"]), Jev(CascadeJev(LexicalJev(), Judge())))

    def test_seal_roundtrip_and_wrong_key(self):
        text = relay.seal(self.pub.read_bytes(), b"payload")
        self.assertEqual(relay.unseal(self.priv.read_bytes(), text), b"payload")
        _, other = relay.keygen(self.tmp / "other")
        with self.assertRaises(Exception):
            relay.unseal(other.read_bytes(), text)
        tampered = text[:-10] + ("A" if text[-10] != "A" else "B") + text[-9:]
        with self.assertRaises(Exception):
            relay.unseal(self.priv.read_bytes(), tampered)

    def test_unpack_rejects_path_traversal(self):
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            data = b"x"
            info = tarfile.TarInfo("../../etc/evil")
            info.size = 1
            tar.addfile(info, io.BytesIO(data))
        with self.assertRaisesRegex(relay.RelayError, "rejected"):
            relay.unpack(buf.getvalue(), self.tmp / "out")

    def test_submit_then_relay_into_private_intake(self):
        gen = self.sop("text.upper", "Convert text to upper case: a generic reusable text utility",
                       GENERIC, ["text", "convert", "format", "utility", "generic"])
        posted = {}
        out = self.reg().submit(gen, create_issue=lambda repo, title, body: posted.update(repo=repo, title=title,
                                                                                            body=body) or "issue#7")
        self.assertEqual(out["issue"], "issue#7")
        # the public issue reveals nothing about the SOP
        self.assertTrue(posted["title"].startswith("SOP submission "))
        self.assertNotIn("upper", posted["body"].lower())
        self.assertNotIn("text.", posted["body"])
        prs = []
        res = relay.receive(posted["body"], "some outsider!", "Prog-Ramen/RamenSOPs#7", self.priv.read_bytes(),
                            str(self.intake), self.tmp / "work", Org(),
                            open_pr=lambda b, base, t, body: prs.append((b, t, body)) or "pr#1")
        self.assertRegex(res["branch"], r"^sop/some-outsider-/text\.upper-")
        files = sh(self.tmp, "--git-dir", str(self.intake), "ls-tree", "-r", "--name-only", res["branch"]).stdout
        self.assertIn("sops/text/upper/run.py", files)
        sopjson = json.loads(sh(self.tmp, "--git-dir", str(self.intake), "show",
                                f"{res['branch']}:sops/text/upper/sop.json").stdout)
        self.assertNotIn("origin", sopjson)                           # sanitized before sealing
        self.assertIn("@some-outsider-", prs[0][2])
        author = sh(self.tmp, "--git-dir", str(self.intake), "log", "-1", "--format=%an", res["branch"]).stdout.strip()
        self.assertEqual(author, "some-outsider-")

    def test_personal_sop_is_never_sealed(self):
        per = self.sop("acme.customers", "Pull our customer records", "print('db.acme.internal')", ["customer"])
        called = []
        with self.assertRaises(RegistryError):
            self.reg().submit(per, create_issue=lambda *a: called.append(a))
        self.assertEqual(called, [])

    def test_relay_rescans_and_rejects(self):
        # a hand-crafted submission that skipped the local checks still can't reach intake
        d = self.tmp / "evil"
        d.mkdir()
        (d / "sop.json").write_text(json.dumps({"id": "x.leak", "description": "d"}))
        (d / "run.py").write_text("TOKEN = 'ghp_abcdefghijklmnopqrstuvwxyz0123456789'\n")
        body = relay.seal(self.pub.read_bytes(), relay.pack(d, {"id": "x.leak"}))
        with self.assertRaisesRegex(relay.RelayError, "scan"):
            relay.receive(body, "x", "#1", self.priv.read_bytes(), str(self.intake), self.tmp / "w", Org())
        self.assertEqual(sh(self.tmp, "--git-dir", str(self.intake), "branch").stdout, "")


if __name__ == "__main__":
    unittest.main()
