#!/usr/bin/env python3
"""Stage 1 Format Diff — clang-format diff → suggestion converter.

Runs clang-format on changed files, compares with the original, and
generates GitHub PR review suggestion blocks for formatting differences.

Suggestions are only generated for lines within the PR diff range.
Lines outside the diff range are converted to regular comments.
Each suggestion is capped at 20 lines; larger diffs are split into chunks.

Usage:
    python -m scripts.stage1_format_diff \\
        --files '["Source/A.cpp"]' \\
        --clang-format-config configs/.clang-format \\
        --diff <diff-file> \\
        --output suggestions-format.json
"""

from __future__ import annotations

import argparse
import difflib
import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.utils.diff_parser import get_added_line_numbers, parse_diff

# Maximum lines per suggestion block
MAX_SUGGESTION_LINES = 20


def find_clang_format() -> Optional[str]:
    """Find clang-format executable.

    Returns:
        Path to clang-format executable, or None if not found.
    """
    return shutil.which("clang-format")


def run_clang_format(
    file_path: str,
    config_path: Optional[str] = None,
    clang_format_bin: Optional[str] = None,
) -> Optional[str]:
    """Run clang-format on a file and return the formatted content.

    Args:
        file_path: Path to the source file.
        config_path: Path to .clang-format config file.
        clang_format_bin: Path to clang-format binary (auto-detected if None).

    Returns:
        Formatted file content as string, or None if clang-format failed.
    """
    binary = clang_format_bin or find_clang_format()
    if binary is None:
        return None

    cmd = [binary]
    if config_path:
        cmd.append(f"--style=file:{config_path}")

    cmd.append(file_path)

    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=30
        )
        if result.returncode != 0:
            print(
                f"Warning: clang-format failed for {file_path}: {result.stderr}",
                file=sys.stderr,
            )
            return None
        return result.stdout
    except (subprocess.TimeoutExpired, FileNotFoundError) as e:
        print(
            f"Warning: clang-format error for {file_path}: {e}",
            file=sys.stderr,
        )
        return None


def _compute_diff_regions(
    original_lines: List[str],
    formatted_lines: List[str],
) -> List[Dict[str, Any]]:
    """Compute regions where original and formatted content differ.

    Uses difflib.SequenceMatcher to find contiguous blocks of changes.

    Args:
        original_lines: Lines of the original file.
        formatted_lines: Lines of the formatted file.

    Returns:
        List of dicts with 'start_line' (1-based), 'end_line' (1-based,
        inclusive), 'original' (list of lines), 'formatted' (list of lines).
    """
    matcher = difflib.SequenceMatcher(None, original_lines, formatted_lines)
    regions = []

    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            continue

        if tag == "insert":
            # Pure insertion (i1 == i2): no original lines to replace.
            # Anchor to an adjacent original line so the suggestion has
            # a valid line range for GitHub PR review comments.
            if i1 > 0:
                # Anchor to the preceding line
                anchor = i1 - 1
                regions.append(
                    {
                        "start_line": anchor + 1,
                        "end_line": anchor + 1,
                        "original": [original_lines[anchor]],
                        "formatted": [original_lines[anchor]]
                        + formatted_lines[j1:j2],
                    }
                )
            elif original_lines:
                # Insertion at the very beginning — anchor to first line
                regions.append(
                    {
                        "start_line": 1,
                        "end_line": 1,
                        "original": [original_lines[0]],
                        "formatted": formatted_lines[j1:j2]
                        + [original_lines[0]],
                    }
                )
            # else: empty original file — nothing to anchor to, skip
            continue

        regions.append(
            {
                "start_line": i1 + 1,  # 1-based
                "end_line": i2,  # 1-based inclusive (i2 is exclusive in 0-based)
                "original": original_lines[i1:i2],
                "formatted": formatted_lines[j1:j2],
            }
        )

    return regions


def _split_into_chunks(
    region: Dict[str, Any], max_lines: int = MAX_SUGGESTION_LINES
) -> List[Dict[str, Any]]:
    """Split a large diff region into chunks of max_lines.

    Args:
        region: A diff region dict.
        max_lines: Maximum lines per chunk.

    Returns:
        List of region dicts, each with at most max_lines of original content.
    """
    orig = region["original"]
    fmt = region["formatted"]

    if len(orig) <= max_lines:
        return [region]

    chunks = []
    start = 0
    fmt_start = 0

    while start < len(orig):
        end = min(start + max_lines, len(orig))

        # Proportionally split formatted lines
        if len(orig) > 0:
            fmt_end = int(len(fmt) * end / len(orig))
        else:
            fmt_end = len(fmt)

        # Ensure we don't overshoot
        if end == len(orig):
            fmt_end = len(fmt)

        chunks.append(
            {
                "start_line": region["start_line"] + start,
                "end_line": region["start_line"] + end - 1,
                "original": orig[start:end],
                "formatted": fmt[fmt_start:fmt_end],
            }
        )

        start = end
        fmt_start = fmt_end

    return chunks


def generate_format_suggestions(
    file_path: str,
    original_content: str,
    formatted_content: str,
    added_lines: Set[int],
) -> List[Dict[str, Any]]:
    """Generate format suggestions by comparing original and formatted content.

    Only lines within the PR diff range (added_lines) produce suggestions.
    Lines outside the range produce regular comments.

    Args:
        file_path: Path of the file being checked.
        original_content: Original file content.
        formatted_content: clang-format output.
        added_lines: Set of line numbers that are within the PR diff range.

    Returns:
        List of suggestion/comment dicts.
    """
    if original_content == formatted_content:
        return []

    original_lines = original_content.splitlines(keepends=True)
    formatted_lines = formatted_content.splitlines(keepends=True)

    regions = _compute_diff_regions(original_lines, formatted_lines)
    suggestions = []

    for region in regions:
        chunks = _split_into_chunks(region)

        for chunk in chunks:
            # Check if all lines in this chunk are within the PR diff range
            chunk_lines = set(
                range(chunk["start_line"], chunk["end_line"] + 1)
            )
            lines_in_diff = chunk_lines & added_lines

            formatted_text = "".join(chunk["formatted"]).rstrip("\n")

            if lines_in_diff == chunk_lines:
                # All lines are in diff range → suggestion
                suggestions.append(
                    {
                        "file": file_path,
                        "line": chunk["start_line"],
                        "end_line": chunk["end_line"],
                        "rule_id": "clang_format",
                        "severity": "suggestion",
                        "message": "clang-format 자동 수정 제안",
                        "suggestion": formatted_text,
                    }
                )
            elif lines_in_diff:
                # Partial overlap → comment on the first in-diff line
                first_in_diff = min(lines_in_diff)
                suggestions.append(
                    {
                        "file": file_path,
                        "line": first_in_diff,
                        "end_line": first_in_diff,
                        "rule_id": "clang_format",
                        "severity": "info",
                        "message": (
                            "clang-format 포맷 차이가 있지만 PR diff 범위를 "
                            "벗어나는 라인이 포함되어 있어 suggestion 대신 "
                            "코멘트로 남깁니다."
                        ),
                        "suggestion": None,
                    }
                )
            # else: no overlap with diff range — skip silently

    return suggestions


def process_file(
    file_path: str,
    added_lines: Set[int],
    config_path: Optional[str] = None,
    clang_format_bin: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Process a single file: run clang-format and generate suggestions.

    Args:
        file_path: Path to the source file.
        added_lines: Set of added line numbers from PR diff.
        config_path: Path to .clang-format config.
        clang_format_bin: Path to clang-format binary.

    Returns:
        List of suggestions/comments for this file.
    """
    path = Path(file_path)
    if not path.exists():
        print(
            f"Warning: File not found, skipping: {file_path}",
            file=sys.stderr,
        )
        return []

    original = path.read_text(encoding="utf-8", errors="replace")
    formatted = run_clang_format(file_path, config_path, clang_format_bin)

    if formatted is None:
        return []

    return generate_format_suggestions(
        file_path, original, formatted, added_lines
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Stage 1 Format Diff — clang-format suggestion generator"
    )
    parser.add_argument(
        "--files",
        required=True,
        help='JSON list of files to check (e.g. \'["Source/A.cpp"]\')',
    )
    parser.add_argument(
        "--clang-format-config",
        default="configs/.clang-format",
        help="Path to .clang-format config (default: configs/.clang-format)",
    )
    parser.add_argument(
        "--diff",
        help="Path to PR diff file (for determining added line ranges)",
    )
    parser.add_argument(
        "--output",
        default=None,
        help="Output JSON file path (default: stdout)",
    )

    args = parser.parse_args()

    # Check clang-format availability
    clang_format_bin = find_clang_format()
    if clang_format_bin is None:
        print(
            "Error: clang-format not found. Install clang-format to use "
            "format checking.",
            file=sys.stderr,
        )
        sys.exit(1)

    files = json.loads(args.files)

    # Parse diff to get added line ranges
    added_lines_map: Dict[str, Set[int]] = {}
    if args.diff:
        diff_path = Path(args.diff)
        if not diff_path.exists():
            print(
                f"Error: Diff file not found: {args.diff}",
                file=sys.stderr,
            )
            sys.exit(1)
        diff_text = diff_path.read_text(encoding="utf-8", errors="replace")
        diff_data = parse_diff(diff_text)
        for fp in files:
            added_lines_map[fp] = get_added_line_numbers(diff_data, fp)

    all_suggestions = []
    for file_path in files:
        added = added_lines_map.get(file_path, set())
        suggestions = process_file(
            file_path,
            added,
            config_path=args.clang_format_config,
            clang_format_bin=clang_format_bin,
        )
        all_suggestions.extend(suggestions)

    # Output
    output_json = json.dumps(all_suggestions, ensure_ascii=False, indent=2)

    if args.output:
        output_path = Path(args.output)
        output_path.write_text(output_json + "\n", encoding="utf-8")
        print(
            f"Format suggestions: {len(all_suggestions)} items. "
            f"Written to: {args.output}"
        )
    else:
        print(output_json)

    sys.exit(0)


if __name__ == "__main__":
    main()
