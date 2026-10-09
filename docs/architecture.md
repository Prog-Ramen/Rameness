# Architecture

**In brief:** Rameness is an agent harness in which a small typed decision model (JEV) makes every
multiple-choice call, a language model does the thinking and writing, and tested procedures (SOPs) run as code.
The diagrams go from the whole system down to each mechanism; the code map is at the end.

## The whole system

Who talks to whom, from the person at the keyboard to the model servers and the public SOP registry.

```mermaid
flowchart TB
    you["You: CLI or web UI (localhost)"]
    subgraph fleet["Team (rameness up)"]
        manager["Manager<br/>fleet/manager.py"]
        gate{"Comfort gate:<br/>routine or significant?"}
        agents["Agents: leads and associates<br/>each = runtime x model slot x environment,<br/>own git worktree and session"]
    end
    subgraph agent["One agent (rameness run)"]
        router["Router<br/>route, effort, SOP search"]
        loop["Agent loop<br/>harness.py"]
        learn["End-of-run learning<br/>learning.py"]
    end
    subgraph jevbox["JEV: decision models (never an LLM)"]
        jev["jev.py<br/>choose / activate, logging, tuning"]
        backends["Clef-Flash :8010 | Kev :8008 | Laya :8000<br/>| Laya in-process | TypeSafe (hosted)"]
    end
    llm["Language models<br/>Anthropic, OpenAI, DeepSeek,<br/>llama.cpp, vLLM, ollama, ninfer"]
    subgraph lib["SOP library (layered folders)"]
        builtin["Built-in<br/>rameness/builtin_sops"]
        pulled["Pulled from the registry<br/>~/.rameness/public"]
        private["Private<br/>~/.rameness/sops, ./.rameness/sops,<br/>your folders"]
    end
    registry[("RamenSOPs<br/>public registry")]
    ramenessrepo[("Rameness repo<br/>built-in SOPs")]

    you --> manager
    you --> router
    manager --> gate
    gate -- "significant" --> you
    manager --> agents
    agents --> router
    router --> loop --> learn
    router -. "decisions" .-> jev
    loop -. "decisions" .-> jev
    manager -. "decisions" .-> jev
    learn -. "decisions" .-> jev
    jev --> backends
    loop --> llm
    learn --> llm
    router --> lib
    loop --> lib
    learn --> private
    private -- "proposal PR" --> registry
    private -- "draft PR" --> ramenessrepo
    registry -- "pull what a task needs" --> pulled
```

## One task, end to end

What happens between typing a task and getting a result.

```mermaid
flowchart TB
    task(["Task"]) --> route{"JEV: route"}
    route -- "plain question" --> answer["One model call, no tools"]
    answer -- "model reaches for a tool" --> search
    route -- "agent / direct / clarify" --> search["SOP search: JEV walks the tree<br/>level by level"]
    search -- "nothing local fits well" --> pull["Pull from RamenSOPs<br/>JEV decides, comfort gate, hash check, tests"]
    pull --> plan
    search --> plan{"Route"}
    plan -- "clarify" --> ask["Ask for the missing information"]
    plan -- "direct" --> direct["Run a validated SOP,<br/>no reasoning model"]
    plan -- "agent" --> turns["Agent loop: turns until done"]
    direct -- "fails" --> turns
    turns --> result(["Result"])
    turns --> review["End of run: model reviews the last 5 runs<br/>for repeated multi-step work"]
    review --> newsop["New or extended SOPs, finished and tested"]
    newsop --> share["Proposal pipeline: private, RamenSOPs or built-in"]
```

## One agent turn

The agent loop: JEV sets how hard to think, the model acts, code checks and guards keep it on track.

```mermaid
flowchart LR
    start(["Turn"]) --> think{"JEV: thinking level<br/>minimal 512 ... maximum 16384"}
    think --> mcall["Model call<br/>schema-constrained when JSON is needed"]
    mcall --> tools{"Tool calls?"}
    tools -- "no" --> stop{"JEV: finished,<br/>or stopped mid-step?"}
    stop -- "mid-step" --> start
    stop -- "finished" --> finish["Finish review:<br/>what was verified, how"]
    finish --> done(["Done"])
    tools -- "yes" --> run["Run tools: bash, files, grep, web,<br/>SOP tools (tested, trusted)"]
    run --> hooks["Lifecycle hooks<br/>e.g. syntax check after edits, edit guard"]
    hooks --> guards{"Guards"}
    guards -- "loop detected" --> lg{"JEV: continue,<br/>reorient, reset, stop"}
    guards -- "every 10 turns" --> pr{"JEV: on track,<br/>drifting, stalled"}
    guards -- "file burst / long investigation" --> gr["Group review /<br/>group debugging"]
    guards --> ctx["Context: compact before the window<br/>fills; note ledger re-injected"]
    lg --> ctx
    pr --> ctx
    gr --> ctx
    ctx --> start
```

## How a JEV decision is made

Every choice is a typed question to a decision model: probabilities over fixed options, no generated text.

```mermaid
flowchart LR
    q["Question + options<br/>(each option: keywords,<br/>description)"] --> state["State: only what this<br/>decision needs, fitted<br/>to the model's window"]
    state --> kind{"Kind"}
    kind -- "choose: pick one" --> choice["One Choice question<br/>probabilities sum to 1"]
    kind -- "activate: which apply" --> noul["One yes/no (Noul) question<br/>per option, in one request"]
    choice --> model["Decision model<br/>Clef-Flash / Kev / Laya / TypeSafe"]
    noul --> model
    model --> probs["Probabilities"]
    probs --> tune["Calibration weights<br/>from past outcomes"]
    tune --> comfort{"Fleet decisions:<br/>significant or uncertain?"}
    comfort -- "yes" --> you["Needs you<br/>(recommendation shown)"]
    comfort -- "no" --> act["Act"]
    act --> log[("decisions.jsonl<br/>+ outcomes:<br/>training data")]
    you --> log
```

## How an SOP is born

From repeated work to a tested, reviewed SOP, without outside help.

```mermaid
flowchart TB
    runs["Last 5 runs"] --> rev["Model: which multi-step tasks<br/>did I repeat?"]
    rev --> score{"Kev/JEV: reusable?<br/>combined with how often it repeated"}
    score --> worth{"Worth it?<br/>saves >= 500 tokens per use,<br/>and more than it costs to make"}
    worth -- "no" --> skip1["Left alone"]
    worth -- "yes" --> dup{"JEV: does an existing SOP<br/>already do it?<br/>(pairwise, against its code)"}
    dup -- "yes" --> skip2["Reuse it"]
    dup -- "no" --> close{"JEV: could a close SOP<br/>be extended?"}
    close -- "yes" --> extend["Extend it: old tests must still pass,<br/>no input dropped or newly required"]
    close -- "no" --> write["Model writes the script<br/>and its test inputs"]
    write --> fin
    subgraph fin["finish_sop"]
        direction TB
        f1["Run the tests; model sees real outputs<br/>and writes what each must assert"]
        f2["Repair script or tests, up to 3 rounds"]
        f3["Rules: every test asserts, 2+ concrete values,<br/>inputs typed; permissions declared from the code"]
        f4["Security review: code finds where inputs could run,<br/>model reports data-injection risks,<br/>JEV: safe / fix / private"]
        f1 --> f2 --> f3 --> f4
    end
    fin --> place["File it: JEV walks the tree"]
    place --> rebal{"Category over 8 SOPs?"}
    rebal -- "yes" --> split["Split into subcategories<br/>(model groups, JEV confirms)"]
    rebal -- "no" --> lib[("Private library")]
    split --> lib
    extend --> lib
```

## Finding and pulling SOPs

The tree stays small at every level, so a search touches a few small listings (Dremel-style).

```mermaid
sequenceDiagram
    participant R as Router / sop_search
    participant J as JEV
    participant L as Local SOP tree
    participant G as RamenSOPs (registry branch)
    R->>L: categories at this level
    R->>J: will this task need each child? (yes/no each)
    J-->>R: probabilities
    Note over R,L: open children above 0.3 (top 4),<br/>select SOPs at 0.55 or above, next level
    alt nothing local fits well
        R->>G: index.json (top level)
        R->>J: score children
        par every category opened at this level
            R->>G: category/_index.json (id, description, keywords only)
        end
        R->>J: score children of each opened category
        R->>G: _meta.json of the picked SOPs (hash pinned by the listing)
        R->>J: pull it? (then the comfort gate, extra permissions need your yes)
        R->>G: the SOP's files, each hash-checked
        R->>L: install, run its tests, keep only if they pass
    end
```

## How an SOP is shared

Domain-specific stays private; generalizable goes to RamenSOPs; super-generic becomes a built-in.

```mermaid
flowchart TB
    sop["Validated private SOP"] --> cls{"JEV: personal or general?<br/>(must be clearly general)"}
    cls -- "personal / unsure" --> keep["Stays private<br/>(.gitignore'd inside a git work tree)"]
    cls -- "general" --> checks["Checks: tests meet the registry's rules,<br/>saves >= 1000 tokens per use,<br/>secret / private-term scan,<br/>JEV judges soft findings,<br/>security review safe for this code"]
    checks -- "fail" --> keep
    checks --> dest{"Used in most runs<br/>and JEV: needed by almost every task?"}
    dest -- "yes" --> bi["Target: Rameness repo<br/>(built-in, draft PR)"]
    dest -- "no" --> reg["Target: RamenSOPs"]
    bi --> placein
    reg --> placein["Place it in the TARGET's tree<br/>(JEV walks its categories)"]
    placein --> crowd{"Category now over 8?"}
    crowd -- "yes" --> reorg["Reorganize it in the same PR<br/>(moved SOPs keep their old ids as aliases)"]
    crowd -- "no" --> push
    reorg --> push["Sanitized copy, pre-push hook scans<br/>every changed file, push, open PR"]
    push --> ci(["Registry / developer review"])
```

## The team

The manager runs agents on any mix of models and machines and hands you the significant decisions.

```mermaid
flowchart TB
    you(["You (director)"]) -- "ask, approve, override" --> m["Manager"]
    m -- "intake: JEV decides do / decompose / research / clarify" --> plan["Work items"]
    plan --> pick{"JEV per agent:<br/>role, runtime, model slot, environment"}
    pick --> a1["Associate<br/>Rameness runtime, local GPU model,<br/>ssh GPU box"]
    pick --> a2["Lead<br/>Claude Code runtime, API model,<br/>local machine"]
    a2 --> a3["Associates under the lead"]
    a1 -- "rameness/<id> branch" --> merge{"Merge per mode<br/>review / local / autonomous"}
    a3 --> a2
    a2 -- "lead branch" --> merge
    m -. "stall, failure, fork winner,<br/>cycle focus: JEV" .-> a1
    m -. "significant or uncertain" .-> you
    merge --> main(["Project"])
```

## Code map

```
rameness/
  jev.py            decision engine: Clef / Kev / Laya / TypeSafe backends (lexical offline fallback), comfort gate, tuning hot-reload
  jevserve.py       setup / start / stop of local Clef, Kev and Laya servers
  clef_serve.py     Clef-Flash behind the System One API (run in Clef's own environment)
  jevbench.py       `rameness jev bench`: accuracy of the active decision model on labelled harness decisions
  router.py         task triage: route, effort, direct SOP execution, registry pulls; skips the SOP search for plain questions
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
  sops.py           hierarchical SOP library and private folders, level-by-level search with defer, executor (local or remote env), isolated tests
  tree.py           keeps the SOP tree small per level: placement by walking it, split on overflow, fold, aliases
  sopsafety.py      security review: code finds where an input could run, the model judges, JEV decides
  builtin_sops/     starter SOP package, including code/syntax_check (the after_output hook)
  deps.py           the packages and programs an SOP may need (from its imports and commands)
  learning.py       end-of-run review → value check → SOP generation, extension → finish_sop (tests, repair, security)
  discover.py       `rameness sop discover`: candidate SOPs from event logs
  improve.py        feedback, calibration, model-proposed cue tuning, training export
  publish.py        scrubber, secret scanning, personal-vs-general, category and built-in-vs-registry decisions
  registry.py       propose SOPs as PRs: built-in (Rameness repo, draft) or registry (RamenSOPs); test rules; target-tree placement; pre-push hooks
  review.py         conservative local registry preflight; execute untrusted tests in a sandbox
  remote.py         lazy, activation-driven pulls of individual SOPs from a sharded, columnar registry index
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

The registry side, where proposals are reviewed and merged, is described in
[RamenSOPs' architecture](https://github.com/Prog-Ramen/RamenSOPs/blob/main/docs/architecture.md).
