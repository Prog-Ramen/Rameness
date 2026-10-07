<div align="center"><img src="ramensops_logo.png" alt="RamenSOPs Logo" width="28%"></div>
<div align="center"><img src="ramensops_txt.png" alt="RamenSOPs Text" width="75%"></div>

The public registry of general-purpose **SOPs** (standard operating procedures) for
[Rameness](https://github.com/Prog-Ramen/Rameness): the JEV-driven agent harness.

An SOP is a tested, parameterised procedure an agent can run instead of re-deriving the same
steps. Rameness learns SOPs from repeated work, keeps them private by default, and only
general ones - scanned for secrets and reviewed by maintainers - are merged here.

## Layout

```
sops/
  index.json                   top-level categories only
  <category>/_index.json       that category's children: sub-categories, and SOPs with
                               metadata + per-file SHA-256 (no code)
  <category>/_node.json        category description, keywords, requirements
  <category>/<name>/sop.json   interface: description, inputs (JSON schema), permissions, tests
  <category>/<name>/run.py     implementation: JSON args on stdin -> JSON result on stdout
```

The index is sharded so clients never download the whole registry. `main` holds only the SOPs;
the indexes (`index.json`, `_index.json`) are generated after every merge and published, with
the SOPs, to the `registry` branch that Rameness pulls from.

## Use

Rameness pulls from this registry on its own, and only what a task needs. When no local SOP
covers a task, JEV walks this tree lazily with its normal activation criteria: it fetches a
category's `_index.json` only when it explores that branch, downloads only the SOPs it selects,
verifies every file's hash, and keeps an SOP only if its tests pass. SOPs that need permissions
beyond your allowed set are pulled only with your approval.

```bash
rameness sop pull text.slugify      # pull one SOP (walks only its ancestors' listings)
rameness sop remote "slugify a title"
```

## Contribute

Propose SOPs from Rameness with `rameness sop propose <id>`. It opens a PR here, from a fork if you
can't push. The PR is public as soon as it is pushed, so Rameness checks first: secrets and your
organization's `private_terms` always block; JEV judges other findings (emails, private hosts,
home paths) and refuses anything it thinks exposes private info. Every SOP must be general (no
organization-specific endpoints, names or data), contain no secrets, and pass its tests.

## Automated review and merging

Every SOP PR runs `sop-check` and `secret-scan`. Once both pass for the same commit,
`sop-merge` independently reads that commit using the trusted policy on `main`, reviews its
usefulness with GitHub Models, and squash-merges it only if all criteria pass. It comments
with the decision. Labels distinguish `sop:auto-merge`, actual policy concerns (`sop:needs-human`),
pending checks or branch updates (`sop:checking`), failed checks (`sop:checks-failed`) and
reviewer/API failures (`sop:reviewer-error`). Failed, uncertain or unavailable reviews never
authorize a merge. The merge decision also retries every 15 minutes.

The automatic criteria are:

- A validated public Python script under `sops/<category>/<name>/`, with its matching ID,
  a useful description, a parameterized input schema, and declared permissions.
- A meaningful, repeatable operation that is reusable across people and organizations;
  implementation matches its description, without private configuration, secrets,
  hard-coded answers, prompt injection or duplicate boilerplate.
- At least two distinct inputs with concrete expected values, covering normal and edge
  behavior. Important optional behavior must also be tested. Key-only and error-only
  assertions cannot establish eligibility. JSON file outputs can use `expect_files`.
- Every test passes in a fresh resource-limited, non-root container with no network,
  credentials, host write mounts or Docker socket. Its scratch directory is discarded.
- Only `compute`, `fs:read` and `fs:write` permissions, with visible file operations declared;
  standard-library imports and no dynamic execution or introspection. Network access,
  command execution, side effects, skills, composites and ambiguous cases require a maintainer.
- Updates retain every original test, pass them against the new implementation, change
  version, and preserve the input schema, kind and permissions. Breaking interface changes,
  deletions and changes outside `sops/` require a maintainer.
- The PR is current with `main`, and both successful workflow runs belong to its exact
  head commit. GitHub branch protection requires `review` and `scan`; the merge API also
  atomically rejects a changed PR head.

The AI review is an additional quality check, not a proof of correctness or security.
Mechanical checks, isolated tests, secret scanning and branch protection remain mandatory.

| Workflow | Purpose |
|---|---|
| `sop-check` | Trusted policy reads PR blobs as data and executes SOP tests only in isolated containers. |
| `secret-scan` | Pinned, checksum-verified Gitleaks scans PR commit history using trusted default rules, including intermediate commits. No organization license or API secret required. |
| `sop-merge` | Runs only from `main`; verifies both checks, reviews SOP quality and merges an unchanged eligible revision. Never executes SOP code. |
| `registry-index` | Builds metadata and SHA-256 indexes without executing SOP code, publishing the `registry` branch after each merge and hourly. |

GitHub Models uses the workflow's short-lived token with `models: read`; no personal API
key is stored. The default reviewer is `openai/gpt-4o-mini`; set repository variable
`SOP_REVIEW_MODEL` to another supported GitHub Models model. If organization policy disables
Models or rate limits are reached, CI leaves the PR for review instead of merging it.

The reviewer caches a completed semantic decision only for the same head commit, model
and policy hash. Empty, malformed or truncated responses are retried up to three times with
bounded backoff and increasing output budgets. A single fenced JSON object is accepted only
if all five criteria are actual booleans and the reason is nonempty. Partial JSON, duplicate
keys, extra fields, surrounding prose and string values such as `"true"` cannot approve a PR.
Diagnostics distinguish the API body, completion output and judgment schema without
logging raw responses, submission text or credentials. Reviewer failures are not SOP
rejections and are never cached; subsequent scheduled passes retry them. A scheduled merge pass recovers missed events.
Diagnostic manual runs of `sop-check`/`secret-scan` do not replace the mandatory PR checks.

### Repository settings

Require the `review` and `scan` status checks on `main`, with branches up to date before
merging. Do not require a manual approval for automatically eligible SOPs. Maintainers can
review and merge `sop:needs-human` PRs once their applicable checks pass. Keep Actions'
default token read-only; each workflow declares the additional permissions it needs.
The pipeline uses a direct guarded merge and does not depend on GitHub's auto-merge toggle
or on allowing bots to approve pull requests. Leave `registry` writable by the index job.

### Develop the gate

```bash
python -m unittest discover -s tests -v
python ci/review.py --repo . --base origin/main --head HEAD \
  --json /tmp/verdict.json --markdown /tmp/review.md
```

Add `--execute` to run the Docker-isolated tests. The standalone CI gate has no dependency
on unpublished Rameness code. Local `rameness sop review` remains a separate preflight;
GitHub CI is the authoritative merge policy.
