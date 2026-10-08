# Experiments

**In brief:** workflow changes start behind config flags and become defaults only after A/B runs (or a
clear design reason, validated with a run). This page lists each flag, its default, and the evidence.

## Flags

| Flag | Default | Evidence so far |
|---|---|---|
| `proportional_checks` (scale verification to the size of the task) | **on** | adopted: same scores (100% on 12/12 runs), research judge 8.5 vs 7.7, 37% faster (bugfix, research, data; 2 runs per arm); not yet measured on long builds |
| `draft_then_revise` + `review_pass` + `batch_workflow` (a complete running first draft of the whole result first, written as whole files; then rounds: review all files together, fix every issue in one pass, run the whole again; whole-result checks before part checks; automated test plan once the draft runs; short status lines) | **on** | adopted 2026-10-06 by design decision, modelled on how Claude Opus built the Minecraft benchmark (26 turns against Rameness's 263); short A/B before adoption: bugfix 100% both, 25% faster; research 100% both, judge 8.0 vs 9.0, slower. Reworded 2026-10-07 so the whole draft runs before any part is perfected: Flash-Next AutoRound went from 64.3% / visual 4.25 (no runnable game until minute 121) to 92.9% / 7.5 (`index.html` at minute 1) |
| `task_scope` (JEV: specific or open-ended; `feature_cycles`, default 3) | **on** (`jev`) | specific tasks build every stated and reasonably expected requirement into the first build and finish when all are checked; open-ended tasks build a tested core, then run N feature cycles (new ideas, built, tested, all checks re-run). Kev sorts 12/12 sample tasks correctly. Each feature cycle runs on git branch `rameness/cycle-N` in a sibling folder (file tools and shell commands are redirected there) and is merged into the project only once it passes its checks, so the project always holds the last verified version (`task_scope.branches`). Seen working in Minecraft runs (core committed, cycle 1 merged, cycle 2 started) |
| `group_review` / `group_debug` | **on** | after a burst of 2+ whole-file writes, Rameness asks for one review of them together (cross-file names, data shapes, units, init order, loops that silently do nothing); after 6 turns of investigating without changing the work it asks for one probe that tests several causes and every anomaly seen so far (repeats every 12), ranking causes by Occam's razor (simplest cause explaining all anomalies; basic failures such as empty data or a code path that never runs before library or maths faults). From the Clef Minecraft run, which spent 85 min on one bug whose clue (blank hotbar icons) it saw at minute 38 |
| trusted SOPs (only validated SOPs offered, a trust line on each, no re-checking their output) | **on** | adopted 2026-10-08: on the 8-task projreport series, task time 42.4 min / 155k tokens against 50.4 min / 166k before, and 62.5 min / 300k with no SOPs; all 100%. One task in 8 still re-checked an SOP's output |
| `progress_review` (JEV checks direction every N turns) | on, every 10 turns | its verdicts track real struggles but it rarely acts (26 reviews, 0 actions in one Minecraft run) |
| `delegation` (JEV-gated sub-agents) | off | models seldom delegated when offered it |
| `vision_describe_first` (images ask for a description before a verdict) | off | no measurable effect (occamy, 2 runs per arm) |
| `vision_final_look` (the finish review asks a seeing model to look at the result) | off | no measurable effect (same A/B) |
| `scope_discipline` | off | overnight A/B: research score regressed (100% to 95.8%); overall score slightly lower |
| `review_scaled` | off | overnight A/B: same scores, 7% slower; research quality grades unavailable |
| `edit_fuzzy` | off | overnight A/B: overall score regressed (98.7% to 97.4%), no time saving |
| `thinking.mode: fixed` | `jev` | promising: 98.7% vs 97.4%, 12% faster; awaiting research quality grades before adoption |

The `scope_discipline`, `review_scaled`, `edit_fuzzy` and `thinking.mode` experiments used bugfix2,
research and data, with two runs per arm. Their research judge failed when Claude reached its usage
limit, so keyword scores alone cannot establish report quality; these settings remain unchanged.

## Running an A/B

`python -m bench.ab` runs paired arms and gives an assessment; it never changes defaults itself.

It reports **reject** for a measured regression, **incomplete** for missing paired results or research
grades, or **eligible** for manual review. Eligibility requires no overall score loss, no task loss beyond
the control's own spread, no research quality loss, and at most 20% extra wall time.
