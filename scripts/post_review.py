#!/usr/bin/env python3
"""Post Review — merge stage findings and publish as a GitHub PR review.

Collects JSON findings from Stage 1 (pattern + format), Stage 2 (clang-tidy),
and Stage 3 (LLM) and posts a single PR review with inline comments and
suggestion blocks.

Usage:
    python -m scripts.post_review \\
        --pr-number 42 \\
        --repo owner/repo \\
        --commit-sha abc123 \\
        --findings findings-stage1.json suggestions-format.json \\
        --token $GIT_ACTION_TOKEN \\
        --api-url https://github.company.com/api/v3 \\
        --output review-result.json

    # Dry-run mode (no API calls):
    python -m scripts.post_review \\
        --findings findings-stage1.json \\
        --dry-run \\
        --output review-payload.json
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.utils.gh_api import GitHubClient
from scripts.utils.diff_parser import parse_diff

# Maximum number of inline comments per review (GitHub API limit).
MAX_COMMENTS_PER_REVIEW = 50

# Severity ordering for deduplication: higher-severity wins on same file+line.
_SEVERITY_PRIORITY = {
    "error": 0,
    "warning": 1,
    "suggestion": 2,
    "info": 3,
}


def load_findings(file_paths: List[str]) -> List[Dict[str, Any]]:
    """Load and merge findings from multiple JSON files.

    Each file should contain a JSON array of finding dicts.
    Missing files are silently skipped (stages may not produce output).

    Args:
        file_paths: List of paths to JSON finding files.

    Returns:
        Merged list of all findings.
    """
    all_findings: List[Dict[str, Any]] = []

    for fp in file_paths:
        path = Path(fp)
        if not path.exists():
            print(
                f"Warning: Findings file not found, skipping: {fp}",
                file=sys.stderr,
            )
            continue

        try:
            text = path.read_text(encoding="utf-8", errors="replace")
            data = json.loads(text)
            if isinstance(data, list):
                all_findings.extend(data)
            else:
                print(
                    f"Warning: Expected JSON array in {fp}, got {type(data).__name__}",
                    file=sys.stderr,
                )
        except json.JSONDecodeError as e:
            print(
                f"Warning: Failed to parse JSON from {fp}: {e}",
                file=sys.stderr,
            )

    return all_findings


def deduplicate_findings(
    findings: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Remove duplicate findings on the same file+line+rule_id.

    Different rules on the same line are kept (e.g., ``logtemp`` and
    ``macro_no_semicolon`` can both fire on a single ``UE_LOG`` line).
    Only when multiple stages report the *same* rule on the *same*
    file+line is the one with the highest severity kept. If severity
    is equal, the first one encountered (earlier stage) has priority.

    Args:
        findings: List of finding dicts.

    Returns:
        Deduplicated list of findings.
    """
    seen: Dict[Tuple[str, int, str], Dict[str, Any]] = {}

    for finding in findings:
        file = finding.get("file", "")
        try:
            line = int(finding.get("line", 0))
        except (TypeError, ValueError):
            line = 0
        # Stage 1/2 use rule_id; Stage 3 (LLM) uses category instead.
        rule_id = finding.get("rule_id") or finding.get("category", "")
        key = (file, line, rule_id)

        if key not in seen:
            seen[key] = finding
        else:
            existing = seen[key]
            existing_priority = _SEVERITY_PRIORITY.get(
                existing.get("severity", "info"), 99
            )
            new_priority = _SEVERITY_PRIORITY.get(
                finding.get("severity", "info"), 99
            )
            if new_priority < existing_priority:
                seen[key] = finding

    return list(seen.values())


def filter_findings_by_diff(
    findings: List[Dict[str, Any]],
    diff_text: str,
) -> List[Dict[str, Any]]:
    """Remove findings whose line is outside any diff hunk for the file.

    GitHub Review API rejects inline comments on lines not present in
    the diff (HTTP 422).  This filter ensures only findings within
    visible diff hunk ranges are posted.

    Args:
        findings: List of finding dicts with ``file`` and ``line`` keys.
        diff_text: Raw unified diff text.

    Returns:
        Filtered list containing only findings within diff hunks.
    """
    diff_data = parse_diff(diff_text)

    # Build a set of (file, line) pairs that are within any hunk.
    # A hunk's [start, end] covers all new-side lines (added + context).
    hunk_ranges: Dict[str, List[Tuple[int, int]]] = {}
    for path, fd in diff_data.items():
        ranges = []
        for hunk in fd.hunks:
            ranges.append((hunk["start"], hunk["end"]))
        hunk_ranges[path] = ranges

    filtered: List[Dict[str, Any]] = []
    for finding in findings:
        file_path = finding.get("file", "")
        try:
            line = int(finding.get("line", 0))
        except (TypeError, ValueError):
            line = 0

        # For multi-line findings, build_review_comments() sends end_line
        # as the API "line" field.  Both line and end_line must be within
        # the same hunk, otherwise GitHub still returns 422.
        try:
            end_line = int(finding["end_line"]) if finding.get("end_line") is not None else None
        except (TypeError, ValueError):
            end_line = None

        ranges = hunk_ranges.get(file_path)
        if ranges is None:
            # File not in diff at all — drop finding
            continue
        for start, end in ranges:
            if end_line and end_line > line:
                # Multi-line: both start and end must be in the same hunk
                if start <= line and end_line <= end:
                    filtered.append(finding)
                    break
            else:
                if start <= line <= end:
                    filtered.append(finding)
                    break

    skipped = len(findings) - len(filtered)
    if skipped > 0:
        print(
            f"Diff filter: dropped {skipped} finding(s) outside diff hunks",
            file=sys.stderr,
        )

    return filtered


def _format_suggestion_block(suggestion: str) -> str:
    """Wrap suggestion text in a GitHub suggestion code block.

    GitHub renders ``suggestion`` fenced code blocks as apply-able
    inline suggestions in PR reviews.

    Args:
        suggestion: The replacement code text.

    Returns:
        Markdown string with suggestion block.
    """
    return f"```suggestion\n{suggestion}\n```"


def _severity_emoji(severity: str) -> str:
    """Map severity to a text label for review comments."""
    return {
        "error": "[ERROR]",
        "warning": "[WARNING]",
        "suggestion": "[SUGGESTION]",
        "info": "[INFO]",
    }.get(severity, "[INFO]")


def format_comment_body(finding: Dict[str, Any]) -> str:
    """Format a finding into a GitHub review comment body.

    Args:
        finding: A finding dict with severity, message, rule_id,
                 and optional suggestion.

    Returns:
        Markdown-formatted comment body string.
    """
    severity = finding.get("severity", "info")
    message = finding.get("message", "")
    # Stage 1/2 use rule_id; Stage 3 (LLM) uses category instead.
    rule_id = finding.get("rule_id") or finding.get("category", "")
    suggestion = finding.get("suggestion")

    parts = []

    # Header line with severity and rule ID / category
    label = _severity_emoji(severity)
    if rule_id:
        parts.append(f"**{label}** `{rule_id}`")
    else:
        parts.append(f"**{label}**")

    # Message
    if message:
        parts.append(message)

    # Suggestion block (only for findings with severity=suggestion or
    # that have an explicit suggestion field)
    if suggestion is not None:
        parts.append("")
        parts.append(_format_suggestion_block(suggestion))

    return "\n".join(parts)


def build_review_comments(
    findings: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Convert findings into GitHub review API comment payloads.

    Each finding becomes a review comment positioned on the relevant
    file and line. Multi-line findings (with end_line > line) use
    the ``start_line``/``line`` range format.

    Args:
        findings: List of deduplicated finding dicts.

    Returns:
        List of comment dicts suitable for the GitHub review API.
    """
    comments: List[Dict[str, Any]] = []

    for finding in findings:
        file_path = finding.get("file", "")

        # Coerce line fields to int — Stage 3 LLM JSON may emit
        # strings, nulls, or other non-int types.
        try:
            line = int(finding.get("line", 0))
        except (TypeError, ValueError):
            line = 0
        try:
            end_line = int(finding["end_line"]) if finding.get("end_line") is not None else None
        except (TypeError, ValueError):
            end_line = None

        if not file_path or line <= 0:
            continue

        body = format_comment_body(finding)

        comment: Dict[str, Any] = {
            "path": file_path,
            "line": end_line if end_line and end_line > line else line,
            "side": "RIGHT",
            "body": body,
        }

        # Multi-line range: start_line < line
        if end_line and end_line > line:
            comment["start_line"] = line
            comment["start_side"] = "RIGHT"

        comments.append(comment)

    return comments


def build_summary(
    findings: List[Dict[str, Any]],
    stages_available: Optional[List[str]] = None,
) -> str:
    """Build a summary body for the top-level review comment.

    Args:
        findings: List of all findings being posted.
        stages_available: List of stage names that ran (for information).

    Returns:
        Markdown-formatted summary string.
    """
    if not findings:
        return (
            "## UE5 Code Review Bot\n\n"
            "No issues found. :white_check_mark:"
        )

    # Count by severity
    counts: Dict[str, int] = {}
    for f in findings:
        sev = f.get("severity", "info")
        counts[sev] = counts.get(sev, 0) + 1

    # Count by rule_id (falls back to category for Stage 3 LLM findings)
    rule_counts: Dict[str, int] = {}
    for f in findings:
        rid = f.get("rule_id") or f.get("category", "unknown")
        rule_counts[rid] = rule_counts.get(rid, 0) + 1

    lines = ["## UE5 Code Review Bot", ""]

    # Summary table
    total = len(findings)
    lines.append(f"**{total}** issues found:")
    lines.append("")

    for sev in ["error", "warning", "suggestion", "info"]:
        count = counts.get(sev, 0)
        if count > 0:
            label = _severity_emoji(sev)
            lines.append(f"- {label}: {count}")

    # Top rule IDs
    if rule_counts:
        lines.append("")
        lines.append("**By rule:**")
        sorted_rules = sorted(rule_counts.items(), key=lambda x: -x[1])
        for rule_id, count in sorted_rules[:10]:
            lines.append(f"- `{rule_id}`: {count}")

    # Stage info
    if stages_available:
        lines.append("")
        lines.append(f"*Stages: {', '.join(stages_available)}*")

    return "\n".join(lines)


def split_into_batches(
    comments: List[Dict[str, Any]],
    batch_size: int = MAX_COMMENTS_PER_REVIEW,
) -> List[List[Dict[str, Any]]]:
    """Split comments into batches respecting the GitHub API limit.

    Args:
        comments: List of review comment dicts.
        batch_size: Maximum comments per review.

    Returns:
        List of comment batches.
    """
    if not comments:
        return []

    batches = []
    for i in range(0, len(comments), batch_size):
        batches.append(comments[i : i + batch_size])
    return batches


def filter_already_posted(
    comments: List[Dict[str, Any]],
    existing: List[Dict[str, Any]],
    commit_sha: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Remove comments that already exist on the PR.

    Compares by (path, start_line, line, body_prefix) to avoid reposting
    the same inline comment on workflow reruns.  Only the first 120
    characters of the body are compared so that minor formatting tweaks
    don't defeat the dedup.

    When *commit_sha* is provided, only existing comments whose
    ``commit_id`` matches the current HEAD are considered duplicates.
    Comments left on older commits are ignored so that new findings on
    updated code are never suppressed after a push.

    Args:
        comments: New review comment dicts to post.
        existing: Existing review comment dicts from the GitHub API.
        commit_sha: Current HEAD commit SHA for this review.

    Returns:
        Filtered list of comments not yet posted.
    """
    existing_keys: Set[Tuple[str, Optional[int], int, str]] = set()
    for ec in existing:
        # Skip comments from older commits — they may reference stale
        # positions and should not prevent posting on the current HEAD.
        if commit_sha and ec.get("commit_id") != commit_sha:
            continue
        path = ec.get("path", "")
        line = ec.get("line") or ec.get("original_line") or 0
        start_line = ec.get("start_line")  # None for single-line comments
        body_prefix = (ec.get("body") or "")[:120]
        existing_keys.add((path, start_line, line, body_prefix))

    filtered = []
    for c in comments:
        body_prefix = (c.get("body") or "")[:120]
        start_line = c.get("start_line")  # None for single-line comments
        key = (c.get("path", ""), start_line, c.get("line", 0), body_prefix)
        if key not in existing_keys:
            filtered.append(c)

    return filtered


def post_review(
    client: GitHubClient,
    owner: str,
    repo: str,
    pr_number: int,
    commit_sha: str,
    findings: List[Dict[str, Any]],
    stages_available: Optional[List[str]] = None,
    existing_comments: Optional[List[Dict[str, Any]]] = None,
) -> List[Dict[str, Any]]:
    """Post findings as a PR review via the GitHub API.

    Handles batching when findings exceed the per-review comment limit.
    The first batch includes the summary body; subsequent batches include
    a continuation note.

    When *existing_comments* is provided (fetched from the PR), comments
    that were already posted are filtered out to prevent duplicates on
    workflow reruns.

    Args:
        client: Authenticated GitHubClient instance.
        owner: Repository owner.
        repo: Repository name.
        pr_number: Pull request number.
        commit_sha: The HEAD commit SHA of the PR.
        findings: Deduplicated list of findings.
        stages_available: List of stage names that ran.
        existing_comments: Already-posted review comments on this PR
            (from ``GitHubClient.get_existing_review_comments``).

    Returns:
        List of API response dicts (one per review batch).
        Entries with an ``"error"`` key indicate failed batches.
    """
    summary = build_summary(findings, stages_available)
    comments = build_review_comments(findings)

    # Filter out comments already posted (workflow rerun protection)
    all_filtered = False
    if existing_comments:
        before = len(comments)
        comments = filter_already_posted(comments, existing_comments, commit_sha)
        skipped = before - len(comments)
        if skipped > 0:
            print(
                f"Skipped {skipped} already-posted comment(s).",
                file=sys.stderr,
            )
        if before > 0 and len(comments) == 0:
            all_filtered = True

    if not comments:
        if all_filtered:
            # All comments were already posted on this commit — skip
            # posting a duplicate summary-only review to avoid noise.
            print(
                "All comments already posted; skipping summary-only review.",
                file=sys.stderr,
            )
            return [{"skipped": "all comments already posted"}]

        # Genuinely no findings — post a clean summary review.
        try:
            resp = client.create_review(
                owner=owner,
                repo=repo,
                pr_number=pr_number,
                commit_sha=commit_sha,
                body=summary,
                comments=[],
                event="COMMENT",
            )
            return [resp]
        except RuntimeError as e:
            print(
                f"Error posting summary-only review: {e}",
                file=sys.stderr,
            )
            return [{"error": str(e)}]

    batches = split_into_batches(comments)
    responses = []

    for idx, batch in enumerate(batches):
        if idx == 0:
            body = summary
        else:
            body = (
                f"## UE5 Code Review Bot (continued {idx + 1}/{len(batches)})"
            )

        try:
            resp = client.create_review(
                owner=owner,
                repo=repo,
                pr_number=pr_number,
                commit_sha=commit_sha,
                body=body,
                comments=batch,
                event="COMMENT",
            )
            responses.append(resp)
        except RuntimeError as e:
            print(
                f"Error posting review batch {idx + 1}/{len(batches)}: {e}",
                file=sys.stderr,
            )
            # Continue with remaining batches
            responses.append({"error": str(e)})

    return responses


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Post Review — merge findings and publish PR review"
    )
    parser.add_argument(
        "--findings",
        nargs="+",
        required=True,
        help="One or more JSON finding files from Stage 1/2/3",
    )
    parser.add_argument(
        "--pr-number",
        type=int,
        help="Pull request number",
    )
    parser.add_argument(
        "--repo",
        help="Repository in owner/repo format",
    )
    parser.add_argument(
        "--commit-sha",
        help="HEAD commit SHA of the PR",
    )
    parser.add_argument(
        "--token",
        default=None,
        help="GitHub token (default: $GIT_ACTION_TOKEN or $GITHUB_TOKEN env)",
    )
    parser.add_argument(
        "--api-url",
        default=None,
        help=(
            "GitHub API base URL "
            "(default: $GHES_URL/api/v3 or https://api.github.com)"
        ),
    )
    parser.add_argument(
        "--diff",
        default=None,
        help="Path to PR unified diff file (filters findings to diff hunks)",
    )
    parser.add_argument(
        "--stages",
        default=None,
        help='Comma-separated list of stages that ran (e.g. "stage1,stage2")',
    )
    parser.add_argument(
        "--output",
        default=None,
        help="Output JSON file for review payload/result (default: stdout)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Build review payload without posting to GitHub",
    )

    args = parser.parse_args()

    # Load and process findings
    findings = load_findings(args.findings)
    findings = deduplicate_findings(findings)

    # Filter out findings on lines not in the PR diff (prevents 422 from
    # the GitHub Review API which only accepts comments on diff hunks).
    if args.diff:
        diff_path = Path(args.diff)
        if diff_path.exists():
            diff_text = diff_path.read_text(encoding="utf-8", errors="replace")
            findings = filter_findings_by_diff(findings, diff_text)
        else:
            print(
                f"Warning: Diff file not found, skipping hunk filter: {args.diff}",
                file=sys.stderr,
            )

    # Sort by file, then line for consistent output.
    # Coerce line to int — Stage 3 may emit string line numbers and
    # mixed int/str comparison raises TypeError in Python 3.
    def _sort_key(f: Dict[str, Any]) -> tuple:
        try:
            line = int(f.get("line", 0))
        except (TypeError, ValueError):
            line = 0
        return (f.get("file", ""), line)

    findings.sort(key=_sort_key)

    stages = args.stages.split(",") if args.stages else None

    if args.dry_run:
        # Dry-run: build payload and output without API calls
        summary = build_summary(findings, stages)
        comments = build_review_comments(findings)
        payload = {
            "summary": summary,
            "total_findings": len(findings),
            "total_comments": len(comments),
            "comments": comments,
            "findings": findings,
        }

        output_json = json.dumps(payload, ensure_ascii=False, indent=2)

        if args.output:
            Path(args.output).write_text(output_json + "\n", encoding="utf-8")
            print(
                f"Dry-run: {len(findings)} findings, "
                f"{len(comments)} comments. "
                f"Written to: {args.output}"
            )
        else:
            print(output_json)

        sys.exit(0)

    # Validate required args for posting
    if not args.pr_number:
        print("Error: --pr-number is required (unless --dry-run)", file=sys.stderr)
        sys.exit(1)
    if not args.repo:
        print("Error: --repo is required (unless --dry-run)", file=sys.stderr)
        sys.exit(1)

    # Resolve token
    token = args.token or os.environ.get("GIT_ACTION_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if not token:
        print(
            "Error: No token provided. Use --token or set "
            "GIT_ACTION_TOKEN / GITHUB_TOKEN environment variable.",
            file=sys.stderr,
        )
        sys.exit(1)

    # Resolve API URL
    api_url = args.api_url
    if not api_url:
        ghes_url = os.environ.get("GHES_URL")
        if ghes_url:
            api_url = f"{ghes_url.rstrip('/')}/api/v3"
        else:
            api_url = "https://api.github.com"

    # Parse owner/repo
    repo_parts = args.repo.split("/")
    if len(repo_parts) != 2:
        print(
            f"Error: --repo must be in owner/repo format, got: {args.repo}",
            file=sys.stderr,
        )
        sys.exit(1)
    owner, repo = repo_parts

    # Resolve commit SHA
    commit_sha = args.commit_sha
    if not commit_sha:
        # Try to fetch from PR metadata
        client = GitHubClient(token=token, api_url=api_url)
        try:
            pr_data = client.get_pull_request(owner, repo, args.pr_number)
            commit_sha = pr_data.get("head", {}).get("sha", "")
        except RuntimeError as e:
            print(f"Error: Could not determine commit SHA: {e}", file=sys.stderr)
            sys.exit(1)

    if not commit_sha:
        print(
            "Error: --commit-sha is required or must be resolvable from PR",
            file=sys.stderr,
        )
        sys.exit(1)

    # Fetch existing comments to avoid duplicates on reruns
    client = GitHubClient(token=token, api_url=api_url)
    existing_comments: List[Dict[str, Any]] = []
    try:
        existing_comments = client.get_existing_review_comments(
            owner, repo, args.pr_number
        )
    except RuntimeError as e:
        print(
            f"Warning: Could not fetch existing comments "
            f"(duplicate prevention disabled): {e}",
            file=sys.stderr,
        )

    # Post review
    responses = post_review(
        client=client,
        owner=owner,
        repo=repo,
        pr_number=args.pr_number,
        commit_sha=commit_sha,
        findings=findings,
        stages_available=stages,
        existing_comments=existing_comments,
    )

    # Output result
    failed = sum(1 for r in responses if "error" in r)
    succeeded = len(responses) - failed

    result = {
        "total_findings": len(findings),
        "total_comments": len(build_review_comments(findings)),
        "reviews_posted": succeeded,
        "reviews_failed": failed,
        "responses": responses,
    }

    output_json = json.dumps(result, ensure_ascii=False, indent=2)

    if args.output:
        Path(args.output).write_text(output_json + "\n", encoding="utf-8")
        print(
            f"Review posted: {len(findings)} findings in "
            f"{len(responses)} review(s) ({failed} failed). "
            f"Written to: {args.output}"
        )
    else:
        print(output_json)

    # Exit non-zero if ALL batches failed — CI should not be green
    # when no review was actually published.
    if failed > 0 and succeeded == 0:
        print(
            "Error: All review batches failed. No review was posted.",
            file=sys.stderr,
        )
        sys.exit(1)

    sys.exit(0)


if __name__ == "__main__":
    main()
