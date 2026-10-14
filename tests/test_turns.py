import json
import os
import tempfile
import threading
import time
import unittest
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

from rameness.fleet.manager import FLEET_DEFAULTS, Fleet
from rameness.fleet.server import Api, make_handler
from rameness.llm import FakeProvider, Response, ToolCall

VOLUME = {"name": "set_volume", "description": "Set the speaker volume (0-100)",
          "input_schema": {"type": "object", "properties": {"level": {"type": "integer"}}, "required": ["level"]}}


class Turns(unittest.TestCase):
    """A client talks to the manager over HTTP: replies stream back, the manager can call tools only the client can
    run, delegated work is acknowledged at once and reported through the chat's event feed."""

    def setUp(self):
        tmp = Path(tempfile.mkdtemp())
        os.environ["RAMENESS_HOME"] = str(tmp / "rhome")
        (tmp / "home" / ".rameness").mkdir(parents=True)
        (tmp / "home" / ".rameness" / "config.json").write_text(json.dumps({"jev": {"backend": "lexical"}}))
        cfg = json.loads(json.dumps(FLEET_DEFAULTS))
        cfg.update(backend="subprocess", discover=False, general=True, decision_policy={"*": "jev"}, slots=[
            {"id": "cli:editor", "kind": "cli", "model": "editor", "capacity": 2, "traits": "coding edits",
             "argv": ["bash", "-c", "echo 'Wrote the report.'", "{task}"]}])
        self.f = Fleet(tmp / "home", cfg, planner=False, probe=False)
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(Api(self.f)))
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.srv.server_address[1]}"

    def tearDown(self):
        self.srv.shutdown()

    def post(self, path, body):
        return urllib.request.urlopen(urllib.request.Request(self.base + path, json.dumps(body).encode(),
                                                             {"Content-Type": "application/json"}), timeout=30)

    def turn(self, chat, body, run_tool=None):
        events = []
        with self.post(f"/api/chats/{chat}/turn", body) as r:
            for line in r:
                if not line.startswith(b"data: "):
                    continue
                ev = json.loads(line[6:])
                events.append(ev)
                if ev["t"] == "tool_call":
                    self.post(f"/api/turns/{ev['turn']}/results", {"id": ev["id"], "content": run_tool(ev)}).read()
                if ev["t"] == "done":
                    break
        return events

    def test_the_manager_answers_with_the_clients_tool_and_streams_its_reply(self):
        def reply(messages):
            if messages[-1]["role"] == "tool":
                return Response(f"Done, the volume is {messages[-1]['content']}.", [], "end_turn")
            return Response("", [ToolCall("c1", "set_volume", {"level": 40})], "tool_use")
        self.f.planner = FakeProvider([reply, reply])
        ran = []
        evs = self.turn("kitchen", {"text": "set the volume to 40", "tools": [VOLUME],
                                    "instructions": "Your reply is read aloud."},
                        run_tool=lambda ev: ran.append(ev["input"]) or "40")
        self.assertEqual(ran, [{"level": 40}])
        self.assertEqual(evs[1], {"t": "route", "route": "answer"})
        self.assertEqual("".join(e["text"] for e in evs if e["t"] == "text"), "Done, the volume is 40.")
        self.assertEqual(evs[-1], {"t": "done", "text": "Done, the volume is 40."})
        self.assertIn("read aloud", self.f.planner.seen[0]["system"])
        self.assertEqual(self.f.planner.seen[0]["tools"], ["set_volume"])
        self.assertEqual([a for a in self.f.store.agents() if a["role"] != "manager"], [])

    def test_delegated_work_is_acknowledged_and_reported_in_the_chats_feed(self):
        evs = self.turn("study", {"text": "research and compare the best open source databases, write a report"})
        route = evs[1]["route"]
        self.assertIn(route, ("research", "delegate"))
        self.assertEqual(evs[-1]["text"], self.f.ACKS[route])
        aid = next(e for e in evs if e["t"] == "agents")["agents"][0]
        end = time.time() + 15
        while time.time() < end and self.f.get(aid)["status"] != "done":
            self.f.tick()
            time.sleep(0.2)
        feed = json.loads(urllib.request.urlopen(f"{self.base}/api/chats/study/events?since=0").read())
        done = [e for e in feed["events"] if e.get("result")]
        self.assertEqual(done[0]["agent"], aid)
        self.assertIn("Wrote the report.", done[0]["result"])
        later = json.loads(urllib.request.urlopen(f"{self.base}/api/chats/study/events?since={feed['last']}").read())
        self.assertEqual(later["events"], [])

    def test_condense(self):
        self.f.planner = FakeProvider([Response("Short version.", [], "end_turn")])
        r = json.loads(self.post("/api/condense", {"text": "A long answer. " * 50, "length": "concise"}).read())
        self.assertEqual(r["text"], "Short version.")
        self.assertIn("one or two sentences", self.f.planner.seen[0]["system"])


    def test_metrics_are_prometheus_text_with_the_teams_state(self):
        from rameness import metrics
        metrics.reset()
        self.f.planner = FakeProvider([Response("Hi there.", [], "end_turn")])
        self.turn("m", {"text": "hello, how are you?"})
        with urllib.request.urlopen(f"{self.base}/metrics") as r:
            self.assertTrue(r.headers["Content-Type"].startswith("text/plain"))
            text = r.read().decode()
        self.assertIn('rameness_turns_total{route="answer"} 1', text)
        self.assertIn('rameness_turn_seconds_count{route="answer"} 1', text)
        self.assertIn("rameness_jev_decisions_total{", text)
        self.assertIn('rameness_agents{role="manager",status="running"} 1', text)
        self.assertIn("rameness_escalations_open 0", text)
        self.assertIn("rameness_chats 1", text)
        for line in text.splitlines():                     # every sample line is "name{labels} value"
            if line and not line.startswith("#"):
                float(line.rsplit(" ", 1)[1])

if __name__ == "__main__":
    unittest.main()
