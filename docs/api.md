# API and monitoring

**In brief:** a running manager (`rameness fleet up`, or `rameness fleet up --home` for the general manager) serves
an HTTP API on 127.0.0.1:7788. Apps talk to it in chats: a message goes in, the reply streams back, and the app can
offer tools of its own for the manager to call. `GET /metrics` serves Prometheus metrics, and
[`dashboards/rameness.json`](../dashboards/rameness.json) is a ready Grafana dashboard.

The API has no authentication and binds to 127.0.0.1. Put an authenticating proxy in front of it before exposing it
to anything else.

## Chats

A chat is one conversation: its messages, the manager's replies, the agents it started and what they reported.
Any string works as a chat id (`[A-Za-z0-9_.:-]`); the client picks it, for example one per app conversation or one
per device. The manager keeps each chat's history, so a follow-up such as "read me the full report" sees the result
of the work the chat asked for.

| Endpoint | What it does |
|---|---|
| `GET /api/chats` | Every chat: `id`, `title`, `mode`, `created`, `updated`. |
| `POST /api/chats/<chat>/turn` | Send a message; the reply streams back (below). |
| `GET /api/chats/<chat>/events?since=<id>` | The chat's events after an event id; finished work carries the agent's report. |
| `POST /api/condense` | `{"text", "length": "concise" \| "medium"}` -> `{"text"}`: a shorter version of a long reply. |
| `POST /api/ask` | Fire-and-forget request (`text`, optional `chat`, `mode`); returns how it was routed. |

## A turn

`POST /api/chats/<chat>/turn` with:

```json
{
  "text": "set the volume to 40",
  "instructions": "Your reply is read aloud: plain sentences, one to three.",
  "tools": [{"name": "set_volume", "description": "Set the speaker volume (0-100)",
             "input_schema": {"type": "object", "properties": {"level": {"type": "integer"}}, "required": ["level"]}}],
  "mode": "team"
}
```

Only `text` is required. `instructions` say how the client wants replies (spoken, short, markdown allowed...).
`tools` are tools only the client can run. `mode` is `team` (the manager routes each message) or `direct` (one agent
takes the whole chat); a chat keeps the mode it was first given.

The reply is a stream of server-sent events (`data: {...}` lines):

| Event | Meaning |
|---|---|
| `{"t": "turn", "turn": "t-..."}` | This turn's id, for posting tool results. |
| `{"t": "route", "route": "answer"}` | How the manager took the message: `answer`, `delegate`, `decompose`, `research`, `clarify`, `direct`, `reuse`. |
| `{"t": "text", "text": "..."}` | A piece of the reply, as it is generated. |
| `{"t": "tool_call", "turn", "id", "name", "input"}` | The manager calls one of the client's tools. |
| `{"t": "agents", "agents": ["a-..."]}` | The work was handed to these agents. |
| `{"t": "done", "text": "..."}` | The whole reply. |
| `{"t": "error", "error": "..."}` | The turn failed. |

When a `tool_call` arrives, run the tool and post its result while the stream waits:
`POST /api/turns/<turn>/results` with `{"id": "<call id>", "content": "<result text>"}` (2 minutes by default,
`tool_timeout` in the request to change it).

Messages that need no work (small talk, a fact, a definition, or something the client's tools can do) are answered
by the manager itself, so they stream back in about a second. Work is acknowledged at once ("I'll have an agent do
that and report back"); the result arrives later in the chat's events, where a client can pick it up and tell the
user.

## The team

The same API drives the team views: `GET /api/state` (agents, open decisions, recent decisions and events),
`GET /api/agents/<id>` (an agent's task, events, log), `POST /api/agents/<id>/prompt`, `/retire`, `/pause`,
`/resume`, `/fork`, and `POST /api/escalations/<id>` with `{"answer"}` for a decision waiting on you.

## Metrics

`GET /metrics` serves the Prometheus text format. Scrape it like any target:

```yaml
scrape_configs:
  - job_name: rameness
    static_configs: [{targets: ["127.0.0.1:7788"]}]
```

| Metric | Type | Labels |
|---|---|---|
| `rameness_up` | gauge | `general`, `work_mode` |
| `rameness_turns_total` | counter | `route` |
| `rameness_turn_first_text_seconds`, `rameness_turn_seconds` | histogram | `route` |
| `rameness_jev_decisions_total` | counter | `backend`, `kind` |
| `rameness_jev_decision_seconds` | histogram | `backend` |
| `rameness_model_requests_total`, `rameness_model_tokens_total` | counter | `provider`, `model`, `direction` |
| `rameness_agents` | gauge | `status`, `role` |
| `rameness_agents_finished` | gauge | `runtime`, `outcome` |
| `rameness_escalations_open`, `rameness_chats` | gauge | |
| `rameness_decisions` | gauge | `gate` (who decided: `jev`, `director`) |

Counters and histograms count from the manager's start; the gauges are read from its store at each scrape.

For short-lived runs that are never scraped, set `RAMENESS_TELEMETRY_URL` (e.g. `http://127.0.0.1:8428`) to push
per-decision samples to VictoriaMetrics or anything that accepts the Prometheus import format.

## The dashboard

Import [`dashboards/rameness.json`](../dashboards/rameness.json) in Grafana (Dashboards -> New -> Import) and pick
your Prometheus datasource. It shows the manager and what is waiting on you, messages by route and reply latency,
decisions per minute and their latency by decision backend, who decided, agents by status and outcome, and the
manager's own model use.
