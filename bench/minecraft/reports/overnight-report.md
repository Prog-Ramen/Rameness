# Rameness report (updated 2026-09-27 07:30)

## Tonight (release prep + A/B loop)

**New non-Minecraft tasks** (`bench/tasks/`, each scorer validated against a reference answer):
`bugfix` (5 symptom-described issues in a small Python library; 12 hidden tests + repo tests), `research`
(8 sources with deliberate conflicts, 6 questions, 12 deterministic checks + 0-10 judge), `data` (412-row messy CSV,
9 exact answers under stated cleaning rules; answers re-derived independently).
**Baseline, Qwen3.8-27B:** both harnesses get the facts right, but Claude Code is faster and more focused:
bugfix 100/100 both, Rameness 10.6 and 5.9 min vs 1.7-1.8; research-1 100/100 both, judge 7.67 vs 9.33, 6.8 vs
3.1 min. Rameness fixed all 5 issues within 88 s, then spent ~9 min on extended verification; in research it added
an unrequested mixed-fleet analysis, which is where both of its errors were.
**A/B loop (keep only what passes; 2 runs x 3 tasks per arm):** proportional-checks, scope-discipline,
thinking-full, edit-fuzzy - all behind flags, off by default. Results in `/root/bench/ab/*.md`.
Release prep done: unproven vision prompts off; README safety/experimental/Jev notes; clean-user install passes
(found + fixed a shared-/tmp SOP staging bug and an uncapped learner-thinking bug); CI workflow; secret scan clean
(LAN hosts moved to git-ignored `bench/models.local.json`). Commit waits for the results.

## Earlier (occamy vision-verification A/B result)

| arm | run | score | visual | images viewed | ended |
|---|---|---|---|---|---|
| on | v24on | 64.3 | 1.0 | 8 (3 late) | itself, 123 min |
| on | v24onb | 42.9 | 2.75 | 0 | itself, 92 min |
| off | v24off | 35.7 | 1.0 | 0 | timeout |
| off | v24offb | 78.6 | 3.0 | 5 | itself, 138 min |

Means: on 53.6% / 1.9, off 57.2% / 2.0 - **no measurable effect**; spread within an arm (35.7-78.6) dwarfs the gap.
The flags only act when occamy looks, and in v24onb it never looked (neither fired). Where they fired (v24on) the
final look made it re-check and find a real issue (stuck behind the pause screen) and work 40 more minutes, but the
world was still broken; describe-first was largely ignored ("The game works!" right after images). Bottleneck:
occamy uses vision in only about half its runs (0-8 views, independent of the arm). Next lever: an independent
check it can't skip (Qwen as advisor on the final screenshot, or a harness-captured final screenshot).

## Earlier (occamy vision-verification A/B setup)

**Why occamy's vision didn't help: not its eyes.** Shown its own end-of-run screenshot standalone, occamy saw the
defects in all 3 framings, like Qwen ("no, individual cube-shaped blocks ... are not visible"). In the run it
looked only twice (min 29, 31, with narrow questions), then trusted numbers (meshMapSize, isLocked), and in its
final review took a screenshot, never opened it, deleted it and declared done. Also fixed: the self-kill guard
mistook `2>/dev/null` for a process pattern and refused a safe pkill.
**Two generic changes (flags, default on):** `vision_describe_first` - every image shown to the model asks it to
describe what the image shows before comparing it with what it should show; `vision_final_look` - when the model
can see and has used images, the finish review asks it to look at the final result fresh, as its user would, and
compare it with the task. 116 tests pass. **A/B running on occamy** (`chain-v24-occamy-ab.sh`): v24on, v24off,
v24onb, v24offb (interleaved, same code; off = both flags false).

## Earlier (occamy vision verdict)

**occamy v23b (vision + self-kill guard): 64.3%, visual 1.25.** The guard fired 3 times (each would have killed the
run). occamy viewed its game twice and concluded "the in-game 3D view renders correctly", while the judge saw a
flat black mass; it declared done at 51 min. **Vision vs no vision (2 runs each):** 35.7 / 64.3 (visual 1.75 / 1.25)
vs 50 / 14.3 (1.25 / 0.25): means 50% / 1.5 vs 32% / 0.75 - slightly better, inconclusive at n=2 (v23 was also cut
short). occamy uses its eyes but doesn't reliably see defects (its card: no visual training; it also missed the
defect Qwen caught in the test). Keep vision (free now), keep the guard. Bigger lever for occamy: Qwen as its
eyes via the advisor proposal (`research-small-models.md` #2).

## Earlier (occamy vision runs)

**occamy v23 (vision on, DFlash off): 35.7%, visual 1.75 (black screen).** It worked much faster than before (172 turns
in 100 min vs ~106 in 150), used vision well (5 screenshots, each prompting the right next step: "WebGL context
failed" -> "context works but the scene is black" -> debugging world generation), and the JEV mid-step check fired
live 8 times, keeping it working. But it killed its own harness at 116 min: `pkill -f "serve"` (restarting its
web server) matched Rameness itself, whose command line holds the task text ("...static web server"). Same trap
as v18b. **Fixed (generic):** the bash tool refuses a pkill/killall whose pattern matches the agent or its parents
and says to kill by PID; tested (116 tests pass). v23b restarted with the fix.

## Earlier (occamy vision setup)

**occamy can now see.** Its model has a vision encoder (Qwen3_5MoeForConditionalGeneration); your server now loads
the projector from the same quant repo (`mmproj-Accio-Lab_occamy-1.0-Q8_0.gguf`, CPU). With the DFlash draft model it
failed every image request ("failed to process speculative batch": the draft is text-only; a known llama.cpp
limit), so DFlash is now off. Measured without DFlash: **23.5 tok/s on 3,000 tokens of code** and 22.9 on prose,
vs 22.4 / 16-18 with it: the draft wasn't paying for itself (acceptance ~25-55%). Vision check: occamy called v16's
render mostly correct and v17's broken (solid black silhouettes); less sharp than Qwen (missed v16's small black
triangle); ~21 s per image on your server's CPU (dropping --no-mmproj-offload would make that ~1-2 s).
Rameness: if a server advertises vision but fails on an image, it now retries without images, tells the model, and
switches vision off for the run (tested live against occamy with DFlash on).

## Earlier (07:35)

**v22 (fresh-start rule):** Qwen v22 100% / visual 6.25, Qwen v22b 71.4% / 1.0 (terrain unrendered) - identical
code, so run-to-run variance dominates single runs; any A/B needs several runs per arm. occamy v22 (first run on the
temp 0.6 default): 50% / 1.25, timed out at 132k context with zero compactions - the pattern research proposal #1
(proactive compaction) targets. Research write-up: `/root/bench/research-small-models.md`.

## Earlier (05:15)

**Qwen v22 (fresh-start rule): 100%, visual 6.25**, finished itself in 118 min, click starts it.
**Is JEV earning its time?** Per turn JEV's thinking decision costs ~0.9 s (Kev on CPU, measured); the whole gap
between a turn's last tool result and the next model call is ~2.3 s median (~8% of a run) - the other ~1.4 s is
being located: each turn now logs `jev_s` and `gap_s` (measurement only, no behaviour change). Thinking per turn is
down from ~500 to ~300 tokens (v8, no per-turn budgets, a rough comparison) ~= 4 s/turn saved at 42 tok/s: a small
net gain. Progress review (every 10 turns, ~0.1 s/turn): "on track" 90% of the time; its ~43 drifting/stalled
verdicts mostly land on real struggles, but it acted only 4 times in 26 runs (confidence >= 0.5).
Progress-review A/B: postponed at your request (no runs started); the review stays on, unchanged.
Procedure selection is left as is.

## Earlier (04:30)

**occamy sampling A/B done (same v21 code, 2 pairs):** temp 0.6 / presence 0 -> 78.6, 42.9 (visual 2.0, 1.0);
model card 1.0 / 1.5 -> 64.3, 21.4 (1.75, 0). Lower temperature won both pairs (mean 60.8 vs 42.9) and decodes
faster (~24 vs ~20 tok/s). The seven earlier card-sampled occamy runs averaged ~36%. **Rameness now defaults occamy
to temp 0.6 / presence 0** (evidence in the profile comment; config "sampling" overrides). occamy v22 + v22b running
with it and the fresh-start rule.

## Earlier (03:00)

**Qwen v21 over 4 runs: 100% every time; visual 6.5 / 4.25 / 3.0 / 6.5 (mean 5.1).** v20 (2 runs): 100 / 85.7,
visual 6.25 / 5.5 (mean 5.9). v21's change targets occamy's mid-step stops, which Qwen doesn't do, so no visual gain
is expected; the spread is run-to-run variance. Behaviour is now reliably 100%; looks vary.
**occamy sampling A/B so far:** temp 0.6 -> 78.6 / 42.9 (visual 2.0 / 1.0); model card -> 64.3 (1.75), v21b running.
No clear effect yet.
Common thread in Qwen's weak runs: it verified states it set up itself (v21c: the camera starts inside terrain;
v21b: close-up views only; v14: tests bypassed the start click). **v22** (generic): check the result from a fresh
start, the way its user first meets it (clean state, default settings, the normal way in); the finish review asks
this per check. Qwen v22 + v22b running.

**occamy sampling A/B, first pair (same v21 code):** temp 0.6 / presence 0 -> 78.6%, visual 2.0, done at 62 min,
23.8 tok/s; model card (1.0 / 1.5) -> 64.3%, 1.75, timed out, ~20 tok/s. Lower temperature also decodes faster
(the DFlash draft is accepted more often). One pair is suggestive only; a second pair (v21sb, v21b) is running.

**occamy v21: 64.3%, visual 1.75** (dark scene behind the start menu). It did not stop mid-step this time, so the new
check never fired; it used the full 150 min for only 106 turns (Qwen: 210 in 124 min). Its thinking stays within
the per-turn budgets (median 50-140 reasoning tokens); the time goes into output at ~20 tok/s (even "minimal" turns
average ~600 tokens, 41 s) - mostly code in tool calls. Next: the v21s sampling A/B (temp 0.6, presence 0) is running.

**Qwen v21: 100%, visual 6.5 - best local build yet** (Claude Code + Qwen 5.5, Claude Code + Opus 7.25-8.25). Finished
on its own in 124 min, a real click starts it; the judge's one complaint is a stray floating triangle. Last three
Qwen runs: 6.25 / 5.5 / 6.5 visual, all 85.7-100% - the library + vision + review changes have made it consistent.

**Why occamy lags (v20 diagnosis).** It didn't run out of time: it stopped at 90 min, mid-debug. Four times it wrote
a short reply announcing its next action ("...Let me fix the condition to stop on water too:") and ended the turn
without the tool call (genuine stops: 130-180 tokens, no lost calls). Rameness pushed back three times ("your todo
list still has open items" - it never updated its 7 todos), which is the finish-check limit, then accepted the
fourth. Qwen almost never does this. By speed alone occamy also does less: 20 tok/s vs Qwen's 42, 106 turns vs 300,
median turn 29 s vs 13 s, and no vision (0 image views vs Qwen's 11).
**v21 (generic, 115 tests pass):** when a reply has no tool call, JEV decides "finished, or stopped right after
announcing a step?". A mid-step stop gets "Continue: make that tool call now" and doesn't use up the finish checks;
an empty reply counts as mid-step. Kev on the real cases: 3/3 mid-step (0.82-0.95), 4/4 finished summaries
(0.05-0.17). Queue (`chain-v21.sh`): occamy v21 (card sampling) + v21s (temp 0.6, presence 0) as a clean A/B on v21
code (replaces v20s); Qwen v21 + v21b; then the judge.

**Qwen v20: 100% (14/14), visual 6.25 - the best local build so far** (Claude Code + Qwen: 100 / 5.5; Claude Code +
Opus: 100 / 7.25-8.25). It followed the new library principle exactly: Three.js with a local copy (`three.min.js`),
own mesher/textures/player on top. Finished on its own in 142 min, and a real click starts it. The judge's only
complaints: thin black gaps between terrain steps and some foliage clipping.
Scorer fix: `click_starts` was a false negative for v20 - the start-button search matched a secondary "New world"
button and never looked at headings (v20's "click to play" is an `h1`). It now prefers play/start controls, looks at
headings too, and falls back to world options only when there's no start control. Checked: v20 True, v19 False
(genuine), v14 False, v14 fixed copy True; scores unchanged. occamy v20: 42.9%, visual 1.25.

**Qwen can now see (v18).** Qwen3.8-27B is a vision-language model; its server now loads the matching vision
projector (`mmproj-Qwen3.8-27B-BF16.gguf`, from the same ISTA-DASLab repo) on the **CPU** (`--no-mmproj-offload`), so
150k turbo4 and VRAM are unchanged (14.46 GB). ~8 s and ~1,050 tokens per 1280x800 screenshot. Asked about two
screenshots, it spotted the small black triangle in v16's good render and described v17's broken terrain correctly.
Old script: `run-qwen3.8.sh.bak-turbo4-novision`.

Rameness (generic, 114 tests pass): `read_file` on png/jpg/gif/webp shows the image to models that can see
(llama-server `/props` modalities, or Claude), and says so plainly to those that can't; images ride with their
tool result (Anthropic: inside tool_result; chat.completions: a user message right after the results); only the
latest 2 stay in view (`vision_keep`), older ones become a note; counted as ~1.5k tokens each; the environment block
tells the model it can see. Also: a direct-answer route whose reply tries to call a tool now escalates to the agent
loop. Bench: the proxy keeps images for servers with vision; reinstalls are locked (two runners collided).

**Qwen v18 (first run with vision): 78.6%, visual 1.75.** Vision works as intended: Qwen screenshotted and viewed
its own game ~20 times and diagnosed the bugs precisely ("only the 4 corner blocks' top faces render; the middle is
missing"). What sank the run: at 72 min it had a working game, at 74 min a spawn tweak broke rendering, and it
spent the last 75 min debugging from scratch. **v19** adds a generic principle: once something is verified working,
keep that version (commit/copy) and diff against it when a later change breaks it. Qwen v18b (same code as v18) is
running; v19 runs follow for both models (`/root/bench/chain-v19.sh`).

**occamy v18 / v18b: 50% / 28.6%** (visual 1.5 for v18). v18b stopped on its own at 54 min with a broken game:
it never wrote a todo list, and the pre-finish review only ran when a todo list existed, so it was never asked to
check its work against the task. Fixed (generic): the review now also runs when the agent did real work without a
plan (5+ steps). Included in v19 for both models (occamy v19 restarted to pick it up).

**Library choice explains most of Qwen's visual spread.** Qwen v18b: 92.9%, visual 3.25 (broken geometry, timed out
debugging its own mesher). Across all judged Qwen builds, the two that used Three.js are the top two (6.0, 4.75);
every hand-written-WebGL Qwen build is at or below 3.5 - its time goes into GL bugs (swapped attribute offsets,
missing faces). **v20** adds a generic principle: for complex, error-prone parts (parsing, dates, crypto, numerics,
3D rendering, charting...) prefer an established, tested library when the task allows, and keep a local copy when the
result must work offline. (occamy's Three.js builds still failed on their own bugs; its CDN was reachable.)
Queued: v19 (both models, running) then v20 and v20b for each model, then the visual judge (`chain-v20.sh`).

**occamy v19: 14.3%** (the page never got past its start menu; visual 0.25). occamy since your restart: 35.7, 64.3,
50, 28.6, 14.3 - all with the model card's sampling (temp 1.0, presence 1.5), which Rameness sends per request. Its
best run (v7, 85.7%) predates that (the server then ran temp 0.6, presence 0). The card recommends 1.0/1.5 for
agentic coding too, and v7 is confounded (older harness, healthy server), so this is an A/B, not a switch:
occamy v20 + v20b (card) vs **v20s** (same code, temp 0.6 / presence 0), `chain-v20s-occamy.sh`.

**Memory, 13:30:** free memory fell to 3.9 GB: Kev (bf16) had grown from 8.3 to 12.7 GB over a day, and the Qwen
server is now 8.9 GB RSS (vision projector on CPU + 4 GB prompt cache). Restarted Kev (runs fall back to lexical
decisions for the ~40 s); free memory back to 13.4 GB. Found that `jevserve.down` could leave the old Kev running
(it sat on SIGTERM) - it now waits and force-stops. `/root/bench/kev-watchdog.sh` restarts Kev overnight if it passes
11 GB while free memory is under 3 GB (log: `kev-watchdog.log`).

**Visual judge (Sonnet, 0-10) for tonight's builds** - the behaviour score can't tell good from broken renders:

| run | score | visual |
|---|---|---|
| Qwen v21b (repeat) | 100 | 4.25 (finished at 79 min; near terrain fine, distant chunks break into slivers) |
| Qwen v21c / v21d | 100 / 100 | 3.0 / 6.5 |
| Qwen v22 / v22b (+ fresh-start rule) | 100 / 71.4 | 6.25 / 1.0 |
| **Qwen v21** (+ JEV mid-step stops) | **100** | **6.5** (new best; finished itself in 124 min; click starts it) |
| **Qwen v20** (+ prefer tested libraries) | **100** | **6.25** (best; Three.js vendored) |
| **Qwen v20b** (repeat) | 85.7 | **5.5** (finished itself, 86 min) - v20 mean 92.9 / 5.9, Qwen's best version |
| **Qwen v19** (vision + keep-working-version + review w/o todos) | 92.9 | **5.25** (finished itself, 133 min; click doesn't start it) |
| Qwen v18b (vision) | 92.9 | 3.25 |
| Qwen v18 (vision) | 78.6 | 1.75 |
| occamy v23 / v23b (+ vision, DFlash off; v23b + self-kill guard) | 35.7 / 64.3 | 1.75 / 1.25 |
| occamy v22 / v22b (temp 0.6 default) | 50 / 14.3 | 1.25 / 0.25 (v22b stopped itself at 75 min) |
| occamy v21sb (temp 0.6) | 42.9 | 1.0 |
| occamy v21b (card) | 21.4 | 0 |
| **occamy v21s (temp 0.6, presence 0)** | **78.6** | 2.0 (finished itself at 62 min; 23.8 tok/s) |
| occamy v21 (card sampling) | 64.3 | 1.75 |
| occamy v20 / v20b | 42.9 / 21.4 | 1.25 / 0 |
| occamy v19 | 14.3 | 0.25 |
| occamy v18 / v18b | 50 / 28.6 | 1.5 / 0.25 |
| Qwen v14 | 100 | **6.0** (best local; CC+Qwen 5.5, CC+Opus 7.25-8.25) |
| Qwen v16 (turbo4 150k) | 92.9 | 4.75 |
| Qwen v14b | 85.7 | 3.5 |
| Qwen v15 | 100 | 3.0 |
| Qwen v17 | 92.9 | 1.75 |
| Qwen v12 / v13 | 50 / 78.6 | 1.75 / 1.5 |
| occamy v17 | 64.3 | 1.0 |
| occamy v15 | 35.7 | 0.25 |

Running overnight (`/root/bench/chain-v18.sh`, `chain-v18-occamy-redo.sh`): Qwen v18 then v18b, occamy v18b then v18,
then the visual judge. occamy's server has no vision projector (its model may have one - your call).

## occamy after your restart (checked ~16:30)

- Settings took: temp 1.0, top_p 0.95, top_k 20, presence 1.5; 1 slot x 200,192; speculative on.
- The long-reply collapse is gone: 17.9 / 16.4 / 17.6 tok/s at 400 / 1500 / 3000 tokens of prose (before: 16 -> 5.8).
- Realistic Rameness request (3000-token code write, thinking on, occamy sampling, all 14 tools): **22.4 tok/s**
  (3.1 before), draft acceptance 55%, tool call parsed correctly. Prose gets only ~28% draft acceptance, at any
  temperature/presence, so the sampling change isn't slowing it.
- occamy v15 started 16:3x; first requests 17.5-23 tok/s in-run (a 5,414-token reply at 23 tok/s).

## v14 can't be started by a person (found 17:20)

The "Click to play" overlay covers the page but the start handler is on the canvas under it, so clicks do nothing.
The scorer missed it: when a click doesn't start the game it calls `requestPointerLock()` directly. Fixed copy
(one listener added): `/srv/bench/fixed/v14-qwen`. The scorer now records `click_starts` (not scored, so scores stay
comparable): v14 original False, fixed copy True.

## Qwen server now TurboQuant turbo4, 150k (18:42, your request)

sirxsniper/llama-cpp-turboquant fork (tested on Qwen3.8-27B), built with CUDA 13.1 for sm_120a; only the KV type
and context changed in `run-qwen3.8.sh` (old one: `run-qwen3.8.sh.bak-q8-100k`). Same test, back to back:

| context | decode q8 | decode turbo4 | prefill q8 | prefill turbo4 |
|---|---|---|---|---|
| 1k | 54.3 | 51.9 | 1371 | 1530 |
| 64k | 38.3 | 44.8 | 1211 | 1509 |
| 96k | 33.4 | 41.9 | 785 | 1090 |
| 145k | - | 37.4 | - | 882 |

VRAM 14.6 of 16.3 GB at 145k, no OOM. Installed cuda-toolkit-13-1 + cmake; patched CUDA's `math_functions.h`
(rsqrt/rsqrtf `noexcept`) for glibc 2.43 (original kept as `.orig`).

**Qwen v15 (discriminating-check review): 100%, click starts the game, textured world** (trees use log texture for
canopies). Last three Qwen runs: 100 / 85.7 / 100.

## Qwen v16 (v15 code on the turbo4 150k server): 92.9%, best-looking build yet

Finished on its own in **89 min** (earlier Qwen runs used the full 150), 182 requests, mean decode **42.7 tok/s**
(v15 on q8: ~39), context up to 131.6k of 150k with only 2 compactions. Real click starts it. Proper grass/dirt
terrain with hills, clouds, correct hotbar icons; only "breaks block" failed (and a small black triangle at the top).
Qwen v17 (lenient anchors + file-tool rule) is running.

## occamy v15 (after your restart): 35.7%, black page

Ran at a healthy 18.8 tok/s mean, but 92% of the 150 min was generation: it kept the whole game in one 27k-char file,
rewrote it wholesale, and when edits missed it fell back to shell tricks (heredocs -> truncated, a Python generator ->
broke on `"""`, then base64: one 33-min turn). The syntax checker did report the errors (also at the end); it didn't
fix them. Two harness causes, both fixed generically in **v17**: (1) occamy pasted whole displayed lines
(`319:8a|}...`) as `edit_lines` anchors and Rameness rejected them; anchors are now read from the first/last pasted
line (the line hash is still verified). (2) the prompt now says to create/change files with the file tools, not
the shell (the file tools are checked and can be undone). occamy v17 started ~19:15; Qwen v17 follows Qwen v16.

## Headline

**Rameness v14 / Qwen scored 100% (14/14) and renders a proper world** (textured terrain, trees, water; screenshot
`/root/bench/results/minecraft/mc-rameness-v14/mc-rameness-v14-rameness-qwen/screenshot.png`). That's the best local
build so far, on par with Claude Code + Qwen (100%) and visibly better looking. The one change from v13 is a
generic prompt principle: *a check that replaces a part with a stand-in (stub, mock, fake) says nothing about that
part - exercise what the user perceives for real and measure its actual output*. v12 had spent 40 min on a test rig
with stubbed WebGL and shipped an invisible world. In v14, at minute 16 the model itself decided to look for a
real browser, installed one, and from then on measured real rendered pixels (76 such checks). The prompt never
mentions browsers. It wrote its own probe (I checked it didn't reuse an earlier run's files).
**Repeat (v14b): 85.7%**, finished on its own at 100 min; real-render checks from minute 2, so the behaviour
reproduces (v14 mean ~93% vs 50% / 78.6% before). But its world is all flat green (texture bug) and it still said
"complete and verified": its pixel checks showed *something* was drawn, not the *right* thing. **v15 (running)**: the
pre-finish review now asks, per check, to name a flaw the user would notice and whether that check would have
caught it, and to run a sharper check where not (generic). Bench fix found along the way: all runs shared the bench
user's /tmp; the runner now clears leftovers before a run (when no other bench run is active).

## Where things stand

- Around 07:25 the machine ran out of memory and Claude Code stopped every background run (Qwen v10, occamy v11).
- **Cause found and fixed:** Kev was not leaking. It serves in fp32 on CPU, so 4B parameters take ~17-19 GB.
  Rameness now starts Kev in **bf16** when the CPU has native bf16 (yours has AVX512-BF16). On 5 thinking-level
  scenarios it made the same choices (probabilities within 0.003), **2.3x faster** (4.2 s vs 9.6 s), with **8.3 GB RAM
  instead of 18.9 GB**. Free memory went from ~4 GB to ~14 GB.
- The bench runner now waits for 5 GB of free memory before starting a run (`--min-free-mb`), has `--serial`, and
  `--rameness-config '{...}'` for flag experiments.
- **Restarted:** v12 = current defaults (everything through anchors, read stubs and note ledger) on Qwen and occamy
  in parallel (separate servers), then v13 = v12 + JEV-gated sub-agents on Qwen. Script: `/root/bench/chain-v12.sh`,
  logs `/root/bench/chain-rameness-v12.log`, `chain-rameness-v13-qwen.log`.

- **occamy at 07:10:** the v12 run decoded at 2-3 tok/s (the server gave 30 tok/s idle minutes before). I stopped it.
  Found a bench bug: when a harness dies, the bench proxy kept its request open, so llama-server went on generating
  for nobody (29k tokens left at 2 tok/s). **Fixed:** the proxy now cancels the upstream request as soon as the client
  hangs up, and on shutdown. The server recovered to ~21-23 tok/s once idle. Leftover requests like this may explain
  some of the overnight slowdowns.
- The runner now also **waits for occamy to reach 15 tok/s** (two probes in a row) before a run, and keeps an earlier
  attempt's directories aside (`*.attempt-HHMMSS`) instead of mixing them into a rerun. occamy v12 restarted 07:55.

- **occamy server has been degraded since ~06:50 and needs your restart.** A second v12 attempt (07:55, after the
  gate passed) ran at 2-3 tok/s again, so I measured it directly, idle, no tools, default sampling:
  **16.4 tok/s over a 400-token reply, 9.2 over 1500, 5.8 over 3000**. Earlier tonight the same server held
  26-32 tok/s at every reply length with Rameness's full request (v7 00:45, v9 05:24). So it's the server's state,
  not the requests; short probes hide it. The gate now probes with a 1500-token reply. occamy v14 is queued
  behind that gate (`/root/bench/chain-v14-occamy.sh`, probes every 15 min for up to 10 h) and starts by itself after
  your planned restart (temp 1.0 / presence 1.5).
- Side result: Rameness's text tool-call mode (for servers without tool templates) now uses the
  `<function=..><parameter=..>` form (code needs no JSON escaping), keeps the model's reasoning in history, and
  stops before the model can invent a `<tool_result>`. Not enabled for any model: native tools were at full
  speed on a healthy server.

## Results (Minecraft task; score = 14 behaviour checks, visual = Claude judge /10)

| Harness / model | Score | Visual | Notes |
|---|---|---|---|
| Rameness v14b / Qwen (repeat) | 85.7% | - | finished at 100 min; world visible but every block flat green |
| Rameness v15 / occamy | 35.7% | - | syntax error -> black page; whole-file rewrites via shell (see above) |
| **Rameness v16 / Qwen (turbo4 150k)** | **92.9%** | - | finished in 89 min; best visuals so far; only breaking blocks failed |
| **Rameness v15 / Qwen** | **100%** | - | + discriminating-check review; real click starts it; textured world |
| **Rameness v14 / Qwen** | **100%** | - | + stand-in principle; timed out at 150 min, 292 requests; real textured world |
| **Rameness v13 / Qwen** | 78.6% | - | v12 + sub-agents (never used) + closest-match edit errors; timed out at 150 min, 385 requests; terrain renders as black streaks (screenshot) - score overstates it |
| **Rameness v12 / Qwen** | 50% | - | finished on its own in 130 min, 256 requests, 8 compactions, no crash; world invisible (see below) |
| Claude Code / Claude Opus 5.5 | 100% | 7.25-8.25 | reference, 14 min |
| Claude Code / Qwen (5k think budget) | 100% | 5.5 | best local build |
| **Rameness v7 / occamy** | **85.7%** | 4.5 | best Rameness; had an index bug (fixed copy renders cleanly) |
| Rameness v3 / Qwen | 85.7% | 2.75 | broken geometry |
| dsh / Qwen | 78.6% | 1.5 | |
| **Rameness v9 / Qwen** | 71.4% | 1.5 | 100 turns in 51 min (2x faster than v8), then crashed: context overflow (fixed, see below) |
| Rameness v8 / Qwen | 57.1% | 0.75 | world invisible: 2 renderer bugs (fixed copy renders) |
| pi / Qwen | 50% | 1.0 | |

**v12 / Qwen, why the world is invisible:** the vertex attribute offsets for UV and shade are swapped (layout is
pos, shade, uv; pointers read uv at 12, shade at 16), so every texture lookup hits a transparent part of the atlas
and the fragment shader discards every pixel. Fixing that alone isn't enough (more renderer bugs remain; copy in
`/srv/bench/fixed/v12-qwen`). The telling part: Qwen spent ~40 min on a thorough test rig - unit smoke tests plus
a "boot simulation" that ran the real game.js against a **stubbed WebGL** - which could never see the one broken
part. Change for v14 (generic): the prompt now says a check that replaces a part with a stand-in (stub, mock,
fake) says nothing about that part; exercise what the user perceives for real and measure its actual output. The
pre-finish self-review also asks, per check, whether it exercised the real thing or a stand-in.

The behaviour score overstates local builds: the recurring failure is a world that runs without errors but renders
wrong, which text-only verification (and the scorer's pixel-change checks) miss.

## Server changes (Qwen, 127.0.0.1:8033)

- 1 slot (was 2), 8-bit KV cache, **100k window**. Measured decode: 55 tok/s at 1k -> 38.5 at 64k -> 33.5 at 96k
  (4-bit KV: 53 -> 21 -> 15). 140k 8-bit does not fit; 120k loads but OOMs on the first request.
- Backups: `/root/models/run-qwen3.8.sh.bak-*`.

## Rameness changes tonight (all generic; 110 tests pass)

| Change | Why (evidence / research) | In run |
|---|---|---|
| Per-turn thinking budget, 5 levels chosen by JEV | Local models don't pace their own thinking like Claude; half of Qwen's turns now get `minimal` | v9 |
| Lifecycle hooks (SOPs the harness runs itself), JEV selects them per task | LLM thinks, JEV decides, SOPs do procedures | v9 |
| Syntax check SOP (`code.syntax_check`), shell toolchains only, any language via config | 80 turns of syntax hunting in earlier runs | v9 |
| **Edit guard**: an edit that breaks a working file is undone | SWE-agent's top interface feature (+3 pts on 12.5%) | v10 |
| **Model-aware sampling** (occamy card: temp 1.0, presence 1.5, keep thinking) | occamy server runs 0.6 / 0.0 | v10 |
| "Re-verify with the check that exposed the bug" | v8 fixed a proxy metric, not the bug | v10 |
| **Compaction fix**: calibrated token estimate, reply reserve, 5-stage compaction (reasoning, old file-write bodies, whole old turns), overflow recovery | v9 crashed: estimate 73k vs real 99k, compaction freed nothing | v10 |
| Anchored edits (`edit_lines`, `N:hh|` handles) | 5% of edits failed "old string not found" | v12 |
| Unchanged re-read stubs (as Claude Code's Read) | 47% of read volume was re-reads | v12 |
| Note ledger (facts, decisions, issues, artifacts) re-injected after compaction/reset | structured state that survives trimming | v12 |
| Failed `edit_file` shows the closest region with anchors (retry without grep+read) | 3 misses in v12 each cost 2-3 turns | v13 |
| Prompt: stand-ins (stubs/mocks) don't verify the part they replace; self-review asks real-vs-stand-in per check | v12's stubbed-WebGL test rig missed the invisible world | v14 |
| Kev in bf16 on CPU | memory kill at 07:25; same choices, 2.3x faster decisions | v12 |
| JEV-gated sub-agents (`delegate`, fresh small context) | conditional spawning; small context = speed | v13 (flag off by default) |
| Per-model harness policies (`model_policies`) | mechanism only, values need evidence | - |
| `rameness sop discover` | finds costly repeated procedures from event logs (report only) | - |

Flags now on by default: `state_ledger`, `dedup_reads`, `line_anchors`. Off: `delegation`.

## Needs your decision

1. **occamy server** (your morning plan): `--temp 1.0 --presence-penalty 1.5`, consider `--fit-ctx 131072`, and
   `--cache-ram 4096` / `-np 1` - the two slowdowns looked like memory pressure on that machine.
