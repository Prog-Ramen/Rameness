# The agent loop

**In brief:** one agent works a task in turns: the model thinks and writes, JEV makes the decisions around
it, and SOPs run as code instead of model turns. `rameness run` runs this loop; every associate on the
Rameness runtime in the fleet does too.

## Routing

Not every task needs an agent; the router picks the cheapest route that works.

```
task ─► Router (JEV) ──┬─ clarify  → ask only for information that changes which SOP applies
                       ├─ direct   → run a validated SOP script, no reasoning-model call
                       ├─ answer   → one model call, no tools (escalates to agent if the model reaches for one)
                       └─ agent    → the loop below
      ─► end of run: review recent runs → new SOPs → tests + privacy checks → PR (see Learning, Sharing)
```

## One agent turn

Each turn: choose a thinking budget, call the model, run tools and hooks, check for trouble, manage context.

1. **Think budget (JEV).** Before every model call JEV picks how hard the next step needs thinking about
   (`minimal` 512, `brief` 2048, `normal` 4096, `deep` 8192, `maximum` 16384 tokens), from what just
   happened: a check that passed, a new error, the same error again. Right after it writes a file, the
   model thinks at `normal` or above (the thinking floor). The level becomes a reasoning-token budget in
   each server's own field (llama.cpp and TabbyAPI `reasoning_budget_tokens`, vLLM
   `thinking_token_budget`, ninfer `thinking_budget`) and an effort level for models that pace themselves.
2. **Model call.** Tools are native tool calls, or a text protocol (`<tool_call><function=…><parameter=…>`,
   values written raw so code needs no escaping) for servers without tool templates. Reasoning is kept in
   the history and sent back in the field its server uses (`reasoning_content`, or `reasoning` on vLLM).
   A call missing a required argument (often a reply cut off mid-call) comes back as an error to retry.
   Wherever Rameness needs JSON from a model, the server is given a JSON schema to constrain the reply
   (`response_format` on llama.cpp, vLLM, TabbyAPI and ninfer; a reply tool on Anthropic models).
3. **Tools.** `bash`, `read_file`, `write_file`, `edit_file`, `edit_lines`, `grep`, `glob`, `web_fetch`,
   `todo_write`, `note`, plus the SOPs selected for the task ([SOPs](sops.md)):
   * `read_file` shows `N:hh|` line anchors; `edit_lines` edits a line range by those handles and refuses
     stale ones. A failed `edit_file` shows where the file comes closest to the old text.
   * Re-reading an unchanged file that is still in context returns a short stub.
   * `read_file` on an image (png, jpg, gif, webp) shows it to models that can see; the latest two images
     stay in view.
   * `bash` refuses a `pkill`/`killall` pattern that would match the agent itself.
   * Output lines over 2,000 characters (minified or generated files) are cut. A large file read without a
     range returns its outline (definitions with line numbers) and its first lines, so the agent reads only
     the parts it needs. Oversized command output is stored and previewed briefly, with a hint to narrow
     the command.
4. **Lifecycle hooks.** SOPs JEV selected for the task run automatically at fixed points (`on_start`,
   `after_output`, `before_finish`, `on_end`) without a model turn. The built-in `code.syntax_check`
   checks every file an edit touched with the language's own toolchain (Python, JS/TS, HTML inline
   scripts, JSON, YAML, TOML, shell, C/C++, Go, Rust, Ruby, PHP, Lua, Swift, Perl…); only new errors are
   reported. The **edit guard** undoes an edit that breaks a file that was fine before it.
5. **Guards between turns.** The [loop guard](#the-loop-guard); a periodic progress review (JEV: on track /
   drifting / stalled); todo reminders when the plan goes stale. After a burst of whole-file writes, a
   **group review** of those files together; after several turns of investigating without changing
   anything, a **group debugging** step that tests several causes at once, simplest first.
6. **Context.** Token counts are calibrated against the server's own numbers; compaction runs in stages
   (old tool results by JEV relevance, old reasoning, large tool-call arguments, long messages, whole old
   turns). Compaction starts while the prompt plus the full requested reply still fits the window
   (`context.reply_tokens`, default 32000, plus 2%), each request's output is cut to the room left, and a
   server-reported overflow still triggers recovery. The **note ledger** (facts, decisions, open issues,
   artifacts the agent recorded) is re-injected after every compaction or reset.
7. **Stopping.** A reply without a tool call is not automatically the end. JEV decides whether the model
   finished or stopped right after announcing a step ("Let me fix…"): the second gets "continue". Open
   todos send it back. Before the first finish, a review asks it to check its work against the task: what
   it verified (output from a tested SOP counts as verified), whether each check exercised the real thing
   (not a stub) from a fresh start, and whether the check would have caught a flaw its user would notice.

## The loop guard

Small and heavily quantized models get stuck; the guard notices without a model call and JEV picks the fix.

The model may repeat the same tool call, oscillate between two actions, repeat an error, restate itself,
or produce degenerate text. Every turn the harness computes these signals, and when one fires JEV
chooses `continue`, `reorient` (restate the goal, what was tried, what not to repeat), `reset` (fresh
context with distilled notes), or `stop` (fail the run so the manager can retry, e.g. on a stronger
model). Interventions escalate if the loop persists, and they appear in the shadow view.

## How it asks models to work

One task-neutral system prompt (code, research, writing, data, media): draft the whole thing, then review and fix in rounds.

Its core rules: first a complete draft of the whole result that runs end to end, then rounds of reviewing
everything together, fixing every issue found in one pass and running it all again; work out early how
the result will be judged and run those checks on the first running draft and after every round; verify
against real use cases and the user's experience; "no errors" is not "done"; a check that replaces a part
with a stand-in says nothing about that part; check from a fresh start, the way the user first meets the
result; when debugging, prefer the simplest cause that explains every anomaly; search narrowly and read
only the parts of large files you need; prefer established, tested libraries for complex, error-prone
parts; keep the last verified version and compare against it when a change breaks it; create and change
files with the file tools, not shell tricks; call a tested SOP instead of redoing its steps, and don't
re-check what it returns. Several of these began as [experiments](experiments.md).

## Local models

Any OpenAI-compatible server works (`--base-url`); Rameness recognizes the common ones and adapts.

Rameness identifies llama.cpp by its `/props`, and vLLM, ninfer and TabbyAPI by `owned_by` in
`/v1/models`, then:

* reads the context window (`/props` n_ctx, else `max_model_len`) and vision support (`/props`
  modalities, else the model's listed input modalities);
* sends per-turn reasoning budgets in the server's own field, nothing to unknown servers (a strict API may
  reject an unknown field), and stops sending it if a server refuses it;
* applies per-model sampling profiles (`sampling: "auto"`), for example Qwen3.x's model-card settings, and
  occamy at temperature 0.6 / presence 0, which beat its card's 1.0 / 1.5 in paired runs;
* applies `model_policies` for per-model harness settings;
* if a server advertises vision but fails on an image (e.g. with a text-only speculative draft model),
  drops images, tells the model, and switches vision off for the run.
