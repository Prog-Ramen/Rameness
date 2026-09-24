<div align="center"><img src="rameness_txt.png" alt="Rameness Text" width="100%"></div>


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

### Autonomy modes

| Mode | Who decides |
|---|---|
| **Restrictive** | You make every *work* decision (intake, roles, forks, cycle focus, which proposals or findings to act on, merges). Agents never reach you directly: the manager relays their questions (firstmate style). Mechanical choices (model, environment) still go through the comfort gate. |
| **Balanced** (default) | JEV decides; the comfort gate hands you significant or uncertain decisions. |
| **Autopilot** | JEV decides everything, within limits: requested cycle counts (plus at most `autopilot_extra_cycles`), capacity, depth. The shadow view marks decisions it *would* have asked about. |
| **Godmode** | Autopilot plus open-ended cycles: JEV picks the next kind of cycle and decides when the work has converged. Off unless `"allow_godmode": true`; optional `godmode.max_cycles` cap. |

Switch in the UI or with `rameness fleet mode <mode>`.

### Cycles: refinement and testing

```bash
rameness fleet ask "build a habit tracker" --cycles 8                    # draft, then 8 cycles; JEV picks each focus
rameness fleet ask "the habit tracker" --cycles 6 --focus testing --on HEAD   # 6 testing cycles on existing work
rameness fleet ask "the app" --cycles 4 --focus ui,ux,accessibility
rameness fleet mode godmode && rameness fleet ask "the app" --cycles godmode
rameness fleet categories        # the taxonomy (ids and groups)
rameness fleet programs          # progress, per-cycle focus, choices, findings
```

The taxonomy (36 kinds in 7 groups: Discovery, Build, Quality, Testing, Interface, Non-functional,
Delivery) is also what JEV uses to categorize any task. There are two cycle shapes:

- **Refine**: a research agent proposes options as JSON, JEV selects (multi-select, gated), and an
  improvement agent implements them on top of the previous cycle's branch.
- **Test** (unit, simulated user testing with personas, interface, accessibility, API contract,
  exploratory/edge-case, regression, performance, security, acceptance, compatibility): the model
  writes and runs the tests and reports findings by severity. JEV judges whether they are *the right
  kind of tests and cover edge cases* (if not, it sends the tester back once with the gaps), picks
  which findings to fix, and a fixer agent fixes them and adds regression tests.

Cycles chain on each other's branches and the result is merged once at the end. When the
requested count is done, JEV decides whether more of that work is needed: autopilot may extend
within its cap, and balanced or restrictive mode brings JEV's recommendation to you.

### The loop guard

Small and heavily quantized models get stuck: they repeat the same tool call, oscillate between two
actions, repeat an error, restate themselves, or produce degenerate text. Every turn the harness
computes these signals without any model call, and when one fires JEV chooses `continue`,
`reorient` (restate the goal, what was tried, what not to repeat), `reset` (fresh context with
distilled notes), or `stop` (fail the run so the manager can retry, e.g. on a stronger model).
Interventions escalate if the loop persists, and they appear in the shadow view.

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

## Private vs public SOPs

Every learned or agent-saved SOP starts in the **private** project library (`.rameness/sops`) and
stays there unless you act. What leaves the machine, and how:

```
private library ──propose──► PRIVATE staging repo ──review + merge──► release ──► public RamenSOPs
  JEV: personal vs general     verified not public before each push     re-scanned    github.com/Prog-Ramen/RamenSOPs
  scrubber + secret scanners   pre-push hook re-scans every push
```

* **Personal vs general.** When an SOP is registered, the scrubber runs first. Any secret, email,
  private host or IP, home path, or `private_terms` match makes it `private`. Otherwise JEV decides
  personal vs general, and it has to be clearly confident (≥ 0.65, margin ≥ 0.15) to mark an SOP
  `shareable`; anything uncertain stays private. `rameness sop classify` shows or redoes this.
* **Hidden until merged.** A PR to a public GitHub repo is public as soon as its branch is pushed,
  and forks of public repos are public too. So proposals go to a separate **private staging repo**.
  `rameness sop propose <id>` pushes a sanitized copy (origin, stats and classification stripped) on
  a `sop/<id>-…` branch there and opens the review PR (`gh`, if installed). It refuses unless the
  staging repo is verifiably not public (checked through `gh`, the GitHub API, or as a local path).
* **Secrets never leave.** Scrubber findings block a proposal, and secret-class findings (keys,
  tokens, private keys, JWTs, credentials in URLs) cannot be overridden. gitleaks and trufflehog
  also run when installed. Every clone rameness manages gets a **pre-push hook** that re-scans the
  whole tree, so a manual `git push` of a secret is blocked too.
* **Release.** After a staging PR is merged, `rameness sop release` re-scans the merged SOPs and
  opens a release PR on the public repo (or pushes directly with `"release": "direct"`), with a
  sharded, metadata-only index for discovery (see below). `rameness sop registry-init <dir>` scaffolds a staging
  repo with a gitleaks CI workflow.

```json
"registry": {"staging": "git@github.com:Prog-Ramen/RamenSOPs-staging.git",
             "public": "https://github.com/Prog-Ramen/RamenSOPs.git", "release": "pr"}
```

### Pulling SOPs from the registry: only what a task needs

Nothing is mirrored. The registry index is **sharded per category**: `sops/index.json` lists only
the top-level categories, and every category has its own `_index.json` listing only its children.
When a task comes in:

1. **Local first.** The remote registry is consulted only when no local SOP clearly covers the task
   (best local activation < `activate_threshold` + `registry.coverage_margin`).
2. **Lazy traversal with the same activation criteria.** JEV scores the root categories in one call.
   Rejected branches are never downloaded; a branch whose `requires` aren't met is deferred without
   being opened; only explored categories have their `_index.json` fetched, and so on down the tree.
3. **Pull decision per candidate.** JEV makes the decision, then the comfort gate applies.
   Permissions outside `permissions.sop_allow` need your yes; with no user present the SOP is skipped.
4. **Verified install.** Only the chosen SOP's files are downloaded, each checked against the SHA-256
   in its listing, into `~/.rameness/public/<registry>/sops`. Its tests must pass, otherwise it is
   removed again.

`registry.auto_pull`: `gated` (default) | `ask` (always ask) | `off`. `rameness sop pull <id>` pulls
one SOP by walking only its ancestors' listings. Every pull decision appears in the shadow view.

The organization profile `org.json` is also private. It holds facts, `defaults` that resolve SOP-tree
requirements, a glossary, and the `private_terms` the scrubber enforces (see `examples/acme`).
`rameness sop search` / `sop remote <query>` / `sop install <git-url|dir>` discover and install
public packages; the builtin starter package ships in `rameness/builtin_sops`.

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
  loopguard.py      loop / degeneration detection and JEV-chosen recovery
  llm.py            Anthropic SDK + OpenAI-compatible (llama-server, ollama, vLLM, DeepSeek) with prompted tools
  tools.py          bash/read/write/edit/grep, environment-aware
  publish.py        scrubber, secret scanning, personal-vs-general classification, local publish, index
  registry.py       private staging → public release flow, visibility checks, pre-push hooks
  remote.py         lazy, activation-driven pulls of individual SOPs from a sharded registry index
  fleet/
    manager.py      the manager: JEV decisions, comfort gate, autonomy modes, CRUD, scheduling, supervision, merging
    cycles.py       cycle programs (refine / test), the work taxonomy
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
path, CLI-agent and rameness runtimes (including a real `claude -p` tester run), a local Ollama
model, and the UI in headless Chrome. The herdr backend follows herdr's
documented socket API but has not been run against a live herdr server yet; ssh and
container environments share the tested exec path but have not been run against a real host.
The server binds to 127.0.0.1 and has no authentication.

