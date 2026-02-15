"""Tests for stage1_format_diff.py — clang-format suggestion generation.

Test cases from STEP3_STAGE1.md:
  - 탭/스페이스 혼용 → suggestion 생성
  - 이미 올바른 포맷 → suggestion 없음
  - 20줄 초과 diff → 청크 분리 확인
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# Adjust path so we can import from scripts/
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.stage1_format_diff import (
    MAX_SUGGESTION_LINES,
    _compute_diff_regions,
    _split_into_chunks,
    generate_format_suggestions,
    find_clang_format,
)


# ============================================================================
# _compute_diff_regions tests
# ============================================================================


class TestComputeDiffRegions:
    """Tests for computing diff regions between original and formatted."""

    def test_identical_content(self):
        lines = ["line1\n", "line2\n", "line3\n"]
        regions = _compute_diff_regions(lines, lines)
        assert regions == []

    def test_single_line_change(self):
        original = ["if(x){\n", "  foo();\n", "}\n"]
        formatted = ["if (x) {\n", "  foo();\n", "}\n"]
        regions = _compute_diff_regions(original, formatted)
        assert len(regions) == 1
        assert regions[0]["start_line"] == 1
        assert regions[0]["end_line"] == 1

    def test_multiple_regions(self):
        original = [
            "line1\n",
            "bad format\n",
            "line3\n",
            "line4\n",
            "also bad\n",
        ]
        formatted = [
            "line1\n",
            "good format\n",
            "line3\n",
            "line4\n",
            "also good\n",
        ]
        regions = _compute_diff_regions(original, formatted)
        assert len(regions) == 2
        assert regions[0]["start_line"] == 2
        assert regions[1]["start_line"] == 5

    def test_added_lines(self):
        """Insert operation should anchor to adjacent line with valid range."""
        original = ["line1\n", "line2\n"]
        formatted = ["line1\n", "inserted\n", "line2\n"]
        regions = _compute_diff_regions(original, formatted)
        assert len(regions) >= 1
        for region in regions:
            assert region["start_line"] <= region["end_line"], (
                f"Invalid range: start_line={region['start_line']} > "
                f"end_line={region['end_line']}"
            )
            assert len(region["original"]) > 0, (
                "Insert region must have anchored original line"
            )

    def test_insert_at_beginning(self):
        """Insertion at the start of the file should anchor to first line."""
        original = ["existing\n"]
        formatted = ["inserted\n", "existing\n"]
        regions = _compute_diff_regions(original, formatted)
        assert len(regions) >= 1
        region = regions[0]
        assert region["start_line"] == 1
        assert region["end_line"] >= region["start_line"]
        assert len(region["original"]) > 0

    def test_insert_at_middle(self):
        """Insertion in the middle should anchor to preceding line."""
        original = ["line1\n", "line2\n", "line3\n"]
        formatted = ["line1\n", "line2\n", "new\n", "line3\n"]
        regions = _compute_diff_regions(original, formatted)
        assert len(regions) >= 1
        for region in regions:
            assert region["start_line"] <= region["end_line"]
            assert len(region["original"]) > 0

    def test_removed_lines(self):
        original = ["line1\n", "extra\n", "line2\n"]
        formatted = ["line1\n", "line2\n"]
        regions = _compute_diff_regions(original, formatted)
        assert len(regions) >= 1

    def test_region_content_preserved(self):
        original = ["old\n"]
        formatted = ["new\n"]
        regions = _compute_diff_regions(original, formatted)
        assert regions[0]["original"] == ["old\n"]
        assert regions[0]["formatted"] == ["new\n"]


# ============================================================================
# _split_into_chunks tests
# ============================================================================


class TestSplitIntoChunks:
    """Tests for splitting large regions into max-20-line chunks."""

    def test_small_region_not_split(self):
        region = {
            "start_line": 1,
            "end_line": 5,
            "original": [f"line{i}\n" for i in range(5)],
            "formatted": [f"fmt{i}\n" for i in range(5)],
        }
        chunks = _split_into_chunks(region)
        assert len(chunks) == 1
        assert chunks[0] == region

    def test_exact_max_not_split(self):
        region = {
            "start_line": 1,
            "end_line": MAX_SUGGESTION_LINES,
            "original": [f"line{i}\n" for i in range(MAX_SUGGESTION_LINES)],
            "formatted": [f"fmt{i}\n" for i in range(MAX_SUGGESTION_LINES)],
        }
        chunks = _split_into_chunks(region)
        assert len(chunks) == 1

    def test_over_max_is_split(self):
        n = MAX_SUGGESTION_LINES + 10
        region = {
            "start_line": 1,
            "end_line": n,
            "original": [f"line{i}\n" for i in range(n)],
            "formatted": [f"fmt{i}\n" for i in range(n)],
        }
        chunks = _split_into_chunks(region, max_lines=MAX_SUGGESTION_LINES)
        assert len(chunks) == 2
        # First chunk has exactly MAX_SUGGESTION_LINES original lines
        assert len(chunks[0]["original"]) == MAX_SUGGESTION_LINES
        # Second chunk has the remainder
        assert len(chunks[1]["original"]) == 10

    def test_chunk_line_numbers(self):
        n = 45
        region = {
            "start_line": 10,
            "end_line": 10 + n - 1,
            "original": [f"line{i}\n" for i in range(n)],
            "formatted": [f"fmt{i}\n" for i in range(n)],
        }
        chunks = _split_into_chunks(region, max_lines=20)
        assert len(chunks) == 3
        assert chunks[0]["start_line"] == 10
        assert chunks[0]["end_line"] == 29
        assert chunks[1]["start_line"] == 30
        assert chunks[1]["end_line"] == 49
        assert chunks[2]["start_line"] == 50
        assert chunks[2]["end_line"] == 54

    def test_all_original_lines_covered(self):
        n = 50
        region = {
            "start_line": 1,
            "end_line": n,
            "original": [f"line{i}\n" for i in range(n)],
            "formatted": [f"fmt{i}\n" for i in range(n)],
        }
        chunks = _split_into_chunks(region, max_lines=20)
        total_orig = sum(len(c["original"]) for c in chunks)
        assert total_orig == n


# ============================================================================
# generate_format_suggestions tests
# ============================================================================


class TestGenerateFormatSuggestions:
    """Tests for generating format suggestions."""

    def test_no_diff_no_suggestions(self):
        content = "line1\nline2\nline3\n"
        result = generate_format_suggestions(
            "test.cpp", content, content, {1, 2, 3}
        )
        assert result == []

    def test_suggestion_for_changed_line_in_diff(self):
        original = "if(x){\n  foo();\n}\n"
        formatted = "if (x) {\n  foo();\n}\n"
        added_lines = {1, 2, 3}
        result = generate_format_suggestions(
            "test.cpp", original, formatted, added_lines
        )
        assert len(result) >= 1
        suggestion = result[0]
        assert suggestion["file"] == "test.cpp"
        assert suggestion["rule_id"] == "clang_format"
        assert suggestion["severity"] == "suggestion"
        assert suggestion["suggestion"] is not None

    def test_comment_for_line_outside_diff(self):
        original = "bad format\nline2\n"
        formatted = "good format\nline2\n"
        # Only line 2 is in the diff range — line 1 is not
        added_lines = {2}
        result = generate_format_suggestions(
            "test.cpp", original, formatted, added_lines
        )
        # The diff region is on line 1 which is NOT in added_lines,
        # so it should be skipped (no overlap)
        assert len(result) == 0

    def test_partial_overlap_becomes_comment(self):
        original = "line1\nbad\nbad2\nline4\n"
        formatted = "line1\ngood\ngood2\nline4\n"
        # Only line 2 is in diff range, but region spans lines 2-3
        added_lines = {2}
        result = generate_format_suggestions(
            "test.cpp", original, formatted, added_lines
        )
        if result:
            # Should be info (comment) not suggestion, because partial overlap
            assert result[0]["severity"] == "info"
            assert result[0]["suggestion"] is None

    def test_full_overlap_becomes_suggestion(self):
        original = "line1\nbad\nbad2\nline4\n"
        formatted = "line1\ngood\ngood2\nline4\n"
        # Both changed lines are in the diff range
        added_lines = {2, 3}
        result = generate_format_suggestions(
            "test.cpp", original, formatted, added_lines
        )
        assert len(result) >= 1
        assert result[0]["severity"] == "suggestion"
        assert result[0]["suggestion"] is not None

    def test_suggestion_content(self):
        original = "if(x){\n}\n"
        formatted = "if (x) {\n}\n"
        added_lines = {1}
        result = generate_format_suggestions(
            "test.cpp", original, formatted, added_lines
        )
        assert len(result) >= 1
        assert "if (x) {" in result[0]["suggestion"]

    def test_chunk_splitting_for_large_diff(self):
        """Diffs over 20 lines should be split into chunks."""
        n = 30
        original = "".join(f"old_line_{i}\n" for i in range(n))
        formatted = "".join(f"new_line_{i}\n" for i in range(n))
        added_lines = set(range(1, n + 1))
        result = generate_format_suggestions(
            "test.cpp", original, formatted, added_lines
        )
        # Should have multiple suggestion blocks
        assert len(result) >= 2

    def test_empty_files(self):
        result = generate_format_suggestions("test.cpp", "", "", set())
        assert result == []

    def test_tab_space_mixing(self):
        """Tab/space mixing should produce suggestions."""
        original = "void foo() {\n    int x = 1;\n\tint y = 2;\n}\n"
        formatted = "void foo() {\n\tint x = 1;\n\tint y = 2;\n}\n"
        added_lines = {1, 2, 3, 4}
        result = generate_format_suggestions(
            "test.cpp", original, formatted, added_lines
        )
        # Line 2 has spaces where tabs are expected → should generate suggestion
        assert len(result) >= 1


# ============================================================================
# Utility tests
# ============================================================================


class TestUtilities:
    """Tests for utility functions."""

    def test_find_clang_format_returns_string_or_none(self):
        result = find_clang_format()
        assert result is None or isinstance(result, str)
