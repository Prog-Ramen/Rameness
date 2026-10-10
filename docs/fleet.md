# The team (fleet)

**In brief:** a manager runs a team of agents (leads and associates), each on its own model, runtime and
machine, in its own git worktree. JEV makes the team's decisions; the comfort gate hands the significant
ones to you, the director.

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

## Roles

Managerial roles: **director** (you), **manager** (root coordinator), **leads** (sub-managers that own a
subtree), **associates** (do one task). Agents form a tree; `depends` edges make it a DAG; forks are
parallel attempts at the same task.

## Resources: runtime, model, machine

Each agent gets three independently chosen resources.

| Resource | Options | Chosen by |
|---|---|---|
| runtime | built-in Rameness agent, or a CLI agent on PATH: claude, codex, pi, opencode, aider, gemini, goose | JEV over slot traits + track record |
| model slot | Anthropic / OpenAI-compatible APIs, **llama-server** (reads real `/props` slots), ollama, vLLM, LM Studio on any port | JEV; capacity is a hard limit |
| environment | `local`, `ssh` hosts, `container` exec (docker/podman), any `exec` prefix (kubectl…), probed for CPUs, RAM, GPUs | JEV over capabilities; GPU needs are a hard filter |

The executing model runs independently of the environment: an agent on a local llama-server can drive
tools on a remote GPU box over ssh. Configure in `~/.rameness/fleet.json` or `.rameness/fleet.json`:

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

## Sessions, operations, merging

Every agent can be watched or taken over, the team survives restarts, and work merges back by branch.

* **Sessions.** Each agent lives in its own session: herdr tab (socket API, agent-aware status), tmux
  window, screen session, or headless subprocess. `rameness fleet attach <id>` drops you in.
* **Operations.** `ask · spawn · show · tree · prompt · reassign (--slot/--env) · pause · resume · fork ·
  retire · rm · logs · attach`, the same in the UI and at `POST /api/agents/...`. State lives in SQLite
  (`.rameness/fleet.db`), so the manager, the UI and every worker can restart without losing the team.
* **Merging.** Deliver agents work on `rameness/<id>` branches in their own worktrees; children merge into
  their lead's branch; top-level work is merged per `mode`: `review` (you approve), `local`, or `review` +
  `autonomous`.

## Autonomy modes

How much JEV decides on its own. Switch in the UI or with `rameness fleet mode <mode>`.

| Mode | Who decides |
|---|---|
| **Restrictive** | You make every *work* decision (intake, roles, forks, cycle focus, which proposals or findings to act on, merges). Agents never reach you directly: the manager relays their questions. Mechanical choices (model, environment) still go through the comfort gate. |
| **Balanced** (default) | JEV decides; the comfort gate hands you significant or uncertain decisions. |
| **Autopilot** | JEV decides everything, within limits: requested cycle counts (plus at most `autopilot_extra_cycles`), capacity, depth. The shadow view marks decisions it *would* have asked about. |
| **Godmode** | Autopilot plus open-ended cycles: JEV picks the next kind of cycle and decides when the work has converged. Off unless `"allow_godmode": true`; optional `godmode.max_cycles` cap. |

## The comfort gate

Every fleet decision asks: is this routine enough to make alone?

1. If you already ruled on this exact decision, use your answer.
2. JEV scores the options.
3. JEV checks itself: *is this significant (irreversible, costly, external, a preference) or too uncertain
   (a near-tie on something non-trivial)?* If so it becomes a **Needs you** item with JEV's
   recommendation and probabilities, and that piece of work waits. Your answer is applied and becomes a
   training label.

Thresholds live in `gate`; `decision_policy` pins any question to `jev` (never ask) or `director` (always ask).

## Cycles: refinement and testing

Ask for N rounds of improvement or testing; JEV picks each round's focus and judges when it's done.

```bash
rameness fleet ask "build a habit tracker" --cycles 8                    # draft, then 8 cycles; JEV picks each focus
rameness fleet ask "the habit tracker" --cycles 6 --focus testing --on HEAD   # 6 testing cycles on existing work
rameness fleet ask "the app" --cycles 4 --focus ui,ux,accessibility
rameness fleet mode godmode && rameness fleet ask "the app" --cycles godmode
rameness fleet categories        # the taxonomy (ids and groups)
rameness fleet programs          # progress, per-cycle focus, choices, findings
```

The taxonomy (36 kinds in 7 groups: Discovery, Build, Quality, Testing, Interface, Non-functional,
Delivery) is also what JEV uses to categorize any task. Two cycle shapes:

- **Refine**: a research agent proposes options as JSON, JEV selects (multi-select, gated), and an
  improvement agent implements them on top of the previous cycle's branch.
- **Test** (unit, simulated user testing with personas, interface, accessibility, API contract,
  exploratory/edge-case, regression, performance, security, acceptance, compatibility): the model writes
  and runs the tests and reports findings by severity. JEV judges whether they are *the right kind of
  tests and cover edge cases* (if not, it sends the tester back once with the gaps), picks which findings
  to fix, and a fixer agent fixes them and adds regression tests.

Cycles chain on each other's branches and the result is merged once at the end. When the requested count
is done, JEV decides whether more of that work is needed: autopilot may extend within its cap, and
balanced or restrictive mode brings JEV's recommendation to you.

## Shadow mode

Watch JEV decide, live.

The UI's shadow strip and **Shadow** tab show every decision, its options and probabilities, whether JEV
decided alone, deferred to you, or you decided, its significance, and the agent it concerned (highlighted
in the org chart as it happens). `rameness fleet shadow -f` is the terminal equivalent.

## Improving JEV

Your corrections become better decisions.

The **Improve** tab (and `improve.py`): mark decisions right or pick what should have won; agent outcomes
are recorded automatically; **Calibrate** re-weights options deterministically; **Ask a model to
improve** proposes new option cues for you to apply or reject; **Export** writes a labelled JSONL
training set for fine-tuning Laya on your own decisions. Tuning hot-reloads into the running JEV.

## Status

Tested end to end with tmux, screen and subprocess sessions, the exec-prefix environment path, CLI-agent
and Rameness runtimes (including a real `claude -p` tester run), a local Ollama model, and the UI in
headless Chrome. The herdr backend follows herdr's documented socket API but has not been run against a
live herdr server yet; ssh and container environments share the tested exec path but have not been run
against a real host.
