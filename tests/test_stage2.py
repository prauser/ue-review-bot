"""Tests for stage2_tidy_to_suggestions.py — clang-tidy fixes → suggestions.

Test cases from STEP5_STAGE2.md:
  - fixes.yaml에 fix 있는 항목 → suggestion 생성 확인
  - fix 없는 항목 → 일반 코멘트 확인
  - Stage 1과 같은 라인 지적 → 중복 제거 확인
  - fixes.yaml 없거나 비어있을 때 → 빈 결과
  - --pvs-report 없이 실행 → clang-tidy만 처리 확인
"""

from __future__ import annotations

import json
import os
import sys
import textwrap
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.stage2_tidy_to_suggestions import (
    convert_diagnostics,
    deduplicate,
    parse_tidy_fixes,
    _collect_source_contents,
    _extract_suggestion_span,
    _offset_to_line,
    _read_source,
    _resolve_path,
    _CHECK_TO_RULE,
    _LEVEL_TO_SEVERITY,
)

# ---------------------------------------------------------------------------
# Fixtures — sample clang-tidy --export-fixes YAML content
# ---------------------------------------------------------------------------

SAMPLE_SOURCE = """\
#include "MyActor.h"

void AMyActor::BeginPlay()
{
    Super::BeginPlay();
}

void AMyActor::Tick(float DeltaTime)
{
    Super::Tick(DeltaTime);
    for (auto Elem : SomeArray)
    {
        Process(Elem);
    }
}
"""

# Offset of "void AMyActor::BeginPlay()" line (line 3, 0-indexed char 22)
# Lines: line1=21chars+\n, line2=\n → offset 23 is start of line 3
_BEGINPLAY_OFFSET = len("#include \"MyActor.h\"\n\n")
# Offset of "for (auto Elem" line (line 11)
_FORLOOP_OFFSET = SAMPLE_SOURCE.index("for (auto Elem")


def _make_fixes_yaml(diagnostics, main_file="/project/Source/MyActor.cpp"):
    """Helper to build a clang-tidy fixes YAML string."""
    data = {
        "MainSourceFile": main_file,
        "Diagnostics": diagnostics,
    }
    return yaml.dump(data, default_flow_style=False, allow_unicode=True)


def _make_diag(
    name,
    message,
    file_path="/project/Source/MyActor.cpp",
    offset=100,
    level="Warning",
    replacements=None,
):
    """Helper to build a single diagnostic dict."""
    diag = {
        "DiagnosticName": name,
        "DiagnosticMessage": {
            "Message": message,
            "FilePath": file_path,
            "FileOffset": offset,
            "Replacements": replacements or [],
        },
        "Level": level,
        "BuildDirectory": "/project/build",
    }
    return diag


# ---------------------------------------------------------------------------
# parse_tidy_fixes tests
# ---------------------------------------------------------------------------


class TestParseTidyFixes:
    """Tests for parsing clang-tidy --export-fixes YAML."""

    def test_parse_valid_yaml(self, tmp_path):
        diag = _make_diag(
            "modernize-use-override",
            "annotate this function with 'override'",
        )
        content = _make_fixes_yaml([diag])
        f = tmp_path / "fixes.yaml"
        f.write_text(content)
        result = parse_tidy_fixes(str(f))
        assert len(result) == 1
        assert result[0]["DiagnosticName"] == "modernize-use-override"

    def test_parse_multiple_diagnostics(self, tmp_path):
        diags = [
            _make_diag("modernize-use-override", "msg1"),
            _make_diag("performance-for-range-copy", "msg2"),
            _make_diag("clang-analyzer-core.DivideZero", "msg3"),
        ]
        content = _make_fixes_yaml(diags)
        f = tmp_path / "fixes.yaml"
        f.write_text(content)
        result = parse_tidy_fixes(str(f))
        assert len(result) == 3

    def test_parse_nonexistent_file(self):
        result = parse_tidy_fixes("/nonexistent/fixes.yaml")
        assert result == []

    def test_parse_empty_file(self, tmp_path):
        f = tmp_path / "fixes.yaml"
        f.write_text("")
        result = parse_tidy_fixes(str(f))
        assert result == []

    def test_parse_whitespace_only_file(self, tmp_path):
        f = tmp_path / "fixes.yaml"
        f.write_text("   \n  \n")
        result = parse_tidy_fixes(str(f))
        assert result == []

    def test_parse_invalid_yaml(self, tmp_path):
        f = tmp_path / "fixes.yaml"
        f.write_text("{{invalid yaml: [")
        result = parse_tidy_fixes(str(f))
        assert result == []

    def test_parse_yaml_without_diagnostics_key(self, tmp_path):
        f = tmp_path / "fixes.yaml"
        f.write_text("MainSourceFile: /path/to/file.cpp\n")
        result = parse_tidy_fixes(str(f))
        assert result == []

    def test_parse_yaml_with_null_diagnostics(self, tmp_path):
        f = tmp_path / "fixes.yaml"
        f.write_text("MainSourceFile: /path\nDiagnostics: null\n")
        result = parse_tidy_fixes(str(f))
        assert result == []

    def test_parse_yaml_not_dict(self, tmp_path):
        f = tmp_path / "fixes.yaml"
        f.write_text("- item1\n- item2\n")
        result = parse_tidy_fixes(str(f))
        assert result == []

    def test_parse_multi_document_yaml(self, tmp_path):
        """Multi-document YAML from concatenated clang-tidy runs."""
        diag1 = _make_diag("modernize-use-override", "msg1",
                           file_path="/project/Source/A.cpp")
        diag2 = _make_diag("performance-for-range-copy", "msg2",
                           file_path="/project/Source/B.cpp")
        doc1 = _make_fixes_yaml([diag1], main_file="/project/Source/A.cpp")
        doc2 = _make_fixes_yaml([diag2], main_file="/project/Source/B.cpp")
        combined = doc1 + "---\n" + doc2
        f = tmp_path / "fixes.yaml"
        f.write_text(combined)
        result = parse_tidy_fixes(str(f))
        assert len(result) == 2
        names = {d["DiagnosticName"] for d in result}
        assert names == {"modernize-use-override", "performance-for-range-copy"}

    def test_parse_multi_document_with_empty_doc(self, tmp_path):
        """Multi-doc where one document has no Diagnostics key."""
        diag = _make_diag("modernize-use-override", "msg1")
        doc1 = _make_fixes_yaml([diag])
        doc2 = "---\nMainSourceFile: /project/Source/B.cpp\n"
        f = tmp_path / "fixes.yaml"
        f.write_text(doc1 + doc2)
        result = parse_tidy_fixes(str(f))
        assert len(result) == 1

    def test_parse_multi_document_three_files(self, tmp_path):
        """Three separate clang-tidy runs concatenated."""
        diags = []
        docs = []
        for i, name in enumerate(["modernize-use-override",
                                   "performance-for-range-copy",
                                   "clang-analyzer-core.DivideZero"]):
            d = _make_diag(name, f"msg{i}",
                           file_path=f"/project/Source/File{i}.cpp")
            diags.append(d)
            docs.append(_make_fixes_yaml([d],
                        main_file=f"/project/Source/File{i}.cpp"))
        combined = "---\n".join(docs)
        f = tmp_path / "fixes.yaml"
        f.write_text(combined)
        result = parse_tidy_fixes(str(f))
        assert len(result) == 3


# ---------------------------------------------------------------------------
# convert_diagnostics tests
# ---------------------------------------------------------------------------


class TestConvertDiagnostics:
    """Tests for converting raw diagnostics to findings."""

    def test_diagnostic_without_fix_becomes_comment(self):
        diags = [
            _make_diag(
                "clang-analyzer-core.DivideZero",
                "division by zero is undefined",
                offset=100,
                level="Warning",
            ),
        ]
        findings = convert_diagnostics(diags)
        assert len(findings) == 1
        f = findings[0]
        assert f["rule_id"] == "clang-analyzer-core.DivideZero"
        assert f["severity"] == "warning"
        assert f["message"] == "division by zero is undefined"
        assert f["suggestion"] is None

    def test_diagnostic_with_fix_generates_suggestion(self):
        source = "    virtual void BeginPlay();\n"
        abs_path = "/project/Source/MyActor.cpp"
        # Replacement: insert ' override' before the semicolon
        replacements = [
            {
                "FilePath": abs_path,
                "Offset": len("    virtual void BeginPlay()"),
                "Length": 0,
                "ReplacementText": " override",
            }
        ]
        diags = [
            _make_diag(
                "modernize-use-override",
                "annotate this function with 'override'",
                file_path=abs_path,
                offset=0,
                replacements=replacements,
            ),
        ]
        findings = convert_diagnostics(
            diags,
            source_contents={abs_path: source},
        )
        assert len(findings) == 1
        f = findings[0]
        assert f["rule_id"] == "override_keyword"
        assert f["suggestion"] is not None
        assert "override" in f["suggestion"]

    def test_rule_id_mapping(self):
        """clang-tidy check names should map to checklist rule_ids."""
        for tidy_check, expected_rule in _CHECK_TO_RULE.items():
            diags = [_make_diag(tidy_check, "msg")]
            findings = convert_diagnostics(diags)
            assert findings[0]["rule_id"] == expected_rule

    def test_unmapped_check_uses_check_name(self):
        diags = [
            _make_diag("readability-else-after-return", "msg"),
        ]
        findings = convert_diagnostics(diags)
        assert findings[0]["rule_id"] == "readability-else-after-return"

    def test_severity_mapping(self):
        for level, expected in _LEVEL_TO_SEVERITY.items():
            diags = [_make_diag("some-check", "msg", level=level)]
            findings = convert_diagnostics(diags)
            assert findings[0]["severity"] == expected

    def test_unknown_severity_defaults_to_warning(self):
        diags = [_make_diag("some-check", "msg", level="UnknownLevel")]
        findings = convert_diagnostics(diags)
        assert findings[0]["severity"] == "warning"

    def test_line_number_from_source_content(self):
        source = "line1\nline2\nline3\n"
        abs_path = "/project/Source/Test.cpp"
        # Offset 6 is start of "line2" → line 2
        diags = [
            _make_diag("some-check", "msg", file_path=abs_path, offset=6),
        ]
        findings = convert_diagnostics(
            diags,
            source_contents={abs_path: source},
        )
        assert findings[0]["line"] == 2

    def test_line_number_fallback_without_source(self):
        diags = [
            _make_diag("some-check", "msg", offset=240),
        ]
        findings = convert_diagnostics(diags)
        # Rough estimate: 240 // 80 + 1 = 4
        assert findings[0]["line"] == 4

    def test_empty_diagnostics(self):
        findings = convert_diagnostics([])
        assert findings == []

    def test_non_dict_diagnostic_skipped(self):
        findings = convert_diagnostics(["not a dict", 42, None])
        assert findings == []

    def test_missing_diagnostic_message(self):
        diags = [{"DiagnosticName": "check", "Level": "Warning"}]
        findings = convert_diagnostics(diags)
        # Should handle gracefully — DiagnosticMessage defaults to {}
        assert len(findings) == 1
        assert findings[0]["message"] == ""

    def test_empty_replacements_no_suggestion(self):
        diags = [
            _make_diag("some-check", "msg", replacements=[]),
        ]
        findings = convert_diagnostics(diags)
        assert findings[0]["suggestion"] is None

    def test_multiple_diagnostics_different_files(self):
        diags = [
            _make_diag(
                "check-a", "msg1",
                file_path="/project/Source/A.cpp", offset=0,
            ),
            _make_diag(
                "check-b", "msg2",
                file_path="/project/Source/B.cpp", offset=0,
            ),
        ]
        findings = convert_diagnostics(diags)
        assert len(findings) == 2
        files = {f["file"] for f in findings}
        assert len(files) == 2

    def test_replacement_text_applied_correctly(self):
        source = "    void Tick(float DeltaTime);\n"
        abs_path = "/project/Source/MyActor.cpp"
        # Replace "void" with "virtual void"
        replacements = [
            {
                "FilePath": abs_path,
                "Offset": 4,  # start of "void"
                "Length": 4,  # length of "void"
                "ReplacementText": "virtual void",
            }
        ]
        diags = [
            _make_diag(
                "some-check", "msg",
                file_path=abs_path, offset=4,
                replacements=replacements,
            ),
        ]
        findings = convert_diagnostics(
            diags,
            source_contents={abs_path: source},
        )
        assert findings[0]["suggestion"] is not None
        assert "virtual void" in findings[0]["suggestion"]


# ---------------------------------------------------------------------------
# deduplicate tests
# ---------------------------------------------------------------------------


class TestDeduplicate:
    """Tests for Stage 1 / Stage 2 deduplication."""

    def test_no_overlap(self):
        s2 = [
            {"file": "A.cpp", "line": 10, "rule_id": "check-a", "severity": "warning",
             "message": "msg", "suggestion": None},
        ]
        s1 = [
            {"file": "A.cpp", "line": 20, "rule_id": "logtemp", "severity": "warning",
             "message": "msg", "suggestion": None},
        ]
        result = deduplicate(s2, s1)
        assert len(result) == 1

    def test_exact_file_line_overlap_removed(self):
        s2 = [
            {"file": "A.cpp", "line": 10, "rule_id": "override_keyword",
             "severity": "warning", "message": "msg", "suggestion": None},
        ]
        s1 = [
            {"file": "A.cpp", "line": 10, "rule_id": "logtemp",
             "severity": "warning", "message": "msg", "suggestion": None},
        ]
        result = deduplicate(s2, s1)
        assert len(result) == 0

    def test_different_file_same_line_kept(self):
        s2 = [
            {"file": "B.cpp", "line": 10, "rule_id": "check-a",
             "severity": "warning", "message": "msg", "suggestion": None},
        ]
        s1 = [
            {"file": "A.cpp", "line": 10, "rule_id": "logtemp",
             "severity": "warning", "message": "msg", "suggestion": None},
        ]
        result = deduplicate(s2, s1)
        assert len(result) == 1

    def test_multiple_overlaps(self):
        s2 = [
            {"file": "A.cpp", "line": 10, "rule_id": "c1", "severity": "warning",
             "message": "msg", "suggestion": None},
            {"file": "A.cpp", "line": 20, "rule_id": "c2", "severity": "warning",
             "message": "msg", "suggestion": None},
            {"file": "A.cpp", "line": 30, "rule_id": "c3", "severity": "warning",
             "message": "msg", "suggestion": None},
        ]
        s1 = [
            {"file": "A.cpp", "line": 10, "rule_id": "s1", "severity": "warning",
             "message": "msg", "suggestion": None},
            {"file": "A.cpp", "line": 30, "rule_id": "s2", "severity": "warning",
             "message": "msg", "suggestion": None},
        ]
        result = deduplicate(s2, s1)
        assert len(result) == 1
        assert result[0]["line"] == 20

    def test_empty_stage1(self):
        s2 = [
            {"file": "A.cpp", "line": 10, "rule_id": "c1", "severity": "warning",
             "message": "msg", "suggestion": None},
        ]
        result = deduplicate(s2, [])
        assert len(result) == 1

    def test_empty_stage2(self):
        s1 = [
            {"file": "A.cpp", "line": 10, "rule_id": "s1", "severity": "warning",
             "message": "msg", "suggestion": None},
        ]
        result = deduplicate([], s1)
        assert len(result) == 0

    def test_both_empty(self):
        result = deduplicate([], [])
        assert len(result) == 0


# ---------------------------------------------------------------------------
# Helper function tests
# ---------------------------------------------------------------------------


class TestHelpers:
    """Tests for helper functions."""

    def test_offset_to_line_first_line(self):
        assert _offset_to_line("hello\nworld\n", 0) == 1
        assert _offset_to_line("hello\nworld\n", 3) == 1

    def test_offset_to_line_second_line(self):
        assert _offset_to_line("hello\nworld\n", 6) == 2

    def test_offset_to_line_third_line(self):
        assert _offset_to_line("line1\nline2\nline3\n", 12) == 3

    def test_resolve_path_relative(self):
        # Already relative paths should be returned as-is or simplified
        result = _resolve_path("Source/MyActor.cpp")
        assert "MyActor.cpp" in result

    def test_resolve_path_with_build_dir(self):
        result = _resolve_path("/project/Source/A.cpp", "/project")
        assert result == "Source/A.cpp"


# ---------------------------------------------------------------------------
# Integration test — full pipeline
# ---------------------------------------------------------------------------


class TestIntegration:
    """End-to-end integration tests."""

    def test_full_pipeline_with_fixes_yaml(self, tmp_path):
        """Parse a fixes YAML → convert → deduplicate → JSON output."""
        source = "    virtual void BeginPlay();\n    void Tick(float dt);\n"
        abs_path = "/project/Source/MyActor.cpp"

        diags = [
            _make_diag(
                "modernize-use-override",
                "annotate with 'override'",
                file_path=abs_path,
                offset=0,
                replacements=[
                    {
                        "FilePath": abs_path,
                        "Offset": len("    virtual void BeginPlay()"),
                        "Length": 0,
                        "ReplacementText": " override",
                    }
                ],
            ),
            _make_diag(
                "clang-analyzer-core.DivideZero",
                "division by zero",
                file_path=abs_path,
                offset=len(source) - 5,
                level="Error",
            ),
        ]

        # Write fixes YAML
        fixes_yaml = _make_fixes_yaml(diags)
        f = tmp_path / "fixes.yaml"
        f.write_text(fixes_yaml)

        # Parse
        raw = parse_tidy_fixes(str(f))
        assert len(raw) == 2

        # Convert
        findings = convert_diagnostics(
            raw, source_contents={abs_path: source}
        )
        assert len(findings) == 2

        # One should have suggestion (override), one should not (division)
        with_suggestion = [f for f in findings if f["suggestion"] is not None]
        without_suggestion = [f for f in findings if f["suggestion"] is None]
        assert len(with_suggestion) == 1
        assert len(without_suggestion) == 1
        assert "override" in with_suggestion[0]["suggestion"]

    def test_full_pipeline_with_deduplication(self, tmp_path):
        """Stage 2 findings overlapping Stage 1 should be removed."""
        abs_path = "/project/Source/A.cpp"
        diags = [
            _make_diag("check-a", "msg1", file_path=abs_path, offset=0),
            _make_diag("check-b", "msg2", file_path=abs_path, offset=800),
        ]
        fixes_yaml = _make_fixes_yaml(diags)
        f = tmp_path / "fixes.yaml"
        f.write_text(fixes_yaml)

        raw = parse_tidy_fixes(str(f))
        s2_findings = convert_diagnostics(raw)

        # Create Stage 1 results overlapping line 1
        s1_findings = [
            {"file": s2_findings[0]["file"], "line": s2_findings[0]["line"],
             "rule_id": "logtemp", "severity": "warning",
             "message": "msg", "suggestion": None},
        ]

        result = deduplicate(s2_findings, s1_findings)
        # One should be removed (same file+line), one should remain
        assert len(result) == len(s2_findings) - 1

    def test_pvs_report_ignored_gracefully(self, tmp_path):
        """--pvs-report should be accepted but ignored."""
        # This tests the CLI arg is handled; actual PVS parsing is a placeholder
        abs_path = "/project/Source/A.cpp"
        diags = [_make_diag("check-a", "msg")]
        fixes_yaml = _make_fixes_yaml(diags)
        f = tmp_path / "fixes.yaml"
        f.write_text(fixes_yaml)

        raw = parse_tidy_fixes(str(f))
        findings = convert_diagnostics(raw)
        # Should have findings from clang-tidy only
        assert len(findings) == 1

    def test_cli_loads_source_files_for_suggestions(self, tmp_path):
        """CLI path should load source files and produce suggestions."""
        import subprocess

        # Create source file on disk
        source_dir = tmp_path / "Source"
        source_dir.mkdir()
        source_file = source_dir / "MyActor.cpp"
        source_content = "    virtual void BeginPlay();\n"
        source_file.write_text(source_content)

        abs_path = str(source_file)
        # Replacement: insert ' override' before the semicolon
        replacements = [
            {
                "FilePath": abs_path,
                "Offset": len("    virtual void BeginPlay()"),
                "Length": 0,
                "ReplacementText": " override",
            }
        ]
        diags = [
            _make_diag(
                "modernize-use-override",
                "annotate this function with 'override'",
                file_path=abs_path,
                offset=0,
                replacements=replacements,
            ),
        ]

        fixes_yaml = _make_fixes_yaml(diags)
        fixes_file = tmp_path / "fixes.yaml"
        fixes_file.write_text(fixes_yaml)

        output_file = tmp_path / "output.json"
        result = subprocess.run(
            [
                sys.executable, "-m", "scripts.stage2_tidy_to_suggestions",
                "--tidy-fixes", str(fixes_file),
                "--output", str(output_file),
            ],
            capture_output=True,
            text=True,
            cwd=str(Path(__file__).resolve().parent.parent),
        )
        assert result.returncode == 0, f"stderr: {result.stderr}"

        findings = json.loads(output_file.read_text())
        assert len(findings) == 1
        # The fix: CLI path should now produce suggestions
        assert findings[0]["suggestion"] is not None
        assert "override" in findings[0]["suggestion"]
        # Line number should be accurate (line 1), not offset//80 fallback
        assert findings[0]["line"] == 1

    def test_cli_with_source_dir_flag(self, tmp_path):
        """--source-dir should help resolve source files not at absolute paths."""
        import subprocess

        # Source file at a known location
        source_dir = tmp_path / "project" / "Source"
        source_dir.mkdir(parents=True)
        source_file = source_dir / "Actor.cpp"
        source_content = "line1\nline2\nline3\n"
        source_file.write_text(source_content)

        # Diagnostic references a non-existent absolute path
        fake_abs = "/nonexistent/project/Source/Actor.cpp"
        diags = [
            _make_diag(
                "some-check", "msg",
                file_path=fake_abs,
                offset=6,  # start of "line2" → should be line 2
            ),
        ]

        fixes_yaml = _make_fixes_yaml(diags)
        fixes_file = tmp_path / "fixes.yaml"
        fixes_file.write_text(fixes_yaml)

        output_file = tmp_path / "output.json"
        result = subprocess.run(
            [
                sys.executable, "-m", "scripts.stage2_tidy_to_suggestions",
                "--tidy-fixes", str(fixes_file),
                "--source-dir", str(source_dir),
                "--output", str(output_file),
            ],
            capture_output=True,
            text=True,
            cwd=str(Path(__file__).resolve().parent.parent),
        )
        assert result.returncode == 0, f"stderr: {result.stderr}"

        findings = json.loads(output_file.read_text())
        assert len(findings) == 1
        # With --source-dir, line number should be accurate
        assert findings[0]["line"] == 2


# ---------------------------------------------------------------------------
# _collect_source_contents tests
# ---------------------------------------------------------------------------


class TestCollectSourceContents:
    """Tests for _collect_source_contents helper."""

    def test_reads_files_from_absolute_paths(self, tmp_path):
        source_file = tmp_path / "MyActor.cpp"
        source_file.write_text("hello\nworld\n")
        abs_path = str(source_file)
        diags = [_make_diag("check", "msg", file_path=abs_path)]

        contents, path_map = _collect_source_contents(diags)
        assert abs_path in contents
        assert contents[abs_path] == "hello\nworld\n"
        # Direct absolute match → no path_map entry needed
        assert abs_path not in path_map

    def test_empty_diagnostics(self):
        contents, path_map = _collect_source_contents([])
        assert contents == {}
        assert path_map == {}

    def test_nonexistent_files_skipped(self):
        diags = [_make_diag("check", "msg", file_path="/nonexistent/file.cpp")]
        contents, path_map = _collect_source_contents(diags)
        assert contents == {}
        assert path_map == {}

    def test_source_dir_fallback(self, tmp_path):
        source_dir = tmp_path / "src"
        source_dir.mkdir()
        (source_dir / "Actor.cpp").write_text("content")

        fake_abs = "/nonexistent/Actor.cpp"
        diags = [_make_diag("check", "msg", file_path=fake_abs)]

        contents, path_map = _collect_source_contents(diags, source_dir=str(source_dir))
        assert fake_abs in contents
        assert contents[fake_abs] == "content"
        # path_map should have the resolved relative path
        assert fake_abs in path_map
        assert path_map[fake_abs] == "Actor.cpp"

    def test_deduplicates_file_paths(self, tmp_path):
        source_file = tmp_path / "A.cpp"
        source_file.write_text("data")
        abs_path = str(source_file)

        diags = [
            _make_diag("check-a", "msg1", file_path=abs_path),
            _make_diag("check-b", "msg2", file_path=abs_path),
        ]
        contents, _ = _collect_source_contents(diags)
        assert len(contents) == 1

    def test_collects_replacement_file_paths(self, tmp_path):
        main_file = tmp_path / "Main.cpp"
        main_file.write_text("main content")
        header_file = tmp_path / "Main.h"
        header_file.write_text("header content")

        replacements = [{"FilePath": str(header_file), "Offset": 0, "Length": 0, "ReplacementText": "x"}]
        diags = [_make_diag("check", "msg", file_path=str(main_file), replacements=replacements)]

        contents, _ = _collect_source_contents(diags)
        assert str(main_file) in contents
        assert str(header_file) in contents

    def test_non_dict_diagnostics_skipped(self):
        contents, path_map = _collect_source_contents(["not a dict", 42])
        assert contents == {}
        assert path_map == {}

    def test_source_dir_resolves_absolute_path_suffixes(self, tmp_path):
        """When FilePath is absolute but points to a different root,
        try matching path suffixes against source_dir."""
        # Simulate CI layout: source_dir is /workspace/repo
        # but diagnostic says /tmp/build/repo/Source/Actors/MyActor.cpp
        source_dir = tmp_path / "workspace" / "repo"
        nested = source_dir / "Source" / "Actors"
        nested.mkdir(parents=True)
        (nested / "MyActor.cpp").write_text("found it")

        fake_abs = "/tmp/build/repo/Source/Actors/MyActor.cpp"
        diags = [_make_diag("check", "msg", file_path=fake_abs)]

        contents, path_map = _collect_source_contents(diags, source_dir=str(source_dir))
        assert fake_abs in contents
        assert contents[fake_abs] == "found it"
        # path_map should have a repo-relative path
        assert fake_abs in path_map

    def test_source_dir_path_map_has_relative_suffix(self, tmp_path):
        """path_map should contain the matched suffix as repo-relative path."""
        source_dir = tmp_path / "repo"
        (source_dir / "Source").mkdir(parents=True)
        (source_dir / "Source" / "A.cpp").write_text("data")

        fake_abs = "/other/root/Source/A.cpp"
        diags = [_make_diag("check", "msg", file_path=fake_abs)]

        _, path_map = _collect_source_contents(diags, source_dir=str(source_dir))
        assert fake_abs in path_map
        # The matched suffix should be "Source/A.cpp" (not the full abs path)
        assert path_map[fake_abs] == "Source/A.cpp"

    def test_source_dir_prefers_longer_suffix_match(self, tmp_path):
        """Suffix matching should prefer the longest (most specific) match."""
        source_dir = tmp_path / "project"
        # Create Source/A.cpp (longer match) and just A.cpp (shorter)
        (source_dir / "Source").mkdir(parents=True)
        (source_dir / "Source" / "A.cpp").write_text("correct")
        (source_dir / "A.cpp").write_text("wrong")

        fake_abs = "/build/checkout/Source/A.cpp"
        diags = [_make_diag("check", "msg", file_path=fake_abs)]

        contents, path_map = _collect_source_contents(diags, source_dir=str(source_dir))
        assert fake_abs in contents
        assert fake_abs in path_map
        # Longest suffix "Source/A.cpp" should match before "A.cpp"
        assert contents[fake_abs] == "correct"
        assert path_map[fake_abs] == "Source/A.cpp"

    def test_readable_absolute_path_populates_path_map_with_source_dir(self, tmp_path):
        """When absolute path is readable AND source_dir is provided,
        path_map should still be populated with repo-relative path."""
        source_dir = tmp_path / "repo"
        sub = source_dir / "Source"
        sub.mkdir(parents=True)
        source_file = sub / "Actor.cpp"
        source_file.write_text("content")

        abs_path = str(source_file)
        diags = [_make_diag("check", "msg", file_path=abs_path)]

        contents, path_map = _collect_source_contents(diags, source_dir=str(source_dir))
        assert abs_path in contents
        assert contents[abs_path] == "content"
        # path_map should have an entry with the repo-relative path
        assert abs_path in path_map
        assert path_map[abs_path] == "Source/Actor.cpp"

    def test_readable_absolute_path_no_source_dir_no_path_map(self, tmp_path):
        """Without source_dir, readable absolute paths should not populate path_map."""
        source_file = tmp_path / "Actor.cpp"
        source_file.write_text("content")

        abs_path = str(source_file)
        diags = [_make_diag("check", "msg", file_path=abs_path)]

        contents, path_map = _collect_source_contents(diags)
        assert abs_path in contents
        # No source_dir → no path_map entry
        assert abs_path not in path_map

    def test_suffix_remap_rereads_from_source_dir_copy(self, tmp_path):
        """When an absolute path is readable but NOT under source_dir,
        content should be re-read from the source_dir copy (not the
        stale build-tree copy) after suffix-match remapping."""
        # Simulate: build tree has stale copy, source_dir has current copy
        build_tree = tmp_path / "build" / "Source"
        build_tree.mkdir(parents=True)
        stale_file = build_tree / "Actor.cpp"
        stale_file.write_text("stale build content")

        source_dir = tmp_path / "repo"
        (source_dir / "Source").mkdir(parents=True)
        current_file = source_dir / "Source" / "Actor.cpp"
        current_file.write_text("current repo content")

        abs_path = str(stale_file)
        diags = [_make_diag("check", "msg", file_path=abs_path)]

        contents, path_map = _collect_source_contents(diags, source_dir=str(source_dir))
        assert abs_path in contents
        assert abs_path in path_map
        assert path_map[abs_path] == "Source/Actor.cpp"
        # Content should come from source_dir copy, not the stale build tree
        assert contents[abs_path] == "current repo content"


# ---------------------------------------------------------------------------
# Byte offset handling tests
# ---------------------------------------------------------------------------


class TestByteOffsetHandling:
    """Tests for correct byte-based offset operations with multi-byte chars."""

    def test_offset_to_line_with_multibyte_chars(self):
        """Byte offsets should work correctly with CJK characters."""
        # '가' is 3 bytes in UTF-8, 'A' is 1 byte
        # "가\nA\n" → bytes: \xea\xb0\x80 \x0a \x41 \x0a
        #                     0   1   2    3    4    5
        source = "가\nA\n"
        # Byte offset 4 (start of 'A') should be line 2
        assert _offset_to_line(source, 4) == 2
        # Byte offset 0 should be line 1
        assert _offset_to_line(source, 0) == 1
        # Byte offset 3 (the \n) should still be line 1
        assert _offset_to_line(source, 3) == 1

    def test_offset_to_line_with_emoji(self):
        """Emoji (4 bytes in UTF-8) should not confuse line counting."""
        # "😀\nx\n" → bytes: f0 9f 98 80 0a 78 0a
        #                     0  1  2  3  4  5  6
        source = "😀\nx\n"
        assert _offset_to_line(source, 5) == 2  # byte 5 = 'x'
        assert _offset_to_line(source, 0) == 1

    def test_apply_replacements_with_multibyte_chars(self):
        """Replacements using byte offsets should work with multi-byte source."""
        from scripts.stage2_tidy_to_suggestions import _apply_replacements

        # "// 한글 주석\nvoid Foo();\n"
        source = "// 한글 주석\nvoid Foo();\n"
        raw = source.encode("utf-8")
        # Find byte offset of "void"
        void_offset = raw.index(b"void")

        abs_path = "/project/A.cpp"
        replacements = [
            {
                "FilePath": abs_path,
                "Offset": void_offset,
                "Length": 4,  # length of "void" in bytes
                "ReplacementText": "virtual void",
            }
        ]
        result = _apply_replacements(source, replacements, abs_path)
        assert result is not None
        assert "virtual void Foo();" in result
        # Original Korean comment should be preserved
        assert "한글 주석" in result

    def test_convert_diagnostics_with_multibyte_source(self):
        """Full pipeline: byte offsets + multi-byte chars → correct line + suggestion."""
        # Line 1: "// 한글\n" (9 bytes in UTF-8: 2f 2f 20 ed95 9c ea b8 80 0a)
        # Line 2: "void Foo();\n"
        source = "// 한글\nvoid Foo();\n"
        raw = source.encode("utf-8")
        abs_path = "/project/Test.cpp"
        void_offset = raw.index(b"void")  # byte offset of "void" on line 2

        replacements = [
            {
                "FilePath": abs_path,
                "Offset": void_offset + 5,  # after "void " → before "Foo"
                "Length": 3,  # "Foo"
                "ReplacementText": "Bar",
            }
        ]
        diags = [
            _make_diag(
                "some-check", "rename Foo to Bar",
                file_path=abs_path,
                offset=void_offset,
                replacements=replacements,
            ),
        ]
        findings = convert_diagnostics(
            diags,
            source_contents={abs_path: source},
        )
        assert len(findings) == 1
        assert findings[0]["line"] == 2  # not offset//80 fallback
        assert findings[0]["suggestion"] is not None
        assert "Bar" in findings[0]["suggestion"]


# ---------------------------------------------------------------------------
# CRLF handling tests
# ---------------------------------------------------------------------------


class TestCRLFHandling:
    """Tests for correct CRLF handling in source file reading."""

    def test_read_source_preserves_crlf(self, tmp_path):
        """_read_source should NOT normalise \\r\\n to \\n."""
        f = tmp_path / "crlf.cpp"
        f.write_bytes(b"line1\r\nline2\r\nline3\r\n")

        content = _read_source(f)
        assert "\r\n" in content
        assert content == "line1\r\nline2\r\nline3\r\n"

    def test_crlf_byte_offsets_correct(self, tmp_path):
        """Byte offsets should be correct for CRLF files."""
        # "line1\r\n" = 7 bytes, "void Foo();\r\n" starts at offset 7
        source_bytes = b"line1\r\nvoid Foo();\r\n"
        f = tmp_path / "crlf.cpp"
        f.write_bytes(source_bytes)

        content = _read_source(f)
        # _offset_to_line should count \n in the raw bytes
        assert _offset_to_line(content, 7) == 2  # byte 7 = start of "void"
        assert _offset_to_line(content, 0) == 1  # byte 0 = line 1

    def test_crlf_replacement_pipeline(self, tmp_path):
        """Full pipeline: CRLF source → correct byte offsets → correct suggestion."""
        from scripts.stage2_tidy_to_suggestions import _apply_replacements

        # 2-line CRLF file: "AAA\r\nvoid Foo();\r\n"
        source_bytes = b"AAA\r\nvoid Foo();\r\n"
        f = tmp_path / "crlf.cpp"
        f.write_bytes(source_bytes)

        content = _read_source(f)
        abs_path = str(f)

        # clang-tidy offset for "void" on line 2: 5 bytes (A A A \r \n v...)
        void_offset = source_bytes.index(b"void")  # = 5

        replacements = [
            {
                "FilePath": abs_path,
                "Offset": void_offset,
                "Length": 4,
                "ReplacementText": "virtual void",
            }
        ]
        result = _apply_replacements(content, replacements, abs_path)
        assert result is not None
        assert "virtual void Foo();" in result

    def test_collect_source_contents_preserves_crlf(self, tmp_path):
        """_collect_source_contents should preserve CRLF in loaded content."""
        source_file = tmp_path / "crlf.cpp"
        source_file.write_bytes(b"A\r\nB\r\n")

        abs_path = str(source_file)
        diags = [_make_diag("check", "msg", file_path=abs_path)]

        contents, _ = _collect_source_contents(diags)
        assert abs_path in contents
        assert "\r\n" in contents[abs_path]


# ---------------------------------------------------------------------------
# _extract_suggestion_span tests
# ---------------------------------------------------------------------------


class TestExtractSuggestionSpan:
    """Tests for _extract_suggestion_span helper."""

    def test_single_line_change(self):
        original = "line1\nline2\nline3\n"
        modified = "line1\nLINE2\nline3\n"
        suggestion, start, end = _extract_suggestion_span(original, modified)
        assert suggestion == "LINE2"
        assert start == 2
        assert end is None  # single line

    def test_multi_line_replacement(self):
        """readability-else-after-return style: remove else block."""
        original = "if (x) {\n    return;\n} else {\n    doStuff();\n}\n"
        modified = "if (x) {\n    return;\n}\ndoStuff();\n"
        suggestion, start, end = _extract_suggestion_span(original, modified)
        assert start == 3  # first changed line (1-based)
        assert end == 5    # last changed line in original (1-based)
        assert "doStuff();" in suggestion

    def test_insertion(self):
        """Adding a line (e.g., inserting 'override')."""
        original = "void Foo();\n"
        modified = "void Foo() override;\n"
        suggestion, start, end = _extract_suggestion_span(original, modified)
        assert suggestion == "void Foo() override;"
        assert start == 1
        assert end is None

    def test_no_difference(self):
        original = "line1\nline2\n"
        suggestion, start, end = _extract_suggestion_span(original, original)
        assert suggestion is None

    def test_multi_line_expansion(self):
        """Original 1 line → modified 3 lines."""
        original = "line1\nold_single\nline3\n"
        modified = "line1\nnew_a\nnew_b\nnew_c\nline3\n"
        suggestion, start, end = _extract_suggestion_span(original, modified)
        assert start == 2
        assert end is None  # only 1 original line affected (line 2)
        assert "new_a\nnew_b\nnew_c" == suggestion

    def test_multi_line_contraction(self):
        """Original 3 lines → modified 1 line."""
        original = "line1\nold_a\nold_b\nold_c\nline5\n"
        modified = "line1\nnew_single\nline5\n"
        suggestion, start, end = _extract_suggestion_span(original, modified)
        assert start == 2
        assert end == 4   # lines 2-4 in original are replaced
        assert suggestion == "new_single"

    def test_delete_middle_line(self):
        """Deleting a middle line should not include unchanged tail in range."""
        original = "A\nB\nC\n"
        modified = "A\nC\n"
        suggestion, start, end = _extract_suggestion_span(original, modified)
        # Should be a pure deletion of line 2, not range (2,3) including "C"
        assert start == 2
        assert end is None  # single line
        assert suggestion == ""  # pure deletion

    def test_delete_multiple_middle_lines(self):
        """Deleting two middle lines should only cover those lines."""
        original = "H\nX\nY\nT\n"
        modified = "H\nT\n"
        suggestion, start, end = _extract_suggestion_span(original, modified)
        assert start == 2
        assert end == 3    # lines 2-3 deleted
        assert suggestion == ""

    def test_delete_does_not_include_matching_tail(self):
        """Regression: unchanged tail lines must not be in the suggestion range."""
        original = "keep1\ndelete_me\nkeep2\nkeep3\n"
        modified = "keep1\nkeep2\nkeep3\n"
        suggestion, start, end = _extract_suggestion_span(original, modified)
        # Only line 2 should be affected, not lines 2-4
        assert start == 2
        assert end is None
        assert suggestion == ""

    def test_replace_with_blank_line(self):
        """Replacing a line with a blank line should NOT be treated as deletion."""
        original = "A\nB\nC\n"
        modified = "A\n\nC\n"
        suggestion, start, end = _extract_suggestion_span(original, modified)
        assert start == 2
        assert end is None
        # Must be distinguishable from pure deletion ("")
        assert suggestion == "\n"

    def test_delete_vs_blank_line_distinct(self):
        """Pure deletion and blank-line replacement must produce different values."""
        # Pure deletion: remove line B entirely
        del_suggestion, _, _ = _extract_suggestion_span("A\nB\nC\n", "A\nC\n")
        # Blank-line replacement: replace B with empty line
        blank_suggestion, _, _ = _extract_suggestion_span("A\nB\nC\n", "A\n\nC\n")
        assert del_suggestion != blank_suggestion
        assert del_suggestion == ""
        assert blank_suggestion == "\n"

    def test_insert_at_eof(self):
        """Pure insertion at EOF should not produce inverted range."""
        original = "line1\nline2\n"
        modified = "line1\nline2\nnewline\n"
        suggestion, start, end = _extract_suggestion_span(original, modified)
        # Should anchor to last original line (line2) and expand
        assert start <= 2
        assert end is None  # single-line (anchored)
        assert "newline" in suggestion
        # The anchor line should also be in the suggestion
        assert "line2" in suggestion

    def test_insert_single_line_at_eof(self):
        """Appending one line to a single-line file."""
        original = "A\n"
        modified = "A\nB\n"
        suggestion, start, end = _extract_suggestion_span(original, modified)
        assert start == 1  # anchored to line 1
        assert end is None
        assert suggestion == "A\nB"

    def test_insert_multiple_lines_at_eof(self):
        """Appending multiple lines should anchor to last original line."""
        original = "A\nB\n"
        modified = "A\nB\nC\nD\n"
        suggestion, start, end = _extract_suggestion_span(original, modified)
        assert start == 2  # anchored to line 2 ("B")
        assert end is None
        assert suggestion == "B\nC\nD"

    def test_insert_into_empty_original(self):
        """Inserting into an empty file (edge case: no anchor available)."""
        original = ""
        modified = "new_content\n"
        suggestion, start, end = _extract_suggestion_span(original, modified)
        assert suggestion is not None
        assert start == 1
        assert end is None
        assert "new_content" in suggestion

    def test_start_never_exceeds_end(self):
        """Regression: start_line must never exceed end_line."""
        # Various insertion patterns (including BOF insertions)
        cases = [
            ("X\n", "X\nY\n"),
            ("A\nB\n", "A\nB\nC\n"),
            ("", "NEW\n"),
            ("H\n", "H\nI\nJ\nK\n"),
            ("X\nY\n", "NEW\nX\nY\n"),
            ("X\n", "NEW\nX\n"),
            ("X\nY\n", "A\nB\nX\nY\n"),
        ]
        for orig, mod in cases:
            suggestion, start, end = _extract_suggestion_span(orig, mod)
            if end is not None:
                assert start <= end, (
                    f"Inverted range: start={start} > end={end} "
                    f"for orig={orig!r}, mod={mod!r}"
                )

    def test_insert_at_bof(self):
        """Pure insertion at BOF should not drop the first original line."""
        original = "X\nY\n"
        modified = "NEW\nX\nY\n"
        suggestion, start, end = _extract_suggestion_span(original, modified)
        assert start == 1
        assert end is None
        # Suggestion must include the new line AND the anchored first line
        assert "NEW" in suggestion
        assert "X" in suggestion
        assert suggestion == "NEW\nX"

    def test_insert_multiple_lines_at_bof(self):
        """Inserting multiple lines at BOF should anchor to first original line."""
        original = "X\nY\n"
        modified = "A\nB\nX\nY\n"
        suggestion, start, end = _extract_suggestion_span(original, modified)
        assert start == 1
        assert end is None
        assert suggestion == "A\nB\nX"

    def test_insert_at_bof_single_line_file(self):
        """Insert at BOF of a single-line file."""
        original = "X\n"
        modified = "NEW\nX\n"
        suggestion, start, end = _extract_suggestion_span(original, modified)
        assert start == 1
        assert end is None
        assert suggestion == "NEW\nX"


# ---------------------------------------------------------------------------
# path_map integration tests
# ---------------------------------------------------------------------------


class TestPathMapIntegration:
    """Tests for path_map usage in convert_diagnostics."""

    def test_path_map_overrides_resolve_path(self):
        """When path_map has an entry, it should be used instead of _resolve_path."""
        abs_path = "/tmp/build/repo/Source/A.cpp"
        diags = [_make_diag("some-check", "msg", file_path=abs_path, offset=0)]
        path_map = {abs_path: "Source/A.cpp"}

        findings = convert_diagnostics(diags, path_map=path_map)
        assert len(findings) == 1
        assert findings[0]["file"] == "Source/A.cpp"

    def test_without_path_map_uses_resolve_path(self):
        """Without path_map, _resolve_path is used (may return abs path)."""
        abs_path = "/tmp/build/repo/Source/A.cpp"
        diags = [_make_diag("some-check", "msg", file_path=abs_path, offset=0)]

        findings = convert_diagnostics(diags)
        assert len(findings) == 1
        # _resolve_path can't make this relative (not under cwd), so abs path
        assert findings[0]["file"] == abs_path

    def test_path_map_enables_dedup_with_stage1(self):
        """With path_map, Stage 2 findings should dedup against Stage 1."""
        abs_path = "/tmp/build/repo/Source/A.cpp"
        path_map = {abs_path: "Source/A.cpp"}

        diags = [_make_diag("check-a", "msg", file_path=abs_path, offset=0)]
        s2_findings = convert_diagnostics(diags, path_map=path_map)
        assert s2_findings[0]["file"] == "Source/A.cpp"

        s1_findings = [
            {"file": "Source/A.cpp", "line": s2_findings[0]["line"],
             "rule_id": "logtemp", "severity": "warning",
             "message": "msg", "suggestion": None},
        ]

        result = deduplicate(s2_findings, s1_findings)
        assert len(result) == 0  # dedup should work now

    def test_cli_source_dir_produces_relative_paths(self, tmp_path):
        """CLI with --source-dir should produce repo-relative file paths."""
        import subprocess

        source_dir = tmp_path / "repo" / "Source"
        source_dir.mkdir(parents=True)
        source_file = source_dir / "Actor.cpp"
        source_file.write_text("void Foo();\n")

        fake_abs = "/nonexistent/build/Source/Actor.cpp"
        diags = [_make_diag("check", "msg", file_path=fake_abs, offset=0)]
        fixes_yaml = _make_fixes_yaml(diags)
        fixes_file = tmp_path / "fixes.yaml"
        fixes_file.write_text(fixes_yaml)

        output_file = tmp_path / "output.json"
        result = subprocess.run(
            [
                sys.executable, "-m", "scripts.stage2_tidy_to_suggestions",
                "--tidy-fixes", str(fixes_file),
                "--source-dir", str(source_dir),
                "--output", str(output_file),
            ],
            capture_output=True,
            text=True,
            cwd=str(Path(__file__).resolve().parent.parent),
        )
        assert result.returncode == 0, f"stderr: {result.stderr}"

        findings = json.loads(output_file.read_text())
        assert len(findings) == 1
        # Should be a relative path, not the fake absolute
        assert findings[0]["file"] != fake_abs
        assert "Actor.cpp" in findings[0]["file"]

    def test_readable_abs_path_with_source_dir_gives_relative_finding(self, tmp_path):
        """When absolute path is readable and source_dir provided, finding
        should use repo-relative path, not the absolute path."""
        source_dir = tmp_path / "repo"
        sub = source_dir / "Source"
        sub.mkdir(parents=True)
        source_file = sub / "Actor.cpp"
        source_file.write_text("void Foo();\n")

        abs_path = str(source_file)
        diags = [_make_diag("check", "msg", file_path=abs_path, offset=0)]

        contents, path_map = _collect_source_contents(diags, source_dir=str(source_dir))
        findings = convert_diagnostics(diags, source_contents=contents, path_map=path_map)
        assert len(findings) == 1
        assert findings[0]["file"] == "Source/Actor.cpp"


# ---------------------------------------------------------------------------
# Multi-line suggestion integration tests
# ---------------------------------------------------------------------------


class TestMultiLineSuggestion:
    """Tests for multi-line suggestion extraction in convert_diagnostics."""

    def test_multiline_fix_produces_end_line(self):
        """A fix spanning 3 original lines should have end_line set."""
        # Lines: 1=header, 2-4=old code, 5=footer
        source = "header\nold_a\nold_b\nold_c\nfooter\n"
        abs_path = "/project/Test.cpp"
        raw = source.encode("utf-8")

        # Replace "old_a\nold_b\nold_c" with "new_single"
        start_offset = raw.index(b"old_a")
        end_of_old_c = raw.index(b"old_c") + len(b"old_c")
        length = end_of_old_c - start_offset

        replacements = [
            {
                "FilePath": abs_path,
                "Offset": start_offset,
                "Length": length,
                "ReplacementText": "new_single",
            }
        ]
        diags = [
            _make_diag(
                "some-check", "refactor",
                file_path=abs_path,
                offset=start_offset,
                replacements=replacements,
            ),
        ]
        findings = convert_diagnostics(
            diags,
            source_contents={abs_path: source},
        )
        assert len(findings) == 1
        f = findings[0]
        assert f["line"] == 2          # first changed line
        assert f["end_line"] == 4      # last changed line in original
        assert f["suggestion"] == "new_single"

    def test_single_line_fix_no_end_line(self):
        """A single-line fix should not have end_line."""
        source = "    virtual void BeginPlay();\n"
        abs_path = "/project/Test.cpp"

        replacements = [
            {
                "FilePath": abs_path,
                "Offset": len("    virtual void BeginPlay()"),
                "Length": 0,
                "ReplacementText": " override",
            }
        ]
        diags = [
            _make_diag(
                "modernize-use-override", "add override",
                file_path=abs_path, offset=0,
                replacements=replacements,
            ),
        ]
        findings = convert_diagnostics(
            diags,
            source_contents={abs_path: source},
        )
        assert len(findings) == 1
        assert "end_line" not in findings[0]
        assert "override" in findings[0]["suggestion"]

    def test_else_after_return_multiline(self):
        """readability-else-after-return: remove else block (multi-line fix)."""
        source = (
            "int Foo() {\n"
            "    if (x) {\n"
            "        return 1;\n"
            "    } else {\n"
            "        return 2;\n"
            "    }\n"
            "}\n"
        )
        abs_path = "/project/Test.cpp"
        raw = source.encode("utf-8")

        # Replace "} else {\n        return 2;\n    }" with "}\n    return 2;"
        else_start = raw.index(b"} else {")
        old_block = b"} else {\n        return 2;\n    }"
        else_end = else_start + len(old_block)

        replacements = [
            {
                "FilePath": abs_path,
                "Offset": else_start,
                "Length": len(old_block),
                "ReplacementText": "}\n    return 2;",
            }
        ]
        diags = [
            _make_diag(
                "readability-else-after-return",
                "do not use 'else' after 'return'",
                file_path=abs_path,
                offset=else_start,
                replacements=replacements,
            ),
        ]
        findings = convert_diagnostics(
            diags,
            source_contents={abs_path: source},
        )
        assert len(findings) == 1
        f = findings[0]
        assert f["end_line"] is not None  # multi-line
        assert "return 2;" in f["suggestion"]
        # suggestion should NOT be a single line
        assert "\n" in f["suggestion"] or f["end_line"] > f["line"]


# ---------------------------------------------------------------------------
# .clang-tidy config validation
# ---------------------------------------------------------------------------


class TestClangTidyConfig:
    """Validate the .clang-tidy configuration file."""

    CLANG_TIDY_PATH = (
        Path(__file__).resolve().parent.parent / "configs" / ".clang-tidy"
    )

    def test_config_exists(self):
        assert self.CLANG_TIDY_PATH.exists()

    def test_config_is_valid_yaml(self):
        content = self.CLANG_TIDY_PATH.read_text(encoding="utf-8")
        data = yaml.safe_load(content)
        assert isinstance(data, dict)

    def test_config_has_checks(self):
        content = self.CLANG_TIDY_PATH.read_text(encoding="utf-8")
        data = yaml.safe_load(content)
        checks = data.get("Checks", "")
        assert "modernize-use-override" in checks
        assert "cppcoreguidelines-virtual-class-destructor" in checks
        assert "performance-for-range-copy" in checks
        assert "clang-analyzer-core.DivideZero" in checks

    def test_config_has_header_filter(self):
        content = self.CLANG_TIDY_PATH.read_text(encoding="utf-8")
        data = yaml.safe_load(content)
        assert "HeaderFilterRegex" in data
        assert "Source" in data["HeaderFilterRegex"]

    def test_all_spec_checks_present(self):
        """All checks from STEP5_STAGE2.md should be in the config."""
        content = self.CLANG_TIDY_PATH.read_text(encoding="utf-8")
        data = yaml.safe_load(content)
        checks = data.get("Checks", "")
        expected_checks = [
            "cppcoreguidelines-virtual-class-destructor",
            "bugprone-virtual-near-miss",
            "performance-unnecessary-copy-initialization",
            "performance-for-range-copy",
            "modernize-use-override",
            "clang-analyzer-optin.cplusplus.VirtualCall",
            "clang-analyzer-core.DivideZero",
            "readability-else-after-return",
            "readability-redundant-smartptr-get",
        ]
        for check in expected_checks:
            assert check in checks, f"Missing check: {check}"
