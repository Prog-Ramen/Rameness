# RamenSOPs CI deployment — 2026-10-01

The pipeline and corrections were deployed to `Prog-Ramen/RamenSOPs/main`:

- [PR #3](https://github.com/Prog-Ramen/RamenSOPs/pull/3): SOP checks, secret scanning, semantic review, guarded automatic merging and registry indexing.
- [PR #4](https://github.com/Prog-Ramen/RamenSOPs/pull/4): resolve the immutable PR SHA within the step that uses it; both live required checks passed before merge.
- [PR #5](https://github.com/Prog-Ramen/RamenSOPs/pull/5): forward JSON input into containers, bound diagnostics, identify exact revisions, and permit meaningful linear utilities to reach semantic review; both live required checks passed before merge.

Last confirmed deployed main: `c55cc54121eb506419964d4ee24f5c3808eb5413`.

Main branch protection requires `review` and `scan` from GitHub Actions, strict up-to-date branches, and applies to administrators. Automatic merging uses the checked head SHA and refuses failed, unavailable or uncertain quality reviews. Tests have no credentials or network access; the separate trusted merger never executes SOP code.

The quality reviewer assesses meaningful repeatable behavior, generality, agreement between implementation and description, normal/edge coverage, and declared permissions. Existing SOP updates retain original tests and preserve interface/kind/permissions. Network, execution, side effects, private data, deletions, maintenance changes and uncertain submissions require human review. An automatic merge explicitly dispatches index publication to the `registry` branch.

## Verification

- 31 policy/runner/merge-guard unit tests passed again after the environment changed.
- All workflow files passed actionlint; Rameness documentation whitespace checks passed.
- The deployed main secret scan and registry publication had succeeded before the final correction; the correction's live PR checks also succeeded.
- The known JSONL SOP's 11 cases passed through the trusted local runner, including an independent assertion of the written JSON report's content. This is a local check of reviewed code, not confirmation of the Docker isolation or GitHub merge result. Details: `local-results.json`.

## Remaining live verification

[JSONL SOP PR #2](https://github.com/Prog-Ramen/RamenSOPs/pull/2) was updated to include the final deployed main and the output-file assertion. Final tested branch revision: `d2cb3447c57773064b538d7608627f50474e799d`.

The environment subsequently restricted network access. GitHub CLI could no longer connect, and web access could not retrieve the public pages. Therefore the following are **not yet confirmed**:

1. `sop-check` successfully executes all 11 cases inside Docker for that revision.
2. `secret-scan` passes for that revision.
3. GitHub Models is accessible from the merge job and approves this SOP.
4. The bot merges PR #2 and publishes `data.jsonl_audit` with its indexes/hashes to `registry`.

Read-only commands to resume when GitHub access is available:

```bash
gh pr checks 2 --repo Prog-Ramen/RamenSOPs
gh api repos/Prog-Ramen/RamenSOPs/pulls/2 \
  --jq '{state, merged, merged_by: .merged_by.login, head: .head.sha}'
gh api repos/Prog-Ramen/RamenSOPs/issues/2/comments \
  --jq '.[] | {user: .user.login, body}'
gh run list --repo Prog-Ramen/RamenSOPs --limit 12 \
  --json databaseId,name,status,conclusion,headSha
gh api 'repos/Prog-Ramen/RamenSOPs/contents/sops/data/jsonl_audit?ref=registry' \
  --jq '.[].path'
```

Do not claim end-to-end automatic merging has been verified until these live results are checked. Do not relax branch protection or bypass failed tests or unavailable semantic review to complete this verification.
