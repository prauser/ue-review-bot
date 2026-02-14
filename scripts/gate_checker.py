#!/usr/bin/env python3
"""Gate Checker — Large PR detection and file filtering for UE5 code review bot.

This script determines whether a PR is "large" and filters out non-reviewable files.
It reads the gate_config.yml configuration and produces a JSON result that downstream
stages (Stage 1/2/3) use to decide what to review.

Two-step logic:
  Step 1: File Filter — apply skip_patterns + C++ extension filter
  Step 2: Size Classification — reviewable count > threshold OR label match → large PR

Usage:
  python gate_checker.py \\
    --diff <path-to-diff-file> \\
    --config configs/gate_config.yml \\
    --output gate-result.json \\
    [--labels migration,large-change]
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import yaml


# C++ file extensions that are reviewable
CPP_EXTENSIONS = {".cpp", ".h", ".inl", ".hpp", ".cc", ".cxx", ".hxx"}


def load_config(config_path: str) -> Dict[str, Any]:
    """Load and validate gate_config.yml.

    Args:
        config_path: Path to the gate configuration YAML file.

    Returns:
        Parsed configuration dictionary.

    Raises:
        FileNotFoundError: If the config file doesn't exist.
        ValueError: If required keys are missing.
    """
    path = Path(config_path)
    if not path.exists():
        raise FileNotFoundError(f"Config file not found: {config_path}")

    with open(path, "r", encoding="utf-8") as f:
        config = yaml.safe_load(f)

    # Validate required keys
    required_keys = ["skip_patterns", "max_reviewable_files", "large_pr_labels"]
    for key in required_keys:
        if key not in config:
            raise ValueError(f"Missing required config key: {key}")

    return config


def _decode_git_path(path: str) -> str:
    """Decode Git escape sequences in a path string.

    Git quotes paths containing non-ASCII or special characters. The regex
    already strips outer quotes, so this function always attempts to decode
    octal escapes (``\\NNN``) and C escapes (``\\\\``, ``\\t``, ``\\n``).

    Octal escapes represent raw UTF-8 bytes, so consecutive sequences must
    be collected into a bytearray and decoded together — converting each
    byte individually via ``chr()`` would produce mojibake for multi-byte
    characters like Korean (e.g. ``\\355\\225\\234`` → ``한``).

    Args:
        path: Path string, possibly containing escape sequences.

    Returns:
        Decoded path string.
    """
    # Fast path: no backslashes means nothing to decode
    if "\\" not in path:
        return path

    # Handle standard C escapes first (use placeholder so \\\\ doesn't
    # interfere with octal matching)
    path = path.replace("\\\\", "\x00BACKSLASH\x00")
    path = path.replace("\\t", "\t").replace("\\n", "\n")

    # Convert octal escapes to raw bytes, then decode as UTF-8.
    parts: list[str] = []
    pending_bytes = bytearray()
    i = 0
    while i < len(path):
        m = re.match(r"\\([0-3][0-7]{2})", path[i:])
        if m:
            pending_bytes.append(int(m.group(1), 8))
            i += len(m.group(0))
        else:
            if pending_bytes:
                parts.append(pending_bytes.decode("utf-8", errors="replace"))
                pending_bytes = bytearray()
            parts.append(path[i])
            i += 1
    if pending_bytes:
        parts.append(pending_bytes.decode("utf-8", errors="replace"))

    path = "".join(parts)
    path = path.replace("\x00BACKSLASH\x00", "\\")
    return path


# Regex for diff headers: handles both quoted and unquoted forms
# Unquoted: diff --git a/path b/path
# Quoted:   diff --git "a/path" "b/path"
_DIFF_HEADER_RE = re.compile(
    r'^diff --git "?a/(.*?)"? "?b/(.*?)"?$'
)


def parse_diff_files(diff_text: str) -> List[str]:
    """Extract changed file paths from a unified diff.

    Parses 'diff --git a/path b/path' lines, including quoted forms
    that Git emits for non-ASCII filenames or paths with special characters
    (e.g. ``diff --git "a/..." "b/..."``).

    Args:
        diff_text: The raw unified diff text.

    Returns:
        List of file paths (deduplicated, preserving order).
    """
    files = []
    seen = set()

    for line in diff_text.splitlines():
        match = _DIFF_HEADER_RE.match(line)
        if match:
            filepath = _decode_git_path(match.group(2))
            if filepath not in seen:
                files.append(filepath)
                seen.add(filepath)

    return files


def filter_files(
    files: List[str],
    skip_patterns: List[str],
) -> Tuple[List[str], List[Dict[str, str]]]:
    """Separate files into reviewable and skipped based on skip_patterns and C++ extension.

    Args:
        files: List of all changed file paths.
        skip_patterns: Regex patterns from gate_config.yml.

    Returns:
        Tuple of (reviewable_files, skipped_files).
        skipped_files contains dicts with 'file' and 'reason' keys.
    """
    compiled_patterns = []
    for pattern in skip_patterns:
        try:
            compiled_patterns.append((pattern, re.compile(pattern)))
        except re.error as e:
            print(f"Warning: Invalid skip pattern '{pattern}': {e}", file=sys.stderr)

    reviewable = []
    skipped = []

    for filepath in files:
        # Check skip patterns first
        skip_matched = False
        for pattern_str, pattern_re in compiled_patterns:
            if pattern_re.search(filepath):
                skipped.append({
                    "file": filepath,
                    "reason": f"경로 필터: {pattern_str}",
                })
                skip_matched = True
                break

        if skip_matched:
            continue

        # Check C++ extension
        ext = Path(filepath).suffix.lower()
        if ext not in CPP_EXTENSIONS:
            skipped.append({
                "file": filepath,
                "reason": f"C++ 파일이 아님: {ext or '(확장자 없음)'}",
            })
            continue

        reviewable.append(filepath)

    return reviewable, skipped


def classify_pr(
    reviewable_count: int,
    max_reviewable_files: int,
    labels: List[str],
    large_pr_labels: List[str],
) -> Tuple[bool, List[str]]:
    """Determine if a PR is classified as "large".

    A PR is large if:
      - reviewable_count > max_reviewable_files, OR
      - any PR label matches large_pr_labels

    Args:
        reviewable_count: Number of reviewable C++ files after filtering.
        max_reviewable_files: Threshold from config.
        labels: PR labels from GitHub.
        large_pr_labels: Labels that force large-PR classification.

    Returns:
        Tuple of (is_large_pr, reasons).
    """
    is_large = False
    reasons = []

    if reviewable_count > max_reviewable_files:
        is_large = True
        reasons.append(
            f"리뷰 대상 파일 수({reviewable_count})가 "
            f"임계값({max_reviewable_files})을 초과"
        )

    matching_labels = set(labels) & set(large_pr_labels)
    if matching_labels:
        is_large = True
        reasons.append(
            f"대규모 PR 라벨 감지: {', '.join(sorted(matching_labels))}"
        )

    return is_large, reasons


def determine_allowed_stages(is_large: bool) -> Tuple[List[int], List[int]]:
    """Determine which review stages are allowed based on PR size.

    Normal PR:  Stage 1 + Stage 2 (if available) + Stage 3
    Large PR:   Stage 1 + Stage 2 (if available), NO Stage 3

    Args:
        is_large: Whether the PR is classified as large.

    Returns:
        Tuple of (allowed_stages, manual_allowed_stages).
        Both follow the same rules per current spec.
    """
    if is_large:
        stages = [1, 2]
    else:
        stages = [1, 2, 3]

    # Per spec: manual review (/review) follows the same rules as automatic
    return stages, stages


def run_gate_check(
    diff_text: str,
    config: Dict[str, Any],
    labels: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """Execute the full gate check pipeline.

    Args:
        diff_text: Raw unified diff text.
        config: Parsed gate configuration.
        labels: PR labels (empty list if not provided).

    Returns:
        Gate check result dictionary matching the output JSON schema.
    """
    if labels is None:
        labels = []

    # Step 1: Parse diff and filter files
    all_files = parse_diff_files(diff_text)
    reviewable_files, skipped_files = filter_files(
        all_files, config["skip_patterns"]
    )

    # Step 2: Classify PR size
    is_large, reasons = classify_pr(
        reviewable_count=len(reviewable_files),
        max_reviewable_files=config["max_reviewable_files"],
        labels=labels,
        large_pr_labels=config["large_pr_labels"],
    )

    # Determine allowed stages
    allowed_stages, manual_allowed_stages = determine_allowed_stages(is_large)

    return {
        "is_large_pr": is_large,
        "reasons": reasons,
        "allowed_stages": allowed_stages,
        "manual_allowed_stages": manual_allowed_stages,
        "total_changed_files": len(all_files),
        "reviewable_files": reviewable_files,
        "reviewable_count": len(reviewable_files),
        "skipped_files": skipped_files,
        "skipped_count": len(skipped_files),
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Gate Checker — Large PR detection and file filtering"
    )
    parser.add_argument(
        "--diff",
        required=True,
        help="Path to the unified diff file",
    )
    parser.add_argument(
        "--config",
        default="configs/gate_config.yml",
        help="Path to gate_config.yml (default: configs/gate_config.yml)",
    )
    parser.add_argument(
        "--output",
        default=None,
        help="Output JSON file path (default: stdout)",
    )
    parser.add_argument(
        "--labels",
        default="",
        help="Comma-separated PR labels",
    )

    args = parser.parse_args()

    # Load config
    config = load_config(args.config)

    # Read diff
    diff_path = Path(args.diff)
    if not diff_path.exists():
        print(f"Error: Diff file not found: {args.diff}", file=sys.stderr)
        sys.exit(1)

    diff_text = diff_path.read_text(encoding="utf-8")

    # Parse labels
    labels = [l.strip() for l in args.labels.split(",") if l.strip()]

    # Run gate check
    result = run_gate_check(diff_text, config, labels)

    # Output
    output_json = json.dumps(result, ensure_ascii=False, indent=2)

    if args.output:
        output_path = Path(args.output)
        output_path.write_text(output_json + "\n", encoding="utf-8")
        print(f"Gate check result written to: {args.output}")
    else:
        print(output_json)

    # Exit code: 0 for normal PR, 0 for large PR (not an error)
    sys.exit(0)


if __name__ == "__main__":
    main()
