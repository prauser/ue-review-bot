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
        --token $GHES_TOKEN \\
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
        line = finding.get("line", 0)
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
        line = finding.get("line", 0)
        end_line = finding.get("end_line")

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


def post_review(
    client: GitHubClient,
    owner: str,
    repo: str,
    pr_number: int,
    commit_sha: str,
    findings: List[Dict[str, Any]],
    stages_available: Optional[List[str]] = None,
) -> List[Dict[str, Any]]:
    """Post findings as a PR review via the GitHub API.

    Handles batching when findings exceed the per-review comment limit.
    The first batch includes the summary body; subsequent batches include
    a continuation note.

    Args:
        client: Authenticated GitHubClient instance.
        owner: Repository owner.
        repo: Repository name.
        pr_number: Pull request number.
        commit_sha: The HEAD commit SHA of the PR.
        findings: Deduplicated list of findings.
        stages_available: List of stage names that ran.

    Returns:
        List of API response dicts (one per review batch).
    """
    summary = build_summary(findings, stages_available)
    comments = build_review_comments(findings)

    if not comments:
        # Post summary-only review (no inline comments)
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
        help="GitHub token (default: $GHES_TOKEN or $GITHUB_TOKEN env)",
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

    # Sort by file, then line for consistent output
    findings.sort(key=lambda f: (f.get("file", ""), f.get("line", 0)))

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
    token = args.token or os.environ.get("GHES_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if not token:
        print(
            "Error: No token provided. Use --token or set "
            "GHES_TOKEN / GITHUB_TOKEN environment variable.",
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

    # Post review
    client = GitHubClient(token=token, api_url=api_url)
    responses = post_review(
        client=client,
        owner=owner,
        repo=repo,
        pr_number=args.pr_number,
        commit_sha=commit_sha,
        findings=findings,
        stages_available=stages,
    )

    # Output result
    result = {
        "total_findings": len(findings),
        "total_comments": len(build_review_comments(findings)),
        "reviews_posted": len(responses),
        "responses": responses,
    }

    output_json = json.dumps(result, ensure_ascii=False, indent=2)

    if args.output:
        Path(args.output).write_text(output_json + "\n", encoding="utf-8")
        print(
            f"Review posted: {len(findings)} findings in "
            f"{len(responses)} review(s). Written to: {args.output}"
        )
    else:
        print(output_json)

    sys.exit(0)


if __name__ == "__main__":
    main()
