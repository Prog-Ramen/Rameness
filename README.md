<div align="center"><img src="rameness_logo.png" alt="Rameness Logo" width="28%"></div>
<div align="center"><img src="rameness_txt1.png" alt="Rameness Text" width="75%"></div>
<p align="center">
  <a href="https://github.com/Prog-Ramen/Rameness/blob/main/LICENSE"><img src="https://img.shields.io/github/license/Prog-Ramen/Rameness" alt="License"></a>
  <a href="https://github.com/Prog-Ramen/Rameness/releases"><img src="https://img.shields.io/github/v/release/Prog-Ramen/Rameness" alt="Release"></a>
  <a href="https://github.com/Prog-Ramen/Rameness/actions/workflows/tests.yml"><img src="https://github.com/Prog-Ramen/Rameness/actions/workflows/tests.yml/badge.svg" alt="tests.yml"></a>
  <img src="https://img.shields.io/badge/python-3.12-3776AB" alt="Python">
</p>
<p align="center">
  <a href="#trade-offs"><img src="https://img.shields.io/badge/%2B%20tradeoffs-Separation%20of%20concerns%2C%20Strategy%20pattern%2C%20Graceful%20degradation%2C%20Local--first%2C%20Memoization-2ea44f" alt="+ tradeoffs: Separation of concerns, Strategy pattern, Graceful degradation, Local-first, Memoization"></a>
  <a href="#trade-offs"><img src="https://img.shields.io/badge/--%20tradeoffs-Operational%20complexity%2C%20Amortized%20cost%2C%20Implicit%20trust%20boundary-d97706" alt="- tradeoffs: Operational complexity, Amortized cost, Implicit trust boundary"></a>
</p>


**Rameness runs coding and research tasks with AI models from your terminal, works well with local
models on your own GPU, and gets faster at work it has done before.**

## What you use it for

**Fix a bug or add a feature in a repository.** Describe the task; one agent reads the code, edits the
files, runs the tests, and stops when the work is checked. It asks before every command unless you pass `-y`.

```bash
cd my-repo
rameness run "test_login fails since the last commit, find out why and fix it"
```

**Run it on your own hardware.** Point it at llama.cpp, vLLM, ollama or any OpenAI-compatible server, or
use Claude, OpenAI or DeepSeek. With local models it sets the thinking budget, context limit and sampling
for the server it finds. On a 150-minute build test, Qwen3.8-27B on one GPU scored, in its best run,
the same as Claude Code with Claude Opus ([Benchmarks](docs/benchmarks.md)).

```bash
rameness --base-url http://localhost:8080/v1 run "add CSV export to the reports page"
```

**Hand a bigger job to a team.** A manager splits the work across agents, each on its own git branch,
model and machine (for example a local model for routine parts, Claude for hard ones, a GPU box over
ssh). It runs rounds of improvement and testing, and brings you only the decisions that matter, in a web UI.

```bash
rameness up                                                   # manager + UI on http://127.0.0.1:7788
rameness fleet ask "build a habit tracker web app" --cycles 4  # a first build, then 4 rounds of improvement
```

**Ask it anything, from anywhere.** One general manager takes every kind of request: it answers simple
questions itself, researches with web search, and starts agents for real work, which find the project the task is
about on their own. Each conversation is a chat with its own history; in *direct* mode one agent takes a whole chat,
like a single coding assistant. Chat apps and voice devices talk to it over a small streaming API, and can offer
their own tools (a speaker's volume, the lights) for the manager to call.

```bash
rameness fleet up --home                                     # the general manager on http://127.0.0.1:7788
rameness fleet ask "what does HTTP 429 mean?" --home --chat c1
rameness fleet ask "fix the flaky upload test in the shop project" --home --chat c1
```

**Stop paying for the same work twice.** After each run, Rameness looks at what it did over its last few
runs. A multi-step procedure it keeps repeating, such as "start a local server and check the page in a
browser" or "run the package's tests plus a check per reported issue", becomes a tested script (an SOP)
that later runs call in one step. On a series of eight similar tasks, that cut task time by a third and
tokens by half. Useful SOPs can be shared publicly in [RamenSOPs](https://github.com/Prog-Ramen/RamenSOPs);
anything specific to you stays on your machine.

## What makes it different

Other harnesses (Claude Code, Codex, pi) leave these choices to fixed settings or to the main model itself:
how hard to think, which procedure applies, which model and machine should do the work, what to do when
something fails. Rameness hands each of them to a small decision model (JEV: Clef-Flash, Kev or Laya running
locally, swappable with one setting, or TypeSafe AI's hosted Jev) that scores the options in well under a second. Routine choices it makes
alone; significant or uncertain ones come to you. You can watch every decision live, and your
corrections make the next ones better.

## Quick start

```bash
curl -fsSL https://raw.githubusercontent.com/Prog-Ramen/Rameness/main/install.sh | bash
rameness jev up                          # start the decision model (Laya, installed by default)
cd your-project && rameness init
rameness run "fix the failing test"      # one agent
```

[Getting started](docs/getting-started.md) covers choosing a decision model, connecting your models, and
every command.

> **Safety:** tools act on your machine as you, and ask before each action by default. Use `-y` (approve
> everything) only somewhere disposable. The manager's API and UI have no authentication and bind to 127.0.0.1;
> put an authenticating proxy in front before exposing them. [More](docs/getting-started.md#safety).

Settings are JSON (`~/.rameness/config.json`, `fleet.json`, per-project overrides), checked against a schema when
they load: a misspelt or mistyped setting is an error, not silently ignored. `rameness config schema` gives editors
autocomplete.

## How it works

Each piece in a sentence; follow the link for the details.

| | In brief |
|---|---|
| [**Decisions (JEV)**](docs/decisions.md) | A typed decision model scores the options for every choice the harness makes; it sees only what that decision needs. |
| [**The agent loop**](docs/agent.md) | One agent works a task in turns: JEV sets how hard to think, guards catch loops, context is compacted before it overflows. |
| [**The team**](docs/fleet.md) | A manager runs leads and associates, each on its own model, runtime and machine, and hands you the significant decisions. |
| [**SOPs**](docs/sops.md) | Tested procedures the agent calls like tools; only tested ones are offered, so the agent can trust them. |
| [**Learning**](docs/learning.md) | After each run, repeated multi-step work becomes a new SOP, if it saves more than it costs. |
| [**Sharing SOPs**](docs/sharing.md) | Private, public registry or built-in: each SOP goes where it belongs, with its tests, past secret and privacy checks. |
| [**API and monitoring**](docs/api.md) | Chats over a streaming HTTP API with client tools; Prometheus metrics and a Grafana dashboard. |

## Results

On a 150-minute open-ended build (a Minecraft clone in the browser, 14 behaviour checks plus a visual
grade), Rameness's best runs with local models match Claude Code with Claude Opus:

| Harness + model | Behaviour | Visual (0-10) |
|---|---|---|
| Claude Code + Claude Opus 5.5 (reference) | 100% | 7.25 |
| Rameness + Qwen3.8-27B UD-Q4_K_M (llama.cpp) | 100% | 7.25 |
| Rameness + Qwen3.8-Flash-Next AutoRound 3bpw (vLLM), Kev | 100% | 7.5 |
| Rameness + Qwen3.8-Flash-Next AutoRound 3bpw (vLLM), Clef-Flash | 100% | 8.0 ([human review](docs/benchmarks.md#human-review-of-the-clef-build); judge 6.5) |

On a series that repeats one procedure, learned SOPs cut task time by a third and tokens by half.
[Benchmarks](docs/benchmarks.md) has every run, the charts, and the caveats (variance is large).

## Docs

| Doc | In brief |
|---|---|
| [Getting started](docs/getting-started.md) | Install, pick a decision model, run your first task, stay safe. |
| [Decisions (JEV)](docs/decisions.md) | The decision model, its backends and accuracy, and every choice it makes. |
| [The agent loop](docs/agent.md) | Routing, turns, tools, hooks, guards, context, stopping, local models. |
| [The team (fleet)](docs/fleet.md) | Roles, resources, autonomy modes, the comfort gate, cycles, shadow mode. |
| [SOPs](docs/sops.md) | Format, tests, layers, your own private folders. |
| [Learning](docs/learning.md) | How repeated work becomes an SOP, and when it is worth one. |
| [Sharing SOPs](docs/sharing.md) | The three tiers, where tests go, safety checks, pulling from the registry. |
| [Experiments](docs/experiments.md) | Features behind flags, and the A/B evidence for each. |
| [Benchmarks](docs/benchmarks.md) | The tasks Rameness is measured on, and the results. |
| [API and monitoring](docs/api.md) | The manager's HTTP API (chats, turns, client tools, events), metrics and the dashboard. |
| [Architecture](docs/architecture.md) | Diagrams of how it all works, and where everything lives in the code. |
| [Iterations](docs/iterations.md) | What changed between versions, and whether each change reached its runs. |

## Acknowledgements

The team orchestration took inspiration from firstmate. Decision models:
[Clef-Flash](https://huggingface.co/Cloudflare/clef-flash) (Cloudflare), [Kev](https://github.com/jaredpalmer/kev)
(Jared Palmer), [Laya](https://github.com/NandhaKishorM/laya) (Convai Innovations) and
[TypeSafe AI's Jev](https://docs.typesafe.ai/api).

## Trade-offs

The design choices behind the badges at the top, named after the patterns and trade-offs they are.

| | Trade-off | What it means here |
|---|---|---|
| Advantage | **Separation of concerns** | Judgement calls (route, effort, thinking level, recovery, "ask a person?") go to a small typed decision model; the LLM only generates. Decisions are fast, calibrated and logged. |
| Advantage | **Strategy pattern** | The decision model (Clef-Flash, Kev, Laya, lexical) and the LLM provider are chosen by configuration, behind one interface. |
| Advantage | **Graceful degradation** | If the chosen decision model is unreachable, Rameness falls back down the chain to a lexical scorer and warns, instead of stopping. |
| Advantage | **Local-first** | Runs entirely on models you host (vLLM, llama.cpp); hosted APIs are optional. |
| Advantage | **Memoization** | Repeated multi-step jobs are learned as tested SOPs and called as tools, so known work isn't re-derived (44% less task time in the projreport benchmark). |
| Disadvantage | **Operational complexity** | A second model service, the decision model, has to run alongside the LLM; Clef-Flash needs a GPU with 16 GB. |
| Disadvantage | **Amortized cost** | An SOP costs time and tokens up front (spotting the repeat, writing and testing it) and pays back only on work that repeats: once learned, a repeated task took 44% less time, while one-off work never earns the cost back. |
| Disadvantage | **Implicit trust boundary** | The HTTP API has no authentication and relies on binding to 127.0.0.1; put an authenticating proxy in front before exposing it. |

## License and disclaimer

Rameness is licensed under the [Apache License 2.0](LICENSE). Copyright 2026 Prog-Ramen.

Rameness is provided **as is, without warranty of any kind**. It runs AI agents that execute commands, edit files
and use the network on the machines you give it. You are solely responsible for how you configure and use it, for
reviewing what its agents and SOPs do, and for the consequences of their actions, including data loss, security
incidents and charges from model providers. To the maximum extent permitted by law, the authors, contributors and
Prog-Ramen are not liable for any damages arising from its use. See [NOTICE](NOTICE) and sections 7 and 8 of the
license.
