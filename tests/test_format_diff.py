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

    def test_insert_region_has_is_insert_flag(self):
        """Insert regions should be tagged with is_insert=True."""
        original = ["line1\n", "line2\n"]
        formatted = ["line1\n", "inserted\n", "line2\n"]
        regions = _compute_diff_regions(original, formatted)
        insert_regions = [r for r in regions if r.get("is_insert")]
        assert len(insert_regions) >= 1
        # insert_adj should point to the line after the anchor
        for r in insert_regions:
            assert "insert_adj" in r

    def test_replace_region_has_no_insert_flag(self):
        """Replace regions should NOT have is_insert flag."""
        original = ["old\n"]
        formatted = ["new\n"]
        regions = _compute_diff_regions(original, formatted)
        assert len(regions) == 1
        assert not regions[0].get("is_insert", False)

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

    def test_formatted_exceeds_max_triggers_split(self):
        """When formatted lines exceed max but original doesn't, still split."""
        region = {
            "start_line": 1,
            "end_line": 5,
            "original": [f"line{i}\n" for i in range(5)],
            "formatted": [f"fmt{i}\n" for i in range(50)],
        }
        chunks = _split_into_chunks(region, max_lines=20)
        # Must split — single chunk would have 50 formatted lines
        assert len(chunks) >= 2
        # Each chunk must have at least one original line
        for chunk in chunks:
            assert len(chunk["original"]) >= 1
        # All original lines must be covered
        total_orig = sum(len(c["original"]) for c in chunks)
        assert total_orig == 5
        # All formatted lines must be covered
        total_fmt = sum(len(c["formatted"]) for c in chunks)
        assert total_fmt == 50

    def test_formatted_exceeds_max_caps_chunk_size(self):
        """Formatted chunks should not exceed max_lines when possible."""
        region = {
            "start_line": 1,
            "end_line": 10,
            "original": [f"line{i}\n" for i in range(10)],
            "formatted": [f"fmt{i}\n" for i in range(60)],
        }
        chunks = _split_into_chunks(region, max_lines=20)
        # With 10 original lines we can have up to 10 chunks,
        # so 60 formatted lines / 3 chunks = 20 each (within cap)
        for chunk in chunks:
            assert len(chunk["formatted"]) <= 20

    def test_formatted_exceeds_with_few_orig_lines(self):
        """When orig is very small, chunks may exceed max on formatted side."""
        region = {
            "start_line": 1,
            "end_line": 2,
            "original": [f"line{i}\n" for i in range(2)],
            "formatted": [f"fmt{i}\n" for i in range(50)],
        }
        chunks = _split_into_chunks(region, max_lines=20)
        # Can only have 2 chunks (one per original line)
        assert len(chunks) == 2
        # Each chunk has exactly one original line
        for chunk in chunks:
            assert len(chunk["original"]) == 1
        # All formatted lines covered
        total_fmt = sum(len(c["formatted"]) for c in chunks)
        assert total_fmt == 50

    def test_both_sides_within_max_no_split(self):
        """When both orig and formatted are within max, no split needed."""
        region = {
            "start_line": 1,
            "end_line": 5,
            "original": [f"line{i}\n" for i in range(5)],
            "formatted": [f"fmt{i}\n" for i in range(15)],
        }
        chunks = _split_into_chunks(region, max_lines=20)
        assert len(chunks) == 1
        assert chunks[0] == region

    def test_formatted_split_preserves_line_numbers(self):
        """Chunks from formatted-driven split should have valid line numbers."""
        region = {
            "start_line": 10,
            "end_line": 14,
            "original": [f"line{i}\n" for i in range(5)],
            "formatted": [f"fmt{i}\n" for i in range(50)],
        }
        chunks = _split_into_chunks(region, max_lines=20)
        # Verify line number continuity
        for chunk in chunks:
            assert chunk["start_line"] <= chunk["end_line"]
            assert chunk["start_line"] >= 10
            assert chunk["end_line"] <= 14


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

    def test_insert_adjacent_to_pr_line_surfaces_comment(self):
        """Insert anchored outside diff but adjacent to PR line → info comment.

        Regression: insert-only regions were silently dropped when the
        anchor line was outside added_lines, even when the insertion
        was directly adjacent to PR-touched code.
        """
        # Original: line1 (not in diff), line2 (in diff)
        # Formatted: line1, INSERTED, line2
        # The insert anchors to line 1, but line 2 is in the diff.
        original = "line1\nline2\n"
        formatted = "line1\ninserted\nline2\n"
        added_lines = {2}  # only line 2 is in the PR diff
        result = generate_format_suggestions(
            "test.cpp", original, formatted, added_lines
        )
        # Should surface at least an info comment (not silently drop)
        assert len(result) >= 1
        info_results = [r for r in result if r["severity"] == "info"]
        assert len(info_results) >= 1

    def test_insert_not_adjacent_to_pr_line_still_skipped(self):
        """Insert anchored far from PR lines should remain skipped."""
        # Original: 4 lines, only line 4 is in diff
        # Insert after line 1, adjacent to line 2 (not in diff)
        original = "line1\nline2\nline3\nline4\n"
        formatted = "line1\ninserted\nline2\nline3\nline4\n"
        added_lines = {4}  # only line 4 is in the diff
        result = generate_format_suggestions(
            "test.cpp", original, formatted, added_lines
        )
        # The insert is between lines 1-2, far from line 4 — should be skipped
        insert_results = [
            r for r in result
            if r["severity"] == "info"
            and "삽입" in r.get("message", "")
        ]
        assert len(insert_results) == 0

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

    def test_formatted_expansion_splits_suggestions(self):
        """When formatted output expands beyond 20 lines, suggestions split.

        Regression test: _split_into_chunks previously only checked
        len(original) <= max_lines, allowing huge formatted suggestion
        bodies to pass through unsplit.
        """
        # 10 original lines expand to 50 formatted lines
        n_orig = 10
        n_fmt = 50
        original = "".join(f"orig_{i}\n" for i in range(n_orig))
        formatted = "".join(f"fmt_{i}\n" for i in range(n_fmt))
        added_lines = set(range(1, n_orig + 1))
        result = generate_format_suggestions(
            "test.cpp", original, formatted, added_lines
        )
        # Must produce multiple suggestions (not one 50-line block)
        assert len(result) >= 2
        # Each suggestion's body should be capped at ~20 lines
        for s in result:
            if s["suggestion"] is not None:
                suggestion_lines = s["suggestion"].split("\n")
                assert len(suggestion_lines) <= MAX_SUGGESTION_LINES


# ============================================================================
# Utility tests
# ============================================================================


class TestUtilities:
    """Tests for utility functions."""

    def test_find_clang_format_returns_string_or_none(self):
        result = find_clang_format()
        assert result is None or isinstance(result, str)


class TestMainGracefulDegradation:
    """Tests for main() behavior when clang-format is absent."""

    def test_exits_zero_when_clang_format_missing(self, monkeypatch):
        """main() should exit 0 (not 1) when clang-format is not found."""
        import scripts.stage1_format_diff as mod

        monkeypatch.setattr(mod, "find_clang_format", lambda: None)
        monkeypatch.setattr(
            "sys.argv",
            ["stage1_format_diff.py", "--files", '["test.cpp"]'],
        )
        with pytest.raises(SystemExit) as exc_info:
            mod.main()
        assert exc_info.value.code == 0

    def test_outputs_empty_json_when_clang_format_missing(
        self, monkeypatch, tmp_path, capsys
    ):
        """main() should output empty JSON array when clang-format is absent."""
        import scripts.stage1_format_diff as mod

        monkeypatch.setattr(mod, "find_clang_format", lambda: None)
        monkeypatch.setattr(
            "sys.argv",
            ["stage1_format_diff.py", "--files", '["test.cpp"]'],
        )
        with pytest.raises(SystemExit):
            mod.main()
        captured = capsys.readouterr()
        assert "[]" in captured.out
