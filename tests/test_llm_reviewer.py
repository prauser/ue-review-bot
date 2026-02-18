"""Tests for stage3_llm_reviewer.py — LLM-based semantic code reviewer.

Test cases from STEP6_STAGE3.md:
  - mock 응답으로 JSON 파싱 정상 동작
  - 토큰 예산 초과 시 파일 skip
  - API 에러 시 graceful degradation
  - exclude-findings 중복 제거
  - 빈 diff → API 호출 안 함
"""

from __future__ import annotations

import json
import os
import sys
import textwrap
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.stage3_llm_reviewer import (
    build_system_prompt,
    build_user_message,
    filter_excluded,
    load_exclude_findings,
    parse_llm_response,
    review_file,
    review_pr,
    validate_finding,
    _reconstruct_file_diff,
    DEFAULT_MODEL,
)
from scripts.utils.token_budget import (
    BUDGET_PER_FILE,
    BUDGET_PER_PR,
    BudgetTracker,
    chunk_diff,
    estimate_cost,
    estimate_tokens,
    should_skip_file,
)


# ---------------------------------------------------------------------------
# Sample data
# ---------------------------------------------------------------------------

SAMPLE_DIFF = textwrap.dedent("""\
    diff --git a/Source/MyActor.cpp b/Source/MyActor.cpp
    --- a/Source/MyActor.cpp
    +++ b/Source/MyActor.cpp
    @@ -10,6 +10,8 @@ void AMyActor::BeginPlay()
     {
         Super::BeginPlay();
    +    auto x = GetSomething();
    +    if (!bFlag) DoThing();
     }
""")

SAMPLE_DIFF_MULTI = textwrap.dedent("""\
    diff --git a/Source/MyActor.cpp b/Source/MyActor.cpp
    --- a/Source/MyActor.cpp
    +++ b/Source/MyActor.cpp
    @@ -10,6 +10,8 @@ void AMyActor::BeginPlay()
     {
         Super::BeginPlay();
    +    auto x = GetSomething();
     }
    diff --git a/Source/MyWidget.h b/Source/MyWidget.h
    --- a/Source/MyWidget.h
    +++ b/Source/MyWidget.h
    @@ -5,3 +5,5 @@ class AMyWidget
     {
    +    UObject* RawPtr;
     };
    diff --git a/README.md b/README.md
    --- a/README.md
    +++ b/README.md
    @@ -1,2 +1,3 @@
     # Project
    +Some text
""")

SAMPLE_LLM_RESPONSE = json.dumps([
    {
        "file": "Source/MyActor.cpp",
        "line": 12,
        "end_line": 12,
        "severity": "warning",
        "category": "convention",
        "message": "auto 사용 금지: 람다 변수가 아닌 경우 명시적 타입을 사용하세요.",
        "suggestion": "FMyType x = GetSomething();",
    },
    {
        "file": "Source/MyActor.cpp",
        "line": 13,
        "severity": "info",
        "category": "convention",
        "message": "조건문에 ! 연산자 사용: if (bFlag == false) 형태를 권장합니다.",
        "suggestion": None,
    },
], ensure_ascii=False)

SAMPLE_LLM_RESPONSE_EMPTY = "[]"

SAMPLE_LLM_RESPONSE_WRAPPED = textwrap.dedent("""\
    Here is my review:

    ```json
    [
      {
        "file": "Source/MyActor.cpp",
        "line": 12,
        "severity": "warning",
        "category": "convention",
        "message": "auto 사용 금지"
      }
    ]
    ```

    That's all the issues I found.
""")


# ---------------------------------------------------------------------------
# Tests: token_budget.py
# ---------------------------------------------------------------------------


class TestEstimateTokens:
    """Tests for estimate_tokens."""

    def test_empty_string(self):
        assert estimate_tokens("") == 0

    def test_short_string(self):
        result = estimate_tokens("hello")
        assert result == 1  # 5 // 3 = 1

    def test_code_string(self):
        code = "void AMyActor::BeginPlay() { Super::BeginPlay(); }"
        result = estimate_tokens(code)
        assert result > 0
        assert result == len(code) // 3

    def test_large_string(self):
        text = "a" * 30000
        assert estimate_tokens(text) == 10000


class TestEstimateCost:
    """Tests for estimate_cost."""

    def test_basic_cost(self):
        cost = estimate_cost(1_000_000)
        # input: 1M * $3/1M = $3, output: 1000 * $15/1M = $0.015
        assert cost > 0

    def test_zero_tokens(self):
        cost = estimate_cost(0, 0)
        assert cost == 0.0


class TestShouldSkipFile:
    """Tests for should_skip_file."""

    def test_normal_file(self):
        assert not should_skip_file("Source/MyActor.cpp")

    def test_thirdparty(self):
        assert should_skip_file("ThirdParty/lib/foo.cpp")

    def test_thirdparty_nested(self):
        assert should_skip_file("Source/ThirdParty/lib.h")

    def test_generated_file(self):
        assert should_skip_file("Source/MyActor.generated.h")

    def test_gen_file(self):
        assert should_skip_file("Source/MyActor.gen.cpp")

    def test_protobuf(self):
        assert should_skip_file("Source/proto/message.pb.h")
        assert should_skip_file("Source/proto/message.pb.cc")

    def test_intermediate(self):
        assert should_skip_file("Intermediate/Build/foo.cpp")


class TestChunkDiff:
    """Tests for chunk_diff."""

    def test_small_diff_no_split(self):
        small_diff = "--- a/f.cpp\n+++ b/f.cpp\n@@ -1,3 +1,4 @@\n line\n+added"
        chunks = chunk_diff(small_diff)
        assert len(chunks) == 1
        assert chunks[0] == small_diff

    def test_large_diff_splits(self):
        # Create a diff large enough to exceed budget
        hunks = []
        for i in range(100):
            hunks.append(f"@@ -{i*10},{10} +{i*10},{11} @@\n")
            hunks.append("+" + "x" * 500 + "\n")
        large_diff = "--- a/f.cpp\n+++ b/f.cpp\n" + "".join(hunks)
        chunks = chunk_diff(large_diff, max_tokens=500)
        assert len(chunks) > 1

    def test_custom_max_tokens(self):
        diff = "@@ -1,3 +1,4 @@\n line\n+added line here"
        chunks = chunk_diff(diff, max_tokens=5)
        assert len(chunks) >= 1


class TestBudgetTracker:
    """Tests for BudgetTracker."""

    def test_initial_state(self):
        bt = BudgetTracker()
        assert bt.total_input_tokens == 0
        assert bt.total_cost == 0.0
        assert bt.files_reviewed == 0

    def test_can_review_within_budget(self):
        bt = BudgetTracker(max_tokens=10000, max_cost=10.0)
        assert bt.can_review_file(5000)

    def test_cannot_review_over_token_budget(self):
        bt = BudgetTracker(max_tokens=10000, max_cost=10.0)
        bt.record_usage(9000, 500)
        assert not bt.can_review_file(2000)

    def test_cannot_review_over_cost_budget(self):
        bt = BudgetTracker(max_tokens=1_000_000, max_cost=0.001)
        assert not bt.can_review_file(500_000)

    def test_record_usage(self):
        bt = BudgetTracker()
        bt.record_usage(1000, 200)
        assert bt.total_input_tokens == 1000
        assert bt.total_output_tokens == 200
        assert bt.files_reviewed == 1

    def test_record_skip(self):
        bt = BudgetTracker()
        bt.record_skip()
        assert bt.files_skipped_budget == 1

    def test_summary(self):
        bt = BudgetTracker(max_tokens=100000, max_cost=2.0)
        bt.record_usage(5000, 500)
        bt.record_skip()
        summary = bt.summary()
        assert summary["total_input_tokens"] == 5000
        assert summary["total_output_tokens"] == 500
        assert summary["files_reviewed"] == 1
        assert summary["files_skipped_budget"] == 1
        assert summary["budget_remaining_tokens"] == 95000


# ---------------------------------------------------------------------------
# Tests: system prompt
# ---------------------------------------------------------------------------


class TestBuildSystemPrompt:
    """Tests for build_system_prompt."""

    def test_with_compile_commands(self):
        prompt = build_system_prompt(has_compile_commands=True)
        assert "UE5 C++ 시니어 코드 리뷰어" in prompt
        assert "auto 사용 금지" in prompt
        assert "GC 안전성" in prompt
        # clang-tidy fallback should NOT be included
        assert "clang-tidy 대체 검사" not in prompt

    def test_without_compile_commands(self):
        prompt = build_system_prompt(has_compile_commands=False)
        assert "UE5 C++ 시니어 코드 리뷰어" in prompt
        # clang-tidy fallback SHOULD be included
        assert "clang-tidy 대체 검사" in prompt
        assert "override 키워드 누락" in prompt

    def test_migrated_items_always_present(self):
        for has_cc in [True, False]:
            prompt = build_system_prompt(has_compile_commands=has_cc)
            assert "요다 컨디션 금지" in prompt
            assert "Sandwich inequality" in prompt
            assert "FSimpleDelegateGraphTask" in prompt
            assert "LOCTEXT_NAMESPACE" in prompt
            assert "ConstructorHelpers" in prompt

    def test_output_format_present(self):
        prompt = build_system_prompt(has_compile_commands=True)
        assert "JSON 배열만 반환" in prompt
        assert '"severity"' in prompt
        assert '"category"' in prompt


# ---------------------------------------------------------------------------
# Tests: user message
# ---------------------------------------------------------------------------


class TestBuildUserMessage:
    """Tests for build_user_message."""

    def test_basic_message(self):
        msg = build_user_message("Source/MyActor.cpp", "+ auto x = 1;")
        assert "Source/MyActor.cpp" in msg
        assert "auto x = 1" in msg
        assert "```diff" in msg

    def test_with_full_source(self):
        msg = build_user_message(
            "Source/MyActor.cpp",
            "+ auto x = 1;",
            full_source="void Foo() {}",
        )
        assert "전체 소스" in msg
        assert "void Foo() {}" in msg
        assert "```cpp" in msg

    def test_without_full_source(self):
        msg = build_user_message("Source/MyActor.cpp", "+ auto x = 1;")
        assert "전체 소스" not in msg


# ---------------------------------------------------------------------------
# Tests: parse_llm_response
# ---------------------------------------------------------------------------


class TestParseLlmResponse:
    """Tests for parse_llm_response."""

    def test_valid_json_array(self):
        findings = parse_llm_response(SAMPLE_LLM_RESPONSE)
        assert len(findings) == 2
        assert findings[0]["file"] == "Source/MyActor.cpp"
        assert findings[0]["line"] == 12

    def test_empty_array(self):
        findings = parse_llm_response(SAMPLE_LLM_RESPONSE_EMPTY)
        assert findings == []

    def test_wrapped_in_markdown(self):
        findings = parse_llm_response(SAMPLE_LLM_RESPONSE_WRAPPED)
        assert len(findings) == 1
        assert findings[0]["category"] == "convention"

    def test_with_code_fence_json(self):
        response = '```json\n[{"file": "a.cpp", "line": 1}]\n```'
        findings = parse_llm_response(response)
        assert len(findings) == 1

    def test_with_surrounding_text(self):
        response = 'Here are findings:\n[{"file": "a.cpp", "line": 1}]\nDone.'
        findings = parse_llm_response(response)
        assert len(findings) == 1

    def test_invalid_json(self):
        findings = parse_llm_response("this is not json at all")
        assert findings == []

    def test_json_object_not_array(self):
        findings = parse_llm_response('{"file": "a.cpp"}')
        assert findings == []

    def test_empty_response(self):
        findings = parse_llm_response("")
        assert findings == []

    def test_malformed_json(self):
        findings = parse_llm_response("[{broken json}]")
        assert findings == []


# ---------------------------------------------------------------------------
# Tests: validate_finding
# ---------------------------------------------------------------------------


class TestValidateFinding:
    """Tests for validate_finding."""

    def test_normal_finding(self):
        raw = {
            "file": "Source/MyActor.cpp",
            "line": 42,
            "end_line": 45,
            "severity": "warning",
            "category": "convention",
            "message": "auto 사용 금지",
            "suggestion": "int x = 1;",
        }
        result = validate_finding(raw, "Source/MyActor.cpp")
        assert result["file"] == "Source/MyActor.cpp"
        assert result["line"] == 42
        assert result["end_line"] == 45
        assert result["severity"] == "warning"
        assert result["category"] == "convention"
        assert result["rule_id"] == "convention"
        assert result["stage"] == "stage3"
        assert result["suggestion"] == "int x = 1;"

    def test_missing_file_uses_fallback(self):
        raw = {"line": 10, "severity": "warning", "message": "test"}
        result = validate_finding(raw, "Source/Fallback.cpp")
        assert result["file"] == "Source/Fallback.cpp"

    def test_missing_line_defaults_zero(self):
        raw = {"file": "a.cpp", "severity": "warning", "message": "test"}
        result = validate_finding(raw, "a.cpp")
        assert result["line"] == 0

    def test_invalid_severity_defaults_warning(self):
        raw = {"file": "a.cpp", "line": 1, "severity": "critical", "message": "x"}
        result = validate_finding(raw, "a.cpp")
        assert result["severity"] == "warning"

    def test_valid_severities(self):
        for sev in ["error", "warning", "info", "suggestion"]:
            raw = {"file": "a.cpp", "line": 1, "severity": sev, "message": "x"}
            result = validate_finding(raw, "a.cpp")
            assert result["severity"] == sev

    def test_no_suggestion_field(self):
        raw = {"file": "a.cpp", "line": 1, "severity": "warning", "message": "x"}
        result = validate_finding(raw, "a.cpp")
        assert "suggestion" not in result

    def test_null_suggestion_not_included(self):
        raw = {
            "file": "a.cpp",
            "line": 1,
            "severity": "warning",
            "message": "x",
            "suggestion": None,
        }
        result = validate_finding(raw, "a.cpp")
        assert "suggestion" not in result

    def test_line_coercion_from_string(self):
        raw = {"file": "a.cpp", "line": "42", "severity": "warning", "message": "x"}
        result = validate_finding(raw, "a.cpp")
        assert result["line"] == 42

    def test_end_line_invalid_ignored(self):
        raw = {
            "file": "a.cpp",
            "line": 1,
            "end_line": "invalid",
            "severity": "warning",
            "message": "x",
        }
        result = validate_finding(raw, "a.cpp")
        assert "end_line" not in result


# ---------------------------------------------------------------------------
# Tests: load_exclude_findings / filter_excluded
# ---------------------------------------------------------------------------


class TestExcludeFindings:
    """Tests for exclude findings loading and filtering."""

    def test_load_from_json_file(self, tmp_path):
        findings = [
            {"file": "Source/A.cpp", "line": 10, "rule_id": "logtemp"},
            {"file": "Source/A.cpp", "line": 20, "rule_id": "format"},
        ]
        f = tmp_path / "stage1.json"
        f.write_text(json.dumps(findings))

        excluded = load_exclude_findings([str(f)])
        assert ("Source/A.cpp", 10) in excluded
        assert ("Source/A.cpp", 20) in excluded

    def test_load_missing_file(self):
        excluded = load_exclude_findings(["/nonexistent/file.json"])
        assert len(excluded) == 0

    def test_load_multiple_files(self, tmp_path):
        f1 = tmp_path / "s1.json"
        f1.write_text(json.dumps([{"file": "a.cpp", "line": 1}]))
        f2 = tmp_path / "s2.json"
        f2.write_text(json.dumps([{"file": "b.cpp", "line": 2}]))

        excluded = load_exclude_findings([str(f1), str(f2)])
        assert ("a.cpp", 1) in excluded
        assert ("b.cpp", 2) in excluded

    def test_filter_excluded(self):
        findings = [
            {"file": "a.cpp", "line": 10, "message": "keep"},
            {"file": "a.cpp", "line": 20, "message": "exclude"},
            {"file": "b.cpp", "line": 5, "message": "keep too"},
        ]
        excluded = {("a.cpp", 20)}
        result = filter_excluded(findings, excluded)
        assert len(result) == 2
        assert result[0]["message"] == "keep"
        assert result[1]["message"] == "keep too"

    def test_filter_empty_excluded(self):
        findings = [{"file": "a.cpp", "line": 10, "message": "keep"}]
        result = filter_excluded(findings, set())
        assert len(result) == 1

    def test_filter_all_excluded(self):
        findings = [{"file": "a.cpp", "line": 10}]
        excluded = {("a.cpp", 10)}
        result = filter_excluded(findings, excluded)
        assert len(result) == 0


# ---------------------------------------------------------------------------
# Tests: review_file with mocked API
# ---------------------------------------------------------------------------


class TestReviewFile:
    """Tests for review_file with mocked API calls."""

    @patch("scripts.stage3_llm_reviewer.call_anthropic_api")
    def test_basic_review(self, mock_api):
        mock_api.return_value = (SAMPLE_LLM_RESPONSE, 500, 200)

        budget = BudgetTracker()
        findings = review_file(
            "Source/MyActor.cpp",
            SAMPLE_DIFF,
            build_system_prompt(True),
            set(),
            budget,
        )

        assert len(findings) == 2
        assert findings[0]["stage"] == "stage3"
        assert budget.files_reviewed == 1
        mock_api.assert_called_once()

    @patch("scripts.stage3_llm_reviewer.call_anthropic_api")
    def test_api_error_graceful(self, mock_api):
        mock_api.side_effect = RuntimeError("API error 500")

        budget = BudgetTracker()
        findings = review_file(
            "Source/MyActor.cpp",
            SAMPLE_DIFF,
            build_system_prompt(True),
            set(),
            budget,
        )

        assert findings == []

    @patch("scripts.stage3_llm_reviewer.call_anthropic_api")
    def test_empty_response(self, mock_api):
        mock_api.return_value = ("[]", 300, 10)

        budget = BudgetTracker()
        findings = review_file(
            "Source/MyActor.cpp",
            SAMPLE_DIFF,
            build_system_prompt(True),
            set(),
            budget,
        )

        assert findings == []
        assert budget.files_reviewed == 1

    @patch("scripts.stage3_llm_reviewer.call_anthropic_api")
    def test_excludes_stage1_findings(self, mock_api):
        # LLM returns findings on lines 12 and 13
        mock_api.return_value = (SAMPLE_LLM_RESPONSE, 500, 200)

        excluded = {("Source/MyActor.cpp", 12)}
        budget = BudgetTracker()
        findings = review_file(
            "Source/MyActor.cpp",
            SAMPLE_DIFF,
            build_system_prompt(True),
            excluded,
            budget,
        )

        # Line 12 should be excluded, only line 13 remains
        assert len(findings) == 1
        assert findings[0]["line"] == 13

    def test_budget_exhausted_skips_file(self):
        budget = BudgetTracker(max_tokens=100, max_cost=0.001)
        # Exhaust budget
        budget.record_usage(99, 50)

        findings = review_file(
            "Source/MyActor.cpp",
            SAMPLE_DIFF,
            build_system_prompt(True),
            set(),
            budget,
        )

        assert findings == []
        assert budget.files_skipped_budget == 1

    @patch("scripts.stage3_llm_reviewer.call_anthropic_api")
    def test_json_parse_failure(self, mock_api):
        mock_api.return_value = ("This is not valid JSON response", 500, 200)

        budget = BudgetTracker()
        findings = review_file(
            "Source/MyActor.cpp",
            SAMPLE_DIFF,
            build_system_prompt(True),
            set(),
            budget,
        )

        assert findings == []

    @patch("scripts.stage3_llm_reviewer.call_anthropic_api")
    def test_with_full_source(self, mock_api):
        mock_api.return_value = ("[]", 1000, 50)

        budget = BudgetTracker()
        findings = review_file(
            "Source/MyActor.cpp",
            SAMPLE_DIFF,
            build_system_prompt(True),
            set(),
            budget,
            full_source="void Foo() {}",
        )

        # Verify user message included full source
        call_args = mock_api.call_args
        user_msg = call_args[0][1]  # second positional arg
        assert "전체 소스" in user_msg
        assert "void Foo() {}" in user_msg


# ---------------------------------------------------------------------------
# Tests: review_pr with mocked API
# ---------------------------------------------------------------------------


class TestReviewPr:
    """Tests for review_pr with mocked API calls."""

    @patch("scripts.stage3_llm_reviewer.call_anthropic_api")
    def test_basic_pr_review(self, mock_api):
        mock_api.return_value = (SAMPLE_LLM_RESPONSE, 500, 200)

        findings, summary = review_pr(SAMPLE_DIFF)

        assert len(findings) == 2
        assert summary["files_reviewed"] == 1

    @patch("scripts.stage3_llm_reviewer.call_anthropic_api")
    def test_multi_file_pr(self, mock_api):
        mock_api.return_value = ("[]", 300, 50)

        findings, summary = review_pr(SAMPLE_DIFF_MULTI)

        # Should review MyActor.cpp and MyWidget.h, skip README.md
        assert summary["files_reviewed"] == 2
        # README.md is not a C++ file, should not be reviewed
        assert mock_api.call_count == 2

    @patch("scripts.stage3_llm_reviewer.call_anthropic_api")
    def test_skips_non_cpp_files(self, mock_api):
        diff = textwrap.dedent("""\
            diff --git a/README.md b/README.md
            --- a/README.md
            +++ b/README.md
            @@ -1,2 +1,3 @@
             # Readme
            +New content
        """)
        mock_api.return_value = ("[]", 100, 10)

        findings, summary = review_pr(diff)

        assert summary["files_reviewed"] == 0
        mock_api.assert_not_called()

    @patch("scripts.stage3_llm_reviewer.call_anthropic_api")
    def test_skips_thirdparty_files(self, mock_api):
        diff = textwrap.dedent("""\
            diff --git a/ThirdParty/lib/foo.cpp b/ThirdParty/lib/foo.cpp
            --- a/ThirdParty/lib/foo.cpp
            +++ b/ThirdParty/lib/foo.cpp
            @@ -1,2 +1,3 @@
             void Foo() {}
            +void Bar() {}
        """)
        mock_api.return_value = ("[]", 100, 10)

        findings, summary = review_pr(diff)

        assert summary["files_reviewed"] == 0
        mock_api.assert_not_called()

    @patch("scripts.stage3_llm_reviewer.call_anthropic_api")
    def test_exclude_findings_dedup(self, mock_api, tmp_path):
        response = json.dumps([
            {"file": "Source/MyActor.cpp", "line": 12, "severity": "warning",
             "category": "conv", "message": "dup"},
            {"file": "Source/MyActor.cpp", "line": 99, "severity": "warning",
             "category": "conv", "message": "unique"},
        ])
        mock_api.return_value = (response, 500, 200)

        # Create exclude file with line 12
        exclude_file = tmp_path / "stage1.json"
        exclude_file.write_text(json.dumps([
            {"file": "Source/MyActor.cpp", "line": 12, "rule_id": "logtemp"},
        ]))

        findings, summary = review_pr(
            SAMPLE_DIFF,
            exclude_files=[str(exclude_file)],
        )

        # Line 12 should be excluded
        assert len(findings) == 1
        assert findings[0]["line"] == 99

    @patch("scripts.stage3_llm_reviewer.call_anthropic_api")
    def test_empty_diff_no_api_call(self, mock_api):
        findings, summary = review_pr("")

        mock_api.assert_not_called()
        assert findings == []
        assert summary["files_reviewed"] == 0

    @patch("scripts.stage3_llm_reviewer.call_anthropic_api")
    def test_has_compile_commands_true(self, mock_api):
        mock_api.return_value = ("[]", 300, 50)

        findings, summary = review_pr(
            SAMPLE_DIFF,
            has_compile_commands=True,
        )

        call_args = mock_api.call_args
        system_prompt = call_args[0][0]
        assert "clang-tidy 대체 검사" not in system_prompt

    @patch("scripts.stage3_llm_reviewer.call_anthropic_api")
    def test_has_compile_commands_false(self, mock_api):
        mock_api.return_value = ("[]", 300, 50)

        findings, summary = review_pr(
            SAMPLE_DIFF,
            has_compile_commands=False,
        )

        call_args = mock_api.call_args
        system_prompt = call_args[0][0]
        assert "clang-tidy 대체 검사" in system_prompt

    @patch("scripts.stage3_llm_reviewer.call_anthropic_api")
    def test_api_error_continues_pipeline(self, mock_api):
        """API error on one file should not stop the entire PR review."""
        # First file errors, second succeeds
        mock_api.side_effect = [
            RuntimeError("API error"),
            ("[]", 300, 50),
        ]

        findings, summary = review_pr(SAMPLE_DIFF_MULTI)

        # One file errored but pipeline continues
        assert summary["files_reviewed"] == 1  # Only the second succeeded

    @patch("scripts.stage3_llm_reviewer.call_anthropic_api")
    def test_source_dir_provides_context(self, mock_api, tmp_path):
        mock_api.return_value = ("[]", 1000, 50)

        # Create a source file
        source = tmp_path / "Source" / "MyActor.cpp"
        source.parent.mkdir(parents=True)
        source.write_text("void AMyActor::BeginPlay() { Super::BeginPlay(); }")

        findings, summary = review_pr(
            SAMPLE_DIFF,
            source_dir=str(tmp_path),
        )

        call_args = mock_api.call_args
        user_msg = call_args[0][1]
        assert "전체 소스" in user_msg


# ---------------------------------------------------------------------------
# Tests: CLI (main function)
# ---------------------------------------------------------------------------


class TestCLI:
    """Tests for the CLI main function."""

    def test_dry_run_mode(self, tmp_path, capsys):
        from scripts.stage3_llm_reviewer import main

        diff_file = tmp_path / "test.diff"
        diff_file.write_text(SAMPLE_DIFF)

        result = main([
            "--diff", str(diff_file),
            "--dry-run",
        ])

        assert result == 0
        captured = capsys.readouterr()
        assert "System Prompt" in captured.out
        assert "UE5 C++ 시니어 코드 리뷰어" in captured.out

    def test_dry_run_with_compile_commands(self, tmp_path, capsys):
        from scripts.stage3_llm_reviewer import main

        diff_file = tmp_path / "test.diff"
        diff_file.write_text(SAMPLE_DIFF)

        result = main([
            "--diff", str(diff_file),
            "--has-compile-commands", "true",
            "--dry-run",
        ])

        assert result == 0
        captured = capsys.readouterr()
        assert "clang-tidy 대체 검사" not in captured.out

    def test_dry_run_without_compile_commands(self, tmp_path, capsys):
        from scripts.stage3_llm_reviewer import main

        diff_file = tmp_path / "test.diff"
        diff_file.write_text(SAMPLE_DIFF)

        result = main([
            "--diff", str(diff_file),
            "--has-compile-commands", "false",
            "--dry-run",
        ])

        assert result == 0
        captured = capsys.readouterr()
        assert "clang-tidy 대체 검사" in captured.out

    def test_missing_diff_file(self, capsys):
        from scripts.stage3_llm_reviewer import main

        result = main([
            "--diff", "/nonexistent/diff.patch",
        ])

        assert result == 1

    @patch("scripts.stage3_llm_reviewer.call_anthropic_api")
    def test_full_run_with_output(self, mock_api, tmp_path):
        from scripts.stage3_llm_reviewer import main

        mock_api.return_value = (SAMPLE_LLM_RESPONSE, 500, 200)

        diff_file = tmp_path / "test.diff"
        diff_file.write_text(SAMPLE_DIFF)
        output_file = tmp_path / "findings-stage3.json"

        # Set env var for API key
        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-key"}):
            result = main([
                "--diff", str(diff_file),
                "--output", str(output_file),
            ])

        assert result == 0
        assert output_file.exists()

        findings = json.loads(output_file.read_text())
        assert isinstance(findings, list)
        assert len(findings) == 2

        # Budget file should also exist
        budget_file = output_file.with_suffix(".budget.json")
        assert budget_file.exists()

    @patch("scripts.stage3_llm_reviewer.call_anthropic_api")
    def test_with_exclude_findings(self, mock_api, tmp_path):
        from scripts.stage3_llm_reviewer import main

        mock_api.return_value = (SAMPLE_LLM_RESPONSE, 500, 200)

        diff_file = tmp_path / "test.diff"
        diff_file.write_text(SAMPLE_DIFF)

        exclude_file = tmp_path / "stage1.json"
        exclude_file.write_text(json.dumps([
            {"file": "Source/MyActor.cpp", "line": 12, "rule_id": "auto"},
        ]))

        output_file = tmp_path / "findings-stage3.json"

        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-key"}):
            result = main([
                "--diff", str(diff_file),
                "--exclude-findings", str(exclude_file),
                "--output", str(output_file),
            ])

        assert result == 0
        findings = json.loads(output_file.read_text())
        # Line 12 excluded, only line 13 remains
        assert len(findings) == 1
        assert findings[0]["line"] == 13


# ---------------------------------------------------------------------------
# Tests: _reconstruct_file_diff
# ---------------------------------------------------------------------------


class TestReconstructFileDiff:
    """Tests for _reconstruct_file_diff."""

    def test_basic_reconstruction_includes_header(self):
        from scripts.utils.diff_parser import FileDiff

        fd = FileDiff(
            path="Source/MyActor.cpp",
            added_lines={12: "auto x = 1;"},
            hunks=[{"start": 10, "end": 14, "content": " line\n+auto x = 1;"}],
        )
        result = _reconstruct_file_diff(fd)
        # Should now contain a reconstructed @@ header
        assert "@@ -10," in result
        assert "+10," in result
        assert "+auto x = 1;" in result

    def test_empty_hunks(self):
        from scripts.utils.diff_parser import FileDiff

        fd = FileDiff(path="Source/Empty.cpp")
        result = _reconstruct_file_diff(fd)
        assert result.strip() == ""

    def test_multiple_hunks_each_get_header(self):
        from scripts.utils.diff_parser import FileDiff

        fd = FileDiff(
            path="Source/A.cpp",
            hunks=[
                {"start": 5, "end": 10, "content": " ctx\n+added1"},
                {"start": 50, "end": 55, "content": " ctx\n+added2"},
            ],
        )
        result = _reconstruct_file_diff(fd)
        assert "@@ -5," in result
        assert "@@ -50," in result


# ---------------------------------------------------------------------------
# Tests: generated file skipping
# ---------------------------------------------------------------------------


class TestGeneratedFileSkipping:
    """Tests that generated/intermediate files are skipped."""

    @patch("scripts.stage3_llm_reviewer.call_anthropic_api")
    def test_generated_h_skipped(self, mock_api):
        diff = textwrap.dedent("""\
            diff --git a/Source/MyActor.generated.h b/Source/MyActor.generated.h
            --- a/Source/MyActor.generated.h
            +++ b/Source/MyActor.generated.h
            @@ -1,2 +1,3 @@
             // generated
            +int x;
        """)
        findings, summary = review_pr(diff)
        mock_api.assert_not_called()

    @patch("scripts.stage3_llm_reviewer.call_anthropic_api")
    def test_protobuf_skipped(self, mock_api):
        diff = textwrap.dedent("""\
            diff --git a/Source/msg.pb.h b/Source/msg.pb.h
            --- a/Source/msg.pb.h
            +++ b/Source/msg.pb.h
            @@ -1,2 +1,3 @@
             // protobuf
            +int x;
        """)
        findings, summary = review_pr(diff)
        mock_api.assert_not_called()


# ---------------------------------------------------------------------------
# Tests: non-dict element filtering (review comment fix #1)
# ---------------------------------------------------------------------------


class TestNonDictElementFiltering:
    """LLM may return non-dict items in the array; they must not crash."""

    @patch("scripts.stage3_llm_reviewer.call_anthropic_api")
    def test_mixed_array_skips_non_dict(self, mock_api):
        """Non-dict elements like strings in the array should be silently skipped."""
        response = json.dumps([
            "this is a stray string",
            {"file": "Source/A.cpp", "line": 10, "severity": "warning",
             "category": "conv", "message": "valid finding"},
            42,
            None,
        ])
        mock_api.return_value = (response, 500, 200)

        budget = BudgetTracker()
        findings = review_file(
            "Source/A.cpp",
            SAMPLE_DIFF,
            build_system_prompt(True),
            set(),
            budget,
        )

        # Only the dict element should survive
        assert len(findings) == 1
        assert findings[0]["file"] == "Source/A.cpp"
        assert findings[0]["line"] == 10

    @patch("scripts.stage3_llm_reviewer.call_anthropic_api")
    def test_all_non_dict_returns_empty(self, mock_api):
        """Array of only non-dict elements should return empty list."""
        response = json.dumps(["text", 123, True, None])
        mock_api.return_value = (response, 500, 200)

        budget = BudgetTracker()
        findings = review_file(
            "Source/A.cpp",
            SAMPLE_DIFF,
            build_system_prompt(True),
            set(),
            budget,
        )

        assert findings == []


# ---------------------------------------------------------------------------
# Tests: per-file budget enforcement in chunking (review comment fix #2)
# ---------------------------------------------------------------------------


class TestChunkingPerFileBudget:
    """Chunks with large full_source must respect BUDGET_PER_FILE."""

    @patch("scripts.stage3_llm_reviewer.call_anthropic_api")
    def test_large_full_source_dropped_when_exceeds_per_file(self, mock_api):
        """When full_source makes chunk exceed BUDGET_PER_FILE, source is dropped."""
        mock_api.return_value = ("[]", 500, 50)

        # Create a very large full_source that would blow the per-file budget
        huge_source = "int x;\n" * 30000  # ~210K chars → ~70K tokens

        # Use a moderately-sized diff that triggers chunking
        # (total_input with huge_source > BUDGET_PER_FILE)
        budget = BudgetTracker(max_tokens=500_000, max_cost=10.0)
        findings = review_file(
            "Source/Big.cpp",
            SAMPLE_DIFF,
            build_system_prompt(True),
            set(),
            budget,
            full_source=huge_source,
        )

        # Should have called API (with source dropped or diff chunked)
        assert mock_api.called
        # The user message should NOT contain the huge source
        # (it was dropped because it exceeded per-file budget)
        call_args = mock_api.call_args
        user_msg = call_args[0][1]
        assert "int x;" not in user_msg or len(user_msg) < len(huge_source)


# ---------------------------------------------------------------------------
# Tests: JSON extraction hardening (review comment fix #3)
# ---------------------------------------------------------------------------


class TestJsonExtractionHardening:
    """parse_llm_response must handle non-JSON brackets before the real array."""

    def test_bracket_in_preamble_text(self):
        """[주의] style text before JSON should not break extraction."""
        response = '[주의] 다음 이슈입니다:\n\n[{"file": "a.cpp", "line": 1, "severity": "warning", "category": "c", "message": "m"}]'
        findings = parse_llm_response(response)
        assert len(findings) == 1
        assert findings[0]["file"] == "a.cpp"

    def test_multiple_non_json_brackets(self):
        """Multiple non-JSON brackets should be skipped."""
        response = 'See [docs] and [API] reference.\n[{"file": "b.cpp", "line": 5}]'
        findings = parse_llm_response(response)
        assert len(findings) == 1
        assert findings[0]["file"] == "b.cpp"

    def test_code_fence_takes_priority(self):
        """Code fence content is parsed first even if brackets exist outside."""
        response = '[참고]\n```json\n[{"file": "c.cpp", "line": 3}]\n```\n[끝]'
        findings = parse_llm_response(response)
        assert len(findings) == 1
        assert findings[0]["file"] == "c.cpp"

    def test_no_brackets_at_all(self):
        findings = parse_llm_response("No issues found in this code.")
        assert findings == []

    def test_only_non_json_brackets(self):
        findings = parse_llm_response("[주의] 이것은 JSON이 아닙니다 [참고]")
        assert findings == []


# ---------------------------------------------------------------------------
# Tests: wrapper overhead in chunk budget (review comment fix #1 regression)
# ---------------------------------------------------------------------------


class TestWrapperOverheadAccounting:
    """chunk_diff budget should account for build_user_message wrapper."""

    @patch("scripts.stage3_llm_reviewer.call_anthropic_api")
    def test_chunks_not_skipped_due_to_wrapper(self, mock_api):
        """Chunks should fit within BUDGET_PER_FILE after wrapping."""
        mock_api.return_value = ("[]", 500, 50)

        # Build a diff whose raw size is near BUDGET_PER_FILE but
        # would exceed it once wrapped without overhead accounting.
        budget = BudgetTracker(max_tokens=500_000, max_cost=10.0)

        # Make a diff with many hunks that needs chunking
        hunks = []
        for i in range(50):
            hunks.append(f"@@ -{i*10+1},5 +{i*10+1},6 @@\n")
            hunks.append("+" + "x" * 200 + "\n")
        diff = "--- a/f.cpp\n+++ b/f.cpp\n" + "".join(hunks)

        findings = review_file(
            "Source/F.cpp",
            diff,
            build_system_prompt(True),
            set(),
            budget,
        )

        # At least one chunk should have been sent to the API
        assert mock_api.called
