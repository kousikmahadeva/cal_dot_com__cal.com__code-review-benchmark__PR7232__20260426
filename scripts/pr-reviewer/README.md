# CR-Killer — PR Reviewer (benchmark fork)

This is a slimmed-down copy of the WaaS `cr-killer` PR reviewer, hosted in this benchmark repo so its review quality can be measured against CodeRabbit on a known PR (`#8087` in the upstream `calcom/cal.com`, mirrored here).

It is built on the Cursor Agent CLI harness (same pattern as WaaS's `auto-regression`/`agent.sh`).

This benchmark fork ships **only** the manual-dispatch reviewer. The nightly `memory.md` condenser that lives in the parent WaaS repo is intentionally NOT included here — benchmarking is the only goal.

- `.github/workflows/pr-reviewer.yml` — manual-dispatch PR reviewer.
  Given a PR number, posts inline review comments (with committable `suggestion` blocks where safe) anchored to diff lines, reconciles prior bot comments via hidden markers, and reply-resolves prior concerns that appear addressed.

## Files

| Path | Purpose |
|---|---|
| `review.sh` | Cursor CLI harness. Mode-agnostic; selected by `PROMPT_PATH` + `AGENT_MODE`. |
| `prompts/reviewer.md` | System prompt for the reviewer agent. Defines the strict `concerns.json` schema. |
| `post-review.py` | Stdlib-only Python. Reconciles prior comments (marker-matched), posts the GitHub review. |

## Hidden marker format

Every inline comment the reviewer posts carries a hidden HTML comment at the end:

```
<!-- cr-killer:concern-id=<12 hex chars>:commit=<HEAD_SHA> -->
```

`concern-id` is a stable 12-char SHA-256 prefix of `path + normalized 5-line context + topic slug`. It survives force-pushes that shift line numbers but keep code shape.

On a subsequent run:
- If the agent re-raises the same `concern-id`, the posting tool **skips** it (no duplicate comment).
- If a prior `concern-id` is not in the new review, the posting tool **replies** to that thread with `Addressed in <sha>. Closing as resolved.` (reply-only per plan; thread is not GraphQL-collapsed).

## Secrets

Configure these as repository secrets on this benchmark repo:

| Secret | Used for |
|---|---|
| `CURSOR_API_KEY` | API key for the Cursor agent CLI. |
| `GH_AUTOMATION_TOKEN` | GitHub PAT (`repo` + `pull_request:write`) used for `gh`, PR checkout, and Reviews API. |

## Triggering the reviewer manually

From the Actions tab on the `coderabbit_killer` branch:

1. Open `CR-Killer PR Reviewer`.
2. Run workflow with:
   - `target_repo` — defaults to this benchmark repo. Override to point at any same-repo PR your `GH_AUTOMATION_TOKEN` can read/write.
   - `pr_number` — the PR number to review.
   - `base_branch` — `main` (safety check).

The run posts a single PR review with all inline comments. Re-running on the same PR is safe: prior markers are reconciled, addressed concerns get reply-resolved, new/remaining concerns get posted.

---

## Local dry-run

Validate the harness end-to-end without hitting GitHub. Requires `cursor-agent` (or `agent`) installed locally and a Cursor API key.

```bash
TARGET_REPO_PATH="$(pwd)"
CTX="/tmp/cr-killer-dryrun"
rm -rf "${CTX}" && mkdir -p "${CTX}"

git fetch origin main --depth=100
merge_base="$(git merge-base origin/main HEAD)"
git diff --no-color "${merge_base}"...HEAD > "${CTX}/pr_diff.patch"
git diff --name-only "${merge_base}"...HEAD > "${CTX}/changed_files.txt"

echo "[]" > "${CTX}/prior_comments.json"

# Reviewer can run without memory.md; an empty stub keeps the prompt happy.
: > "${CTX}/memory.md"
cp .cursor/rules/projectrules.mdc "${CTX}/projectrules.mdc" 2>/dev/null || \
  : > "${CTX}/projectrules.mdc"

export CURSOR_API_KEY="<your-key>"
export AGENT_WORKSPACE="${TARGET_REPO_PATH}"
export TARGET_REPO_PATH
export CONTEXT_DIR="${CTX}"
export PROMPT_PATH="${TARGET_REPO_PATH}/scripts/pr-reviewer/prompts/reviewer.md"
export CONCERNS_OUT="${CTX}/concerns.json"
export SOURCE_PR_REPO="local/dry-run"
export SOURCE_PR_NUMBER="0"
export SOURCE_PR_URL="local"
export SOURCE_PR_HEAD_REF="$(git rev-parse --abbrev-ref HEAD)"
export SOURCE_PR_HEAD_SHA="$(git rev-parse HEAD)"
export SOURCE_PR_BASE_REF="main"
export AGENT_MODE="review"

bash scripts/pr-reviewer/review.sh
jq '.concerns | length, .risk_level, .pr_summary' "${CONCERNS_OUT}"
```

Expected output: a non-empty `concerns.json` that parses cleanly, with every `path` in `changed_files.txt` and every `line` within a diff hunk.

### Dry-run post-review.py without hitting GitHub

```bash
python3 -c "
import json
sample = {
  'pr_summary': 'dry',
  'risk_level': 'low',
  'stats': {'files_changed': 1, 'blockers': 0, 'issues': 1, 'suggestions': 0, 'nits': 0},
  'concerns': [{
    'concern_id': 'aaaabbbbcccc',
    'path': 'README.md', 'line': 1, 'side': 'RIGHT',
    'severity': 'issue', 'topic': 'smoke',
    'title': 'Smoke test',
    'body': 'Dry-run comment body.',
    'suggestion': None,
  }],
}
json.dump(sample, open('/tmp/concerns.sample.json','w'))
print('wrote sample')
"

GH_TOKEN="<pat with no scope needed>" \
TARGET_REPO="local/dry-run" \
PR_NUMBER="0" \
HEAD_SHA="$(git rev-parse HEAD)" \
CONCERNS_PATH="/tmp/concerns.sample.json" \
PRIOR_COMMENTS_PATH="/dev/null" \
DIFF_PATH="/tmp/cr-killer-dryrun/pr_diff.patch" \
python3 scripts/pr-reviewer/post-review.py || true
```

The call will fail at the final POST (fake repo), but the summary output above the failure exercises reconciliation and anchor validation — which is the part you actually want to sanity-check locally.

## Known limitations (explicitly deferred)

- Manual `workflow_dispatch` only. Auto-run on `pull_request` opened/synchronize is deferred.
- Portable target repo via workflow inputs, but only **same-repo** branches (no fork PRs) — matches the WaaS auto-regression MVP.
- Inline review comments only; no walkthrough/summary top-level comment with mermaid diagrams.
- Resolution is reply-only; the thread is not collapsed (no GraphQL `resolveReviewThread`).
- No nightly `memory.md` condenser in this benchmark fork. Reviewer reads `memory.md` from the target repo if present; otherwise runs without learned context.
