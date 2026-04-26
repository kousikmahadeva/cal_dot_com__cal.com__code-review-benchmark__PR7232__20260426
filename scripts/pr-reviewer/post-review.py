#!/usr/bin/env python3
"""Post CR-Killer review to GitHub.

Responsibilities (per plan):
  1. Load agent-produced concerns.json.
  2. Load prior bot-marked PR review comments on this PR.
  3. Parse hidden markers: <!-- cr-killer:concern-id=<id>:commit=<sha> -->
  4. For each prior concern_id NOT present in current concerns: post a reply-only
     "Addressed in <HEAD_SHA>. Closing as resolved." on that thread.
  5. For each current concern: post a single review (event=COMMENT) with all
     inline comments. Skip ids already present in prior comments (idempotent).
  6. Wrap each body with a hidden marker and optional ```suggestion block.
  7. Print a clean summary table to workflow log.

Stdlib only: json, os, sys, re, urllib, hashlib (unused here but imported safely).

Environment variables required:
  GH_TOKEN                 GitHub token with repo + pull_request:write.
  TARGET_REPO              owner/name
  PR_NUMBER                PR number
  HEAD_SHA                 PR head commit SHA (post review against this commit).
  CONCERNS_PATH            Path to concerns.json.
  PRIOR_COMMENTS_PATH      Path to prior_comments.json (pre-filtered to cr-killer).
  DIFF_PATH                Path to pr_diff.patch (for anchor validation).
  MARKER_PREFIX            Optional; defaults to "cr-killer".
"""

from __future__ import annotations

import json
import os
import re
import sys
import urllib.error
import urllib.request
from typing import Any

GITHUB_API = "https://api.github.com"

MARKER_PREFIX_DEFAULT = "cr-killer"


def env_required(name: str) -> str:
    val = os.environ.get(name, "").strip()
    if not val:
        print(f"ERROR: missing required env var {name}", file=sys.stderr)
        sys.exit(2)
    return val


def env_optional(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def gh_request(
    token: str,
    method: str,
    path: str,
    body: dict[str, Any] | None = None,
) -> tuple[int, dict[str, Any] | list[Any] | None]:
    url = f"{GITHUB_API}{path}"
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url=url, method=method, data=data)
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("X-GitHub-Api-Version", "2022-11-28")
    req.add_header("User-Agent", "cr-killer-bot")
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read().decode("utf-8") if resp.length != 0 else ""
            parsed = json.loads(raw) if raw else None
            return resp.status, parsed
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace") if exc.fp else ""
        try:
            parsed = json.loads(raw) if raw else {"message": str(exc)}
        except json.JSONDecodeError:
            parsed = {"message": raw or str(exc)}
        return exc.code, parsed


def parse_marker(body: str, marker_prefix: str) -> tuple[str, str] | None:
    pattern = rf"<!--\s*{re.escape(marker_prefix)}:concern-id=([a-f0-9]{{6,64}}):commit=([0-9a-f]{{7,40}})\s*-->"
    m = re.search(pattern, body or "")
    if not m:
        return None
    return m.group(1), m.group(2)


def parse_diff_right_lines(diff_text: str) -> dict[str, set[int]]:
    """Return {file_path: set of valid RIGHT-side line numbers for comments}."""
    file_lines: dict[str, set[int]] = {}
    current_path: str | None = None
    current_new_line: int | None = None
    for raw in diff_text.splitlines():
        if raw.startswith("+++ "):
            target = raw[4:]
            if target.startswith("b/"):
                target = target[2:]
            elif target == "/dev/null":
                target = None
            current_path = target
            current_new_line = None
            if current_path is not None and current_path not in file_lines:
                file_lines[current_path] = set()
            continue
        if raw.startswith("--- "):
            continue
        if raw.startswith("@@"):
            m = re.match(r"@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@", raw)
            if m:
                current_new_line = int(m.group(1))
            else:
                current_new_line = None
            continue
        if current_path is None or current_new_line is None:
            continue
        if raw.startswith("+") and not raw.startswith("+++"):
            file_lines.setdefault(current_path, set()).add(current_new_line)
            current_new_line += 1
        elif raw.startswith(" "):
            file_lines.setdefault(current_path, set()).add(current_new_line)
            current_new_line += 1
        elif raw.startswith("-") and not raw.startswith("---"):
            pass
        else:
            pass
    return file_lines


def load_json(path: str, default: Any) -> Any:
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except FileNotFoundError:
        return default
    except json.JSONDecodeError as exc:
        print(f"ERROR: failed to parse JSON at {path}: {exc}", file=sys.stderr)
        sys.exit(2)


SEVERITY_PILL = {
    "blocker": ("🛑", "_Blocker_", "🔴 Critical"),
    "issue": ("⚠️", "_Potential issue_", "🟠 Major"),
    "suggestion": ("💡", "_Suggestion_", "🟡 Minor"),
    "nit": ("✨", "_Nitpick_", "🟢 Trivial"),
}


def format_comment_body(
    concern: dict[str, Any],
    marker_prefix: str,
    head_sha: str,
) -> str:
    severity = concern.get("severity", "issue")
    title = concern.get("title", "").strip()
    body = concern.get("body", "").strip()
    suggestion = concern.get("suggestion")
    concern_id = concern.get("concern_id", "").strip()
    topic = str(concern.get("topic", "")).strip()
    analysis = str(concern.get("analysis", "")).strip()
    citation = str(concern.get("citation", "")).strip()

    emoji, pill_label, severity_tag = SEVERITY_PILL.get(
        severity, SEVERITY_PILL["issue"]
    )

    parts: list[str] = []
    parts.append(f"{emoji} {pill_label} | {severity_tag}")
    parts.append("")

    if analysis:
        parts.append("<details>")
        parts.append("<summary>🧩 Analysis chain</summary>")
        parts.append("")
        parts.append(analysis)
        parts.append("")
        parts.append("</details>")
        parts.append("")

    parts.append(f"**{title}**")
    parts.append("")
    parts.append(body)

    if citation:
        parts.append("")
        parts.append(f"> {citation}")

    if suggestion is not None and isinstance(suggestion, str) and suggestion.strip():
        parts.append("")
        parts.append("```suggestion")
        parts.append(suggestion.rstrip("\n"))
        parts.append("```")

    if topic:
        parts.append("")
        parts.append(
            f"<sub>topic: `{topic}` · id: `{concern_id}` · "
            f"from CR-Killer, open an issue if this was unhelpful.</sub>"
        )

    parts.append("")
    parts.append(
        f"<!-- {marker_prefix}:concern-id={concern_id}:commit={head_sha} -->"
    )
    return "\n".join(parts)


def decide_review_event(concerns: list[dict[str, Any]]) -> str:
    """Return 'REQUEST_CHANGES' if any concern is a blocker, else 'COMMENT'."""
    for c in concerns:
        if str(c.get("severity", "")).lower() == "blocker":
            return "REQUEST_CHANGES"
    return "COMMENT"


ACTIONABLE_SEVERITIES = ("blocker", "issue", "suggestion")
NITPICK_SEVERITIES = ("nit",)


def _concern_one_liner(concern: dict[str, Any]) -> str:
    """Render a concern as a compact 'path:line — title' line for summary lists."""
    path = str(concern.get("path", "")).strip() or "<unknown>"
    line = concern.get("line")
    title = str(concern.get("title", "")).strip() or "(untitled concern)"
    if isinstance(line, int):
        return f"`{path}:{line}` — {title}"
    return f"`{path}` — {title}"


def build_review_header(
    event: str,
    risk_level: str,
    pr_summary: str,
    actionable_concerns: list[dict[str, Any]],
    nit_concerns: list[dict[str, Any]],
    kept_prior: int,
    addressed: int,
    head_sha: str,
    changed_files_count: int,
    pr_number: str,
    repo: str,
) -> str:
    """Top-of-review body, styled after CodeRabbit.

    Layout:
        Actionable comments posted: N

        <details>Nitpick comments (M)   (only if M > 0)
          - path:line — title
        </details>

        ---

        <details>Review info
          risk / summary / reconciliation
        </details>

        <details>Review details
          commit, files, repo
        </details>
    """
    actionable_count = len(actionable_concerns)
    nit_count = len(nit_concerns)

    lines: list[str] = []

    if event == "REQUEST_CHANGES":
        lines.append(f"**Actionable comments posted: {actionable_count}** — requesting changes on this PR.")
    else:
        lines.append(f"**Actionable comments posted: {actionable_count}**")
    lines.append("")

    if nit_count:
        lines.append("<details>")
        lines.append(f"<summary>🧹 Nitpick comments ({nit_count})</summary>")
        lines.append("")
        for c in nit_concerns[:25]:
            lines.append(f"- {_concern_one_liner(c)}")
        if nit_count > 25:
            lines.append(f"- _…and {nit_count - 25} more._")
        lines.append("")
        lines.append("</details>")
        lines.append("")

    lines.append("---")
    lines.append("")

    risk_emoji = {
        "low": "🟢",
        "medium": "🟡",
        "high": "🔴",
    }.get(str(risk_level).lower(), "⚪")

    lines.append("<details>")
    lines.append("<summary>ℹ️ Review info</summary>")
    lines.append("")
    lines.append(f"**Overall risk:** {risk_emoji} {risk_level}")
    if pr_summary.strip():
        lines.append("")
        lines.append("**Summary**")
        lines.append("")
        lines.append(pr_summary.strip())
    if kept_prior or addressed:
        lines.append("")
        lines.append("**Reconciliation with prior runs**")
        if kept_prior:
            lines.append(f"- Kept **{kept_prior}** concern(s) from a prior run that are still valid.")
        if addressed:
            lines.append(f"- Replied to **{addressed}** concern(s) that now appear addressed.")
    lines.append("")
    lines.append("</details>")
    lines.append("")

    lines.append("<details>")
    lines.append("<summary>📋 Review details</summary>")
    lines.append("")
    lines.append(f"- **Repo:** `{repo}`")
    lines.append(f"- **PR:** [#{pr_number}](https://github.com/{repo}/pull/{pr_number})")
    lines.append(f"- **Commit reviewed:** `{head_sha[:12]}`")
    lines.append(f"- **Files changed:** {changed_files_count}")
    lines.append(f"- **Actionable:** {actionable_count} · **Nitpicks:** {nit_count}")
    lines.append("")
    lines.append(
        "_Generated by CR-Killer. Reply to any inline thread once addressed; the next run will mark it resolved._"
    )
    lines.append("")
    lines.append("</details>")

    return "\n".join(lines).rstrip() + "\n"


def main() -> int:
    token = env_required("GH_TOKEN")
    repo = env_required("TARGET_REPO")
    pr_number = env_required("PR_NUMBER")
    head_sha = env_required("HEAD_SHA")
    concerns_path = env_required("CONCERNS_PATH")
    prior_path = env_required("PRIOR_COMMENTS_PATH")
    diff_path = env_required("DIFF_PATH")
    marker_prefix = env_optional("MARKER_PREFIX", MARKER_PREFIX_DEFAULT)

    if "/" not in repo:
        print(f"ERROR: TARGET_REPO must be owner/name, got {repo!r}", file=sys.stderr)
        return 2
    owner, name = repo.split("/", 1)

    payload = load_json(concerns_path, {"concerns": []})
    concerns: list[dict[str, Any]] = payload.get("concerns", []) or []
    pr_summary = payload.get("pr_summary", "")
    risk_level = payload.get("risk_level", "unknown")

    prior_comments: list[dict[str, Any]] = load_json(prior_path, []) or []

    try:
        with open(diff_path, encoding="utf-8") as fh:
            diff_text = fh.read()
    except FileNotFoundError:
        diff_text = ""

    valid_right_lines = parse_diff_right_lines(diff_text)

    prior_by_id: dict[str, dict[str, Any]] = {}
    for entry in prior_comments:
        body = entry.get("body") or ""
        parsed = parse_marker(body, marker_prefix)
        if not parsed:
            continue
        cid, _commit = parsed
        prior_by_id.setdefault(cid, entry)

    current_ids: set[str] = set()
    valid_concerns: list[dict[str, Any]] = []
    skipped_invalid: list[tuple[str, str]] = []
    skipped_duplicate: list[str] = []

    for concern in concerns:
        cid = str(concern.get("concern_id", "")).strip()
        path = str(concern.get("path", "")).strip()
        line = concern.get("line")
        side = concern.get("side", "RIGHT")
        if not cid or not path or not isinstance(line, int):
            skipped_invalid.append((cid or "<no-id>", "missing concern_id/path/line"))
            continue
        if side != "RIGHT":
            skipped_invalid.append((cid, f"unsupported side={side}"))
            continue
        if diff_text and path not in valid_right_lines:
            skipped_invalid.append((cid, f"path {path} not in PR diff"))
            continue
        if diff_text and line not in valid_right_lines.get(path, set()):
            skipped_invalid.append(
                (cid, f"{path}:{line} not in RIGHT-side hunk lines")
            )
            continue

        current_ids.add(cid)

        if cid in prior_by_id:
            skipped_duplicate.append(cid)
            continue

        valid_concerns.append(concern)

    addressed_ids = [cid for cid in prior_by_id.keys() if cid not in current_ids]

    severity_counts: dict[str, int] = {"blocker": 0, "issue": 0, "suggestion": 0, "nit": 0}
    actionable_concerns: list[dict[str, Any]] = []
    nit_concerns: list[dict[str, Any]] = []
    for c in valid_concerns:
        sev = str(c.get("severity", "issue")).lower()
        if sev in severity_counts:
            severity_counts[sev] += 1
        if sev in NITPICK_SEVERITIES:
            nit_concerns.append(c)
        else:
            actionable_concerns.append(c)

    changed_files_count = len(
        {
            str(c.get("path", "")).strip()
            for c in valid_concerns
            if str(c.get("path", "")).strip()
        }
    )
    if diff_text:
        changed_files_count = max(changed_files_count, len(valid_right_lines))

    review_event = decide_review_event(valid_concerns)

    review_posted = False
    review_comment_count = 0
    review_error: str | None = None

    if valid_concerns:
        review_comments = []
        for c in valid_concerns:
            review_comments.append(
                {
                    "path": c["path"],
                    "line": int(c["line"]),
                    "side": c.get("side", "RIGHT"),
                    "body": format_comment_body(c, marker_prefix, head_sha),
                }
            )

        review_body = build_review_header(
            event=review_event,
            risk_level=risk_level,
            pr_summary=pr_summary,
            actionable_concerns=actionable_concerns,
            nit_concerns=nit_concerns,
            kept_prior=len(skipped_duplicate),
            addressed=len(addressed_ids),
            head_sha=head_sha,
            changed_files_count=changed_files_count,
            pr_number=pr_number,
            repo=repo,
        )

        review_payload = {
            "commit_id": head_sha,
            "body": review_body,
            "event": review_event,
            "comments": review_comments,
        }
        status, resp = gh_request(
            token,
            "POST",
            f"/repos/{owner}/{name}/pulls/{pr_number}/reviews",
            body=review_payload,
        )
        if 200 <= status < 300:
            review_posted = True
            review_comment_count = len(review_comments)
        else:
            msg = ""
            if isinstance(resp, dict):
                msg = resp.get("message", "") or ""
                if "errors" in resp:
                    msg = f"{msg} | errors={resp['errors']}"
            review_error = f"HTTP {status}: {msg}"
            print(f"ERROR: failed to post review: {review_error}", file=sys.stderr)

    replied_to = 0
    reply_failures: list[tuple[str, str]] = []
    for cid in addressed_ids:
        prior = prior_by_id[cid]
        prior_comment_id = prior.get("id")
        if not prior_comment_id:
            reply_failures.append((cid, "prior comment missing id"))
            continue
        reply_body = (
            f"Addressed in `{head_sha[:12]}`. Closing as resolved.\n\n"
            f"<!-- {marker_prefix}:concern-id={cid}:commit={head_sha}:resolved=1 -->"
        )
        status, resp = gh_request(
            token,
            "POST",
            f"/repos/{owner}/{name}/pulls/{pr_number}/comments/{prior_comment_id}/replies",
            body={"body": reply_body},
        )
        if 200 <= status < 300:
            replied_to += 1
        else:
            msg = ""
            if isinstance(resp, dict):
                msg = resp.get("message", "") or ""
            reply_failures.append((cid, f"HTTP {status}: {msg}"))

    print("")
    print("===== CR-Killer summary =====")
    print(f"Repo:             {repo}")
    print(f"PR:               #{pr_number}")
    print(f"Head SHA:         {head_sha}")
    print(f"Risk level:       {risk_level}")
    print(f"Review event:     {review_event}")
    print(f"Concerns total:   {len(concerns)}")
    print(
        f"  blockers:       {severity_counts['blocker']}, "
        f"issues: {severity_counts['issue']}, "
        f"suggestions: {severity_counts['suggestion']}, "
        f"nits: {severity_counts['nit']}"
    )
    print(f"  posted new:     {review_comment_count}")
    print(f"  kept (dup):     {len(skipped_duplicate)}")
    print(f"  dropped (bad):  {len(skipped_invalid)}")
    print(f"Addressed:        {len(addressed_ids)} (replied: {replied_to})")
    if review_posted:
        print(f"Review status:    posted as {review_event}")
    elif valid_concerns:
        print(f"Review status:    FAILED ({review_error})")
    else:
        print("Review status:    no new concerns; no review posted")
    if skipped_invalid:
        print("")
        print("Dropped (invalid anchors):")
        for cid, why in skipped_invalid[:20]:
            print(f"  - {cid}: {why}")
        if len(skipped_invalid) > 20:
            print(f"  ... and {len(skipped_invalid) - 20} more")
    if reply_failures:
        print("")
        print("Reply failures:")
        for cid, why in reply_failures[:20]:
            print(f"  - {cid}: {why}")
    print("=============================")

    if valid_concerns and not review_posted:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
