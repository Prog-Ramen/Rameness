# Getting started

**In brief:** install Rameness, start a decision model, run a task. Tools act on your machine, so read
[Safety](#safety) before using `-y`.

## Install

One command installs a self-contained environment.

```bash
curl -fsSL https://raw.githubusercontent.com/Prog-Ramen/Rameness/main/install.sh | bash
# or from a checkout:  ./install.sh [--with-kev] [--no-laya] [--with-herdr] [--prefix DIR] [--dev]
```

The environment (`~/.rameness/env`) holds the harness, the fleet manager, the UI, the built-in SOPs,
both model SDKs and [Laya](https://github.com/NandhaKishorM/laya) (pulls PyTorch). The `rameness`
launcher goes in `~/.local/bin`.

## Pick a decision model

Rameness's choices come from a typed decision model, not an LLM. Kev is the better one if you have the memory.

| | Laya (installed by default) | Kev (`--with-kev`) | TypeSafe Jev (subscription) |
|---|---|---|---|
| What | open-source, 421M params | open-source, Qwen3.5-4B based | hosted by TypeSafe AI |
| Accuracy (`rameness jev bench`) | 0.73 | 0.93 | not measured here |
| Speed | ~0.13 s CPU | ~0.9 s CPU (bf16), ~20 ms GPU | network round trip |
| Memory | ~1.7 GB | ~9 GB GPU, ~8-12 GB CPU | none locally |
| Cost | free | free | per call |

A run makes one or two decisions per turn, so Kev on CPU adds roughly a second to each turn.

**Laya** is installed with Rameness. Start its server once per machine:

```bash
rameness jev up                    # starts the configured server (jev.serve, default laya) on 127.0.0.1:8000
rameness jev status                # what's installed, what's answering, which one decisions use
```

`rameness up` starts it for you. Without a server, a single `rameness run` still uses Laya in-process
(`laya-local`); the server is what lets the manager and every fleet worker share one copy.

**Kev** gets its own environment (Python 3.13 and a pinned PyTorch) under `~/.rameness/jev/kev`:

```bash
rameness jev setup kev             # or install.sh --with-kev; weights (~9 GB) download on first start
rameness jev up kev                # 127.0.0.1:8008; uses the GPU if it has ~10 GB free
```

To make `rameness up` start Kev, and run it on CPU when the GPU is busy, set in `~/.rameness/config.json`:

```json
{"jev": {"serve": "kev", "serve_device": "cpu"}}
```

**TypeSafe's hosted Jev** needs an API key. Keep it in the environment, not a config file:

```bash
export TYPESAFE_API_KEY=ts_...
```

With a key set, TypeSafe is used when no local model runs, and covers a local server outage. To use it
for every decision, set `{"jev": {"backend": "typesafe"}}`.

`auto` (the default) picks a running Kev server, then a running Laya server, then Laya in-process,
then TypeSafe if a key is set. With none, Rameness warns and falls back to an offline keyword scorer.
More in [Decisions](decisions.md).

## First tasks

A single agent needs only a model; the team needs the manager running.

```bash
rameness jev up                    # the decision model
cd your-project && rameness init   # ./.rameness with config.json + org.json
rameness run "fix the failing test"                          # single agent, no team
rameness --base-url http://localhost:8080/v1 run "..."       # any OpenAI-compatible server on a port
rameness fleet slots               # models: API keys, llama-server/ollama/vLLM/LM Studio ports, CLI agents
rameness fleet envs                # where agents can run: local + your ssh/container environments
rameness up                        # manager + UI → http://127.0.0.1:7788
rameness fleet ask "add rate limiting to the API and benchmark two approaches"
rameness fleet shadow -f           # follow the decisions in the terminal
```

## Everyday commands

The CLI covers single runs, planning, SOPs and the decision model.

```bash
rameness plan "summarize sales.csv"             # show route + SOP traversal, no execution
rameness run "the tests are failing, fix the bug"
rameness chat
rameness sop tree | list | show ID | search Q | run ID --args '{...}' | test | promote ID | propose ID
rameness sop discover .rameness/runs --top 10   # recurring, costly procedures in your run logs (report only)
rameness --sops ~/sops/client-a run "..."       # a private SOP folder for this session (repeatable)
rameness jev bench                              # measure the active decision model on real harness decisions
rameness --provider deepseek run "..."          # DEEPSEEK_API_KEY
rameness --provider ollama --model qwen3:14b run "..."
```

**Providers:** `anthropic` (default: `claude-opus-5` main, `claude-haiku-4-5` fast; adaptive thinking,
effort from the router, server-side refusal fallbacks), `openai`, `deepseek`, `ollama`, or any
OpenAI-compatible `base_url`. Local servers are detected and configured automatically; see
[The agent loop](agent.md#local-models).

**Event log:** `RAMENESS_EVENT_LOG=path.jsonl` writes a timestamped log of everything the loop does
(turns, tool calls, every decision with its probabilities, hooks, compactions, reviews), starting with a
`system_prompt` event that hashes the exact prompt and code the run used; full tool inputs go to
`tool_calls.jsonl` beside it. `sop discover` and the benchmarks read it.

## Safety

Rameness acts on your machine as you; the defaults ask before acting.

* **No sandbox by default.** `bash` and the file tools run as your user. Permission modes: `ask`
  (default: you approve each action), `auto` (`-y`, approve everything), `readonly`. Use `-y` only in a
  disposable environment (a container, a VM, a throwaway checkout), or run agents in a `container`
  environment (see [The team](fleet.md)).
* **The manager UI has no authentication.** It binds to 127.0.0.1; don't expose the port.
* **Validated, shareable SOPs are proposed as public PRs** after successful tasks, behind secret and
  privacy checks ([Sharing SOPs](sharing.md)). Set `registry.auto_propose: false` to keep proposals
  manual. Private SOPs never leave your machine.

## Tests

```bash
.venv/bin/python -m unittest discover tests
```
