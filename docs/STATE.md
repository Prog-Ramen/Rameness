# Rameness: current state

A snapshot to pick up from later: what exists, how it fits together, what is verified, what is
pending, and the rules to keep when changing it. The README is the user guide; this document is
for whoever continues the work.

_Last updated: 2026-09-23. Update it whenever a PR merges._

---

## 1. What Rameness is

Rameness is an agent harness and a team manager in which a **JEV decision model** makes the
multiple-choice and multi-probability calls that other harnesses (claude-code, codex, pi,
firstmate) hard-code:

- whether a task needs a model at all
- which SOPs (tested, parameterised procedures) apply
- which model, agent runtime and machine run each agent
- when to fork, retry, stop a looping agent or merge
- which refinements or tests a cycle should do
- whether an SOP is safe to share publicly

Before any decision takes effect, JEV checks whether it is routine enough to make alone or
significant or uncertain enough for the user (the *comfort gate*). Every decision is visible live
in the UI's *shadow* view.

**JEV itself is external.** Its real API is not known yet. Rameness talks to it through
`HttpJev` (`rameness/jev.py`) with an assumed contract:

- request: `POST {mode, question, query, context, options:[{id, text}]}`
- response: `{probs: {id: p}}`

Until a real JEV is plugged in, decisions come from a keyword scorer, optionally backed by an LLM
(`cascade`). If the real JEV's API differs, change `HttpJev` only; call sites don't need to change.

## 2. Repositories, branches and PRs

| Repo | Purpose | State |
|---|---|---|
| [Prog-Ramen/Rameness](https://github.com/Prog-Ramen/Rameness) | harness code (package `rameness`) | PR #1 **merged**. PR #2 `sop-review-and-relay` **open** (shareability, intake, relay, fixes) |
| [Prog-Ramen/RamenSOPs](https://github.com/Prog-Ramen/RamenSOPs) | public registry of reviewed, general SOPs | `main` has only the README (sharded-index docs, PR #1 merged) and a secret-scan workflow. **No SOPs yet.** |
| `Prog-Ramen/RamenSOPs-intake` | private review queue for proposed SOPs | **does not exist yet** (see §9) |

Conventions:

- Commits are authored as `ssajnani <samarsajnani@gmail.com>`, with **no Claude co-author
  trailers** and no Claude attribution in PR text.
- New work goes on a branch off `origin/main`. The user sometimes edits `main` directly (e.g. the
  README title "Rameness"), so rebase before opening a PR.
- The project was renamed from *jevness* to *rameness*. No references to the old name remain in
  the code or on GitHub, but the local checkout directory is still `/root/jevness`.

## 3. Verification status

**Verified**

- **Unit tests:** 67 pass (`python -m unittest discover tests`), about 20 s. They cover:
  - harness: routing, SOPs, learning, context, loop guard
  - fleet: full lifecycle, CRUD, capacity limits, hierarchy, forks, comfort gate, autonomy modes,
    refine and testing cycles
  - SOP registry: classification, propose, release, pre-push hook
  - lazy remote pulls: only explored branches fetched, hash mismatch, permission guard, failing
    tests, offline registry
  - encrypted relay, including a ciphertext-only issue body
  - CLI reachability
- **End-to-end runs:**
  - a local Ollama model (qwen 35B) fixed a bug in its worktree inside a tmux session, and the
    manager committed and merged it
  - the UI was checked in headless Chrome against a live demo fleet: team, shadow, "Needs you",
    capacity
  - a real `claude -p` session ran as a CLI-agent tester, and the cycle engine parsed its report
- **Session backends** tmux, screen and subprocess, and the command-prefix environment path.
- **Shareability eval** (`evals/shareability.py`) with the local LLM as JEV: **0 leaks** on the
  subtle business-rule cases (churn window, partner discount, quarterly KPI). One general SOP was
  over-blocked (`MAX_RETRIES = 5`), which is the safe direction. Keyword-only JEV marks everything
  ambiguous or private by design.

**Not yet exercised live**

- The herdr session backend (written against herdr's documented socket API; herdr isn't installed here).
- Real ssh and container environments (they share the tested command-prefix code path).
- The Anthropic API (no credentials were available on the dev machine).
- The GitHub side of the SOP flow: intake repo, relay workflow, release workflow (§9).

## 4. Architecture map

```
rameness/
  jev.py            JEV engine. Backends: lexical | llm | http | cascade | cascade-http.
                    Comfort gate, tuning hot-reload, decision log (.rameness/decisions.jsonl).
  router.py         Task triage: route (direct/answer/agent/clarify), effort, direct SOP args,
                    remote SOP pulls on a coverage gap.
  harness.py        Single-agent loop: tools, context, loop guard, learning hook, fleet hooks.
  loopguard.py      Stuck/degenerate-loop detection; JEV picks continue|reorient|reset|stop.
  context.py        Artifact store + recall; JEV-ranked eviction (nothing deleted).
  llm.py            Anthropic SDK + OpenAI-compatible (llama-server, ollama, vLLM, DeepSeek);
                    prompted tool-call fallback for servers without native tools.
  tools.py          bash/read/write/edit/grep; environment-aware (local/ssh/container/exec).
  sops.py           Hierarchical SOP library, activation traversal with defer, executor.
  learning.py       Trace mining -> SOP generation -> tests -> classification.
  improve.py        JEV feedback, calibration, model-proposed cue tuning, training export.
  org.py            Private org profile (facts, defaults, glossary, private_terms).
  publish.py        Scrubber, secret scanners, detail extraction, JEV shareability
                    (shareable|ambiguous|private), sharded index builder, local publish.
  registry.py       Private intake -> public release, visibility checks, pre-push hooks,
                    sign-off, `submit` via relay, intake scaffold.
  relay.py          Encrypted submission relay (RSA-OAEP + AES-GCM), safe unpack, receive.
  remote.py         Lazy, activation-driven pulls of single SOPs from a sharded registry.
  config.py         Layered config (defaults <- ~/.rameness/config.json <- ./.rameness/config.json).
  cli.py            `rameness` command.
  builtin_sops/     Starter SOPs (http, data, fs, git, dev, resolve). Ship with the harness only.
  fleet/
    manager.py      The manager: Fleet.decide (all fleet decisions), gate, autonomy modes, CRUD,
                    scheduling, supervision, forks, merges, background model calls.
    cycles.py       Cycle programs (refine / test), the 36-kind work taxonomy.
    worker.py       Process for a jevness-runtime agent (ask_manager, inbox, transcripts).
    slots.py        Model/runtime discovery (API keys, local ports, CLI agents on PATH).
    envs.py         Environments + capability probes (CPUs, RAM, GPUs).
    sessions.py     herdr / tmux / screen / subprocess backends.
    worktree.py     Per-agent git worktrees, commits, merges.
    store.py        SQLite state (agents, events, decisions, escalations, messages, settings).
    server.py       JSON API + UI server (127.0.0.1, no auth).
    ui/index.html   Single-page UI (Team, Capacity, Shadow, Improve, Needs you).
evals/shareability.py   Labelled leak check for JEV's shareability decisions.
install.sh              One-environment installer (~/.rameness/env + `rameness` launcher).
```

## 5. Invariants: keep these when changing things

1. **Every fleet decision goes through `Fleet.decide` / `decide_many`**, never a raw `jev.choose`.
   That is what applies the autonomy mode, the comfort gate and decision policies, and what feeds
   the shadow view. In-agent decisions (loop guard, SOP pulls) report through the `on_decision` hook.
2. **Deterministic guards are never overridden by a probability:** capacity, GPU requirements,
   permissions (`sop_allow`), merge policy, secret scans, repo visibility checks.
3. **No model calls inside `Fleet.tick()`.** Use `Fleet._background(...)`. One slow planner once
   stalled every program.
4. **SOPs are private by default.** Only a *model-backed* JEV can mark an SOP `shareable`, and only
   at ≥ `share_confidence` (0.85) with nothing flagged. Uncertainty means `ambiguous` and needs the
   contributor's sign-off. Secret findings can never be overridden.
5. **Nothing reaches RamenSOPs without private review** (the intake repo) and a re-scan on release.
   GitHub has no private PRs on public repos, and forks of public repos are public.
6. **Remote pulls fetch only what a task needs**: sharded listings for explored branches, then only
   the chosen SOP's files, hash-verified, and kept only if their tests pass.
7. **Demos and tests must not auto-discover real CLI agents** (`"discover": false`). A demo once
   started a real `claude -p` session.

## 6. Key flows (short)

- **Single task** (`rameness run`):
  1. JEV walks the SOP tree.
  2. If local SOPs don't cover the task, JEV lazily walks the remote registry and pulls chosen SOPs.
  3. JEV picks the route and effort; the gate may ask the user.
  4. The task runs (direct SOP, one answer call, or the agent loop with the loop guard).
  5. The learner turns repeated work into SOPs, classified for shareability.
- **Team** (`rameness fleet …`, `rameness up`):
  1. Intake decision.
  2. Decompose into leads and associates.
  3. Per agent, JEV picks runtime × model slot × environment.
  4. Each agent gets a session and a worktree.
  5. Supervision handles stalls, failures, forks, questions and merges.
  - Autonomy: `restrictive` | `balanced` | `autopilot` | `godmode` (godmode needs `allow_godmode`).
- **Cycles:** a draft, then N cycles or godmode.
  - Refine: research → JEV selects → implement.
  - Test: the model writes and runs tests → JEV judges test adequacy (sends the tester back once) →
    JEV picks findings to fix → fix.
  - When the requested count is done, JEV decides whether more is needed. It asks the user in
    balanced/restrictive mode and extends within `autopilot_extra_cycles` in autopilot.
- **Sharing an SOP:**
  1. `rameness sop classify` shows the verdict.
  2. Members run `rameness sop propose`, which opens a PR in the private intake repo. Outsiders run
     `rameness sop submit`, which posts a ciphertext-only issue that the relay turns into an intake PR.
  3. Internal review.
  4. The intake repo's `release-to-public` workflow opens a release PR on RamenSOPs.

## 7. Configuration and files

| What | Where |
|---|---|
| Config | `~/.rameness/config.json`, `./.rameness/config.json` (see `DEFAULTS` in `config.py`) |
| Fleet config | `~/.rameness/fleet.json`, `./.rameness/fleet.json` (see `FLEET_DEFAULTS` in `fleet/manager.py`) |
| Private SOPs | `./.rameness/sops`, `~/.rameness/sops` |
| Pulled public SOPs | `~/.rameness/public/<registry>/sops` |
| Org profile | `./.rameness/org.json`, `~/.rameness/org.json` |
| Fleet state | `./.rameness/fleet.db` (SQLite, WAL) |
| JEV decision log / tuning | `./.rameness/decisions.jsonl`, `jev_tuning.json`, `jev_proposals.json` |
| Installed environment | `~/.rameness/env` (installer) |

Knobs that matter most:

- `jev.backend` (use `cascade` or `http` for real decisions) and `jev.llm_timeout`
- `permissions.mode`, `permissions.sop_allow`
- `registry.staging` (intake), `registry.public`, `registry.relay_key`, `registry.relay_repo`,
  `registry.auto_pull`, `registry.coverage_margin`
- fleet: `autonomy`, `allow_godmode`, `gate`, `decision_policy`, `mode` (merge policy), `max_active`,
  `discover`, `manager_slot`

## 8. Running and testing

```bash
python -m venv .venv && .venv/bin/pip install -e .      # or ./install.sh
.venv/bin/python -m unittest discover tests              # 67 tests
.venv/bin/python -m evals.shareability                   # keyword JEV: expect 0 leaks, everything asked
.venv/bin/python -m evals.shareability --llm http://127.0.0.1:11434/v1 --model <model>   # model-backed
rameness plan "..."          # routing + SOP traversal, no execution
rameness up                  # manager + UI at http://127.0.0.1:7788 (deep links: #shadow #improve #needs)
```

On a CPU-only machine a local 35B model takes about a minute per JEV decision; use a smaller
model or a GPU for anything interactive.

## 9. Pending setup (GitHub; needs PR #2 merged first)

1. Create the private repo `Prog-Ramen/RamenSOPs-intake` and a `@Prog-Ramen/sop-reviewers` team.
   Scaffold the repo with `rameness sop registry-init <dir>` (README, CODEOWNERS, PR template,
   secret-scan CI, release-to-public workflow) and push it.
2. In a RamenSOPs checkout, run `rameness sop relay-init .`. It adds the relay workflow and the
   public key and prints the path of the generated private key. Store these RamenSOPs secrets,
   then delete the local private key:
   - `INTAKE_PRIVATE_KEY`: that private key
   - `INTAKE_RELAY_TOKEN`: contents + pull requests write on intake, issues write on RamenSOPs
3. Add `RAMENSOPS_RELEASE_TOKEN` to the intake repo: contents + pull requests write on RamenSOPs.
4. Optionally install gitleaks and trufflehog locally; the scanners use them when present.

## 10. Known gaps and backlog

- **The keyword JEV is weak on subtle calls** (test adequacy, shareability, some routing). Real
  decisions need the served JEV or an LLM backend. The cascade only escalates options in the
  0.3–0.7 band.
- **Shareability eval:** keep extending `evals/shareability.py` and run it against every new JEV
  backend. Consider a "generalize" action that rewrites flagged constants into parameters.
- **Relay hardening:** per-submitter rate limiting; optionally delete relay issues (needs an admin
  token) rather than wiping their bodies; run submitted SOPs' tests in a sandboxed intake CI job.
- **Live verification:** herdr, ssh and container environments, and the Anthropic path.
- **UI:** no SOP-library or registry view yet; no auth on the server (keep it on 127.0.0.1).
- **Context eviction** edits earlier tool results. Models with *preserved thinking* reject edited
  history; for those, switch to server-side context editing.

## 11. Dev machine notes (the environment this was built in)

- WSL2 Ubuntu 24.04. RTX 5070 Ti (16 GB). Ollama on :11434 with a qwen 35B MoE that runs mostly
  on CPU.
- `gh` 2.101 at `~/.local/bin/gh`, logged in as ssajnani.
- Git over SSH with `~/.ssh/id_ed25519`. `~/.ssh/config` has `IgnoreUnknown UseKeychain`, added
  because the config came from GitHub's macOS instructions; the original is at `~/.ssh/config.bak`.
- Checkout at `/root/jevness`, venv at `/root/jevness/.venv` (editable install).
