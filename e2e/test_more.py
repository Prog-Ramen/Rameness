"""End-to-end: context offloading, SOP management, the fleet CLI without a server, godmode, chat."""

import hashlib
import json
import subprocess

from .helpers import FAKE_AGENT, E2E
from .test_fleet import fleet_cfg


class TestContextOffload(E2E):
    def test_large_output_is_stored_and_recalled(self):
        self.cli("init")
        self.config({"context": {"offload_chars": 2000}, "learning": {"enabled": False}})
        out = subprocess.run("seq 1 20000", shell=True, capture_output=True, text=True).stdout
        ref = "art_" + hashlib.sha1(f"exit=0\n{out}".encode()).hexdigest()[:10]
        r = self.cli("-y", "--provider", "replay", "run", "count to 20000 and find 15000", replay={"chat": [
            {"tool_calls": [{"name": "bash", "input": {"command": "seq 1 20000"}}]},
            {"tool_calls": [{"name": "recall", "input": {"ref": ref, "grep": "^15000$"}}]},
            {"text": "found it"}]})
        self.assertIn("-> recall", r.stderr)
        self.assertTrue((self.proj / ".rameness" / "artifacts" / f"{ref}.txt").exists())
        transcript_hint = (self.proj / ".rameness" / "artifacts" / f"{ref}.txt").read_text()
        self.assertIn("15000", transcript_hint)


class TestSopManagement(E2E):
    def test_sop_commands(self):
        self.cli("init")
        self.assertIn("http/", self.cli("sop", "tree").stdout)
        self.assertIn("data.json_extract", self.cli("sop", "search", "extract a field from json").stdout)
        show = json.loads(self.cli("sop", "show", "http.get").stdout)
        self.assertEqual(show["scope"], "public")
        (self.proj / "a.json").write_text('{"user": {"name": "ada"}}')
        run = json.loads(self.cli("sop", "run", "data.json_extract", "--args",
                                  json.dumps({"paths": ["user.name"], "file": "a.json"})).stdout)
        self.assertEqual(run["values"]["user.name"], "ada")
        self.assertIn("ok", self.cli("sop", "test").stdout)
        # a private candidate SOP: promote, publish to a local package, index, install, search remotely
        d = self.proj / ".rameness" / "sops" / "text" / "shout"
        d.mkdir(parents=True)
        (d / "sop.json").write_text(json.dumps({"id": "text.shout", "description": "Upper-case text generically",
            "status": "candidate", "keywords": ["text", "upper"],
            "inputs": {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]},
            "tests": [{"input": {"text": "a"}, "expect": {"out": "A"}}]}))
        (d / "run.py").write_text("import json,sys\nprint(json.dumps({'out': json.load(sys.stdin)['text'].upper()}))\n")
        self.assertIn("text.shout -> validated", self.cli("sop", "promote", "text.shout").stdout)
        pkg = self.tmp / "pkg"
        self.cli("sop", "publish", "text.shout", "--to", str(pkg), input="y\n")
        self.assertTrue((pkg / "text" / "shout" / "run.py").exists())
        self.cli("sop", "index", str(pkg))
        self.assertIn("text.shout", self.cli("sop", "remote", "shout some text", "--index", str(pkg / "index.json")).stdout)
        self.cli("sop", "install", str(pkg), "--name", "mypkg")
        self.assertTrue((self.home / "public" / "mypkg" / "text" / "shout" / "sop.json").exists())
        self.assertIn("removed text.shout", self.cli("sop", "remove", "text.shout").stdout)


class TestFleetCli(E2E):
    def test_fleet_commands_without_a_server(self):
        self.repo(files={"app.txt": "v0\n"})
        self.config({"jev": {"backend": "lexical"}}, fleet_cfg(self.tmp, mode="local"))
        self.assertIn("cli:fake", self.cli("fleet", "slots").stdout)
        self.assertIn("local", self.cli("fleet", "envs").stdout)
        self.assertIn("unit_tests", self.cli("fleet", "categories").stdout)
        r = json.loads(self.cli("fleet", "ask", "fix the version string in app.txt").stdout)
        aid = r["agents"][0]
        self.until(lambda: "✓ " + aid in self.cli("fleet", "tick").stdout, 60, 0.5, "agent done via ticks")
        self.assertEqual((self.proj / "app.txt").read_text(), "v0\nchange\n")          # local mode merged
        self.assertIn("done", self.cli("fleet", "standup").stdout)
        self.assertIn("Which model or agent runtime", self.cli("fleet", "shadow").stdout)
        self.assertIn("autonomy: balanced", self.cli("fleet", "mode").stdout)
        self.cli("fleet", "mode", "restrictive")
        r = json.loads(self.cli("fleet", "ask", "change app.txt again").stdout)
        self.assertEqual(r["route"], "awaiting-director")
        needs = self.cli("fleet", "needs").stdout
        eid = needs.split()[0]
        self.cli("fleet", "answer", eid, "delegate")
        self.assertNotIn(eid, self.cli("fleet", "needs").stdout)
        self.assertIn(aid, self.cli("fleet", "show", aid).stdout)


class TestGodmode(E2E):
    def test_godmode_runs_until_jev_or_the_cap_stops_it(self):
        self.repo(files={"app.txt": "v0\n"})
        self.config({"jev": {"backend": "lexical"}},
                    fleet_cfg(self.tmp, allow_godmode=True, godmode={"max_cycles": 2}, decision_policy={"*": "auto"}))
        base = self.serve()
        self.api(base, "/api/autonomy", {"mode": "godmode"})
        pid = self.api(base, "/api/ask", {"text": "build app A", "cycles": "godmode"})["program"]
        p = self.until(lambda: next((x for x in self.api(base, "/api/state")["programs"]
                                     if x["id"] == pid and x["status"] == "done"), None), 180, 1, "godmode done")
        self.assertIsNone(p["total"])
        self.assertLessEqual(len(p["history"]), 2)
        self.assertFalse([e for e in self.api(base, "/api/state")["escalations"] if e["kind"] == "decision"])
        self.assertIn("change", (self.proj / "app.txt").read_text())                  # JEV merged on its own


class TestChat(E2E):
    def test_chat_session_keeps_history(self):
        self.cli("init")
        r = self.cli("--provider", "replay", "chat", input="what is 2+2\nand doubled?\n", replay={"chat": [
            {"text": "4"}, {"text": "8, doubling the previous answer"}]})
        self.assertIn("4", r.stdout)
        self.assertIn("doubling the previous answer", r.stdout)
