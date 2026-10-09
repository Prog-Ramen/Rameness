# Sharing SOPs

**In brief:** every learned SOP starts private. Domain-specific ones stay private; significant,
generalizable ones are proposed to the public RamenSOPs registry; super-generic ones used in most runs are
proposed as built-ins. An SOP's tests go with it. Secrets and private data block every proposal.

## Three tiers

Where an SOP belongs depends on how general it is and how often runs need it.

| Tier | Which SOPs | Where they go | Who merges |
|---|---|---|---|
| **Private** | domain- or organization-specific, or anything uncertain | your private folders ([SOPs](sops.md#where-sops-live)); never pushed | — |
| **Registry** (RamenSOPs) | significant, generalizable SOPs | a PR on [RamenSOPs](https://github.com/Prog-Ramen/RamenSOPs) | its CI gate, or a maintainer |
| **Built-in** (`rameness/builtin_sops/`) | used in at least half of recent successful runs (`builtin_min_share`, over at least `builtin_min_runs` = 10) **and** JEV judges almost every task needs it | a **draft** PR on the Rameness repo | a Rameness developer verifies and merges it |

Built-ins save a network pull on most runs; registry SOPs are fetched only by tasks that need them.
Either kind may list packages it needs. `rameness sop propose <id> --dest builtin|registry` overrides the
choice.

**Only worth-it SOPs are shared:** an SOP must save at least `registry.min_tokens_saved` (1,000) tokens
per use. Each PR states the saving per use and, for a registry SOP, after how many uses its one-time
fetch pays back.

## The proposal pipeline

With `registry.auto_propose: true` (default), a successful task sends validated, shareable SOPs through it.

```
private library ──automatic proposal──► PR on RamenSOPs or Rameness ──CI review + merge──►
  JEV: category, personal vs general    sanitized copy, via a fork if needed
  JEV: private info? secrets block       pre-push hook re-scans every push
```

1. **Categorize.** JEV checks the SOP's proposed `category.name` against the categories that exist and
   moves it into the best fit, keeping a new category only when none fits.
2. **Re-test.** The SOP's tests run again, and must meet the destination's test rules (below).
3. **Scan.** Hard findings always block (below); JEV judges the rest.
4. **Classify.** JEV must be clearly confident (≥ 0.65, margin ≥ 0.15) that the SOP is general to mark it
   `shareable`; anything uncertain stays private unless you pass `--override-personal` (scans still
   apply). `rameness sop classify` shows or redoes this.
5. **Push and open the PR.** A sanitized copy (origin, stats and classification stripped) goes on a
   `sop/<id>-…` branch: to the target repo if you can push there, otherwise to `registry.fork` or a fork
   made with `gh`. The PR is public as soon as it is pushed.

Personal, uncertain, untested or failing SOPs stay private. `rameness sop propose <id>` runs the same
pipeline by hand.

## Tests go where the SOP goes

The tests are part of `sop.json`, so they land wherever the SOP lands, and must be runnable there.

* **Built-in:** in the Rameness repo, where its test suite runs every built-in's tests.
* **Registry:** in RamenSOPs, where CI runs every test in an isolated container, fixtures (`files`,
  `setup`) and error cases included.
* **Private:** with the SOP, on your machine only.

Before proposing, Rameness applies the same rules RamenSOPs CI does, so a proposal does not bounce: 2–40
tests, each asserting concrete values, output keys or an error; at least two with concrete values;
distinct cases; declared inputs; fixtures only at relative paths. A test that only proves the script runs
does not count.

## Safety checks

Secrets never go out; anything that looks private is judged before it does.

* **Hard findings always block.** Secrets (keys, tokens, private keys, JWTs, credentials in URLs,
  gitleaks and trufflehog hits when installed) and your `org.json` `private_terms` can never be proposed
  or overridden.
* **JEV judges the rest.** Emails, private hosts and IPs, and home paths are often harmless
  (`user@example.com` in a docstring). JEV reads the line each one sits on and decides whether it exposes
  private info; if so the proposal is refused.
* **A pre-push hook guards every clone Rameness manages.** It scans every file a push adds or changes,
  wherever it is, so a manual `git push` of a secret is blocked too. It scans the change, not the repo's
  existing content: the Rameness and RamenSOPs repos keep fake secrets in their own tests on purpose.
* **The registry checks again.** RamenSOPs has its own CI gate: metadata and permissions, every test in
  an isolated container, pinned secret scanning, and a model review of usefulness, generality,
  implementation and coverage. Eligible Python SOPs with `compute`, `fs:read` or `fs:write` merge at the
  exact checked commit; compatible updates must keep and pass the original tests; risky or uncertain
  proposals need a maintainer. See the
  [registry policy](https://github.com/Prog-Ramen/RamenSOPs#automated-review-and-merging).
  `rameness sop review . --static` is a conservative local preflight; GitHub CI is authoritative.

## Retries and failures

A proposal never fails your task, and a retry reuses its branch.

Proposal state is saved under `~/.rameness/registry/proposals.json`. Unchanged SOPs already proposed are
skipped. If GitHub fails after the push, the next successful task retries opening the PR on that same
branch. Failures are reported in the task result, `.rameness/sop-proposals.json` and the event log.
Publishing needs authenticated `gh` and Git push access (or a fork); a compare link is never reported as
an opened PR.

## Controls

```json
"registry": {"public": "https://github.com/Prog-Ramen/RamenSOPs.git",
             "builtin": "https://github.com/Prog-Ramen/Rameness.git",
             "builtin_min_share": 0.5, "builtin_min_runs": 10, "min_tokens_saved": 1000,
             "fork": null, "auto_propose": true}
```

`registry.auto_propose: false` disables automatic publishing; readonly mode also disables it. Benchmark
runs disable learning and proposals to keep comparisons isolated; `bench.minecraft.run --sop-pipeline`
turns them on with proposals sent to local stand-in repos.

## Pulling SOPs from the registry

Nothing is mirrored: a task fetches only the SOPs it needs, one tree level at a time, at two moments:

* **When the task is planned,** before the first turn, if no local SOP clearly covers the task.
* **During the task,** when the agent's `sop_search` finds no strong local match. Needs that only appear once
  the work starts (the task said "fix the blank page"; the agent finds it needs a headless browser) are looked
  up then. The pulled SOP is listed in the search result, marked as just pulled, and is callable at once.

Both use the same steps below; a registry that is unreachable leaves the search with its local results.

The registry index is **sharded per category**: `sops/index.json` lists only the top-level categories,
and every category has its own `_index.json` listing only its children. Listings are **columnar**, like
Dremel reading only the columns a query needs: an SOP's entry holds only what JEV chooses by (id,
description, keywords). Its inputs, permissions and file hashes are in its own `_meta.json`, fetched only for
the SOPs JEV picks and checked against the hash the listing gives for it. Categories stay small
([The tree](sops.md#the-tree-stays-small-at-every-level)), so every listing stays small too.

1. **Local first.** The registry is consulted only when no local SOP clearly covers the task or the search
   (best local match < `activate_threshold` + `registry.coverage_margin`).
2. **Lazy traversal, one level at a time** (the fan-out of Google's Dremel serving tree). Every category
   JEV chose to explore at a level is fetched **in parallel**, and their children are scored in parallel,
   one JEV decision per category. A search costs one round trip per tree level: about 0.3 s instead of
   0.9 s on a three-level registry at 100 ms per request. Rejected branches are never downloaded; a branch
   whose `requires` aren't met is deferred without being opened; a listing that misses the level's
   deadline uses its cached copy or is left out instead of stalling the search. Listings are cached for 6
   hours; after that a conditional request (ETag / Last-Modified) turns an unchanged listing into a 304.
3. **Pull decision per candidate.** JEV decides, then the comfort gate applies. Permissions outside
   `permissions.sop_allow` need your yes; with no user present the SOP is skipped.
4. **Verified install.** Only the chosen SOP's files are downloaded, in parallel, each checked against the
   SHA-256 in its listing, into `~/.rameness/public/<registry>/sops`. Its tests must pass, otherwise it is
   removed again.

`registry.auto_pull`: `gated` (default) | `ask` (always ask) | `off`. `rameness sop pull <id>` pulls one
SOP by walking only its ancestors' listings. Every pull decision appears in the shadow view.
