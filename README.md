# rameness

**A JEV-driven agent harness and team manager.** A JEV decision model makes the
multiple-choice and multi-probability calls that other harnesses (claude-code, codex, pi,
firstmate) hard-code: whether a task needs reasoning, which procedures apply, which model,
agent runtime and machine should do the work, when to fork, what to do when something fails.
Before any decision takes effect, JEV asks itself whether it is routine enough to make alone,
or significant enough for you. Everything it decides is visible in a live **shadow** view.

```
                         you (director)
                               │  UI · CLI
                        ┌──────▼──────┐     JEV: intake · role · fork · slot · environment ·
                        │   manager   │          failure · stall · questions · fork winner
                        └──┬───────┬──┘     comfort gate → "Needs you" when significant/uncertain
                  ┌────────▼┐     ┌▼────────┐
                  │  lead   │     │associate│  each agent = runtime × model slot × environment
                  └┬───────┬┘     └─────────┘  in its own git worktree and session
            ┌──────▼┐ ┌────▼──┐                (herdr tab · tmux window · screen · headless)
            │assoc. │ │assoc. │
            └───────┘ └───────┘
```

## Install

```bash
curl -fsSL https://raw.githubusercontent.com/Prog-Ramen/Rameness/main/install.sh | bash
# or from a checkout:  ./install.sh [--with-herdr] [--prefix DIR] [--dev]
```

One self-contained environment (`~/.rameness/env`) with the harness, the fleet manager, the UI,
the builtin SOPs and both model SDKs. A `rameness` launcher goes in `~/.local/bin`.

## Quick start

```bash
cd your-project && rameness init
rameness fleet slots               # models: API keys, llama-server/ollama/vLLM/LM Studio ports, CLI agents
rameness fleet envs                # where agents can run: local + your ssh/container environments
rameness up                        # manager + UI → http://127.0.0.1:7788
rameness fleet ask "add rate limiting to the API and benchmark two approaches"
rameness fleet shadow -f           # follow JEV's decisions in the terminal
rameness run "fix the failing test"                          # single agent, no team
rameness --base-url http://localhost:8080/v1 run "..."       # any llama-server on a port
```

## The team (fleet)

Firstmate-style orchestration with managerial roles: **director** (you), **manager**
(root coordinator), **leads** (sub-managers that own a subtree), **associates** (do one task).
Agents form a tree; `depends` edges make it a DAG; forks are parallel attempts at the same task.

Each agent gets three independently chosen resources:

| Resource | Options | Chosen by |
|---|---|---|
| runtime | built-in rameness agent, or a CLI agent on PATH: claude, codex, pi, opencode, aider, gemini, goose | JEV over slot traits + track record |
| model slot | Anthropic / OpenAI-compatible APIs, **llama-server** (reads real `/props` slots), ollama, vLLM, LM Studio on any port | JEV; capacity is a hard limit |
| environment | `local`, `ssh` hosts, `container` exec (docker/podman), any `exec` prefix (kubectl…), probed for CPUs, RAM, GPUs | JEV over capabilities; GPU needs are a hard filter |

The executing model runs independently of the environment: an agent on a local llama-server can
drive tools on a remote GPU box over ssh. Configure in `~/.rameness/fleet.json` or `.rameness/fleet.json`:

```json
{
  "backend": "auto",
  "mode": "review",
  "slots": [{"id": "gpu-llama", "kind": "llama-server", "url": "http://10.0.0.7:8080", "traits": "local private"}],
  "scan_ports": [8090],
  "environments": [
    {"id": "gpu-box", "kind": "ssh", "host": "me@10.0.0.7", "workdir": "/srv/repo", "max_agents": 4},
    {"id": "sandbox", "kind": "container", "engine": "docker", "container": "dev", "workdir": "/work"}
  ],
  "decision_policy": {"*": "auto", "Which parallel attempt produced the best result (tests pass, complete, simplest)?": "director"}
}
```

**Sessions.** Every agent lives in its own session so you can watch or take over: herdr tab
(socket API, agent-aware status), tmux window, screen session, or headless subprocess.
`rameness fleet attach <id>` drops you in.

**CRUD.** `ask · spawn · show · tree · prompt · reassign (--slot/--env) · pause · resume · fork · retire · rm · logs · attach`,
the same operations in the UI and at `POST /api/agents/...`. State lives in SQLite (`.rameness/fleet.db`),
so the manager, the UI and every worker can restart without losing the team.

**Merging.** Deliver agents work on `rameness/<id>` branches in their own worktrees; children merge
into their lead's branch; top-level work is merged per `mode`: `review` (you approve), `local`,
or `review` + `autonomous`.

### The comfort gate

Every fleet decision runs through `Fleet.decide`:

1. If you already ruled on this exact decision, use your answer.
2. JEV scores the options.
3. JEV checks itself: *is this significant (irreversible, costly, external, a preference) or too
   uncertain (a near-tie on something non-trivial)?* If so it becomes a **Needs you** item with
   JEV's recommendation and probabilities, and that piece of work waits. Your answer is applied
   and becomes a training label.

Thresholds live in `gate`; `decision_policy` pins any question to `jev` (never ask) or
`director` (always ask).

### Shadow mode

The UI's shadow strip and **Shadow** tab follow JEV live: every decision, its options and
probabilities, whether JEV decided alone, deferred to you, or you decided, its significance,
and the agent it concerned (highlighted in the org chart as it happens).
`rameness fleet shadow -f` is the terminal equivalent.

### Improving JEV

The **Improve** tab (and `improve.py`) turns oversight into better decisions:
mark decisions right or pick what should have won; agent outcomes are recorded automatically;
**Calibrate** re-weights options deterministically; **Ask a model to improve** proposes new
option cues for you to apply or reject; **Export** writes a labelled JSONL training set for
fine-tuning an open-source JEV. Tuning hot-reloads into the running JEV.

## The single-agent harness

Every associate on the rameness runtime runs this loop, and `rameness run` runs it on its own:

```
task ─► Router (JEV) ──┬─ clarify  → ask only for information that changes which SOP applies
                       ├─ direct   → run a validated SOP script, no reasoning-model call
                       ├─ answer   → one model call, no tool schemas
                       └─ agent    → tool loop with only the pre-selected SOPs exposed
                                      ├ large outputs → artifact refs (recall)
                                      └ context pressure → JEV-ranked eviction (never deletes)
      ─► Learner (JEV + frequency) → new SOPs in the private library
```

Local models are first-class: any OpenAI-compatible server works (`--base-url`), and servers
or models without native tool calling automatically fall back to a prompted
`<tool_call>{json}</tool_call>` protocol.

### What the JEV decides in the harness

| Decision point | Call | Where |
|---|---|---|
| Which SOP categories/leaves will this task use (hierarchical traversal) | `activate` per tree level | `sops.activate` |
| Route: direct / answer / agent | `choose` | `router.Router.plan` |
| Reasoning effort: low / medium / high (maps to the model's effort setting) | `choose` | `router.Router.plan` |
| Which old observations are still relevant when context is full | `activate` | `context.ContextManager.compact` |
| Which trace steps are standard procedures worth scripting | `activate` + noisy-OR with frequency | `learning.Learner.candidates` |
| Does an SOP for this already exist (dedupe) | `activate` | `learning.Learner._duplicate` |
| Is a private SOP generic enough to publish (advisory only) | `choose` | `publish.generality` |

Backends (`jev.backend` in config):

* `lexical`: offline IDF token overlap. Free; settles the obvious cases.
* `llm`: the provider's fast model returns calibrated probabilities as JSON.
* `http`: your served JEV model. Contract: POST `{mode, question, query, context, options:[{id,text}]}` → `{probs:{id:p}}`.
* `cascade` (default): lexical first; only options that land in the uncertain band (0.3–0.7) go to the LLM.
* `cascade-http`: lexical first, then your served JEV.

Every decision goes to `.rameness/decisions.jsonl` (with `feedback` records for outcomes), which
gives you a training set for fine-tuning an open-source JEV on real harness decisions.

## SOP library

A directory tree, layered lowest precedence first:

```
rameness/builtin_sops/      public starter package (http, data, fs, git, dev, resolve)
~/.rameness/public/<pkg>/   installed public packages
~/.rameness/sops/           private: yours
./.rameness/sops/           private: this project / organization
```

* Category: a directory with `_node.json` (`description`, `keywords`, `requires`).
  `requires` lists the information needed to choose among the children, e.g.
  `data_source`. If neither the task, the org context nor the org defaults supply it,
  traversal **defers** at that node instead of guessing, and the router asks.
* Leaf: `sop.json` with a `kind` of:
  * `script`: `run.py`/`run.sh`; JSON args on stdin, JSON result on stdout
  * `composite`: steps over other SOPs with `${input.x}` / `${steps.N.field}` templating
  * `skill`: instructions injected into the prompt (for procedures too fuzzy to script)
* `inputs` is a JSON schema; `permissions` (`fs:read`, `network`, `exec`, `side-effect`, …)
  are enforced deterministically. Anything outside `permissions.sop_allow` needs approval.
  JEV confidence never grants permission.
* `tests` run before an SOP is marked `validated`. Only validated scripts can be run on the
  `direct` route; `candidate` SOPs are offered to the model but never auto-executed.

Selected SOPs are exposed to the model as normal tools (`sop_http__get`, …), and only those are
exposed. The model can pull in more with `sop_search`, or save a procedure it just worked out
with `sop_save` (tested before registration).

## Learning

After each task the trace is stored in `.rameness/runs/`. The learner:

1. mines step n-grams that recur across different runs (shape-normalised: paths, numbers and strings abstracted),
2. optionally has the fast model segment the trace into generic sub-procedures,
3. scores them with the JEV, combined with frequency: `p = 1 - (1 - p_jev)(1 - p_freq)`,
4. drops duplicates of existing SOPs,
5. generates the SOP: exact repeated shell sequences become a script with no model call;
   otherwise the fast model writes the script and tests,
6. registers it privately: `validated` if its tests pass, otherwise `candidate`
   (`rameness sop promote <id>` after review).

## Private vs public

* `org.json` (private) holds facts, `defaults` that resolve tree requirements, a glossary
  ("customer report" means …), and `private_terms`. See `examples/acme`.
* `rameness sop publish <id> --to <pkg>` scrubs for secrets, keys, emails, private IPs/hosts,
  home paths and your `private_terms`, then strips origin metadata and requires explicit
  confirmation. Nothing is published automatically.
* `rameness sop index <pkg>` writes a metadata-only `index.json`; `rameness sop remote <query>
  --index <url>` searches it without downloading code; `rameness sop install <git-url|dir>`
  installs a package.

## Usage

```bash
python -m venv .venv && .venv/bin/pip install -e .
rameness init                                   # ./.rameness with config.json + org.json
rameness plan "summarize sales.csv"             # show route + SOP traversal, no execution
rameness run "the tests are failing, fix the bug"
rameness chat
rameness sop tree | list | show ID | search Q | run ID --args '{...}' | test | promote ID
rameness --provider deepseek run "..."          # DEEPSEEK_API_KEY
rameness --provider ollama --model qwen3:14b run "..."
```

Providers: `anthropic` (default: `claude-opus-5` main, `claude-haiku-4-5` fast; adaptive
thinking, effort from the router, server-side refusal fallbacks), `openai`, `deepseek`, `ollama`,
or any OpenAI-compatible `base_url`. Permissions: `ask` (default), `auto` (`-y`), `readonly`.

## Tests

```bash
.venv/bin/python -m unittest discover tests
```

## Layout

```
rameness/
  jev.py            decision engine: backends (lexical, llm, http, cascade), comfort gate, tuning hot-reload
  router.py         task triage: route, effort, direct SOP execution
  harness.py        agent loop, context management, learning hook
  sops.py           hierarchical SOP library, traversal with defer, executor (local or remote env)
  learning.py       trace mining → SOP generation → validation
  context.py        artifact store, recall, JEV-ranked eviction
  improve.py        feedback, calibration, model-proposed cue tuning, training export
  llm.py            Anthropic SDK + OpenAI-compatible (llama-server, ollama, vLLM, DeepSeek) with prompted tools
  tools.py          bash/read/write/edit/grep, environment-aware
  publish.py        scrubbed publishing, registry index, install
  fleet/
    manager.py      the manager: JEV decisions, comfort gate, CRUD, scheduling, supervision, merging
    worker.py       rameness-runtime agent process (ask_manager, inbox, transcripts for resume/fork)
    slots.py        model / runtime discovery and capacity
    envs.py         local / ssh / container / exec environments and capability probes
    sessions.py     herdr, tmux, screen, subprocess backends
    worktree.py     per-agent git worktrees and merges
    store.py        SQLite state (agents, events, decisions, escalations, messages)
    server.py       JSON API + UI server
    ui/index.html   the UI
install.sh          one-environment installer
```

## Status

Tested end to end here with tmux, screen and subprocess sessions, the exec-prefix environment
path, CLI-agent and rameness runtimes, and a local Ollama model. The herdr backend follows herdr's
documented socket API but has not been run against a live herdr server yet; ssh and
container environments share the tested exec path but have not been run against a real host.
The server binds to 127.0.0.1 and has no authentication.

