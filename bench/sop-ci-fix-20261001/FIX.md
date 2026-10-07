# Semantic reviewer recovery fix

The bot comment for JSONL SOP PR #2, head `d2cb3447c57773064b538d7608627f50474e799d`, reports `Semantic review unavailable (JSONDecodeError)`. This is an infrastructure/parsing failure, not a negative SOP judgment. The deployed merger reaches this step only after both exact-head SOP and secret-scanning workflows have succeeded. Its existing handler obscures whether the malformed JSON is the API response body or the model's completion text.

## Prepared changes

- Distinguish API-body parsing, response structure, completion truncation, model JSON, judgment schema, transport and HTTP errors. Report safe metadata and parser positions without logging raw bodies, completions, submission text or credentials.
- Explicitly request non-streaming JSON output; retry eligible failures at most three times with 2/4-second backoff and 1,000/2,000/4,000-token output budgets. HTTP authorization errors and refusals do not get blind retries.
- Normalize an entire, single JSON code fence. Reject surrounding prose, duplicate keys, partial/truncated output, extra fields, non-boolean criteria and empty reasons. No parse error or partial response can approve a merge.
- Label reviewer outages `sop:reviewer-error`, pending checks/branch updates `sop:checking`, and failed mechanical checks `sop:checks-failed`. Reserve `sop:needs-human` for actual policy concerns or negative judgments. Remove old managed labels when the state changes.
- Retain exact-head checks, branch protection and refusal to merge on unavailable semantic review. Infrastructure errors are not cached and the existing scheduled pass retries them.

## Validation

The initial parsing-recovery validation passed 48 local tests, including malformed/empty/truncated JSON, valid negative judgments, fenced JSON with strict booleans, duplicate keys, authorization failures, bounded retries, diagnostics without response contents, label cleanup and an integration test proving an unavailable reviewer never calls the merge API. Existing policy/runner/check-identity/cache tests also pass. The expanded patch passes `git apply --check` against the last local RamenSOPs checkout.

The exact cause of the live `JSONDecodeError` remains unknown without response-stage diagnostics. The recovery fix is prepared and tested, **not deployed**: this session cannot write outside the Rameness workspace or connect to GitHub.

## Resume deployment with authorized access

`reviewer-recovery.patch` is ready to apply in a clean RamenSOPs checkout at the deployed CI baseline (`c55cc54121eb506419964d4ee24f5c3808eb5413`). If main has advanced, review and reconcile the patch against current files first.

```bash
git switch -c ci/semantic-review-recovery origin/main
git apply /root/projects/Rameness/bench/sop-ci-fix-20261001/reviewer-recovery.patch
PYTHONDONTWRITEBYTECODE=1 python -m unittest discover -s tests -q
git diff --check
```

Publish the correction as a maintenance PR and merge it only after the required checks pass. Then update the JSONL SOP branch with current main (strict branch protection requires it), let its PR checks rerun, and inspect the new reviewer diagnostics/decision. Confirm the automatic merge and registry publication. Do not remove the quality gate or override failed review to make the verification succeed.

## Additional prepared security changes

Independent Gitleaks scanning precedes external model review, including intermediate PR commits. Explicit private/non-public markers and catastrophic filesystem deletion trigger rejection. Other suspected prohibited content needs two evidence-grounded model judgments agreeing on category, file and line; disagreement or unavailable review blocks merging. This does not establish provenance or eliminate adversarial model errors.

Confirmed prohibited submissions are closed and an unchanged, exclusively owned `sop/` branch in this repository is deleted with an atomic lease. Forks, shared branches and changed heads are preserved for appropriate follow-up. Comments never publish secret values or private snippets. Closing and deleting a branch cannot purge GitHub PR refs, caches or forks; exposed credentials must be revoked and retained copies handled separately.

The optional Kev quality adapter is described in KEV.md. It is prepared but its trusted service startup and adversarial evaluation are still outstanding. None of these security or Kev changes are deployed. The latest local suite has 73 passing tests; live deployment checks remain outstanding.

## Current deployment preference

Keep GPT-4o mini for CI review until Kev hosting is available and validated. Leave `SOP_REVIEW_BACKEND` unset or set it to `github-models`; leave `SOP_REVIEW_MODEL` unset or set it to `openai/gpt-4o-mini`. The optional Kev adapter must remain disabled.

PR #2 remains unverified live from this session: the latest GitHub API status request failed because network access is unavailable. The last observed bot result was a JSONDecodeError in semantic review at d2cb344, which blocks automatic merge. Deploy the parsing recovery change before retrying; then require a successful review and exact-head checks rather than bypassing the gate.
