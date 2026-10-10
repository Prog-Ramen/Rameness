# Rameness iteration history

**In brief:** Rameness went from 0% to 100% on the Minecraft benchmark between 2026-09-25 and
2026-10-07. This is the version-by-version record: what changed, why, how the next runs scored, and whether
each change actually reached them.

How Rameness changed between 2026-09-25 and 2026-10-07, what each change was meant to fix, how the runs that
followed scored, and whether each change actually reached those runs. Everything here comes from the run records
(copied into [`bench/minecraft/runs/`](../bench/minecraft/runs/README.md): scores, judge verdicts, event and request
logs), the launcher scripts and logs ([`bench/minecraft/launchers/`](../bench/minecraft/launchers/)), the reports
written during the runs ([`bench/minecraft/reports/`](../bench/minecraft/reports/)), and the Claude Code session
transcript that made the edits.

## The benchmark

- **Task:** "Build a Minecraft clone that runs in the browser" in an empty folder, served statically with no build
  step (`bench/minecraft/task.md`). One agent run, 150 minutes maximum.
- **Score:** 14 scripted behaviour checks in a real browser (loads, renders, moves, looks around with the mouse,
  jumps, breaks and places blocks, ...), as a percentage.
- **Visual:** a Claude judge rates one final screenshot from 0 to 10. A single screenshot is noisy, so read the
  visual score together with the screenshot (`screenshot.png`) and with repeat runs.
- **Run to run spread is large.** Identical code on the same model has scored 35.7% and 78.6% (occamy v24off and
  v24offb). Treat a single run as a sample, not a verdict.

Models over time:
- 09-25 to 09-30: **occamy-1.0** (the user's server) and **Qwen3.8-27B** on llama.cpp (`:8033`; from v16 on a
  TurboQuant `turbo4` KV build with a 150k window).
- 10-02 to 10-05: a model and runtime comparison with the harness frozen (section 3).
- 10-06 on: mostly Qwen3.8-27B (several quantisations and runtimes) and Qwen3.8-Flash-Next.

## 1. Baseline (2026-09-25)

Four harnesses on the same task before any of the work below:

| Harness | Opus 5.5 | Qwen3.8-27B | occamy-1.0 |
|---|---|---|---|
| Claude Code | **100%**, 13.7 min | 92.9% (`mc-main-1`), 100% (`mc-qwen-rb5k`) | 64.3% |
| Rameness (start) | - | 0% (`mc-main-1`), 64.3% (`mc-qwen-rb5k`) | 0%, then 42.9% (`mc-rameness-fix`) |
| pi | - | 0%, 50.0% | 64.3% |
| dsh | - | 0%, 78.6% | 14.3% |

Claude Code with Opus is the reference: 100%, visual 7.25, in under 14 minutes.

## 2. Rameness v3 to q1b (2026-09-25 to 09-30): occamy and Qwen3.8-27B

Times are UTC (the server clock was UTC until 10-06). "Changes" lists what went in *before* that version's runs.
Repeats (`b`, `c`, `d`) use the same code. `s` runs (v20s, v21s, v21sb) are occamy sampling tests on the same code:
temperature 0.6 and presence penalty 0, against the model card's 1.0 and 1.5.

| Ver. | Changes (before its runs) | occamy | Qwen 27B |
|---|---|---|---|
| v3 | Todo tool; larger context limits; system prompt rewritten with todos and finish checks; the explicit browser instruction replaced by "research and write your own test plan" | 64.3 / 2.0 | 85.7 / 2.75 |
| v5 | Loop signature over the full tool input; richer planning; one self-review; compaction follows the model's real window; `glob` and `web_fetch` tools; bash timeout cap | 14.3 / 0.0 | (ran v6 code, below) |
| v6 | Loop guard and progress review detect recurring errors | 64.3 / 1.25 | 85.7 / 1.75 (labelled "v5") |
| v7 | Stale-todo reminder; the model's reasoning kept across turns; environment block and working rules; high effort when P(high) > 10%; syntax diagnostics after every write/edit | 85.7 / 4.5 | - |
| v8 | Prompt: verify by observable output | - | 57.1 / 0.75 |
| v9 | Diagnostics via the machine's own toolchains, packaged as the `code.syntax_check` SOP; lifecycle hooks chosen by JEV; task-neutral prompt; `sop discover`; **per-turn thinking budgets** (5 levels, JEV decides) | 35.7 / 0.25 | 71.4 / 1.5 |
| v10-v12 | Edit guard (undo an edit that breaks a working file); per-model sampling profiles; "re-verify with the check that exposed the bug"; `edit_lines` anchored edits; re-read stubs; note ledger re-injected after compaction; `delegate` sub-agents (flag, off); compaction rewrite (calibrated estimate, reply reserve, 5 stages, overflow recovery); Kev in bf16 | 0 (v12, twice) | 50.0 / 1.75 (v12) |
| v13 | A failed `edit_file` shows the closest match with anchors; XML text tool-call format | - | 78.6 / 1.5 |
| **v14** | **Prompt: a check that replaces a part with a stand-in (stub, mock) says nothing about that part**; same question in the self-review | - | **100 / 6.0**; repeat v14b 85.7 / 3.5 |
| v15 | The self-review asks for a check that would actually catch the flaw | 35.7 / 0.25 | 100 / 3.0 |
| v16 | No harness change (v15 code; Qwen server moved to turbo4 KV, 150k window) | - | 92.9 / 4.75, finished in 89 min |
| v17 | Lenient `edit_lines` anchors; "change files with the file tools, not the shell" | 64.3 / 1.0 | 92.9 / 1.75 |
| v18 | **Vision**: the model can view images (screenshots); answers escalate to the agent route when the model reaches for tools | 50.0 / 1.5; v18b 28.6 / 0.25 | 78.6 / 1.75; v18b 92.9 / 3.25 |
| v19 | Prompt: keep a working version; the pre-finish review runs even without todos | 14.3 / 0.25 | 92.9 / 5.25 |
| v20 | Prompt: prefer tested libraries | 42.9 / 1.25; v20b 21.4 / 0; v20s 42.9 / - | **100 / 6.25**; v20b 85.7 / 5.5 |
| v21 | JEV decides whether to stop mid-step; empty-reply rule | 64.3 / 1.75; v21b 21.4 / 0; v21s 78.6 / 2.0; v21sb 42.9 / 1.0 | **100 / 6.5**; v21b 100 / 4.25; v21c 100 / 3.0; v21d **100 / 6.5** |
| v22 | Prompt: when a fix keeps failing, rebuild that part fresh; occamy default sampling set to 0.6 / 0 after the v20s/v21s tests | 50.0 / 1.25; v22b 14.3 / 0.25 | 100 / 6.25; v22b 71.4 / 1.0 |
| v23 | Fall back to text when a server fails on images; refuse a `pkill`/`killall` that would kill the agent itself (v23b) | 35.7 / 1.75; v23b 64.3 / 1.25 | - |
| v24 | A/B of two vision prompts (describe the image first; take a fresh final look). No measurable effect (on 53.6% / 1.9, off 57.2% / 2.0); switched off by default on 09-30 | on 64.3 / 1.0, 42.9 / 2.75; off 35.7 / 1.0, 78.6 / 3.0 | - |
| q1, q1b | Learner thinking capped; whitespace-tolerant edits (flag); **proportional checks adopted**; scope discipline and scaled finish review (flags) | 85.7; q1b 50.0 | - |

What moved the scores:
- **v14's stand-in principle** was the largest single step on Qwen. v12 had built a test rig with stubbed WebGL and
  shipped an invisible world. In v14 the model went looking for a real browser on its own at minute 16 and measured
  real rendered pixels from then on. The prompt never mentions browsers.
- **Qwen stabilised on score** from v15 to v22b: 11 of 14 runs at 92.9% or more. Visual scores still ranged from
  1.0 to 6.5; the best (6.25-6.5) were v20, v21, v21d and v22.
- **occamy never stabilised** (14.3-85.7% across the same code). The v24 analysis found it rarely looks at its own
  screenshots, and when it does it misreads them, so prompt changes aimed at verification reach it weakly.

## 3. Model and runtime comparison (2026-10-02 to 10-06): harness unchanged

The only Rameness change in this window was a config deep-merge fix (10-03). Results are for the same harness:

| Run | Model / runtime | Score | Visual |
|---|---|---|---|
| mc-flashnext-1 | Flash-Next IQ3_S, llama.cpp | 92.9 | 4.5 |
| mc-flashnext-vision-1 | same, with vision | 92.9 | 6.0 |
| mc-exl3-vision-1 | Flash-Next EXL3 3.05bpw, TabbyAPI | 92.9 | 2.75 |
| mc-exl3-405-vision-1 | Flash-Next EXL3 4.05bpw | 85.7 | 6.75 |
| mc-q27bf16-vision-1 | 27B BF16, vLLM (80k window) | 100 | 6.25 |
| mc-q27-ud-q4km-vision-1 | 27B UD-Q4_K_M, llama.cpp | **100** | **7.25** |
| mc-iq3s-mtp-xhigh-196k-1 | Flash-Next IQ3_S + MTP, 196k | 85.7 | 6.25 |
| mc-flash-ud-q4kxl-vision-1 | Flash-Next UD-Q4_K_XL | 92.9 | 6.0 |
| mc-exl3-505-vision-1 | Flash-Next EXL3 5.05bpw | 57.1 | 0.5 |
| mc-glm-gsq-3bit-vision-1 | GLM-5.3 Flash 3-bit | 0 | 0 |
| mc-q27-tnz-vision-1 | 27B GPTQ, TnzGit vLLM + DFlash2 | 100 | 6.75 |
| mc-swift15-q4km-vision-1 | Swift-1.5 27B Q4_K_M | 92.9 | 6.75 |

The 27B UD-Q4_K_M run (100%, 7.25, 107 min) matched Claude Code + Opus on score and visual, and became the
reference for the workflow changes below.

## 4. Workflow v2 and v3 (2026-10-06 to 10-07)

Derived from comparing Rameness's transcripts with Claude Code + Opus on the same task. Changes with a clear
rationale were made default and checked with one Minecraft run each, not with long A/B queues.

| Step | Changes | Runs |
|---|---|---|
| **v2** (10-06 03:00-07:03Z) | Draft-then-revise; fix several issues per round, not one; a review pass before each run of the result; batch workflow with a test plan written once the first draft runs; **task scope** decided by JEV (specific vs open-ended) with **feature cycles** for open-ended tasks; made default | q27q4-v2-1 100 / 5.5 (OOM-killed at 104 min); v2-2 92.9 / 5.5; v2-clef 71.4 / 3.25 (Clef as decision model); q27bf16-v2 85.7 / 4.0 |
| **v3** (10-06 17:16-17:48Z) | Tool inputs logged to `tool_calls.jsonl`; progress review back to every 10 turns; thinking floor (at least `normal` after a write); feature cycles on their own git branch and folder; group review of all drafted files; group debugging with Occam's razor | q27q4-v3-1 100 / 5.5 |
| 10-06 19:46-20:17Z | Core prompt: verify against real use cases and user experience; thinking budgets one level higher (512 / 2048 / 4096 / 8192 / 16384) | - |
| 10-07 01:45Z | A tool call missing a required argument returns an error instead of crashing | - |
| **10-07 05:26-05:42Z** | Workflow prompts rewritten: first a complete draft that runs end to end, then rounds of review-all, fix-all, run-again; "work out early how the result will be judged and run those checks on the first running draft and after every round"; a cut-off reply means split into smaller modules | flashar-mtp3-3 **92.9 / 7.5**; q27gptq-df15-3 78.6 / 6.75; exl3-505t-r1 85.7 / 5.5; ninfer27-df7-1 100 / 5.0 |

The 10-07 runs compare runtimes as well as the workflow:
- **Flash-Next AutoRound 3bpw** (vLLM, MTP 3): from 64.3% / 4.25 under the v3 prompts (mc-flashar-mtp3-1, no
  runnable game until minute 121) to **92.9% / 7.5**, the best visual of any local run, after the rewrite.
- **27B GPTQ** (TnzGit vLLM, DFlash2-15) and **27B on ninfer** (DFlash2-7): 78.6% / 6.75 and 100% / 5.0.

## 5. Changes not yet in any run (2026-10-07 evening)

- **Compaction leaves room for the requested reply.** The reserve was 20,000 tokens while every request asks for
  32,000, so a 230-242k prompt in a 262k window overflowed before compaction ran (3 failed requests in
  mc-flashar-mtp3-3). The reserve is now the reply size plus 2% (`context.reply_tokens`, default 32000), and each
  request's output is cut to what still fits.
- **Large outputs.** Output lines over 2,000 characters are cut (minified files); a large file read without a range
  returns an outline and the first 150 lines; oversized shell output gets a short preview and a hint to narrow the
  command; one prompt line asks for narrow searches and partial reads.
- **Reasoning on vLLM** (see section 6).
- **Each run logs a `system_prompt` event** with a hash of the system prompt and of Rameness's code, so a later
  comparison can tell exactly which prompt and code a run used.

## 6. Did each change take effect?

### The code reached the runs it was meant for

The bench runs an installed copy of Rameness (`/opt/rameness-bench`), because the bench user cannot read `/root`.
Since 2026-09-25 08:22Z, before every run in sections 2-4, each batch compares that copy with the source
(`bench/minecraft/run.py`, `sync_rameness`) and reinstalls when any `.py` file differs. The launcher logs show
the reinstall after every batch of edits ("rameness copy is out of date; reinstalling").

Consequences:
- **Every run used the source as it stood when its batch started.** Edits made a few seconds after a run had
  started (for example the v20 principle, 35 s into Qwen v19) were meant for the next version, and reached it.
- **One mislabelled run.** The occamy v6 batch reinstalled at 22:13Z, after the v6 loop-guard change (22:05Z). The
  Qwen run labelled v5 started at 22:33Z without needing a reinstall, so it ran **v6 code**.
- **Only `.py` files are compared.** The SOP definitions and the UI page are not; they are identical today, and no
  run in this history depended on a change to them.

### Fingerprints in the event logs

| Change | Visible as | First run showing it |
|---|---|---|
| Hooks and `code.syntax_check` (v9) | `hooks_selected` event | every run checked (v13 onward) |
| Thinking budgets (v9) | `thinking` events with a budget | every run checked (v13 onward) |
| Budget levels shifted up (10-06) | maximum budget 16384 instead of 8192 | mc-q27gptq-df15-1 and every run after |
| Task scope (v2) | `scope` in the plan event | mc-q27q4-v2-2 (v2-1 started 10 s before the logging line went in; the decision itself was active) |
| Progress review every 40 turns (v2), back to 10 (v3) | 4 reviews in the v2 runs; 15-57 in each full-length run from v3 on | as expected |
| Group review and group debugging (v3) | `group_review`, `group_debug` events | mc-q27q4-v3-1 and every run after |
| Feature branches (v3) | `feature_branch` events | mc-q27q4-v3-1 (2), mc-q27gptq-df15-3 (core, cycle 1 merged, cycle 2) |
| Tool log (v3) | `tool_calls.jsonl` | mc-q27q4-v3-1 and every run after |
| Draft first (10-07 rewrite) | minutes until `index.html` first written | AutoRound 121 min before, **1.1 min** after; the 27B and EXL3 runs after the rewrite: 14-26 min |

### Where a change was sent but did not fully act

1. **Reasoning was dropped on every vLLM run.** vLLM returns the model's thinking as `reasoning` and reads back
   only that field; llama.cpp, TabbyAPI and ninfer use `reasoning_content`, the only one Rameness read. On
   mc-q27bf16-vision-1, mc-q27-tnz-vision-1, mc-q27gptq-df15-1/2/3 and mc-flashar-mtp3-1/3 the thinking was
   neither logged nor sent back, so "keep the model's reasoning across turns" (v7) did not act there. Rameness now
   reads both fields and sends the thinking back in the field the server uses.
2. **Thinking budgets above 5,000 never reached the model in the bench.** The bench proxy caps every request at
   the model entry's `reasoning_budget` (5,000), so `deep` and `maximum` both became 5,000. The 10-06 shift up
   therefore changed only the lower levels (512, 2048, 4096) in bench runs. **Fixed 10-07:** the proxy now passes a
   budget the harness sends up to 16,384; the entry's value is only the default for harnesses that send none.
3. **The TnzGit vLLM (V2 runner) ignores thinking budgets.** The 27B GPTQ runs got none, whatever Rameness asked.
4. **Outside the bench, only llama-server gets a budget.** Rameness sends a budget, and learns the context window,
   only when the server answers llama.cpp's `/props`. The bench proxy answers `/props` for every server, which hid
   this; run directly against vLLM, TabbyAPI or ninfer, Rameness sent no budget and assumed a 200k window.
   **Fixed 10-07:** Rameness identifies the server (llama.cpp `/props`, else `owned_by` in `/v1/models`), sends the
   budget in that server's field (`reasoning_budget_tokens`, `thinking_token_budget`, `thinking_budget`), reads the
   window from `max_model_len`, sends nothing to unknown servers, and stops sending the field if a server rejects it.
5. **Prompt wording cannot be checked directly in the older runs.** No run saved its system prompt. The evidence
   is the sync record above plus behaviour, and behaviour is mixed: the draft-first rewrite changed the AutoRound
   run sharply, while the 27B and EXL3 runs still wrote their first `index.html` 14-26 minutes in. The new
   `system_prompt` event closes this gap for future runs.

## 7. Lessons

- Generic principles beat task hints: v14's stand-in rule and the 10-07 draft-first rewrite produced the largest
  improvements, and neither names the task or a tool.
- Single runs mislead. Read every result next to its repeats and its screenshot.
- Verify the plumbing, not only the prompt: three of the five gaps in section 6 were field names and proxy caps
  that silently disabled a feature on some servers.
- For long agent sessions the model's architecture matters as much as the harness: dense 27B decode slows from
  ~105 to ~50 tok/s as the context grows to 230k, while Flash-Next's sparse attention holds ~150 tok/s.
