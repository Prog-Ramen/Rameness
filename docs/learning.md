# Learning

**In brief:** at the end of each run, the model reviews its recent runs for multi-step tasks it repeated;
Kev decides which are reusable, a value check keeps only the ones worth it, and the model writes each as a
tested SOP. Nothing is learned mid-run, and nothing is a hand-written rule.

## The flow

Review, score, check value, deduplicate, write, test, register.

1. **Review.** After a successful run, the model is shown its last `learning.review_runs` (5) runs, step by
   step, and names the multi-step tasks it carried out more than once, in one run or across runs, with the
   steps of each repetition. The same task on other files or another language counts as the same (e.g.
   syntax-checking Python, then Rust). Single commands, editing the project's own content, exploration, SOP
   calls and re-checks of an SOP's output are left out. The reply is schema-constrained JSON.
2. **Score.** Kev judges how likely each is to recur in future tasks, combined with how often it repeated:
   `p = 1 - (1 - p_kev)(1 - p_repeat)`.
3. **Worth it?** Using an SOP costs the model too: reading its interface and writing the call. The saving
   per use is the tokens the model spent producing those steps (measured from the turns that produced them,
   or estimated from their size) minus that cost. A candidate must save at least
   `learning.min_tokens_saved_per_use` (500) per use, and over the uses expected, more than generating and
   testing it costs (`learning.sop_creation_tokens`, 4,000). A short procedure the model writes in seconds
   is left alone.
4. **Already exists?** Kev shortlists existing SOPs by description, then confirms each one pairwise against
   the procedure's steps and the SOP's own code; sharing a word ("check", "verify") is not a duplicate.
5. **Write it.** An exact repeated shell sequence becomes a script with no model call. Otherwise the model
   writes the script and its tests (with fixtures where they need files). A script that doesn't compile is
   sent back with the compiler error.
6. **Extend before adding** (below), else register it as a new SOP.
7. **Test and repair.** The tests run; on failures the model sees what each test actually returned and
   repairs the script or the tests, up to 3 rounds. A repair that drops tests or their assertions is
   refused; one that makes things worse is reverted. Every attempt is recorded on the SOP
   (`origin.repair_attempts`).
8. **Register.** `validated` if its tests pass, else `candidate` (hidden from the agent; `rameness sop
   promote <id>` after review). A failing near-copy of an SOP that already works is dropped.
9. **Share.** After the task succeeds, validated, shareable SOPs go through the
   [proposal pipeline](sharing.md).

Run history is per user (`~/.rameness/runs`), so repeats across projects count.

## Extend before adding

A close SOP grows a small, backward-compatible feature instead of getting a near-duplicate.

When Kev judges a small change would cover the new procedure (an extra optional parameter or output), the
model extends the existing SOP. The extension is kept only if every existing test still passes, its new
tests pass, no input is dropped and no new input is required. A private SOP is updated in place (minor
version bump; `origin.extended` records what was added); a built-in or registry SOP gets a private
override with the same id, which proposing turns into a PR that updates the original. The agent's own
`sop_save` follows the same path.

## Measured savings

Every SOP call records what it actually saved.

Each call is measured against the cost of generating those steps (recorded when the SOP was learned, else
estimated from its code): tokens and seconds saved, minus the call's own cost. The totals are kept per SOP
(`sop_stats.json`) and per run, and feed the decision to share an SOP ([Sharing](sharing.md)).

## Finding candidates yourself

`rameness sop discover` reports recurring, costly procedures in your run logs (report only).

```bash
rameness sop discover .rameness/runs --top 10
```

## Settings

```json
"learning": {"enabled": true, "review_runs": 5, "sop_threshold": 0.6,
             "min_tokens_saved_per_use": 500, "sop_creation_tokens": 4000}
```

`--no-learn` turns the end-of-run review off for a run; agent-saved SOPs still work.
