# Decisions (JEV)

**In brief:** a typed decision model (Kev, Laya or TypeSafe's Jev), never an LLM, makes Rameness's
multiple-choice calls. It returns calibrated probabilities in one forward pass, sees only the context
each decision needs, and every decision is logged.

## How a decision works

The model reads a short state and scores a fixed set of options; no text is generated.

A Jev "System One" model returns probabilities over the options. Rameness uses two kinds of question:
`choose` (a Jev **Choice**: pick one) and `activate` (a batch of **Noul** yes/no questions, one per
option, used where several options can apply). Before a decision takes effect, the
[comfort gate](fleet.md#the-comfort-gate) asks whether it is routine enough to make alone.

## What it decides

Every fork in the harness that other agents hard-code.

| Decision point | Call | Where |
|---|---|---|
| Which SOP categories/leaves this task will use (hierarchical traversal) | `activate` per tree level | `sops.activate` |
| Route: direct / answer / agent | `choose` | `router.Router.plan` |
| Reasoning effort for the task | `choose` | `router.Router.plan` |
| How much to think before the next step (5 levels) | `choose` | `thinking.Thinking.decide` |
| Which standard procedures run as lifecycle hooks | `activate` | `hooks.Hooks.select` |
| Stuck in a loop: continue / reorient / reset / stop | `choose` | `loopguard.LoopGuard.check` |
| Is the work on track / drifting / stalled | `choose` | `progress.ProgressReview.review` |
| Finished, or stopped mid-step | `choose` | `harness.Harness._stopped_mid_step` |
| Run a sub-task in a fresh sub-agent (off by default) | `choose` | `harness.Harness._delegate` |
| Which old observations are still relevant when context is full | `activate` | `context.ContextManager.compact` |
| Which repeated tasks from the end-of-run review are reusable procedures | `activate` + noisy-OR with how often they repeated | `learning.Learner.candidates` |
| Does an existing SOP already do this (shortlist, then pairwise against its code) | `activate` + `yes` | `learning.Learner._duplicate` |
| Does the generated procedure's behavior already exist | `activate` shortlist + pairwise `yes` | `learning.Learner._duplicate_generated` |
| Could a close SOP be extended to cover it | `activate` + `yes` | `learning.Learner.extension_target` |
| Is a private SOP generic enough to share | `choose` | `publish.classify` |
| Built-in or registry | `choose` | `publish.destination` |
| Do scrubber findings expose private info | `choose` | `publish.private_info` |

The fleet makes more: intake, role, fork, slot, environment, failure, stall, questions, fork winner
(see [The team](fleet.md)).

## Backends

Set with `jev.backend`; `auto` picks the best one available.

* `kev`: [Kev](https://github.com/jaredpalmer/kev), Jared Palmer's open-source (Apache-2.0) Jev-like
  models on Qwen3.5, served by `rameness jev up kev` on `http://127.0.0.1:8008/v1/systemone`
  (`jev.kev_checkpoint`, default `jaredpalmer/kev-4b`).
* `clef`: [Clef-Flash](https://huggingface.co/Cloudflare/clef-flash), Cloudflare's open-source (Apache-2.0)
  9B decision model on Qwen3.5-9B, Jev / System One compatible. `rameness jev setup clef` builds its own
  environment and `rameness jev up clef` serves it on `http://127.0.0.1:8010/v1/systemone`, 8-bit so it fits a
  16 GB GPU (`jev.clef_checkpoint`, `clef_bits`; `clef_python` reuses an existing environment). It is a
  drop-in for Kev: set `"backend": "clef"` (or `--jev clef`). See [Kev vs Clef](#kev-vs-clef).
* `laya`: [Laya](https://github.com/NandhaKishorM/laya), Convai Innovations' open-source (Apache-2.0)
  Jev-compatible model, served by `rameness jev up laya` on `http://127.0.0.1:8000/v1/systemone`, with
  its fine-tuned `typed-decisions` checkpoint (`jev.laya_model`).
* `laya-local`: the same Laya in-process, no server, at the checkpoint's trained 1,024-token window
  (`jev.laya_max_len` / `laya_head_max_len` override; wider measured worse). Each process loads its own
  copy (~1.7 GB), so it is the fallback when no server runs.
* `typesafe`: [TypeSafe AI's Jev](https://docs.typesafe.ai/api), the hosted subscription. Set
  `TYPESAFE_API_KEY`; nothing is sent to it unless it is selected or `auto` falls through to it.
* `auto` (default): the first available in `jev.local_order` (`clef`, `kev`, `laya`, `laya-local`), else
  TypeSafe if a key is set. With a local model selected and a key set, TypeSafe also covers an outage.
* `lexical`: offline keyword overlap, no model. A last resort when no decision model is reachable, and
  for tests; Rameness warns while it is in use.

## Accuracy

`rameness jev bench` measures the active backend on labelled real harness decisions.

Measured on 41 decisions, 16 of them paraphrased to avoid the options' keywords (CPU):

| Backend | Accuracy | Paraphrased | P(correct option) | Per decision |
|---|---|---|---|---|
| Kev-4B | 0.93 | 0.88 | ~0.65 | 1.5 s CPU (~20 ms on a GPU) |
| Laya `typed-decisions` | 0.73 | 0.88 | ~0.43 | 0.2 s |
| Laya `typed-decisions` in-process, 1024/256 (with the 36-option category set) | 0.73 | 0.78 | ~0.40 | 0.13 s |
| same, widened to 2048/512 | 0.69 | 0.78 | ~0.38 | 0.17 s |
| Laya base | 0.61 | 0.75 | ~0.45 | 0.17 s |
| lexical (no model) | 0.98 (cues written for these phrasings) | 0.69 | — | ~0 |

Run it before switching models or checkpoints.

## Kev vs Clef

Clef-Flash is more accurate and far more confident than Kev, and on a GPU much faster; it needs a GPU.

**Labelled decisions** (`rameness jev bench`, 48 harness decisions in 9 kinds, 2026-10-05; Kev-4B on CPU in
bf16, Clef-Flash on an RTX 5070 Ti in 8-bit). Accuracy / probability given to the correct option:

| Decision kind | n | Kev-4B | Clef-Flash |
|---|---|---|---|
| route (direct / answer / agent) | 8 | 1.00 / 0.72 | 1.00 / 0.92 |
| effort | 6 | 1.00 / 0.65 | 1.00 / 0.87 |
| significance (comfort gate) | 6 | 0.83 / 0.70 | 1.00 / 0.93 |
| intake (fleet) | 7 | 1.00 / 0.64 | 0.71 / 0.68 |
| failure (what to do next) | 4 | 0.75 / 0.46 | 1.00 / 0.83 |
| stall | 3 | 1.00 / 0.65 | 1.00 / 0.92 |
| question (relay to the director) | 4 | 1.00 / 0.90 | 1.00 / 0.89 |
| loop guard | 3 | 0.67 / 0.38 | 1.00 / 0.87 |
| task category (36 kinds) | 7 | 0.86 / 0.61 | 1.00 / 0.92 |
| **all** | **48** | **0.92 / 0.65** | **0.96 / 0.87** |

Kev's misses: treating "send the invoice email to the client?" as routine (it is significant: external);
forking instead of moving to a stronger model after 40 failed turns; stopping instead of reorienting on a
repeated "file not found"; "security" instead of "security_testing". Clef's two misses are both fleet intake:
"migrate the whole monolith to microservices" (decompose, not research) and "make it better" (clarify, not
research).

**In a real run** (the Minecraft build, Flash-Next AutoRound on the CMP 170HX, 2026-10-07 with Kev and
2026-10-09 with Clef; [Benchmarks](benchmarks.md#minecraft-kev-vs-clef)):

| | Kev-4B (CPU, bf16) | Clef-Flash (5070 Ti, 8-bit) |
|---|---|---|
| Time per decision (median) | 1.50 s | **0.18 s** |
| Thinking level chosen most | minimal, 50% of turns | maximum, 34% of turns (minimal 5%) |
| Behaviour / visual | 100% / 7.5 | 100% / 8.0 (human review; automated judge 6.5) |
| Minecraft features built (of 56) | 47 | 48 (53 with an unmerged feature round) |

The two runs are one each, two days apart, so harness changes sit between them as well as the decision
model; the automated judge graded each from one random-seed screenshot (see
[Benchmarks](benchmarks.md#minecraft-kev-vs-clef) for the human review grade). The decision-level numbers are the
controlled comparison.

## Each decision sees only its own context

A decision gets its summary and its options, never the whole conversation or the org profile.

Routing sees the task; a failure sees the task title and the error tail; a stall sees the last output;
an SOP evaluation sees the SOP's own code (`SOP.digest()`). The state is fitted to the model's window
(`jev.max_state_chars`; by default ~1,200 characters for Laya's 512-token base checkpoint, ~3,000 for its
1,024-token checkpoints, 8,000 for Kev and TypeSafe), keeping the start and the end, where errors and
results are. Laya truncates silently, so Rameness trims deliberately.

Models read each option's keyword cues by default (`jev.option_text`); plain-sentence descriptions
(`Option.desc`, `"option_text": "sentences"`) measured no better, and are kept in the decision log so
every logged decision is readable.

## The decision log

Every decision is a training example.

Decisions go to `.rameness/decisions.jsonl`, with `feedback` records for their outcomes. The fleet's
**Improve** tab turns oversight into better decisions ([Improving JEV](fleet.md#improving-jev)), and the
log is a training set for fine-tuning the decision model on real harness decisions.
