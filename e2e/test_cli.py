"""End-to-end: the single-agent harness through the real CLI."""

import json
import shutil
import subprocess

from .helpers import E2E, ROOT


def write(path, text):
    return {"name": "write_file", "input": {"path": path, "content": text}}


def bash(cmd):
    return {"name": "bash", "input": {"command": cmd}}


class TestInstall(E2E):
    def test_installer_builds_a_working_environment(self):
        env = {**self.env, "HOME": str(self.tmp / "userhome")}
        p = subprocess.run(["bash", str(ROOT / "install.sh"), "--prefix", str(self.tmp / "env"),
                            "--bin", str(self.tmp / "bin"), "--source", str(ROOT)],
                           env=env, capture_output=True, text=True, timeout=600)
        self.assertEqual(p.returncode, 0, p.stderr[-2000:])
        launcher = self.tmp / "bin" / "rameness"
        v = subprocess.run([str(launcher), "--version"], capture_output=True, text=True, env=env)
        self.assertEqual(v.stdout.strip(), "0.1.0")
        # the packaged starter SOPs and UI are inside the environment
        t = subprocess.run([str(launcher), "sop", "test"], capture_output=True, text=True, env=env, cwd=self.proj)
        self.assertEqual(t.returncode, 0, t.stdout + t.stderr)
        ui = list((self.tmp / "env").rglob("fleet/ui/index.html"))
        self.assertTrue(ui)
        self.assertTrue((self.tmp / "userhome" / ".rameness" / "fleet.json").exists())


class TestSingleAgent(E2E):
    def test_direct_route_runs_an_sop_without_any_model(self):
        self.cli("init")
        (self.proj / "sales.csv").write_text("region,amount\neu,10\nus,30\n")
        plan = self.cli("plan", "summarize sales.csv").stdout
        self.assertIn("route:  direct", plan)
        self.assertIn("direct execution: data.csv_summary", plan)
        r = self.cli("--provider", "fake", "run", "summarize sales.csv")
        self.assertEqual(json.loads(r.stdout.split("\n\n[")[0])["rows"], 2)
        self.assertIn("[direct] llm_calls=0", r.stderr)

    def test_agent_loop_writes_runs_and_verifies(self):
        self.cli("init")
        r = self.cli("-y", "--provider", "replay", "run", "create hello.py that prints hi and run it", replay={"chat": [
            {"tool_calls": [write("hello.py", "print('hi')\n")]},
            {"tool_calls": [bash("python3 hello.py")]},
            {"text": "Created hello.py and ran it: prints hi."}]})
        self.assertIn("prints hi", r.stdout)
        self.assertTrue((self.proj / "hello.py").exists())
        self.assertIn("-> bash", r.stderr)
        self.assertEqual(len(list((self.proj / ".rameness" / "runs").glob("*.json"))), 1)
        self.assertTrue((self.proj / ".rameness" / "decisions.jsonl").stat().st_size > 0)

    def test_repeated_work_becomes_an_sop(self):
        self.cli("init")
        script = {"chat": [{"tool_calls": [bash("echo build")]}, {"tool_calls": [bash("echo package")]},
                           {"text": "built and packaged"}]}
        for task in ("build and package the project", "build and package it again"):
            self.cli("-y", "--provider", "replay", "run", task, replay=script)
        listing = self.cli("sop", "list").stdout
        self.assertRegex(listing, r"learned\.\w+\s+script\s+private\s+candidate")

    def test_loop_guard_stops_a_stuck_model(self):
        self.cli("init")
        stuck = {"chat": [{"tool_calls": [bash("cat missing.txt")]} for _ in range(30)]}
        r = self.cli("-y", "--provider", "replay", "run", "summarise the config file", replay=stuck)
        self.assertIn("stopped by the loop guard", r.stdout)
        self.assertIn("loop guard: reorient", r.stderr)
        self.assertLess(r.stderr.count("-> bash"), 30)

    def test_readonly_mode_blocks_writes(self):
        self.cli("init")
        self.config({"permissions": {"mode": "readonly"}})
        self.cli("--provider", "replay", "run", "write a file", replay={"chat": [
            {"tool_calls": [write("x.txt", "nope")]}, {"text": "tried"}]})
        self.assertFalse((self.proj / "x.txt").exists())

    def test_org_context_resolves_or_defers(self):
        acme = self.tmp / "acme"
        shutil.copytree(ROOT / "examples" / "acme", acme)
        plan = self.cli("plan", "retrieve customer data and generate a customer report", cwd=acme).stdout
        self.assertIn("customer_data.postgres", plan)                 # org facts name the data source
        org = json.loads((acme / ".rameness" / "org.json").read_text())
        org["facts"] = []
        (acme / ".rameness" / "org.json").write_text(json.dumps(org))
        plan = self.cli("plan", "retrieve customer data and generate a customer report", cwd=acme).stdout
        self.assertIn("customer_data: needs data_source", plan)       # deferred, not guessed
