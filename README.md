<div align="center"><img src="rameness_logo.png" alt="Rameness Logo" width="28%"></div>
<div align="center"><img src="rameness_txt1.png" alt="Rameness Text" width="75%"></div>


**An agent harness where a decision model, not an LLM, makes the calls, and that gets better with use.**

Other harnesses (Claude Code, Codex, pi) hard-code their choices: whether a task needs reasoning, which
procedure applies, which model and machine should do the work, what to do when something fails.
Rameness hands those choices to a small typed decision model (JEV: Kev or Laya running locally, or
TypeSafe AI's hosted Jev) that returns calibrated probabilities in one pass. Before a decision takes
effect, JEV asks itself whether it is routine enough to make alone or significant enough for you, and
you can watch every decision live.

It improves without retraining the model that does the work:

* **It learns procedures.** Tasks it repeats become tested SOPs that later runs call as code instead of
  re-deriving them, and the useful ones are shared.
* **Its decisions get better.** Every decision is logged with its outcome, to calibrate JEV and to
  fine-tune it.
* **It refines its own work.** Refine and test cycles let JEV pick the next improvement and decide when
  the work is done.

## Quick start

```bash
curl -fsSL https://raw.githubusercontent.com/Prog-Ramen/Rameness/main/install.sh | bash
rameness jev up                          # start the decision model
cd your-project && rameness init
rameness run "fix the failing test"      # one agent
rameness up                              # or a whole team: manager + UI on http://127.0.0.1:7788
```

Any model works: Anthropic, OpenAI, DeepSeek, ollama, or any OpenAI-compatible server
(`rameness --base-url http://localhost:8080/v1 run "..."`). [Getting started](docs/getting-started.md)
covers choosing a decision model and every command.

> **Safety:** tools act on your machine as you, and ask before each action by default. Use `-y` (approve
> everything) only somewhere disposable. The UI has no authentication and binds to 127.0.0.1.
> [More](docs/getting-started.md#safety).

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

## Results

On a 150-minute open-ended build (a Minecraft clone in the browser, 14 behaviour checks plus a visual
grade), Rameness's best runs with local models match Claude Code with Claude Opus:

| Harness + model | Behaviour | Visual (0-10) |
|---|---|---|
| Claude Code + Claude Opus 5.5 (reference) | 100% | 7.25 |
| Rameness + Qwen3.8-27B UD-Q4_K_M (llama.cpp) | 100% | 7.25 |
| Rameness + Qwen3.8-Flash-Next AutoRound 3bpw (vLLM) | 92.9% | 7.5 |

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
| [Architecture](docs/architecture.md) | Where everything lives in the code. |
| [Iterations](docs/iterations.md) | What changed between versions, and whether each change reached its runs. |

## Acknowledgements

The team orchestration took inspiration from firstmate. Decision models:
[Kev](https://github.com/jaredpalmer/kev) (Jared Palmer), [Laya](https://github.com/NandhaKishorM/laya)
(Convai Innovations) and [TypeSafe AI's Jev](https://docs.typesafe.ai/api).
