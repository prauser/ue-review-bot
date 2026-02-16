"""Tests for stage1_pattern_checker.py — Stage 1 regex pattern matching.

Test cases from STEP3_STAGE1.md:
  - sample_bad.cpp diff에서 7개 패턴 전부 검출 확인
  - sample_good.cpp diff에서 false positive 0 확인
  - 주석 안의 LogTemp는 (선택적으로) 무시 확인
  - 이관된 항목(auto, yoda 등)이 Stage 1에서 검출되지 않음을 확인
"""

from __future__ import annotations

import os
import sys
import textwrap
from pathlib import Path

import pytest

# Adjust path so we can import from scripts/
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.stage1_pattern_checker import (
    check_diff,
    check_line,
    get_diff_from_git,
    load_tier1_patterns,
    _generate_suggestion,
    _split_code_comment,
    _strip_comments,
)
from scripts.utils.diff_parser import FileDiff, parse_diff

# --- Fixtures ---

FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"
CHECKLIST_PATH = Path(__file__).resolve().parent.parent / "configs" / "checklist.yml"


@pytest.fixture
def patterns():
    """Load Tier 1 patterns from checklist.yml."""
    return load_tier1_patterns(str(CHECKLIST_PATH))


@pytest.fixture
def sample_bad_diff():
    """Create a diff as if sample_bad.cpp were entirely added."""
    bad_cpp = (FIXTURES_DIR / "sample_bad.cpp").read_text(encoding="utf-8")
    lines = bad_cpp.splitlines()
    diff_lines = [
        "diff --git a/Source/sample_bad.cpp b/Source/sample_bad.cpp",
        "new file mode 100644",
        "index 0000000..1234567",
        "--- /dev/null",
        "+++ b/Source/sample_bad.cpp",
        f"@@ -0,0 +1,{len(lines)} @@",
    ]
    for line in lines:
        diff_lines.append(f"+{line}")
    return "\n".join(diff_lines)


@pytest.fixture
def sample_good_diff():
    """Create a diff as if sample_good.cpp were entirely added."""
    good_cpp = (FIXTURES_DIR / "sample_good.cpp").read_text(encoding="utf-8")
    lines = good_cpp.splitlines()
    diff_lines = [
        "diff --git a/Source/sample_good.cpp b/Source/sample_good.cpp",
        "new file mode 100644",
        "index 0000000..2345678",
        "--- /dev/null",
        "+++ b/Source/sample_good.cpp",
        f"@@ -0,0 +1,{len(lines)} @@",
    ]
    for line in lines:
        diff_lines.append(f"+{line}")
    return "\n".join(diff_lines)


# ============================================================================
# diff_parser tests
# ============================================================================


class TestDiffParser:
    """Tests for the unified diff parser."""

    def test_parse_simple_new_file(self):
        diff = textwrap.dedent("""\
            diff --git a/Source/Foo.cpp b/Source/Foo.cpp
            new file mode 100644
            --- /dev/null
            +++ b/Source/Foo.cpp
            @@ -0,0 +1,3 @@
            +line one
            +line two
            +line three
        """)
        result = parse_diff(diff)
        assert "Source/Foo.cpp" in result
        fd = result["Source/Foo.cpp"]
        assert fd.added_lines == {1: "line one", 2: "line two", 3: "line three"}

    def test_parse_modification(self):
        diff = textwrap.dedent("""\
            diff --git a/Source/Foo.cpp b/Source/Foo.cpp
            index abc1234..def5678 100644
            --- a/Source/Foo.cpp
            +++ b/Source/Foo.cpp
            @@ -10,4 +10,5 @@
             context line
            -old line
            +new line
            +another new line
             more context
        """)
        result = parse_diff(diff)
        fd = result["Source/Foo.cpp"]
        # Line 11 is the new line (replacing old), line 12 is the added line
        assert 11 in fd.added_lines
        assert fd.added_lines[11] == "new line"
        assert 12 in fd.added_lines
        assert fd.added_lines[12] == "another new line"
        # Context lines should not be in added_lines
        assert 10 not in fd.added_lines
        assert 13 not in fd.added_lines

    def test_parse_multiple_files(self):
        diff = textwrap.dedent("""\
            diff --git a/Source/A.cpp b/Source/A.cpp
            new file mode 100644
            --- /dev/null
            +++ b/Source/A.cpp
            @@ -0,0 +1,1 @@
            +file A
            diff --git a/Source/B.h b/Source/B.h
            new file mode 100644
            --- /dev/null
            +++ b/Source/B.h
            @@ -0,0 +1,2 @@
            +file B line 1
            +file B line 2
        """)
        result = parse_diff(diff)
        assert len(result) == 2
        assert "Source/A.cpp" in result
        assert "Source/B.h" in result

    def test_parse_multiple_hunks(self):
        diff = textwrap.dedent("""\
            diff --git a/Source/Foo.cpp b/Source/Foo.cpp
            --- a/Source/Foo.cpp
            +++ b/Source/Foo.cpp
            @@ -1,3 +1,4 @@
             line1
            +inserted at line 2
             line2
             line3
            @@ -10,3 +11,4 @@
             line10
            +inserted at line 12
             line11
             line12
        """)
        result = parse_diff(diff)
        fd = result["Source/Foo.cpp"]
        assert 2 in fd.added_lines
        assert fd.added_lines[2] == "inserted at line 2"
        assert 12 in fd.added_lines
        assert fd.added_lines[12] == "inserted at line 12"
        assert len(fd.hunks) == 2

    def test_hunk_content_preserved(self):
        diff = textwrap.dedent("""\
            diff --git a/Source/Foo.cpp b/Source/Foo.cpp
            --- a/Source/Foo.cpp
            +++ b/Source/Foo.cpp
            @@ -5,3 +5,4 @@
             context
            +added
             more context
        """)
        result = parse_diff(diff)
        hunk = result["Source/Foo.cpp"].hunks[0]
        assert hunk["start"] == 5
        assert "added" in hunk["content"]

    def test_deleted_file_excluded(self):
        diff = textwrap.dedent("""\
            diff --git a/Source/Deleted.cpp b/Source/Deleted.cpp
            deleted file mode 100644
            --- a/Source/Deleted.cpp
            +++ /dev/null
            @@ -1,3 +0,0 @@
            -line1
            -line2
            -line3
        """)
        result = parse_diff(diff)
        # +++ /dev/null doesn't match +++ b/... so no file should be parsed
        assert len(result) == 0

    def test_deleted_file_after_normal_does_not_corrupt(self):
        """Deletion after a normal file must not attach hunks to the prior file."""
        diff = textwrap.dedent("""\
            diff --git a/Source/Good.cpp b/Source/Good.cpp
            new file mode 100644
            --- /dev/null
            +++ b/Source/Good.cpp
            @@ -0,0 +1,2 @@
            +line1
            +line2
            diff --git a/Source/Removed.cpp b/Source/Removed.cpp
            deleted file mode 100644
            --- a/Source/Removed.cpp
            +++ /dev/null
            @@ -1,3 +0,0 @@
            -old1
            -old2
            -old3
        """)
        result = parse_diff(diff)
        # Only Good.cpp should be in the result
        assert "Source/Good.cpp" in result
        assert "Source/Removed.cpp" not in result
        # Good.cpp must have exactly 2 added lines — no contamination
        good = result["Source/Good.cpp"]
        assert len(good.added_lines) == 2
        assert good.added_lines[1] == "line1"
        assert good.added_lines[2] == "line2"

    def test_sample_diff_patch(self):
        """Parse the existing sample_diff.patch fixture."""
        patch = (FIXTURES_DIR / "sample_diff.patch").read_text(encoding="utf-8")
        result = parse_diff(patch)
        # Should include C++ source files from the patch
        assert "Source/MyGame/Actors/MyActor.cpp" in result
        assert "Source/MyGame/Actors/MyActor.h" in result
        # Should include the auto-generated header (parser doesn't filter)
        assert "Intermediate/Build/Win64/MyGame.generated.h" in result
        # Verify line content
        actor_cpp = result["Source/MyGame/Actors/MyActor.cpp"]
        assert 1 in actor_cpp.added_lines
        assert '#include "MyActor.h"' in actor_cpp.added_lines[1]


# ============================================================================
# Pattern loading tests
# ============================================================================


class TestPatternLoading:
    """Tests for loading Tier 1 patterns from checklist.yml."""

    def test_load_tier1_patterns(self, patterns):
        """Should load exactly 7 Tier 1 patterns."""
        assert len(patterns) == 7

    def test_pattern_ids(self, patterns):
        """All expected pattern IDs should be present."""
        ids = {p["id"] for p in patterns}
        expected = {
            "logtemp",
            "pragma_optimize_off",
            "hard_asset_path",
            "macro_no_semicolon",
            "declaration_macro_semicolon",
            "check_side_effect_suspicious",
            "sync_load_runtime",
        }
        assert ids == expected

    def test_patterns_have_required_fields(self, patterns):
        """Each pattern should have all required fields."""
        for pat in patterns:
            assert "id" in pat
            assert "compiled" in pat
            assert "severity" in pat
            assert "summary" in pat

    def test_file_not_found(self):
        with pytest.raises(FileNotFoundError):
            load_tier1_patterns("/nonexistent/path.yml")


# ============================================================================
# Individual pattern tests
# ============================================================================


class TestLogTemp:
    """Tests for logtemp pattern."""

    def test_detect_logtemp(self, patterns):
        findings = check_line('\tUE_LOG(LogTemp, Warning, TEXT("msg"));', patterns)
        rule_ids = [f["rule_id"] for f in findings]
        assert "logtemp" in rule_ids

    def test_no_match_custom_category(self, patterns):
        findings = check_line('\tUE_LOG(LogMyActor, Warning, TEXT("msg"));', patterns)
        rule_ids = [f["rule_id"] for f in findings]
        assert "logtemp" not in rule_ids

    def test_no_match_in_comment(self, patterns):
        findings = check_line("\t// UE_LOG(LogTemp, Warning, TEXT(\"msg\"));", patterns)
        rule_ids = [f["rule_id"] for f in findings]
        assert "logtemp" not in rule_ids

    def test_detect_in_comment_when_disabled(self, patterns):
        findings = check_line(
            '\t// UE_LOG(LogTemp, Warning, TEXT("msg"));',
            patterns,
            skip_comments=False,
        )
        rule_ids = [f["rule_id"] for f in findings]
        assert "logtemp" in rule_ids


class TestPragmaOptimizeOff:
    """Tests for pragma_optimize_off pattern."""

    def test_detect_pragma_optimize_off(self, patterns):
        findings = check_line('#pragma optimize("", off)', patterns)
        rule_ids = [f["rule_id"] for f in findings]
        assert "pragma_optimize_off" in rule_ids

    def test_detect_pragma_with_spaces(self, patterns):
        findings = check_line('#  pragma   optimize ( "" , off )', patterns)
        rule_ids = [f["rule_id"] for f in findings]
        assert "pragma_optimize_off" in rule_ids

    def test_no_match_pragma_on(self, patterns):
        findings = check_line('#pragma optimize("", on)', patterns)
        rule_ids = [f["rule_id"] for f in findings]
        assert "pragma_optimize_off" not in rule_ids

    def test_severity_is_error(self, patterns):
        findings = check_line('#pragma optimize("", off)', patterns)
        for f in findings:
            if f["rule_id"] == "pragma_optimize_off":
                assert f["severity"] == "error"


class TestHardAssetPath:
    """Tests for hard_asset_path pattern."""

    def test_detect_game_path(self, patterns):
        findings = check_line(
            '\tFString Path = TEXT("/Game/Path/To/MyBlueprint");', patterns
        )
        rule_ids = [f["rule_id"] for f in findings]
        assert "hard_asset_path" in rule_ids

    def test_detect_engine_path(self, patterns):
        findings = check_line(
            '\tFString Path = TEXT("/Engine/BasicShapes/Cube");', patterns
        )
        rule_ids = [f["rule_id"] for f in findings]
        assert "hard_asset_path" in rule_ids

    def test_no_match_relative_path(self, patterns):
        findings = check_line(
            '\tFString Path = TEXT("Relative/Path/To/Asset");', patterns
        )
        rule_ids = [f["rule_id"] for f in findings]
        assert "hard_asset_path" not in rule_ids

    def test_no_match_regular_string(self, patterns):
        findings = check_line('\tFString Msg = TEXT("Hello World");', patterns)
        rule_ids = [f["rule_id"] for f in findings]
        assert "hard_asset_path" not in rule_ids


class TestMacroNoSemicolon:
    """Tests for macro_no_semicolon pattern."""

    def test_detect_ue_log_no_semicolon(self, patterns):
        findings = check_line(
            '\tUE_LOG(LogTemp, Log, TEXT("Missing semicolon"))', patterns
        )
        rule_ids = [f["rule_id"] for f in findings]
        assert "macro_no_semicolon" in rule_ids

    def test_detect_check_no_semicolon(self, patterns):
        findings = check_line("\tcheck(IsValid(this))", patterns)
        rule_ids = [f["rule_id"] for f in findings]
        assert "macro_no_semicolon" in rule_ids

    def test_detect_ensure_no_semicolon(self, patterns):
        findings = check_line("\tensure(SomeCondition)", patterns)
        rule_ids = [f["rule_id"] for f in findings]
        assert "macro_no_semicolon" in rule_ids

    def test_no_match_with_semicolon(self, patterns):
        findings = check_line(
            '\tUE_LOG(LogMyActor, Log, TEXT("Proper semicolon"));', patterns
        )
        rule_ids = [f["rule_id"] for f in findings]
        assert "macro_no_semicolon" not in rule_ids

    def test_no_match_check_with_semicolon(self, patterns):
        findings = check_line("\tcheck(IsValid(this));", patterns)
        rule_ids = [f["rule_id"] for f in findings]
        assert "macro_no_semicolon" not in rule_ids

    def test_no_match_with_spaces_before_semicolon(self, patterns):
        """Ensure no false positive when semicolon follows whitespace."""
        findings = check_line("\tcheck(IsValid(this))  ;", patterns)
        rule_ids = [f["rule_id"] for f in findings]
        assert "macro_no_semicolon" not in rule_ids

    def test_auto_fixable(self, patterns):
        findings = check_line(
            "\tcheck(IsValid(this))", patterns
        )
        for f in findings:
            if f["rule_id"] == "macro_no_semicolon":
                assert f["suggestion"] is not None
                assert f["suggestion"].endswith(";")


class TestDeclarationMacroSemicolon:
    """Tests for declaration_macro_semicolon pattern."""

    def test_detect_generated_body_semicolon(self, patterns):
        findings = check_line("\tGENERATED_BODY();", patterns)
        rule_ids = [f["rule_id"] for f in findings]
        assert "declaration_macro_semicolon" in rule_ids

    def test_detect_uproperty_semicolon(self, patterns):
        findings = check_line(
            '\tUPROPERTY(EditAnywhere, meta=(AllowPrivateAccess="true"));',
            patterns,
        )
        rule_ids = [f["rule_id"] for f in findings]
        assert "declaration_macro_semicolon" in rule_ids

    def test_no_match_without_semicolon(self, patterns):
        findings = check_line("\tGENERATED_BODY()", patterns)
        rule_ids = [f["rule_id"] for f in findings]
        assert "declaration_macro_semicolon" not in rule_ids

    def test_no_match_uproperty_no_semicolon(self, patterns):
        findings = check_line(
            '\tUPROPERTY(EditAnywhere, meta=(AllowPrivateAccess="true"))',
            patterns,
        )
        rule_ids = [f["rule_id"] for f in findings]
        assert "declaration_macro_semicolon" not in rule_ids

    def test_auto_fixable_removes_semicolon(self, patterns):
        findings = check_line("\tGENERATED_BODY();", patterns)
        for f in findings:
            if f["rule_id"] == "declaration_macro_semicolon":
                assert f["suggestion"] is not None
                assert not f["suggestion"].endswith(";")
                assert f["suggestion"].endswith(")")


class TestCheckSideEffectSuspicious:
    """Tests for check_side_effect_suspicious pattern."""

    def test_detect_increment(self, patterns):
        findings = check_line("\tcheck(++Index < 10)", patterns)
        rule_ids = [f["rule_id"] for f in findings]
        assert "check_side_effect_suspicious" in rule_ids

    def test_detect_function_call(self, patterns):
        findings = check_line("\tcheck(ProcessItem(SomeItem))", patterns)
        rule_ids = [f["rule_id"] for f in findings]
        assert "check_side_effect_suspicious" in rule_ids

    def test_no_match_verify(self, patterns):
        """verify() is excluded — side effects are OK in verify()."""
        findings = check_line("\tverify(ProcessItem(SomeItem));", patterns)
        rule_ids = [f["rule_id"] for f in findings]
        assert "check_side_effect_suspicious" not in rule_ids

    def test_no_match_simple_comparison(self, patterns):
        findings = check_line("\tcheck(Index >= 0);", patterns)
        rule_ids = [f["rule_id"] for f in findings]
        assert "check_side_effect_suspicious" not in rule_ids

    def test_detect_assignment(self, patterns):
        findings = check_line("\tcheck(Value = GetResult())", patterns)
        rule_ids = [f["rule_id"] for f in findings]
        assert "check_side_effect_suspicious" in rule_ids


class TestSyncLoadRuntime:
    """Tests for sync_load_runtime pattern."""

    def test_detect_load_object(self, patterns):
        findings = check_line(
            "\tUObject* Obj = LoadObject<UStaticMesh>(nullptr, Path);", patterns
        )
        rule_ids = [f["rule_id"] for f in findings]
        assert "sync_load_runtime" in rule_ids

    def test_detect_static_load_object(self, patterns):
        findings = check_line(
            "\tStaticLoadObject(UStaticMesh::StaticClass(), nullptr, Path);",
            patterns,
        )
        rule_ids = [f["rule_id"] for f in findings]
        assert "sync_load_runtime" in rule_ids

    def test_detect_load_synchronous(self, patterns):
        findings = check_line(
            "\tUObject* Obj = LoadSynchronous(SoftRef);", patterns
        )
        rule_ids = [f["rule_id"] for f in findings]
        assert "sync_load_runtime" in rule_ids

    def test_no_match_async_load(self, patterns):
        findings = check_line(
            "\tManager.RequestAsyncLoad(SoftObjectPath, Delegate);", patterns
        )
        rule_ids = [f["rule_id"] for f in findings]
        assert "sync_load_runtime" not in rule_ids

    def test_no_match_load_in_comment(self, patterns):
        findings = check_line(
            "\t// LoadObject<UStaticMesh>(nullptr, Path);", patterns
        )
        rule_ids = [f["rule_id"] for f in findings]
        assert "sync_load_runtime" not in rule_ids


# ============================================================================
# Integration tests — sample_bad.cpp
# ============================================================================


class TestSampleBadCpp:
    """Integration: check sample_bad.cpp diff for expected detections."""

    def test_all_tier1_detected(self, patterns, sample_bad_diff):
        diff_data = parse_diff(sample_bad_diff)
        findings = check_diff(diff_data, patterns)

        found_rules = {f["rule_id"] for f in findings}

        # These Tier 1 patterns should be detected in sample_bad.cpp
        expected_rules = {
            "logtemp",
            "pragma_optimize_off",
            "hard_asset_path",
            "macro_no_semicolon",
            "declaration_macro_semicolon",
            "check_side_effect_suspicious",
            "sync_load_runtime",
        }
        assert expected_rules.issubset(found_rules), (
            f"Missing detections: {expected_rules - found_rules}"
        )

    def test_logtemp_line_numbers(self, patterns, sample_bad_diff):
        diff_data = parse_diff(sample_bad_diff)
        findings = check_diff(diff_data, patterns)

        logtemp_findings = [f for f in findings if f["rule_id"] == "logtemp"]
        assert len(logtemp_findings) >= 1
        # LogTemp appears on line 16 of sample_bad.cpp
        logtemp_lines = {f["line"] for f in logtemp_findings}
        assert 16 in logtemp_lines

    def test_pragma_optimize_off_detected(self, patterns, sample_bad_diff):
        diff_data = parse_diff(sample_bad_diff)
        findings = check_diff(diff_data, patterns)

        pragma_findings = [
            f for f in findings if f["rule_id"] == "pragma_optimize_off"
        ]
        assert len(pragma_findings) >= 1
        # #pragma optimize("", off) is on line 20
        pragma_lines = {f["line"] for f in pragma_findings}
        assert 20 in pragma_lines

    def test_hard_asset_path_detected(self, patterns, sample_bad_diff):
        diff_data = parse_diff(sample_bad_diff)
        findings = check_diff(diff_data, patterns)

        path_findings = [
            f for f in findings if f["rule_id"] == "hard_asset_path"
        ]
        assert len(path_findings) >= 1

    def test_sync_load_detected(self, patterns, sample_bad_diff):
        diff_data = parse_diff(sample_bad_diff)
        findings = check_diff(diff_data, patterns)

        sync_findings = [
            f for f in findings if f["rule_id"] == "sync_load_runtime"
        ]
        assert len(sync_findings) >= 1

    def test_all_findings_have_required_fields(self, patterns, sample_bad_diff):
        diff_data = parse_diff(sample_bad_diff)
        findings = check_diff(diff_data, patterns)

        for f in findings:
            assert "file" in f
            assert f["file"] == "Source/sample_bad.cpp"
            assert "line" in f
            assert "rule_id" in f
            assert "severity" in f
            assert "message" in f
            assert "suggestion" in f  # can be None


# ============================================================================
# Integration tests — sample_good.cpp (false positive = 0)
# ============================================================================


class TestSampleGoodCpp:
    """Integration: check sample_good.cpp diff for zero false positives."""

    def test_no_false_positives(self, patterns, sample_good_diff):
        diff_data = parse_diff(sample_good_diff)
        findings = check_diff(diff_data, patterns)

        # Stage 1 patterns should NOT fire on sample_good.cpp
        # Filter out: logtemp might match the DEFINE_LOG_CATEGORY line comment
        # but sample_good.cpp uses LogMyActor, not LogTemp
        tier1_rules = {
            "logtemp",
            "pragma_optimize_off",
            "hard_asset_path",
            "macro_no_semicolon",
            "declaration_macro_semicolon",
            "check_side_effect_suspicious",
            "sync_load_runtime",
        }
        false_positives = [
            f for f in findings if f["rule_id"] in tier1_rules
        ]
        assert len(false_positives) == 0, (
            f"False positives in sample_good.cpp: "
            f"{[(f['rule_id'], f['line']) for f in false_positives]}"
        )


# ============================================================================
# Stage 3 migration check — ensure migrated items are NOT detected
# ============================================================================


class TestMigratedItemsNotDetected:
    """Ensure Stage 3 (migrated) items are NOT detected by Stage 1."""

    def test_auto_not_detected(self, patterns, sample_bad_diff):
        diff_data = parse_diff(sample_bad_diff)
        findings = check_diff(diff_data, patterns)
        rule_ids = {f["rule_id"] for f in findings}
        assert "auto_non_lambda" not in rule_ids

    def test_yoda_not_detected(self, patterns, sample_bad_diff):
        diff_data = parse_diff(sample_bad_diff)
        findings = check_diff(diff_data, patterns)
        rule_ids = {f["rule_id"] for f in findings}
        assert "yoda_condition" not in rule_ids

    def test_not_operator_not_detected(self, patterns, sample_bad_diff):
        diff_data = parse_diff(sample_bad_diff)
        findings = check_diff(diff_data, patterns)
        rule_ids = {f["rule_id"] for f in findings}
        assert "not_operator_in_if" not in rule_ids

    def test_fsimpledelegate_not_detected(self, patterns, sample_bad_diff):
        diff_data = parse_diff(sample_bad_diff)
        findings = check_diff(diff_data, patterns)
        rule_ids = {f["rule_id"] for f in findings}
        assert "fsimpledelegate" not in rule_ids

    def test_loctext_not_detected(self, patterns, sample_bad_diff):
        diff_data = parse_diff(sample_bad_diff)
        findings = check_diff(diff_data, patterns)
        rule_ids = {f["rule_id"] for f in findings}
        assert "loctext_no_undef" not in rule_ids

    def test_constructorhelpers_not_detected(self, patterns, sample_bad_diff):
        diff_data = parse_diff(sample_bad_diff)
        findings = check_diff(diff_data, patterns)
        rule_ids = {f["rule_id"] for f in findings}
        assert "constructorhelpers_outside_ctor" not in rule_ids


# ============================================================================
# Comment handling tests
# ============================================================================


class TestCommentHandling:
    """Tests for comment stripping logic."""

    def test_strip_full_line_comment(self):
        assert _strip_comments("  // This is a comment") == ""

    def test_strip_preserves_url_in_macro(self):
        """// inside macro parentheses (e.g., URL) must NOT be stripped."""
        line = '\tUE_LOG(LogTemp, Log, TEXT("http://example.com"))'
        result = _strip_comments(line)
        assert "http://example.com" in result

    def test_strip_url_in_macro_then_real_comment(self):
        """Real comment after a macro with URL should still be stripped."""
        line = '\tUE_LOG(LogTemp, Log, TEXT("http://x.com")) // note'
        result = _strip_comments(line)
        assert "http://x.com" in result
        assert "// note" not in result

    def test_strip_inline_comment(self):
        result = _strip_comments("int x = 5; // inline comment")
        assert "int x = 5;" in result
        assert "inline comment" not in result

    def test_no_comment(self):
        assert _strip_comments("int x = 5;") == "int x = 5;"

    def test_empty_line(self):
        assert _strip_comments("") == ""

    def test_strip_preserves_url_in_bare_string(self):
        """// inside a bare string literal (no macro parens) must NOT be stripped."""
        line = 'FString Url = "http://example.com";'
        result = _strip_comments(line)
        assert "http://example.com" in result

    def test_strip_bare_string_url_then_real_comment(self):
        """Real comment after a bare string with // should be stripped."""
        line = 'FString Url = "http://x.com"; // note'
        result = _strip_comments(line)
        assert "http://x.com" in result
        assert "// note" not in result


# ============================================================================
# Suggestion generation tests
# ============================================================================


class TestSplitCodeComment:
    """Tests for _split_code_comment helper."""

    def test_no_comment(self):
        code, comment = _split_code_comment("\tcheck(x)")
        assert code == "\tcheck(x)"
        assert comment == ""

    def test_inline_comment(self):
        code, comment = _split_code_comment("\tcheck(x) // reason")
        assert code == "\tcheck(x) "
        assert comment == "// reason"

    def test_url_inside_macro(self):
        """// inside parentheses (e.g., URL in TEXT) should not split."""
        line = '\tUE_LOG(LogTemp, Log, TEXT("http://example.com"))'
        code, comment = _split_code_comment(line)
        assert code == line
        assert comment == ""

    def test_url_inside_then_comment(self):
        line = '\tUE_LOG(LogTemp, Log, TEXT("http://x.com")) // note'
        code, comment = _split_code_comment(line)
        assert comment == "// note"
        assert "http://x.com" in code


    def test_url_in_bare_string_literal(self):
        """// inside a bare string literal (no parentheses) should not split."""
        line = 'FString Url = "http://example.com";'
        code, comment = _split_code_comment(line)
        assert code == line
        assert comment == ""

    def test_bare_string_url_then_comment(self):
        """Real comment after a bare string with // should still split."""
        line = 'FString Url = "http://x.com"; // note'
        code, comment = _split_code_comment(line)
        assert comment == "// note"
        assert "http://x.com" in code

    def test_char_literal_with_quote(self):
        """char literal containing double-quote should not confuse parser."""
        line = "char c = '\"'; // comment"
        code, comment = _split_code_comment(line)
        assert comment == "// comment"

    def test_escaped_quote_in_string(self):
        """Escaped quote inside string literal should not end the string."""
        line = r'FString s = "say \"hello\""; // comment'
        code, comment = _split_code_comment(line)
        assert comment == "// comment"
        assert r"\"hello\"" in code


class TestSuggestionGeneration:
    """Tests for auto-fix suggestion generation."""

    def test_macro_no_semicolon_suggestion(self):
        suggestion = _generate_suggestion(
            "macro_no_semicolon", "\tcheck(IsValid(this))"
        )
        assert suggestion == "\tcheck(IsValid(this));"

    def test_macro_no_semicolon_with_inline_comment(self):
        """Semicolon should be inserted BEFORE the inline comment."""
        suggestion = _generate_suggestion(
            "macro_no_semicolon", "\tcheck(IsValid(this)) // reason"
        )
        assert suggestion == "\tcheck(IsValid(this)); // reason"

    def test_macro_no_semicolon_with_url_in_text(self):
        """// inside TEXT() macro should not be treated as a comment."""
        suggestion = _generate_suggestion(
            "macro_no_semicolon",
            '\tUE_LOG(LogTemp, Log, TEXT("http://example.com"))',
        )
        assert suggestion.endswith(';')
        assert "http://example.com" in suggestion

    def test_declaration_macro_semicolon_suggestion(self):
        suggestion = _generate_suggestion(
            "declaration_macro_semicolon", "\tGENERATED_BODY();"
        )
        assert suggestion == "\tGENERATED_BODY()"

    def test_declaration_macro_semicolon_with_comment(self):
        """Semicolon removed, comment preserved."""
        suggestion = _generate_suggestion(
            "declaration_macro_semicolon", "\tGENERATED_BODY(); // required"
        )
        assert suggestion == "\tGENERATED_BODY() // required"

    def test_no_suggestion_for_other_rules(self):
        suggestion = _generate_suggestion("logtemp", "UE_LOG(LogTemp, ...)")
        assert suggestion is None


# ============================================================================
# Edge case tests
# ============================================================================


class TestEdgeCases:
    """Tests for edge cases and boundary conditions."""

    def test_url_in_macro_still_detected(self, patterns):
        """LogTemp in a macro with a URL-like string must still be detected."""
        line = '\tUE_LOG(LogTemp, Log, TEXT("http://example.com"))'
        findings = check_line(line, patterns)
        rule_ids = {f["rule_id"] for f in findings}
        assert "logtemp" in rule_ids
        assert "macro_no_semicolon" in rule_ids

    def test_empty_diff(self, patterns):
        diff_data = parse_diff("")
        findings = check_diff(diff_data, patterns)
        assert findings == []

    def test_diff_with_no_cpp_content(self, patterns):
        diff = textwrap.dedent("""\
            diff --git a/README.md b/README.md
            new file mode 100644
            --- /dev/null
            +++ b/README.md
            @@ -0,0 +1,1 @@
            +# Hello
        """)
        diff_data = parse_diff(diff)
        findings = check_diff(diff_data, patterns)
        assert findings == []

    def test_multiple_violations_on_same_line(self, patterns):
        """A line with LogTemp AND missing semicolon should trigger both."""
        line = '\tUE_LOG(LogTemp, Log, TEXT("test"))'
        findings = check_line(line, patterns)
        rule_ids = {f["rule_id"] for f in findings}
        assert "logtemp" in rule_ids
        assert "macro_no_semicolon" in rule_ids

    def test_nested_parens_in_macro(self, patterns):
        """Macro with nested parentheses should still be checked."""
        line = '\tUE_LOG(LogTemp, Log, TEXT("Value: %d"), GetValue())'
        findings = check_line(line, patterns)
        rule_ids = {f["rule_id"] for f in findings}
        # Should detect logtemp and possibly macro_no_semicolon
        assert "logtemp" in rule_ids

    def test_pragma_in_string_literal(self, patterns):
        """#pragma in a string literal should NOT match."""
        line = '\tFString S = TEXT("#pragma optimize off");'
        findings = check_line(line, patterns)
        rule_ids = [f["rule_id"] for f in findings]
        # #pragma pattern looks for lines starting with #, so string content
        # should not match (the # is inside TEXT("..."))
        assert "pragma_optimize_off" not in rule_ids

    def test_non_cpp_file_with_matching_content_skipped(self, patterns):
        """Non-C++ files with pattern-matching strings must be skipped.

        Regression: check_diff previously had no extension filter, so
        Markdown docs containing 'LogTemp' or 'check(...)' would trigger
        false positives.
        """
        diff = textwrap.dedent("""\
            diff --git a/docs/review.md b/docs/review.md
            new file mode 100644
            --- /dev/null
            +++ b/docs/review.md
            @@ -0,0 +1,3 @@
            +# Review Notes
            +Do not use UE_LOG(LogTemp, ...) in production.
            +Use check(IsValid(ptr)) to assert invariants.
        """)
        diff_data = parse_diff(diff)
        findings = check_diff(diff_data, patterns)
        assert findings == [], (
            f"Non-C++ file should produce no findings, got: {findings}"
        )

    def test_cpp_file_with_matching_content_detected(self, patterns):
        """C++ files with pattern-matching strings must still be detected."""
        diff = textwrap.dedent("""\
            diff --git a/Source/MyActor.cpp b/Source/MyActor.cpp
            new file mode 100644
            --- /dev/null
            +++ b/Source/MyActor.cpp
            @@ -0,0 +1,1 @@
            +\tUE_LOG(LogTemp, Warning, TEXT("test"))
        """)
        diff_data = parse_diff(diff)
        findings = check_diff(diff_data, patterns)
        rule_ids = {f["rule_id"] for f in findings}
        assert "logtemp" in rule_ids

    def test_yaml_file_excluded(self, patterns):
        """YAML files with UE macro patterns must not produce findings."""
        diff = textwrap.dedent("""\
            diff --git a/configs/checklist.yml b/configs/checklist.yml
            new file mode 100644
            --- /dev/null
            +++ b/configs/checklist.yml
            @@ -0,0 +1,2 @@
            +# pattern: UE_LOG(LogTemp
            +# example: LoadObject<UStaticMesh>(nullptr, TEXT("/Game/Mesh"))
        """)
        diff_data = parse_diff(diff)
        findings = check_diff(diff_data, patterns)
        assert findings == []


class TestGetDiffFromGit:
    """Tests for get_diff_from_git."""

    def test_empty_files_returns_empty_string(self):
        """Passing an empty file list should return empty diff immediately."""
        result = get_diff_from_git([], "origin/main")
        assert result == ""

    def test_uses_merge_base_semantics(self, monkeypatch):
        """git diff should use three-dot (merge-base) syntax.

        Regression: plain ``git diff base_ref -- <files>`` includes
        upstream-only changes when the PR branch is behind base_ref.
        ``base_ref...HEAD`` scopes analysis to actual PR changes.
        """
        import subprocess

        captured_cmd = []
        original_run = subprocess.run

        def mock_run(cmd, **kwargs):
            captured_cmd.extend(cmd)
            # Return a successful but empty result
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

        monkeypatch.setattr(subprocess, "run", mock_run)
        get_diff_from_git(["Source/A.cpp"], "origin/main")
        # Should use three-dot merge-base syntax
        assert "origin/main...HEAD" in captured_cmd
        assert "origin/main" not in captured_cmd  # no bare ref
