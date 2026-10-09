# Architecture

**In brief:** a map of the code: the decision engine, the agent loop, SOPs and learning, sharing, and the
fleet.

```
rameness/
  jev.py            decision engine: Kev / Laya / TypeSafe Jev backends (lexical offline fallback), comfort gate, tuning hot-reload
  jevserve.py       setup / start / stop of local Kev and Laya servers
  jevbench.py       `rameness jev bench`: accuracy of the active decision model on labelled harness decisions
  router.py         task triage: route, effort, direct SOP execution
  harness.py        agent loop: turns, stop/finish checks, group review/debug, feature cycles, note ledger, images, delegation, learning hook
  featurebranch.py  feature cycles on their own git branch and folder, merged only once verified
  thinking.py       per-turn thinking levels (JEV) → reasoning budgets / effort
  hooks.py          lifecycle hooks: JEV-selected SOPs run at on_start / after_output / before_finish / on_end
  progress.py       periodic progress review (JEV)
  loopguard.py      loop / degeneration detection and JEV-chosen recovery
  context.py        artifact store, recall, calibrated token counts, staged compaction
  llm.py            Anthropic SDK + OpenAI-compatible (llama-server, ollama, vLLM, DeepSeek), text tool protocol, sampling profiles, vision, server detection
  schemas.py        JSON schemas for every model call whose reply must be JSON
  tools.py          bash/read/write/edit/edit_lines/grep/glob/web_fetch, line anchors, images, environment-aware
  sops.py           hierarchical SOP library and private folders, traversal with defer, executor (local or remote env), isolated tests
  tree.py           keeps the SOP tree small per level: placement by walking it, split on overflow, fold, aliases
  sopsafety.py      security review: code finds where an input could run, the model judges, JEV decides
  builtin_sops/     starter SOP package, including code/syntax_check (the after_output hook)
  deps.py           the packages and programs an SOP may need (from its imports and commands)
  learning.py       end-of-run review → value check → SOP generation, extension, repair → validation
  discover.py       `rameness sop discover`: candidate SOPs from event logs
  improve.py        feedback, calibration, model-proposed cue tuning, training export
  publish.py        scrubber, secret scanning, personal-vs-general, category and built-in-vs-registry decisions
  registry.py       propose SOPs as PRs: built-in (Rameness repo, draft) or registry (RamenSOPs); test rules; pre-push hooks
  review.py         conservative local registry preflight; execute untrusted tests in a sandbox
  remote.py         lazy, activation-driven pulls of individual SOPs from a sharded registry index
  telemetry.py      optional metrics push (RAMENESS_TELEMETRY_URL)
  fleet/
    manager.py      the manager: JEV decisions, comfort gate, autonomy modes, CRUD, scheduling, supervision, merging
    cycles.py       cycle programs (refine / test), the work taxonomy
    worker.py       Rameness-runtime agent process (ask_manager, inbox, transcripts for resume/fork)
    slots.py        model / runtime discovery and capacity
    envs.py         local / ssh / container / exec environments and capability probes
    sessions.py     herdr, tmux, screen, subprocess backends
    worktree.py     per-agent git worktrees and merges
    store.py        SQLite state (agents, events, decisions, escalations, messages)
    server.py       JSON API + UI server
    ui/index.html   the UI
install.sh          one-environment installer
docs/               these docs
bench/              benchmarks (see Benchmarks)
examples/acme/      an organization profile and private SOPs
```

## How the pieces connect

A task enters the router; the agent loop runs it; learning and sharing follow.

1. `router.py` asks JEV for a route and activates SOPs from `sops.py`.
2. `harness.py` runs the loop: `thinking.py`, `llm.py`, `tools.py`, `hooks.py`, `loopguard.py`,
   `progress.py`, `context.py`.
3. At the end of a run, `learning.py` reviews recent runs and writes new SOPs into the private library.
4. `registry.py` (with `publish.py`) proposes validated, shareable SOPs; `remote.py` pulls them back in
   for later tasks.
5. In a team, `fleet/manager.py` runs many such agents, each in its own worktree, session and
   environment.
