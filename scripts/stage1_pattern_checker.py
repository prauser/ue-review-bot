#!/usr/bin/env python3
"""Stage 1 Pattern Checker — regex-based pattern matching for UE5 code review.

Checks only added lines in a diff for Tier 1 patterns defined in checklist.yml.
Produces a JSON list of findings with file, line, rule_id, severity, message,
and optional suggestion.

Usage:
    # From a diff file:
    python -m scripts.stage1_pattern_checker \\
        --diff <diff-file> \\
        --checklist configs/checklist.yml \\
        --output findings-stage1.json

    # From git (requires working tree):
    python -m scripts.stage1_pattern_checker \\
        --files '["Source/A.cpp", "Source/B.h"]' \\
        --base-ref origin/main \\
        --output findings-stage1.json
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

# Add project root for imports when run as script
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.utils.diff_parser import FileDiff, parse_diff


# Patterns for detecting single-line C/C++ comments
_SINGLE_LINE_COMMENT_RE = re.compile(r"^\s*//")
_INLINE_COMMENT_RE = re.compile(r"//.*$")


def load_tier1_patterns(checklist_path: str) -> List[Dict[str, Any]]:
    """Load Tier 1 patterns from checklist.yml.

    Extracts items with tier=1 and a 'pattern' field from all categories.

    Args:
        checklist_path: Path to the checklist YAML file.

    Returns:
        List of pattern info dicts with compiled regex.

    Raises:
        FileNotFoundError: If checklist file doesn't exist.
        ValueError: If a pattern regex is invalid.
    """
    path = Path(checklist_path)
    if not path.exists():
        raise FileNotFoundError(f"Checklist file not found: {checklist_path}")

    with open(path, "r", encoding="utf-8") as f:
        checklist = yaml.safe_load(f)

    if not isinstance(checklist, dict):
        raise ValueError(f"Checklist file is empty or not a YAML mapping: {checklist_path}")

    patterns = []
    for category in checklist.get("categories", []):
        for item in category.get("items", []):
            if item.get("tier") == 1 and "pattern" in item:
                try:
                    compiled = re.compile(item["pattern"])
                except re.error as e:
                    raise ValueError(
                        f"Invalid regex for pattern '{item['id']}': {e}"
                    ) from e

                patterns.append(
                    {
                        "id": item["id"],
                        "compiled": compiled,
                        "raw_pattern": item["pattern"],
                        "severity": item.get("severity", "warning"),
                        "summary": item.get("summary", ""),
                        "description": item.get("description", "").strip(),
                        "auto_fixable": item.get("auto_fixable", False),
                        "tags": item.get("tags", []),
                    }
                )

    return patterns


def _split_code_comment(line: str) -> tuple:
    """Split a line into code and inline comment parts.

    Tracks parenthesis depth **and** string/char literal boundaries
    to avoid matching ``//`` inside macro arguments (e.g.,
    ``TEXT("http://...")``) or bare string literals (e.g.,
    ``"http://example.com"``).

    Returns:
        (code_part, comment_part) where comment_part includes
        the leading // and everything after it. comment_part is
        empty string if no inline comment is found.
    """
    depth = 0
    in_string = False
    in_char = False
    i = 0
    while i < len(line):
        ch = line[i]
        # Skip escaped characters inside string/char literals
        if ch == "\\" and (in_string or in_char):
            i += 2
            continue
        if in_char:
            if ch == "'":
                in_char = False
        elif in_string:
            if ch == '"':
                in_string = False
        else:
            if ch == '"':
                in_string = True
            elif ch == "'":
                in_char = True
            elif ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
            elif ch == "/" and i + 1 < len(line) and line[i + 1] == "/" and depth <= 0:
                return line[:i], line[i:]
        i += 1
    return line, ""


def _strip_comments(line: str) -> str:
    """Remove inline C++ comments from a line for pattern matching.

    Uses parenthesis-depth tracking (via _split_code_comment) so that
    ``//`` inside macro arguments — e.g. ``TEXT("http://...")`` — is
    preserved. Only true inline comments at depth <= 0 are stripped.

    Block comments (/* */) spanning multiple lines are not handled
    (would require multi-line state tracking).

    Args:
        line: Source code line.

    Returns:
        Line with // comments removed (code portion only).
        Empty string if the entire line is a comment.
    """
    # Don't strip if entire line is a comment (return empty to skip matching)
    if _SINGLE_LINE_COMMENT_RE.match(line):
        return ""

    code, _comment = _split_code_comment(line)
    return code


def _generate_suggestion(rule_id: str, line: str) -> Optional[str]:
    """Generate auto-fix suggestion for fixable patterns.

    Args:
        rule_id: The pattern rule ID.
        line: The original source line.

    Returns:
        Suggested replacement line, or None if not auto-fixable.
    """
    if rule_id == "macro_no_semicolon":
        code, comment = _split_code_comment(line)
        code = code.rstrip()
        if comment:
            return code + "; " + comment
        return code + ";"

    if rule_id == "declaration_macro_semicolon":
        code, comment = _split_code_comment(line)
        code = code.rstrip()
        if code.endswith(";"):
            code = code[:-1]
        if comment:
            return code + " " + comment
        return code

    return None


def check_line(
    line: str,
    patterns: List[Dict[str, Any]],
    skip_comments: bool = True,
) -> List[Dict[str, Any]]:
    """Check a single line against all Tier 1 patterns.

    Args:
        line: Source code line to check.
        patterns: Compiled pattern list from load_tier1_patterns().
        skip_comments: If True, skip lines that are entirely comments
                       and strip inline comments before matching.

    Returns:
        List of finding dicts (without file/line info).
    """
    check_target = line
    if skip_comments:
        check_target = _strip_comments(line)
        if not check_target.strip():
            return []

    findings = []
    for pat in patterns:
        if pat["compiled"].search(check_target):
            suggestion = _generate_suggestion(pat["id"], line)
            findings.append(
                {
                    "rule_id": pat["id"],
                    "severity": pat["severity"],
                    "message": pat["summary"],
                    "suggestion": suggestion,
                }
            )

    return findings


def check_diff(
    diff_data: Dict[str, FileDiff],
    patterns: List[Dict[str, Any]],
    skip_comments: bool = True,
) -> List[Dict[str, Any]]:
    """Check all added lines in parsed diff data against Tier 1 patterns.

    Args:
        diff_data: Parsed diff data from parse_diff().
        patterns: Compiled pattern list from load_tier1_patterns().
        skip_comments: If True, skip comment lines.

    Returns:
        List of finding dicts with file, line, rule_id, severity,
        message, and suggestion fields.
    """
    all_findings = []

    for filepath in sorted(diff_data.keys()):
        file_diff = diff_data[filepath]
        for line_num in sorted(file_diff.added_lines.keys()):
            line = file_diff.added_lines[line_num]
            findings = check_line(line, patterns, skip_comments=skip_comments)
            for finding in findings:
                all_findings.append(
                    {
                        "file": filepath,
                        "line": line_num,
                        **finding,
                    }
                )

    return all_findings


def get_diff_from_git(files: List[str], base_ref: str) -> str:
    """Generate diff from git for specified files.

    Args:
        files: List of file paths to diff.
        base_ref: Base git ref (e.g., 'origin/main').

    Returns:
        Unified diff text.

    Raises:
        RuntimeError: If git command fails.
    """
    cmd = ["git", "diff", base_ref, "--"] + files
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    if result.returncode != 0:
        raise RuntimeError(f"git diff failed: {result.stderr}")
    return result.stdout


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Stage 1 Pattern Checker — regex-based UE5 code review"
    )
    parser.add_argument(
        "--diff",
        help="Path to a unified diff file",
    )
    parser.add_argument(
        "--files",
        help='JSON list of files to check (e.g. \'["Source/A.cpp"]\')',
    )
    parser.add_argument(
        "--base-ref",
        help="Base git ref for generating diff (e.g. origin/main)",
    )
    parser.add_argument(
        "--checklist",
        default="configs/checklist.yml",
        help="Path to checklist.yml (default: configs/checklist.yml)",
    )
    parser.add_argument(
        "--output",
        default=None,
        help="Output JSON file path (default: stdout)",
    )
    parser.add_argument(
        "--no-skip-comments",
        action="store_true",
        help="Don't skip comment lines (check everything)",
    )

    args = parser.parse_args()

    # Get diff text
    if args.diff:
        diff_path = Path(args.diff)
        if not diff_path.exists():
            print(f"Error: Diff file not found: {args.diff}", file=sys.stderr)
            sys.exit(1)
        diff_text = diff_path.read_text(encoding="utf-8", errors="replace")
    elif args.files and args.base_ref:
        files = json.loads(args.files)
        diff_text = get_diff_from_git(files, args.base_ref)
    else:
        print(
            "Error: Either --diff or (--files + --base-ref) is required",
            file=sys.stderr,
        )
        sys.exit(1)

    # Load patterns
    patterns = load_tier1_patterns(args.checklist)

    # Parse diff and run checks
    diff_data = parse_diff(diff_text)
    findings = check_diff(
        diff_data, patterns, skip_comments=not args.no_skip_comments
    )

    # Output
    output_json = json.dumps(findings, ensure_ascii=False, indent=2)

    if args.output:
        output_path = Path(args.output)
        output_path.write_text(output_json + "\n", encoding="utf-8")
        print(
            f"Stage 1 findings: {len(findings)} issues found. "
            f"Written to: {args.output}"
        )
    else:
        print(output_json)

    sys.exit(0)


if __name__ == "__main__":
    main()
