#!/usr/bin/env python3
"""Unified diff parser for UE5 code review bot.

Parses unified diff text into structured per-file data including
added lines with new-file line numbers and hunk information.

Output format:
    {
        "Source/MyActor.cpp": {
            "path": "Source/MyActor.cpp",
            "added_lines": {42: "auto x = GetSomething();", 43: "..."},
            "hunks": [{"start": 40, "end": 50, "content": "..."}]
        }
    }
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional


@dataclass
class FileDiff:
    """Represents diff data for a single file."""

    path: str
    added_lines: Dict[int, str] = field(default_factory=dict)
    hunks: List[Dict] = field(default_factory=list)

    def to_dict(self) -> Dict:
        return {
            "path": self.path,
            "added_lines": self.added_lines,
            "hunks": self.hunks,
        }


def _decode_git_path(path: str) -> str:
    """Decode Git escape sequences in a path string.

    Git quotes paths containing non-ASCII or special characters.
    Octal escapes represent raw UTF-8 bytes, so consecutive sequences
    must be collected into a bytearray and decoded together.

    Args:
        path: Path string, possibly containing escape sequences.

    Returns:
        Decoded path string.
    """
    if "\\" not in path:
        return path

    path = path.replace("\\\\", "\x00BACKSLASH\x00")
    path = path.replace('\\"', '"')
    path = path.replace("\\t", "\t").replace("\\n", "\n")

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


# --- Regex patterns for parsing unified diff ---
_DIFF_MARKER_RE = re.compile(r"^diff --git ")
_PLUS_HEADER_RE = re.compile(r'^\+\+\+ "?b/(.*?)"?$')
_MINUS_HEADER_RE = re.compile(r"^--- ")
_HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


def parse_diff(diff_text: str) -> Dict[str, FileDiff]:
    """Parse unified diff text into structured per-file data.

    Extracts added lines (with new-file line numbers) and hunk information
    for each changed file.

    Args:
        diff_text: Raw unified diff text.

    Returns:
        Dictionary mapping file paths to FileDiff objects.
        Deleted files (with +++ /dev/null) are excluded.
    """
    result: Dict[str, FileDiff] = {}
    current_file: Optional[str] = None
    in_header = False
    in_hunk = False
    line_num = 0

    # Hunk accumulation state
    hunk_start = 0
    hunk_lines: List[str] = []

    def _flush_hunk() -> None:
        """Save the accumulated hunk to the current file's data."""
        nonlocal hunk_lines
        if current_file and hunk_lines and current_file in result:
            # Calculate end line from hunk content
            end_line = hunk_start
            for hl in hunk_lines:
                if hl.startswith("+") or hl.startswith(" ") or hl == "":
                    end_line += 1
            end_line -= 1  # last increment was one too many
            if end_line < hunk_start:
                end_line = hunk_start

            result[current_file].hunks.append(
                {
                    "start": hunk_start,
                    "end": end_line,
                    "content": "\n".join(hunk_lines),
                }
            )
            hunk_lines = []

    for raw_line in diff_text.splitlines():
        # --- New file section ---
        if _DIFF_MARKER_RE.match(raw_line):
            _flush_hunk()
            in_header = True
            in_hunk = False
            continue

        # --- File path headers ---
        if in_header:
            m = _PLUS_HEADER_RE.match(raw_line)
            if m:
                filepath = _decode_git_path(m.group(1))
                current_file = filepath
                if filepath not in result:
                    result[filepath] = FileDiff(path=filepath)
                continue

            if _MINUS_HEADER_RE.match(raw_line):
                continue

        # --- Hunk header ---
        m = _HUNK_RE.match(raw_line)
        if m:
            _flush_hunk()
            in_header = False
            in_hunk = True
            line_num = int(m.group(3))
            hunk_start = line_num
            hunk_lines = []
            continue

        # --- Hunk body ---
        if in_hunk and current_file and current_file in result:
            if raw_line.startswith("+"):
                # Added line — record with new-file line number
                content = raw_line[1:]
                result[current_file].added_lines[line_num] = content
                hunk_lines.append(raw_line)
                line_num += 1
            elif raw_line.startswith("-"):
                # Removed line — don't increment new-file line counter
                hunk_lines.append(raw_line)
            elif raw_line.startswith(" "):
                # Context line
                hunk_lines.append(raw_line)
                line_num += 1
            elif raw_line.startswith("\\"):
                # "\ No newline at end of file"
                hunk_lines.append(raw_line)
            else:
                # Not a valid hunk line — exit hunk state
                _flush_hunk()
                in_hunk = False

    # Flush final hunk
    _flush_hunk()
    return result


def get_added_line_numbers(diff_data: Dict[str, FileDiff], filepath: str) -> set:
    """Get the set of added line numbers for a specific file.

    Useful for determining which lines are within the PR diff range
    (e.g., for format suggestions).

    Args:
        diff_data: Parsed diff data from parse_diff().
        filepath: File path to query.

    Returns:
        Set of line numbers that were added in the diff.
    """
    if filepath not in diff_data:
        return set()
    return set(diff_data[filepath].added_lines.keys())
