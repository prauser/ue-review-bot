"""Tests for post_review.py — PR review posting and finding aggregation.

Covers:
  - Finding loading from multiple JSON files
  - Deduplication by file+line (higher severity wins)
  - Comment body formatting with suggestion blocks
  - Multi-line comment range handling
  - Review summary generation
  - Batch splitting for large reviews
  - Dry-run CLI mode
  - API posting with mock GitHubClient
  - Error handling for missing/malformed files
"""

from __future__ import annotations

import json
import os
import sys
import textwrap
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.post_review import (
    MAX_COMMENTS_PER_REVIEW,
    build_review_comments,
    build_summary,
    deduplicate_findings,
    filter_already_posted,
    filter_findings_by_diff,
    format_comment_body,
    load_findings,
    post_review,
    split_into_batches,
)
from scripts.utils.gh_api import GitHubClient


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def stage1_findings():
    """Sample Stage 1 pattern checker findings."""
    return [
        {
            "file": "Source/MyActor.cpp",
            "line": 42,
            "rule_id": "logtemp",
            "severity": "warning",
            "message": "LogTemp 대신 적절한 로그 카테고리를 사용하세요.",
            "suggestion": None,
        },
        {
            "file": "Source/MyActor.cpp",
            "line": 100,
            "rule_id": "pragma_optimize_off",
            "severity": "error",
            "message": "#pragma optimize(\"\", off)는 제거하세요.",
            "suggestion": None,
        },
        {
            "file": "Source/MyPawn.h",
            "line": 15,
            "rule_id": "macro_no_semicolon",
            "severity": "warning",
            "message": "매크로 호출 뒤에 세미콜론을 추가하세요.",
            "suggestion": "\tUE_LOG(LogMyGame, Warning, TEXT(\"test\"));",
        },
    ]


@pytest.fixture
def format_findings():
    """Sample Stage 1 format diff findings."""
    return [
        {
            "file": "Source/MyActor.cpp",
            "line": 10,
            "end_line": 12,
            "rule_id": "clang_format",
            "severity": "suggestion",
            "message": "clang-format 자동 수정 제안",
            "suggestion": "    if (bFlag == false)\n    {\n        DoSomething();\n    }",
        },
    ]


@pytest.fixture
def stage2_findings():
    """Sample Stage 2 clang-tidy findings."""
    return [
        {
            "file": "Source/MyActor.cpp",
            "line": 55,
            "rule_id": "modernize-use-override",
            "severity": "warning",
            "message": "override 키워드를 추가하세요.",
            "suggestion": "    virtual void BeginPlay() override;",
        },
    ]


@pytest.fixture
def stage3_findings():
    """Sample Stage 3 LLM findings."""
    return [
        {
            "file": "Source/MyActor.cpp",
            "line": 80,
            "end_line": 85,
            "rule_id": "gc_safety",
            "severity": "error",
            "message": "UObject 파생 포인터 멤버에 UPROPERTY가 누락되었습니다.",
            "suggestion": None,
        },
    ]


@pytest.fixture
def tmp_findings_dir(tmp_path):
    """Create temp directory with sample finding JSON files."""

    def _write(name, data):
        path = tmp_path / name
        path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        return str(path)

    return _write


# ---------------------------------------------------------------------------
# load_findings
# ---------------------------------------------------------------------------


class TestLoadFindings:

    def test_load_single_file(self, tmp_findings_dir, stage1_findings):
        path = tmp_findings_dir("stage1.json", stage1_findings)
        result = load_findings([path])
        assert len(result) == 3
        assert result[0]["rule_id"] == "logtemp"

    def test_load_multiple_files(
        self, tmp_findings_dir, stage1_findings, format_findings
    ):
        p1 = tmp_findings_dir("stage1.json", stage1_findings)
        p2 = tmp_findings_dir("format.json", format_findings)
        result = load_findings([p1, p2])
        assert len(result) == 4

    def test_missing_file_skipped(self, tmp_findings_dir, stage1_findings):
        p1 = tmp_findings_dir("stage1.json", stage1_findings)
        result = load_findings([p1, "/nonexistent/file.json"])
        assert len(result) == 3

    def test_empty_array(self, tmp_findings_dir):
        path = tmp_findings_dir("empty.json", [])
        result = load_findings([path])
        assert result == []

    def test_invalid_json(self, tmp_path):
        path = tmp_path / "bad.json"
        path.write_text("not valid json{{{", encoding="utf-8")
        result = load_findings([str(path)])
        assert result == []

    def test_non_array_json(self, tmp_path):
        path = tmp_path / "obj.json"
        path.write_text('{"key": "value"}', encoding="utf-8")
        result = load_findings([str(path)])
        assert result == []

    def test_no_files(self):
        result = load_findings([])
        assert result == []


# ---------------------------------------------------------------------------
# deduplicate_findings
# ---------------------------------------------------------------------------


class TestDeduplicateFindings:

    def test_no_duplicates(self, stage1_findings):
        result = deduplicate_findings(stage1_findings)
        assert len(result) == 3

    def test_same_file_line_same_rule_keeps_higher_severity(self):
        """Same file+line+rule_id from different stages: higher severity wins."""
        findings = [
            {
                "file": "Source/A.cpp",
                "line": 10,
                "severity": "warning",
                "rule_id": "logtemp",
                "message": "stage1 warning",
            },
            {
                "file": "Source/A.cpp",
                "line": 10,
                "severity": "error",
                "rule_id": "logtemp",
                "message": "stage3 error",
            },
        ]
        result = deduplicate_findings(findings)
        assert len(result) == 1
        assert result[0]["severity"] == "error"

    def test_same_file_line_different_rules_kept(self):
        """Different rules on the same line must both be kept."""
        findings = [
            {
                "file": "Source/A.cpp",
                "line": 10,
                "severity": "warning",
                "rule_id": "logtemp",
                "message": "LogTemp warning",
            },
            {
                "file": "Source/A.cpp",
                "line": 10,
                "severity": "warning",
                "rule_id": "macro_no_semicolon",
                "message": "Missing semicolon",
            },
        ]
        result = deduplicate_findings(findings)
        assert len(result) == 2
        rule_ids = {r["rule_id"] for r in result}
        assert rule_ids == {"logtemp", "macro_no_semicolon"}

    def test_same_file_different_lines_kept(self):
        findings = [
            {"file": "Source/A.cpp", "line": 10, "severity": "warning"},
            {"file": "Source/A.cpp", "line": 20, "severity": "warning"},
        ]
        result = deduplicate_findings(findings)
        assert len(result) == 2

    def test_different_files_same_line_kept(self):
        findings = [
            {"file": "Source/A.cpp", "line": 10, "severity": "warning"},
            {"file": "Source/B.cpp", "line": 10, "severity": "warning"},
        ]
        result = deduplicate_findings(findings)
        assert len(result) == 2

    def test_equal_severity_same_rule_keeps_first(self):
        """Same file+line+rule_id with equal severity: first one wins."""
        findings = [
            {
                "file": "Source/A.cpp",
                "line": 10,
                "severity": "warning",
                "rule_id": "logtemp",
                "message": "first",
            },
            {
                "file": "Source/A.cpp",
                "line": 10,
                "severity": "warning",
                "rule_id": "logtemp",
                "message": "second",
            },
        ]
        result = deduplicate_findings(findings)
        assert len(result) == 1
        assert result[0]["message"] == "first"

    def test_empty_findings(self):
        assert deduplicate_findings([]) == []

    def test_stage3_category_as_rule_id(self):
        """Stage 3 findings use 'category' instead of 'rule_id'; dedup should use it."""
        findings = [
            {
                "file": "Source/A.cpp",
                "line": 10,
                "severity": "warning",
                "category": "gc_safety",
                "message": "UPROPERTY missing",
            },
            {
                "file": "Source/A.cpp",
                "line": 10,
                "severity": "error",
                "category": "network",
                "message": "Reliable RPC misuse",
            },
        ]
        result = deduplicate_findings(findings)
        assert len(result) == 2
        categories = {r.get("category") for r in result}
        assert categories == {"gc_safety", "network"}

    def test_stage3_same_category_deduped(self):
        """Same file+line+category across duplicate Stage 3 calls → keep higher severity."""
        findings = [
            {
                "file": "Source/A.cpp",
                "line": 10,
                "severity": "warning",
                "category": "gc_safety",
                "message": "first",
            },
            {
                "file": "Source/A.cpp",
                "line": 10,
                "severity": "error",
                "category": "gc_safety",
                "message": "second",
            },
        ]
        result = deduplicate_findings(findings)
        assert len(result) == 1
        assert result[0]["severity"] == "error"

    def test_mixed_rule_id_and_category_on_same_line(self):
        """Stage 1 rule_id + Stage 3 category on same line → both kept."""
        findings = [
            {
                "file": "Source/A.cpp",
                "line": 10,
                "severity": "warning",
                "rule_id": "logtemp",
                "message": "Stage 1",
            },
            {
                "file": "Source/A.cpp",
                "line": 10,
                "severity": "error",
                "category": "gc_safety",
                "message": "Stage 3",
            },
        ]
        result = deduplicate_findings(findings)
        assert len(result) == 2

    def test_severity_priority_order(self):
        """error > warning > suggestion > info (same rule_id across stages)."""
        findings = [
            {"file": "A.cpp", "line": 1, "severity": "info", "rule_id": "r1"},
            {"file": "A.cpp", "line": 1, "severity": "suggestion", "rule_id": "r1"},
        ]
        result = deduplicate_findings(findings)
        assert len(result) == 1
        assert result[0]["severity"] == "suggestion"

        findings2 = [
            {"file": "A.cpp", "line": 1, "severity": "suggestion", "rule_id": "r2"},
            {"file": "A.cpp", "line": 1, "severity": "warning", "rule_id": "r2"},
        ]
        result2 = deduplicate_findings(findings2)
        assert len(result2) == 1
        assert result2[0]["severity"] == "warning"


# ---------------------------------------------------------------------------
# format_comment_body
# ---------------------------------------------------------------------------


class TestFormatCommentBody:

    def test_warning_without_suggestion(self):
        finding = {
            "severity": "warning",
            "message": "LogTemp 대신 적절한 로그 카테고리를 사용하세요.",
            "rule_id": "logtemp",
        }
        body = format_comment_body(finding)
        assert "[WARNING]" in body
        assert "`logtemp`" in body
        assert "LogTemp" in body
        assert "```suggestion" not in body

    def test_error_without_suggestion(self):
        finding = {
            "severity": "error",
            "message": "check() 내 부작용 함수 호출이 감지되었습니다.",
            "rule_id": "check_side_effect",
        }
        body = format_comment_body(finding)
        assert "[ERROR]" in body
        assert "`check_side_effect`" in body

    def test_with_suggestion_block(self):
        finding = {
            "severity": "suggestion",
            "message": "clang-format 자동 수정 제안",
            "rule_id": "clang_format",
            "suggestion": "    if (bFlag)\n    {\n    }",
        }
        body = format_comment_body(finding)
        assert "```suggestion" in body
        assert "    if (bFlag)" in body
        assert "```" in body

    def test_warning_with_autofix_suggestion(self):
        finding = {
            "severity": "warning",
            "message": "매크로 호출 뒤에 세미콜론을 추가하세요.",
            "rule_id": "macro_no_semicolon",
            "suggestion": "\tUE_LOG(LogMyGame, Warning, TEXT(\"test\"));",
        }
        body = format_comment_body(finding)
        assert "[WARNING]" in body
        assert "```suggestion" in body
        assert 'UE_LOG(LogMyGame, Warning, TEXT("test"));' in body

    def test_info_severity(self):
        finding = {
            "severity": "info",
            "message": "Format difference outside PR range.",
            "rule_id": "clang_format",
        }
        body = format_comment_body(finding)
        assert "[INFO]" in body

    def test_no_rule_id(self):
        finding = {
            "severity": "warning",
            "message": "Some message",
        }
        body = format_comment_body(finding)
        assert "[WARNING]" in body
        assert "`" not in body  # No backtick-wrapped rule_id

    def test_empty_message(self):
        finding = {"severity": "error", "rule_id": "test", "message": ""}
        body = format_comment_body(finding)
        assert "[ERROR]" in body
        assert "`test`" in body

    def test_stage3_category_shown_as_rule(self):
        """Stage 3 findings without rule_id use category in the header."""
        finding = {
            "severity": "error",
            "category": "gc_safety",
            "message": "UPROPERTY missing",
        }
        body = format_comment_body(finding)
        assert "`gc_safety`" in body
        assert "[ERROR]" in body

    def test_no_rule_id_no_category(self):
        """Neither rule_id nor category → no backtick label."""
        finding = {
            "severity": "warning",
            "message": "Some generic message",
        }
        body = format_comment_body(finding)
        assert "`" not in body


# ---------------------------------------------------------------------------
# build_review_comments
# ---------------------------------------------------------------------------


class TestBuildReviewComments:

    def test_single_line_comment(self, stage1_findings):
        comments = build_review_comments(stage1_findings)
        assert len(comments) == 3

        logtemp = comments[0]
        assert logtemp["path"] == "Source/MyActor.cpp"
        assert logtemp["line"] == 42
        assert logtemp["side"] == "RIGHT"
        assert "start_line" not in logtemp

    def test_multi_line_comment(self, stage3_findings):
        comments = build_review_comments(stage3_findings)
        assert len(comments) == 1

        comment = comments[0]
        assert comment["path"] == "Source/MyActor.cpp"
        assert comment["line"] == 85  # end_line
        assert comment["start_line"] == 80
        assert comment["start_side"] == "RIGHT"

    def test_multi_line_format_suggestion(self, format_findings):
        comments = build_review_comments(format_findings)
        assert len(comments) == 1

        comment = comments[0]
        assert comment["line"] == 12  # end_line
        assert comment["start_line"] == 10
        assert "```suggestion" in comment["body"]

    def test_skips_invalid_findings(self):
        findings = [
            {"file": "", "line": 10, "severity": "warning", "message": "x"},
            {"file": "A.cpp", "line": 0, "severity": "warning", "message": "x"},
            {"file": "A.cpp", "line": -1, "severity": "warning", "message": "x"},
        ]
        comments = build_review_comments(findings)
        assert len(comments) == 0

    def test_skips_non_int_line_values(self):
        """Malformed line values (string, null, float) should be skipped, not crash."""
        findings = [
            {"file": "A.cpp", "line": None, "severity": "warning", "message": "x"},
            {"file": "A.cpp", "line": "not a number", "severity": "warning", "message": "x"},
            {"file": "A.cpp", "line": "", "severity": "warning", "message": "x"},
        ]
        comments = build_review_comments(findings)
        assert len(comments) == 0

    def test_string_int_line_coerced(self):
        """A line value like '42' (string) should be coerced to int and work."""
        findings = [
            {"file": "A.cpp", "line": "42", "severity": "warning", "rule_id": "t", "message": "x"},
        ]
        comments = build_review_comments(findings)
        assert len(comments) == 1
        assert comments[0]["line"] == 42

    def test_non_int_end_line_ignored(self):
        """Malformed end_line should fall back to single-line comment."""
        findings = [
            {
                "file": "A.cpp",
                "line": 10,
                "end_line": "bad",
                "severity": "warning",
                "rule_id": "t",
                "message": "x",
            },
        ]
        comments = build_review_comments(findings)
        assert len(comments) == 1
        assert comments[0]["line"] == 10
        assert "start_line" not in comments[0]

    def test_empty_findings(self):
        assert build_review_comments([]) == []

    def test_same_start_and_end_line(self):
        """When end_line == line, no start_line should be set."""
        findings = [
            {
                "file": "A.cpp",
                "line": 10,
                "end_line": 10,
                "severity": "warning",
                "rule_id": "test",
                "message": "msg",
            }
        ]
        comments = build_review_comments(findings)
        assert len(comments) == 1
        assert comments[0]["line"] == 10
        assert "start_line" not in comments[0]


# ---------------------------------------------------------------------------
# build_summary
# ---------------------------------------------------------------------------


class TestBuildSummary:

    def test_no_findings(self):
        summary = build_summary([])
        assert "No issues found" in summary
        assert "UE5 Code Review Bot" in summary

    def test_with_findings(self, stage1_findings):
        summary = build_summary(stage1_findings)
        assert "3" in summary
        assert "[ERROR]" in summary
        assert "[WARNING]" in summary
        assert "`logtemp`" in summary

    def test_with_stages(self, stage1_findings):
        summary = build_summary(stage1_findings, ["stage1", "stage2"])
        assert "stage1" in summary
        assert "stage2" in summary

    def test_all_severities(self):
        findings = [
            {"severity": "error", "rule_id": "a"},
            {"severity": "warning", "rule_id": "b"},
            {"severity": "suggestion", "rule_id": "c"},
            {"severity": "info", "rule_id": "d"},
        ]
        summary = build_summary(findings)
        assert "[ERROR]: 1" in summary
        assert "[WARNING]: 1" in summary
        assert "[SUGGESTION]: 1" in summary
        assert "[INFO]: 1" in summary

    def test_rule_count_summary(self):
        findings = [
            {"severity": "warning", "rule_id": "logtemp"},
            {"severity": "warning", "rule_id": "logtemp"},
            {"severity": "error", "rule_id": "pragma_optimize_off"},
        ]
        summary = build_summary(findings)
        assert "`logtemp`: 2" in summary
        assert "`pragma_optimize_off`: 1" in summary


# ---------------------------------------------------------------------------
# split_into_batches
# ---------------------------------------------------------------------------


class TestSplitIntoBatches:

    def test_empty_list(self):
        assert split_into_batches([]) == []

    def test_under_limit(self):
        comments = [{"body": f"c{i}"} for i in range(10)]
        batches = split_into_batches(comments)
        assert len(batches) == 1
        assert len(batches[0]) == 10

    def test_exact_limit(self):
        comments = [{"body": f"c{i}"} for i in range(MAX_COMMENTS_PER_REVIEW)]
        batches = split_into_batches(comments)
        assert len(batches) == 1

    def test_over_limit(self):
        n = MAX_COMMENTS_PER_REVIEW + 10
        comments = [{"body": f"c{i}"} for i in range(n)]
        batches = split_into_batches(comments)
        assert len(batches) == 2
        assert len(batches[0]) == MAX_COMMENTS_PER_REVIEW
        assert len(batches[1]) == 10

    def test_custom_batch_size(self):
        comments = [{"body": f"c{i}"} for i in range(25)]
        batches = split_into_batches(comments, batch_size=10)
        assert len(batches) == 3
        assert len(batches[0]) == 10
        assert len(batches[1]) == 10
        assert len(batches[2]) == 5


# ---------------------------------------------------------------------------
# post_review (with mock client)
# ---------------------------------------------------------------------------


class TestPostReview:

    def _make_client(self) -> MagicMock:
        client = MagicMock(spec=GitHubClient)
        client.create_review.return_value = {"id": 12345}
        return client

    def test_post_no_findings(self):
        client = self._make_client()
        responses = post_review(
            client, "owner", "repo", 1, "abc123", [], ["stage1"]
        )
        assert len(responses) == 1
        client.create_review.assert_called_once()
        call_kwargs = client.create_review.call_args
        assert call_kwargs[1]["comments"] == []
        assert "No issues found" in call_kwargs[1]["body"]

    def test_post_with_findings(self, stage1_findings):
        client = self._make_client()
        responses = post_review(
            client, "owner", "repo", 1, "abc123", stage1_findings, ["stage1"]
        )
        assert len(responses) == 1
        call_kwargs = client.create_review.call_args
        assert len(call_kwargs[1]["comments"]) == 3
        assert "3" in call_kwargs[1]["body"]

    def test_post_batched(self):
        """When comments exceed MAX_COMMENTS_PER_REVIEW, multiple reviews."""
        client = self._make_client()
        findings = [
            {
                "file": f"Source/File{i}.cpp",
                "line": 10,
                "severity": "warning",
                "rule_id": "logtemp",
                "message": "msg",
            }
            for i in range(MAX_COMMENTS_PER_REVIEW + 5)
        ]

        responses = post_review(
            client, "owner", "repo", 1, "abc123", findings
        )
        assert len(responses) == 2
        assert client.create_review.call_count == 2

        # First call should have summary
        first_call = client.create_review.call_args_list[0]
        assert "UE5 Code Review Bot" in first_call[1]["body"]

        # Second call should have continuation
        second_call = client.create_review.call_args_list[1]
        assert "continued" in second_call[1]["body"]

    def test_post_api_error_continues(self, stage1_findings):
        """API errors on one batch shouldn't stop remaining batches."""
        client = self._make_client()
        client.create_review.side_effect = RuntimeError("API error")

        responses = post_review(
            client, "owner", "repo", 1, "abc123", stage1_findings
        )
        assert len(responses) == 1
        assert "error" in responses[0]

    def test_post_commit_sha_passed(self, stage1_findings):
        client = self._make_client()
        post_review(client, "owner", "repo", 42, "deadbeef", stage1_findings)
        call_kwargs = client.create_review.call_args
        assert call_kwargs[1]["commit_sha"] == "deadbeef"
        assert call_kwargs[1]["pr_number"] == 42

    def test_post_event_is_comment(self, stage1_findings):
        client = self._make_client()
        post_review(client, "owner", "repo", 1, "abc123", stage1_findings)
        call_kwargs = client.create_review.call_args
        assert call_kwargs[1]["event"] == "COMMENT"

    def test_post_filters_existing_comments(self, stage1_findings):
        """Already-posted comments should be filtered out."""
        client = self._make_client()

        # Build what the existing comments look like on the PR
        from scripts.post_review import build_review_comments

        existing = []
        for c in build_review_comments(stage1_findings[:1]):
            existing.append({
                "path": c["path"],
                "line": c["line"],
                "body": c["body"],
                "commit_id": "abc123",  # same commit as the review
            })

        responses = post_review(
            client, "owner", "repo", 1, "abc123", stage1_findings,
            existing_comments=existing,
        )
        assert len(responses) == 1
        # Should have only 2 comments (first one was filtered)
        call_kwargs = client.create_review.call_args
        assert len(call_kwargs[1]["comments"]) == 2

    def test_post_no_existing_comments(self, stage1_findings):
        """When existing_comments is empty, all comments are posted."""
        client = self._make_client()
        responses = post_review(
            client, "owner", "repo", 1, "abc123", stage1_findings,
            existing_comments=[],
        )
        call_kwargs = client.create_review.call_args
        assert len(call_kwargs[1]["comments"]) == 3

    def test_all_failures_tracked_in_response(self):
        """All-failure responses should have 'error' keys for exit code check."""
        client = self._make_client()
        client.create_review.side_effect = RuntimeError("token expired")

        findings = [
            {
                "file": "A.cpp",
                "line": 10,
                "severity": "warning",
                "rule_id": "logtemp",
                "message": "msg",
            }
        ]
        responses = post_review(
            client, "owner", "repo", 1, "abc123", findings
        )
        assert all("error" in r for r in responses)

    def test_partial_failure_has_mixed_responses(self):
        """Some batches succeed, some fail → mixed responses."""
        client = self._make_client()
        # First call succeeds, second fails
        client.create_review.side_effect = [
            {"id": 1},
            RuntimeError("rate limit"),
        ]

        findings = [
            {
                "file": f"Source/File{i}.cpp",
                "line": 10,
                "severity": "warning",
                "rule_id": "logtemp",
                "message": "msg",
            }
            for i in range(MAX_COMMENTS_PER_REVIEW + 5)
        ]

        responses = post_review(
            client, "owner", "repo", 1, "abc123", findings
        )
        assert len(responses) == 2
        assert "error" not in responses[0]
        assert "error" in responses[1]

    def test_all_filtered_skips_summary_review(self, stage1_findings):
        """When all comments were already posted, no review is created."""
        client = self._make_client()

        from scripts.post_review import build_review_comments

        # Build existing comments matching all findings on same commit
        existing = []
        for c in build_review_comments(stage1_findings):
            existing.append({
                "path": c["path"],
                "line": c["line"],
                "body": c["body"],
                "commit_id": "abc123",
            })

        responses = post_review(
            client, "owner", "repo", 1, "abc123", stage1_findings,
            existing_comments=existing,
        )
        assert len(responses) == 1
        assert "skipped" in responses[0]
        # No API call should have been made
        client.create_review.assert_not_called()

    def test_summary_only_api_error_returns_error_dict(self):
        """Summary-only review API failure should return error dict, not crash."""
        client = self._make_client()
        client.create_review.side_effect = RuntimeError("transient 500")

        responses = post_review(
            client, "owner", "repo", 1, "abc123", [], ["stage1"]
        )
        assert len(responses) == 1
        assert "error" in responses[0]
        assert "transient 500" in responses[0]["error"]


# ---------------------------------------------------------------------------
# filter_already_posted
# ---------------------------------------------------------------------------


class TestFilterAlreadyPosted:

    def test_no_existing_keeps_all(self):
        comments = [
            {"path": "A.cpp", "line": 10, "body": "**[WARNING]** `logtemp`\nmsg"},
            {"path": "B.cpp", "line": 20, "body": "**[ERROR]** `x`\nmsg"},
        ]
        result = filter_already_posted(comments, [])
        assert len(result) == 2

    def test_exact_match_removed(self):
        comments = [
            {"path": "A.cpp", "line": 10, "body": "**[WARNING]** `logtemp`\nmsg"},
        ]
        existing = [
            {"path": "A.cpp", "line": 10, "body": "**[WARNING]** `logtemp`\nmsg"},
        ]
        result = filter_already_posted(comments, existing)
        assert len(result) == 0

    def test_different_line_kept(self):
        comments = [
            {"path": "A.cpp", "line": 10, "body": "**[WARNING]** msg"},
        ]
        existing = [
            {"path": "A.cpp", "line": 99, "body": "**[WARNING]** msg"},
        ]
        result = filter_already_posted(comments, existing)
        assert len(result) == 1

    def test_different_body_kept(self):
        comments = [
            {"path": "A.cpp", "line": 10, "body": "new comment"},
        ]
        existing = [
            {"path": "A.cpp", "line": 10, "body": "old comment"},
        ]
        result = filter_already_posted(comments, existing)
        assert len(result) == 1

    def test_partial_match_filters_duplicate(self):
        """Only first 120 chars of body compared — long tails ignored."""
        prefix = "x" * 120
        comments = [
            {"path": "A.cpp", "line": 10, "body": prefix + "NEW_TAIL"},
        ]
        existing = [
            {"path": "A.cpp", "line": 10, "body": prefix + "OLD_TAIL"},
        ]
        result = filter_already_posted(comments, existing)
        assert len(result) == 0

    def test_mixed_keeps_new_only(self):
        comments = [
            {"path": "A.cpp", "line": 10, "body": "dup"},
            {"path": "A.cpp", "line": 20, "body": "new"},
        ]
        existing = [
            {"path": "A.cpp", "line": 10, "body": "dup"},
        ]
        result = filter_already_posted(comments, existing)
        assert len(result) == 1
        assert result[0]["body"] == "new"

    def test_old_commit_comments_not_suppressing(self):
        """Comments from an older commit should NOT suppress new findings."""
        comments = [
            {"path": "A.cpp", "line": 10, "body": "same body"},
        ]
        existing = [
            {
                "path": "A.cpp",
                "line": 10,
                "body": "same body",
                "commit_id": "old_commit_aaa",
            },
        ]
        result = filter_already_posted(comments, existing, commit_sha="new_commit_bbb")
        assert len(result) == 1  # Not suppressed — different commit

    def test_same_commit_comments_suppressed(self):
        """Comments from the current commit SHOULD suppress duplicates."""
        comments = [
            {"path": "A.cpp", "line": 10, "body": "same body"},
        ]
        existing = [
            {
                "path": "A.cpp",
                "line": 10,
                "body": "same body",
                "commit_id": "abc123",
            },
        ]
        result = filter_already_posted(comments, existing, commit_sha="abc123")
        assert len(result) == 0

    def test_no_commit_sha_falls_back_to_all(self):
        """When commit_sha is None, all existing comments are considered."""
        comments = [
            {"path": "A.cpp", "line": 10, "body": "dup"},
        ]
        existing = [
            {"path": "A.cpp", "line": 10, "body": "dup", "commit_id": "whatever"},
        ]
        result = filter_already_posted(comments, existing, commit_sha=None)
        assert len(result) == 0

    def test_start_line_distinguishes_multiline(self):
        """Different start_line on same end line should not match."""
        comments = [
            {"path": "A.cpp", "start_line": 5, "line": 10, "body": "msg"},
        ]
        existing = [
            {"path": "A.cpp", "start_line": 1, "line": 10, "body": "msg"},
        ]
        result = filter_already_posted(comments, existing)
        assert len(result) == 1  # Different start_line → not a duplicate

    def test_single_vs_multiline_not_matched(self):
        """A single-line comment (no start_line) should not match a multi-line one."""
        comments = [
            {"path": "A.cpp", "line": 10, "body": "msg"},
        ]
        existing = [
            {"path": "A.cpp", "start_line": 5, "line": 10, "body": "msg"},
        ]
        result = filter_already_posted(comments, existing)
        assert len(result) == 1  # Single-line vs multi-line → different

    def test_multiline_exact_match_removed(self):
        """Multi-line comment with matching start_line+line should be filtered."""
        comments = [
            {"path": "A.cpp", "start_line": 5, "line": 10, "body": "msg"},
        ]
        existing = [
            {"path": "A.cpp", "start_line": 5, "line": 10, "body": "msg"},
        ]
        result = filter_already_posted(comments, existing)
        assert len(result) == 0


# ---------------------------------------------------------------------------
# filter_findings_by_diff
# ---------------------------------------------------------------------------


class TestFilterFindingsByDiff:

    SAMPLE_DIFF = (
        "diff --git a/Source/MyActor.cpp b/Source/MyActor.cpp\n"
        "index aaa..bbb 100644\n"
        "--- a/Source/MyActor.cpp\n"
        "+++ b/Source/MyActor.cpp\n"
        "@@ -40,6 +40,8 @@ void AMyActor::BeginPlay()\n"
        "     Super::BeginPlay();\n"
        " \n"
        "     // Context line\n"
        "+    UE_LOG(LogTemp, Warning, TEXT(\"Added line 43\"));\n"
        "+    UE_LOG(LogTemp, Warning, TEXT(\"Added line 44\"));\n"
        "     // More context\n"
        " \n"
        "     DoSomething();\n"
        "@@ -100,3 +102,4 @@ void AMyActor::Tick(float DeltaTime)\n"
        "     Super::Tick(DeltaTime);\n"
        " \n"
        "     UpdateStuff();\n"
        "+    NewTickLogic();\n"
    )

    def test_finding_inside_hunk_kept(self):
        findings = [
            {"file": "Source/MyActor.cpp", "line": 43, "severity": "warning",
             "rule_id": "logtemp", "message": "msg"},
        ]
        result = filter_findings_by_diff(findings, self.SAMPLE_DIFF)
        assert len(result) == 1

    def test_finding_outside_hunk_dropped(self):
        findings = [
            {"file": "Source/MyActor.cpp", "line": 80, "severity": "warning",
             "rule_id": "logtemp", "message": "msg"},
        ]
        result = filter_findings_by_diff(findings, self.SAMPLE_DIFF)
        assert len(result) == 0

    def test_finding_in_second_hunk_kept(self):
        findings = [
            {"file": "Source/MyActor.cpp", "line": 105, "severity": "error",
             "rule_id": "some_rule", "message": "msg"},
        ]
        result = filter_findings_by_diff(findings, self.SAMPLE_DIFF)
        assert len(result) == 1

    def test_file_not_in_diff_dropped(self):
        findings = [
            {"file": "Source/OtherFile.cpp", "line": 10, "severity": "warning",
             "rule_id": "logtemp", "message": "msg"},
        ]
        result = filter_findings_by_diff(findings, self.SAMPLE_DIFF)
        assert len(result) == 0

    def test_mixed_findings_partial_filter(self):
        findings = [
            {"file": "Source/MyActor.cpp", "line": 43, "severity": "warning",
             "rule_id": "logtemp", "message": "in hunk 1"},
            {"file": "Source/MyActor.cpp", "line": 80, "severity": "error",
             "rule_id": "gc_safety", "message": "outside hunks"},
            {"file": "Source/MyActor.cpp", "line": 105, "severity": "warning",
             "rule_id": "tick", "message": "in hunk 2"},
            {"file": "Source/Missing.cpp", "line": 1, "severity": "info",
             "rule_id": "x", "message": "missing file"},
        ]
        result = filter_findings_by_diff(findings, self.SAMPLE_DIFF)
        assert len(result) == 2
        rules = {r["rule_id"] for r in result}
        assert rules == {"logtemp", "tick"}

    def test_empty_findings(self):
        result = filter_findings_by_diff([], self.SAMPLE_DIFF)
        assert result == []

    def test_empty_diff(self):
        findings = [
            {"file": "Source/MyActor.cpp", "line": 10, "severity": "warning",
             "rule_id": "x", "message": "msg"},
        ]
        result = filter_findings_by_diff(findings, "")
        assert len(result) == 0

    def test_context_line_in_hunk_kept(self):
        """Findings on context lines (not added, but within hunk range) are kept."""
        findings = [
            {"file": "Source/MyActor.cpp", "line": 40, "severity": "warning",
             "rule_id": "x", "message": "context line at hunk start"},
        ]
        result = filter_findings_by_diff(findings, self.SAMPLE_DIFF)
        assert len(result) == 1

    def test_string_line_number_coerced(self):
        """String line numbers should be coerced to int."""
        findings = [
            {"file": "Source/MyActor.cpp", "line": "43", "severity": "warning",
             "rule_id": "logtemp", "message": "msg"},
        ]
        result = filter_findings_by_diff(findings, self.SAMPLE_DIFF)
        assert len(result) == 1

    def test_null_line_number_dropped(self):
        """Null/invalid line → line=0 → won't be in any hunk."""
        findings = [
            {"file": "Source/MyActor.cpp", "line": None, "severity": "warning",
             "rule_id": "x", "message": "msg"},
        ]
        result = filter_findings_by_diff(findings, self.SAMPLE_DIFF)
        assert len(result) == 0

    def test_multiline_both_in_hunk_kept(self):
        """Multi-line finding with both line and end_line in same hunk is kept."""
        findings = [
            {"file": "Source/MyActor.cpp", "line": 41, "end_line": 44,
             "severity": "suggestion", "rule_id": "clang_format", "message": "msg"},
        ]
        result = filter_findings_by_diff(findings, self.SAMPLE_DIFF)
        assert len(result) == 1

    def test_multiline_end_line_outside_hunk_dropped(self):
        """Multi-line finding with end_line outside hunk range is dropped."""
        findings = [
            {"file": "Source/MyActor.cpp", "line": 43, "end_line": 55,
             "severity": "suggestion", "rule_id": "clang_format", "message": "msg"},
        ]
        result = filter_findings_by_diff(findings, self.SAMPLE_DIFF)
        assert len(result) == 0

    def test_multiline_start_outside_hunk_dropped(self):
        """Multi-line finding with start line before hunk range is dropped."""
        findings = [
            {"file": "Source/MyActor.cpp", "line": 38, "end_line": 43,
             "severity": "suggestion", "rule_id": "clang_format", "message": "msg"},
        ]
        result = filter_findings_by_diff(findings, self.SAMPLE_DIFF)
        assert len(result) == 0

    def test_multiline_spanning_two_hunks_dropped(self):
        """Multi-line finding spanning across two separate hunks is dropped."""
        findings = [
            {"file": "Source/MyActor.cpp", "line": 43, "end_line": 105,
             "severity": "warning", "rule_id": "x", "message": "msg"},
        ]
        result = filter_findings_by_diff(findings, self.SAMPLE_DIFF)
        assert len(result) == 0

    def test_multiline_equal_lines_treated_as_single(self):
        """end_line == line → treated as single-line, normal hunk check."""
        findings = [
            {"file": "Source/MyActor.cpp", "line": 43, "end_line": 43,
             "severity": "warning", "rule_id": "x", "message": "msg"},
        ]
        result = filter_findings_by_diff(findings, self.SAMPLE_DIFF)
        assert len(result) == 1


# ---------------------------------------------------------------------------
# Integration: load + dedup + build comments
# ---------------------------------------------------------------------------


class TestIntegration:

    def test_full_pipeline(
        self,
        tmp_findings_dir,
        stage1_findings,
        format_findings,
        stage2_findings,
        stage3_findings,
    ):
        """Load multiple stage files, dedup, build comments."""
        p1 = tmp_findings_dir("stage1.json", stage1_findings)
        p2 = tmp_findings_dir("format.json", format_findings)
        p3 = tmp_findings_dir("stage2.json", stage2_findings)
        p4 = tmp_findings_dir("stage3.json", stage3_findings)

        findings = load_findings([p1, p2, p3, p4])
        assert len(findings) == 6  # 3 + 1 + 1 + 1

        deduped = deduplicate_findings(findings)
        # All have unique file+line combinations
        assert len(deduped) == 6

        comments = build_review_comments(deduped)
        assert len(comments) == 6

        # Verify multi-line comments exist
        multi_line = [c for c in comments if "start_line" in c]
        assert len(multi_line) >= 2  # format + stage3

    def test_cross_stage_same_rule_dedup(self, tmp_findings_dir):
        """Same rule from Stage 1 (warning) and Stage 3 (error) → keep error."""
        s1 = [
            {
                "file": "Source/A.cpp",
                "line": 42,
                "severity": "warning",
                "rule_id": "logtemp",
                "message": "Stage 1 warning",
            }
        ]
        s3 = [
            {
                "file": "Source/A.cpp",
                "line": 42,
                "severity": "error",
                "rule_id": "logtemp",
                "message": "Stage 3 escalated to error",
            }
        ]
        p1 = tmp_findings_dir("s1.json", s1)
        p3 = tmp_findings_dir("s3.json", s3)

        findings = load_findings([p1, p3])
        deduped = deduplicate_findings(findings)
        assert len(deduped) == 1
        assert deduped[0]["severity"] == "error"

    def test_cross_stage_different_rules_kept(self, tmp_findings_dir):
        """Different rules from different stages on same line → both kept."""
        s1 = [
            {
                "file": "Source/A.cpp",
                "line": 42,
                "severity": "warning",
                "rule_id": "logtemp",
                "message": "Stage 1 logtemp",
            }
        ]
        s3 = [
            {
                "file": "Source/A.cpp",
                "line": 42,
                "severity": "error",
                "rule_id": "gc_safety",
                "message": "Stage 3 gc_safety",
            }
        ]
        p1 = tmp_findings_dir("s1.json", s1)
        p3 = tmp_findings_dir("s3.json", s3)

        findings = load_findings([p1, p3])
        deduped = deduplicate_findings(findings)
        assert len(deduped) == 2
        rule_ids = {d["rule_id"] for d in deduped}
        assert rule_ids == {"logtemp", "gc_safety"}

    def test_suggestion_block_in_comment(self, tmp_findings_dir):
        """Findings with suggestion produce correct markdown."""
        findings = [
            {
                "file": "Source/A.cpp",
                "line": 10,
                "end_line": 12,
                "severity": "suggestion",
                "rule_id": "clang_format",
                "message": "Format fix",
                "suggestion": "    int x = 0;\n    int y = 1;",
            }
        ]
        p = tmp_findings_dir("fmt.json", findings)
        loaded = load_findings([p])
        comments = build_review_comments(loaded)
        assert len(comments) == 1
        body = comments[0]["body"]
        assert "```suggestion\n    int x = 0;\n    int y = 1;\n```" in body


# ---------------------------------------------------------------------------
# GitHubClient unit tests
# ---------------------------------------------------------------------------


class TestGitHubClient:

    def test_init_defaults(self):
        client = GitHubClient(token="test-token")
        assert client.api_url == "https://api.github.com"
        assert client.max_retries == 3

    def test_init_ghes(self):
        client = GitHubClient(
            token="test-token",
            api_url="https://github.company.com/api/v3/",
        )
        assert client.api_url == "https://github.company.com/api/v3"

    def test_create_review_payload(self):
        """Verify the create_review method constructs correct API path."""
        client = GitHubClient(token="test-token")

        with patch.object(client, "_request") as mock_req:
            mock_req.return_value = {"id": 1}
            result = client.create_review(
                owner="myorg",
                repo="myrepo",
                pr_number=42,
                commit_sha="abc123",
                body="Summary",
                comments=[{"path": "A.cpp", "line": 1, "body": "Fix"}],
            )

            mock_req.assert_called_once_with(
                "POST",
                "/repos/myorg/myrepo/pulls/42/reviews",
                {
                    "commit_id": "abc123",
                    "body": "Summary",
                    "event": "COMMENT",
                    "comments": [{"path": "A.cpp", "line": 1, "body": "Fix"}],
                },
            )

    def test_get_existing_review_comments_path(self):
        client = GitHubClient(token="test-token")

        with patch.object(client, "_get_all_pages") as mock_pages:
            mock_pages.return_value = []
            client.get_existing_review_comments("org", "repo", 5)
            mock_pages.assert_called_once_with(
                "/repos/org/repo/pulls/5/comments"
            )

    def test_get_all_pages_single_page(self):
        """Single page of results (less than per_page) returns all items."""
        client = GitHubClient(token="test-token")
        items = [{"id": i} for i in range(10)]

        with patch.object(client, "_request") as mock_req:
            mock_req.return_value = items
            result = client._get_all_pages("/test/path", per_page=100)
            assert len(result) == 10
            mock_req.assert_called_once_with(
                "GET", "/test/path?per_page=100&page=1"
            )

    def test_get_all_pages_multiple_pages(self):
        """Multiple pages are fetched until an incomplete page is returned."""
        client = GitHubClient(token="test-token")
        page1 = [{"id": i} for i in range(100)]
        page2 = [{"id": i} for i in range(100, 130)]

        with patch.object(client, "_request") as mock_req:
            mock_req.side_effect = [page1, page2]
            result = client._get_all_pages("/test/path", per_page=100)
            assert len(result) == 130
            assert mock_req.call_count == 2
            mock_req.assert_any_call("GET", "/test/path?per_page=100&page=1")
            mock_req.assert_any_call("GET", "/test/path?per_page=100&page=2")

    def test_get_all_pages_empty_first_page(self):
        """Empty first page returns empty list."""
        client = GitHubClient(token="test-token")

        with patch.object(client, "_request") as mock_req:
            mock_req.return_value = []
            result = client._get_all_pages("/test/path")
            assert result == []
            mock_req.assert_called_once()

    def test_get_all_pages_exact_page_boundary(self):
        """When a page has exactly per_page items, fetch one more to confirm end."""
        client = GitHubClient(token="test-token")
        page1 = [{"id": i} for i in range(100)]
        page2: list = []

        with patch.object(client, "_request") as mock_req:
            mock_req.side_effect = [page1, page2]
            result = client._get_all_pages("/test/path", per_page=100)
            assert len(result) == 100
            assert mock_req.call_count == 2

    def test_get_pull_request_path(self):
        client = GitHubClient(token="test-token")

        with patch.object(client, "_request") as mock_req:
            mock_req.return_value = {"head": {"sha": "abc"}}
            result = client.get_pull_request("org", "repo", 10)
            mock_req.assert_called_once_with(
                "GET", "/repos/org/repo/pulls/10"
            )
            assert result["head"]["sha"] == "abc"


# ---------------------------------------------------------------------------
# CLI dry-run test
# ---------------------------------------------------------------------------


class TestCLIDryRun:

    def test_dry_run_output(self, tmp_path, stage1_findings):
        """Dry-run mode should write payload to output file."""
        findings_path = tmp_path / "findings.json"
        findings_path.write_text(
            json.dumps(stage1_findings, ensure_ascii=False), encoding="utf-8"
        )
        output_path = tmp_path / "result.json"

        from scripts.post_review import main

        with patch(
            "sys.argv",
            [
                "post_review",
                "--findings",
                str(findings_path),
                "--dry-run",
                "--output",
                str(output_path),
            ],
        ):
            with pytest.raises(SystemExit) as exc_info:
                main()
            assert exc_info.value.code == 0

        assert output_path.exists()
        result = json.loads(output_path.read_text(encoding="utf-8"))
        assert result["total_findings"] == 3
        assert result["total_comments"] == 3
        assert len(result["comments"]) == 3

    def test_dry_run_with_multiple_files(
        self, tmp_path, stage1_findings, format_findings
    ):
        s1 = tmp_path / "s1.json"
        s1.write_text(json.dumps(stage1_findings), encoding="utf-8")
        fmt = tmp_path / "fmt.json"
        fmt.write_text(json.dumps(format_findings), encoding="utf-8")
        output_path = tmp_path / "result.json"

        from scripts.post_review import main

        with patch(
            "sys.argv",
            [
                "post_review",
                "--findings",
                str(s1),
                str(fmt),
                "--dry-run",
                "--stages",
                "stage1",
                "--output",
                str(output_path),
            ],
        ):
            with pytest.raises(SystemExit) as exc_info:
                main()
            assert exc_info.value.code == 0

        result = json.loads(output_path.read_text(encoding="utf-8"))
        assert result["total_findings"] == 4

    def test_dry_run_missing_findings_file(self, tmp_path):
        """Missing findings file should not crash in dry-run."""
        output_path = tmp_path / "result.json"

        from scripts.post_review import main

        with patch(
            "sys.argv",
            [
                "post_review",
                "--findings",
                "/nonexistent/file.json",
                "--dry-run",
                "--output",
                str(output_path),
            ],
        ):
            with pytest.raises(SystemExit) as exc_info:
                main()
            assert exc_info.value.code == 0

        result = json.loads(output_path.read_text(encoding="utf-8"))
        assert result["total_findings"] == 0

    def test_dry_run_with_stages_info(self, tmp_path, stage1_findings):
        s1 = tmp_path / "s1.json"
        s1.write_text(json.dumps(stage1_findings), encoding="utf-8")
        output_path = tmp_path / "result.json"

        from scripts.post_review import main

        with patch(
            "sys.argv",
            [
                "post_review",
                "--findings",
                str(s1),
                "--dry-run",
                "--stages",
                "stage1,stage2,stage3",
                "--output",
                str(output_path),
            ],
        ):
            with pytest.raises(SystemExit) as exc_info:
                main()
            assert exc_info.value.code == 0

        result = json.loads(output_path.read_text(encoding="utf-8"))
        assert "stage1" in result["summary"]
        assert "stage2" in result["summary"]
        assert "stage3" in result["summary"]

    def test_dry_run_with_diff_filter(self, tmp_path, stage1_findings):
        """--diff flag should filter findings to diff hunks in dry-run."""
        findings_path = tmp_path / "findings.json"
        findings_path.write_text(
            json.dumps(stage1_findings, ensure_ascii=False), encoding="utf-8"
        )
        # Diff that only covers lines 40-43 of Source/MyActor.cpp
        diff_text = (
            "diff --git a/Source/MyActor.cpp b/Source/MyActor.cpp\n"
            "index aaa..bbb 100644\n"
            "--- a/Source/MyActor.cpp\n"
            "+++ b/Source/MyActor.cpp\n"
            "@@ -40,3 +40,4 @@ void AMyActor::BeginPlay()\n"
            "     Super::BeginPlay();\n"
            "+    UE_LOG(LogTemp, Warning, TEXT(\"test\"));\n"
            "     DoSomething();\n"
        )
        diff_path = tmp_path / "pr.diff"
        diff_path.write_text(diff_text, encoding="utf-8")
        output_path = tmp_path / "result.json"

        from scripts.post_review import main

        with patch(
            "sys.argv",
            [
                "post_review",
                "--findings",
                str(findings_path),
                "--diff",
                str(diff_path),
                "--dry-run",
                "--output",
                str(output_path),
            ],
        ):
            with pytest.raises(SystemExit) as exc_info:
                main()
            assert exc_info.value.code == 0

        result = json.loads(output_path.read_text(encoding="utf-8"))
        # Only line 42 from Source/MyActor.cpp is in the hunk;
        # line 100 and Source/MyPawn.h are outside / not in diff.
        assert result["total_findings"] == 1
        assert result["findings"][0]["rule_id"] == "logtemp"

    def test_dry_run_mixed_int_str_line_values(self, tmp_path):
        """Mixed int/str line values must not raise TypeError during sort."""
        findings = [
            {"file": "Source/B.cpp", "line": 20, "severity": "warning",
             "rule_id": "logtemp", "message": "int line"},
            {"file": "Source/A.cpp", "line": "5", "severity": "error",
             "category": "gc_safety", "message": "string line"},
            {"file": "Source/A.cpp", "line": None, "severity": "info",
             "rule_id": "x", "message": "null line"},
        ]
        fp = tmp_path / "mixed.json"
        fp.write_text(json.dumps(findings), encoding="utf-8")
        output_path = tmp_path / "result.json"

        from scripts.post_review import main

        with patch(
            "sys.argv",
            [
                "post_review",
                "--findings",
                str(fp),
                "--dry-run",
                "--output",
                str(output_path),
            ],
        ):
            with pytest.raises(SystemExit) as exc_info:
                main()
            assert exc_info.value.code == 0

        result = json.loads(output_path.read_text(encoding="utf-8"))
        # null-line finding is skipped by build_review_comments
        assert result["total_comments"] == 2
        # Sorted: A.cpp line 5, then B.cpp line 20
        assert result["comments"][0]["path"] == "Source/A.cpp"
        assert result["comments"][1]["path"] == "Source/B.cpp"


# ---------------------------------------------------------------------------
# CLI validation tests
# ---------------------------------------------------------------------------


class TestCLIValidation:

    def test_no_pr_number_without_dry_run(self, tmp_path, stage1_findings):
        """Should exit with error when --pr-number is missing and not --dry-run."""
        s1 = tmp_path / "s1.json"
        s1.write_text(json.dumps(stage1_findings), encoding="utf-8")

        from scripts.post_review import main

        with patch(
            "sys.argv",
            ["post_review", "--findings", str(s1), "--repo", "org/repo"],
        ):
            with pytest.raises(SystemExit) as exc_info:
                main()
            assert exc_info.value.code == 1

    def test_no_repo_without_dry_run(self, tmp_path, stage1_findings):
        s1 = tmp_path / "s1.json"
        s1.write_text(json.dumps(stage1_findings), encoding="utf-8")

        from scripts.post_review import main

        with patch(
            "sys.argv",
            ["post_review", "--findings", str(s1), "--pr-number", "1"],
        ):
            with pytest.raises(SystemExit) as exc_info:
                main()
            assert exc_info.value.code == 1

    def test_no_token_without_dry_run(self, tmp_path, stage1_findings):
        s1 = tmp_path / "s1.json"
        s1.write_text(json.dumps(stage1_findings), encoding="utf-8")

        from scripts.post_review import main

        env = os.environ.copy()
        env.pop("GIT_ACTION_TOKEN", None)
        env.pop("GITHUB_TOKEN", None)

        with patch.dict(os.environ, env, clear=True):
            with patch(
                "sys.argv",
                [
                    "post_review",
                    "--findings",
                    str(s1),
                    "--pr-number",
                    "1",
                    "--repo",
                    "org/repo",
                ],
            ):
                with pytest.raises(SystemExit) as exc_info:
                    main()
                assert exc_info.value.code == 1

    def test_all_post_failures_exit_nonzero(self, tmp_path, stage1_findings):
        """When every create_review call fails, CLI should exit 1."""
        s1 = tmp_path / "s1.json"
        s1.write_text(json.dumps(stage1_findings), encoding="utf-8")

        from scripts.post_review import main

        mock_client = MagicMock(spec=GitHubClient)
        mock_client.create_review.side_effect = RuntimeError("bad token")
        mock_client.get_existing_review_comments.return_value = []

        with patch.dict(os.environ, {"GITHUB_TOKEN": "fake"}, clear=False):
            with patch("scripts.post_review.GitHubClient", return_value=mock_client):
                with patch(
                    "sys.argv",
                    [
                        "post_review",
                        "--findings",
                        str(s1),
                        "--pr-number",
                        "1",
                        "--repo",
                        "org/repo",
                        "--commit-sha",
                        "abc123",
                    ],
                ):
                    with pytest.raises(SystemExit) as exc_info:
                        main()
                    assert exc_info.value.code == 1

    def test_partial_post_failure_exit_zero(self, tmp_path):
        """When at least one batch succeeds, CLI should exit 0."""
        # Need > 50 findings to trigger 2 batches
        findings = [
            {
                "file": f"Source/File{i}.cpp",
                "line": 10,
                "severity": "warning",
                "rule_id": "logtemp",
                "message": "msg",
            }
            for i in range(MAX_COMMENTS_PER_REVIEW + 5)
        ]
        s1 = tmp_path / "s1.json"
        s1.write_text(json.dumps(findings), encoding="utf-8")

        from scripts.post_review import main

        mock_client = MagicMock(spec=GitHubClient)
        # First batch succeeds, second fails
        mock_client.create_review.side_effect = [
            {"id": 1},
            RuntimeError("rate limit"),
        ]
        mock_client.get_existing_review_comments.return_value = []

        with patch.dict(os.environ, {"GITHUB_TOKEN": "fake"}, clear=False):
            with patch("scripts.post_review.GitHubClient", return_value=mock_client):
                with patch(
                    "sys.argv",
                    [
                        "post_review",
                        "--findings",
                        str(s1),
                        "--pr-number",
                        "1",
                        "--repo",
                        "org/repo",
                        "--commit-sha",
                        "abc123",
                    ],
                ):
                    with pytest.raises(SystemExit) as exc_info:
                        main()
                    # Partial success → exit 0
                    assert exc_info.value.code == 0
