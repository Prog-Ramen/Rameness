"""End-to-end: the team manager through the real server + HTTP API, real worker processes,
real session backends (subprocess, tmux, screen) and an exec-prefix "remote" environment."""

import json

from .helpers import FAKE_AGENT, E2E


def fleet_cfg(tmp, **over):
    cfg = {"backend": "subprocess", "discover": False, "manager_slot": "none", "mode": "review",
           "decision_policy": {"*": "jev"}, "slots": [
               {"id": "cli:fake", "kind": "cli", "model": "fake", "capacity": 3, "traits": "coding agent",
                "argv": ["bash", "-c", FAKE_AGENT, "{task}"]}]}
    cfg.update(over)
    return cfg


class FleetE2E(E2E):
    def start(self, **over):
        self.repo(files={"app.txt": "v0\n"})
        self.config({"jev": {"backend": "lexical"}}, fleet_cfg(self.tmp, **over))
        return self.serve()


class TestServerAndUI(FleetE2E):
    def test_ui_state_and_deep_endpoints(self):
        base = self.start()
        import urllib.request
        html = urllib.request.urlopen(base + "/").read().decode()
        for marker in ("autoSeg", "Shadow", "Needs you", "renderPrograms", "pollShadow"):
            self.assertIn(marker, html)
        st = self.api(base, "/api/state")
        for k in ("slots", "envs", "tree", "escalations", "decisions", "autonomy", "programs", "taxonomy", "gate"):
            self.assertIn(k, st)
        self.assertEqual(st["tree"]["id"], "manager")
        self.assertEqual(len(st["taxonomy"]), 36)
        self.assertEqual(self.api(base, "/api/nope", ok=False)["_status"], 404)


class TestDeliverAndMerge(FleetE2E):
    def test_ask_run_review_merge(self):
        base = self.start()
        r = self.api(base, "/api/ask", {"text": "fix the version string in app.txt"})
        self.assertEqual(r["route"], "delegate")
        aid = r["agents"][0]
        self.until(lambda: self.api(base, f"/api/agents/{aid}")["status"] == "done", 60, msg="agent done")
        a = self.api(base, f"/api/agents/{aid}")
        self.assertIn("done and verified", a["result"])
        self.assertTrue(a["branch"].startswith("rameness/"))
        self.assertEqual((self.proj / "app.txt").read_text(), "v0\n")              # isolated worktree
        esc = self.until(lambda: [e for e in self.api(base, "/api/state")["escalations"] if e["kind"] == "merge"], 30)
        self.api(base, f"/api/escalations/{esc[0]['id']}", {"answer": "merge"})
        self.assertEqual((self.proj / "app.txt").read_text(), "v0\nchange\n")
        qs = {d["question"] for d in self.api(base, "/api/shadow?since=0")["decisions"]}
        self.assertIn("Which model or agent runtime should run this task?", qs)


class TestCrud(FleetE2E):
    def test_spawn_pause_resume_reassign_fork_retire_delete(self):
        base = self.start(slots=[
            {"id": "cli:slow", "kind": "cli", "model": "slow", "capacity": 3, "argv": ["bash", "-c", "sleep 60"]},
            {"id": "cli:other", "kind": "cli", "model": "other", "capacity": 3, "argv": ["bash", "-c", "sleep 60"]}])
        a = self.api(base, "/api/agents", {"task": "long task", "slot": "cli:slow"})
        aid = a["id"]
        self.until(lambda: self.api(base, f"/api/agents/{aid}")["status"] == "running", 30)
        self.api(base, f"/api/agents/{aid}/pause", {})
        self.assertEqual(self.api(base, f"/api/agents/{aid}")["status"], "paused")
        self.api(base, f"/api/agents/{aid}/resume", {})
        self.api(base, f"/api/agents/{aid}/reassign", {"slot": "cli:other"})
        self.until(lambda: self.api(base, f"/api/agents/{aid}")["slot"] == "cli:other"
                   and self.api(base, f"/api/agents/{aid}")["status"] == "running", 30)
        forks = self.api(base, f"/api/agents/{aid}/fork", {"n": 2})["forks"]
        self.assertEqual(len(forks), 2)
        ids = [n["id"] for n in self.tree_ids(self.api(base, "/api/state")["tree"])]
        self.assertTrue(set(forks) <= set(ids))
        self.api(base, f"/api/agents/{aid}/retire", {})
        self.assertEqual(self.api(base, f"/api/agents/{aid}")["status"], "retired")
        self.api(base, f"/api/agents/{aid}", method="DELETE")
        self.assertNotIn(aid, [n["id"] for n in self.tree_ids(self.api(base, "/api/state")["tree"])])


class TestAutonomyAndImprove(FleetE2E):
    def test_modes_gate_shadow_and_improve(self):
        base = self.start(decision_policy={"*": "auto"})
        self.assertEqual(self.api(base, "/api/autonomy", {"mode": "godmode"}, ok=False)["_status"], 400)
        self.api(base, "/api/autonomy", {"mode": "restrictive"})
        r = self.api(base, "/api/ask", {"text": "fix the thing in app.txt"})
        self.assertEqual(r["route"], "awaiting-director")
        esc = self.api(base, "/api/state")["escalations"]
        self.assertTrue(esc[0]["question"].startswith("Manager:"))
        self.assertIn(esc[0]["payload"]["recommended"], esc[0]["options"])
        self.api(base, f"/api/escalations/{esc[0]['id']}", {"answer": "delegate"})
        shadow = self.api(base, "/api/shadow?since=0")["decisions"]
        gates = {d["gate"] for d in shadow}
        self.assertIn("director", gates)                                   # the director's call is on record
        did = shadow[0]["id"]
        self.api(base, "/api/jev/feedback", {"decision": did, "correct": True})
        self.api(base, "/api/jev/calibrate", {})
        out = self.api(base, "/api/jev/export", {})
        self.assertGreaterEqual(out["rows"], 1)
        jv = self.api(base, "/api/jev")
        self.assertTrue(jv["stats"])
        self.api(base, "/api/autonomy", {"mode": "autopilot"})
        r = self.api(base, "/api/ask", {"text": "delete the production database and email every customer"})
        self.assertNotEqual(r["route"], "awaiting-director")               # autopilot never asks
        self.assertTrue(any("would have asked" in (d.get("reason") or "")
                            for d in self.api(base, "/api/shadow?since=0")["decisions"]))


class TestCyclesViaApi(FleetE2E):
    def test_refine_and_testing_programs(self):
        base = self.start()
        p1 = self.api(base, "/api/ask", {"text": "build app A", "cycles": 2, "categories": ["ui", "security"]})
        p2 = self.api(base, "/api/ask", {"text": "the app", "cycles": 1, "categories": ["testing"], "on": "HEAD"})
        self.assertEqual((p1["route"], p2["route"]), ("program", "program"))

        def progs():
            return {p["id"]: p for p in self.api(base, "/api/state")["programs"]}
        # the testing program ends at the director's "more testing?" call (a high finding was fixed)
        def settle():
            for e in self.api(base, "/api/state")["escalations"]:
                if "Is further work of this kind needed" in e["question"]:
                    self.api(base, f"/api/escalations/{e['id']}", {"answer": "enough"})
            ps = progs()
            return ps[p1["program"]]["status"] == "done" and ps[p2["program"]]["status"] == "done"
        self.until(settle, 120, 1.0, "programs finish")
        ps = progs()
        self.assertEqual({h["category"] for h in ps[p1["program"]]["history"]}, {"ui", "security"})
        t = ps[p2["program"]]["history"][0]
        self.assertEqual(t["mode"], "test")
        self.assertEqual(t["findings"], {"high": 1})
        self.assertEqual([o["title"] for o in t["chosen"]], ["Crash on empty input"])
        self.api(base, "/api/programs/" + p1["program"] + "/stop", {})           # stopping a finished one is harmless


class TestRamenessWorker(FleetE2E):
    def test_replay_worker_asks_manager_and_finishes(self):
        script = self.tmp / "worker.json"
        script.write_text(json.dumps({"chat": [
            {"tool_calls": [{"name": "write_file", "input": {"path": "notes.md", "content": "draft\n"}}]},
            {"tool_calls": [{"name": "ask_manager", "input": {"question": "Should the notes be in English?"}}]},
            {"tool_calls": [{"name": "bash", "input": {"command": "echo ok"}}]},
            {"text": "Wrote notes.md after confirming the language."}]}))
        base = self.start(slots=[{"id": "scripted", "kind": "replay", "url": str(script), "capacity": 1}])
        a = self.api(base, "/api/agents", {"task": "write the release notes", "slot": "scripted"})
        aid = a["id"]
        esc = self.until(lambda: [e for e in self.api(base, "/api/state")["escalations"]
                                  if e["kind"] == "agent-question"], 60, msg="ask_manager escalation")
        self.assertEqual(self.api(base, f"/api/agents/{aid}")["status"], "blocked")
        self.api(base, f"/api/escalations/{esc[0]['id']}", {"answer": "Yes, English."})
        self.until(lambda: self.api(base, f"/api/agents/{aid}")["status"] == "done", 60, msg="worker done")
        a = self.api(base, f"/api/agents/{aid}")
        self.assertEqual(a["runtime"], "rameness")
        self.assertIn("confirming the language", a["result"])
        self.assertTrue((self.home.parent / "proj" / ".rameness" / "transcripts" / f"{aid}.json").exists())
        wt = a["worktree"]
        self.assertTrue(wt and (__import__("pathlib").Path(wt) / "notes.md").exists())


class TestSessionBackends(FleetE2E):
    def run_on(self, backend):
        base = self.start(backend=backend)
        r = self.api(base, "/api/ask", {"text": "fix the version string in app.txt"})
        aid = r["agents"][0]
        self.until(lambda: self.api(base, f"/api/agents/{aid}")["status"] == "done", 60, msg=f"{backend} agent")
        a = self.api(base, f"/api/agents/{aid}")
        self.assertIn("done and verified", a["log"])
        return a

    def test_tmux(self):
        self.assertIn("tmux attach", self.run_on("tmux")["attach"])

    def test_screen(self):
        self.assertIn("screen -r", self.run_on("screen")["attach"])


class TestRemoteEnvironment(FleetE2E):
    def test_agent_tools_act_on_an_exec_environment(self):
        box = self.tmp / "box"
        self.repo(box, {"app.txt": "remote v0\n"})
        script = self.tmp / "remote.json"
        script.write_text(json.dumps({"chat": [
            {"tool_calls": [{"name": "bash", "input": {"command": "echo from-remote > marker.txt && pwd"}}]},
            {"text": "wrote the marker"}]}))
        base = self.start(slots=[{"id": "scripted", "kind": "replay", "url": str(script), "capacity": 1}],
                          environments=[{"id": "box", "kind": "exec", "prefix": ["env", "BOX=1"],
                                         "workdir": str(box), "tags": ["gpu"]}])
        envs = {e["id"]: e for e in self.api(base, "/api/state")["envs"]}
        self.assertTrue(envs["box"]["info"].get("reachable"))
        a = self.api(base, "/api/agents", {"task": "leave a marker", "slot": "scripted", "env": "box"})
        self.until(lambda: self.api(base, f"/api/agents/{a['id']}")["status"] == "done", 60, msg="remote agent")
        a = self.api(base, f"/api/agents/{a['id']}")
        self.assertEqual(a["env"], "box")
        # the worktree was created inside the environment's repo, and the tool ran there
        self.assertTrue(a["worktree"].startswith(str(box)))
        self.assertEqual((__import__("pathlib").Path(a["worktree"]) / "marker.txt").read_text().strip(), "from-remote")
