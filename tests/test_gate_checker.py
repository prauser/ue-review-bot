"""Tests for gate_checker.py — Large PR detection and file filtering.

Test cases from STEP2_GATE.md:
  1. reviewable 3개 + skipped 100개 → 일반 PR (Stage 1~3)
  2. reviewable 60개 → 대규모 PR (Stage 1 only)
  3. migration 라벨 + reviewable 5개 → 대규모 PR
  4. 라벨 없음 + reviewable 50개 → 일반 PR (경계값)
  5. 모든 파일이 ThirdParty → reviewable 0개 → 일반 PR
  6. .generated.h, .uasset 필터 확인
"""

from __future__ import annotations

import json
import os
import textwrap
from pathlib import Path

import pytest
import yaml

# Adjust path so we can import from scripts/
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.gate_checker import (
    classify_pr,
    determine_allowed_stages,
    filter_files,
    load_config,
    parse_diff_files,
    run_gate_check,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"
CONFIG_PATH = Path(__file__).resolve().parent.parent / "configs" / "gate_config.yml"


def _load_test_config():
    """Load the real gate_config.yml for integration tests."""
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def _make_diff_header(filepath: str) -> str:
    """Generate a minimal diff --git header for a single file."""
    return f"diff --git a/{filepath} b/{filepath}"


def _make_diff(filepaths: list[str]) -> str:
    """Generate a minimal unified diff with multiple files."""
    lines = []
    for fp in filepaths:
        lines.append(f"diff --git a/{fp} b/{fp}")
        lines.append("new file mode 100644")
        lines.append("index 0000000..1234567")
        lines.append(f"--- /dev/null")
        lines.append(f"+++ b/{fp}")
        lines.append("@@ -0,0 +1,3 @@")
        lines.append("+// content")
        lines.append("+void Foo() {}")
        lines.append("+")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Unit Tests: parse_diff_files
# ---------------------------------------------------------------------------


class TestParseDiffFiles:
    def test_basic_diff_parsing(self):
        diff = textwrap.dedent("""\
            diff --git a/Source/MyActor.cpp b/Source/MyActor.cpp
            --- a/Source/MyActor.cpp
            +++ b/Source/MyActor.cpp
            @@ -1 +1 @@
            -old
            +new
        """)
        files = parse_diff_files(diff)
        assert files == ["Source/MyActor.cpp"]

    def test_multiple_files(self):
        diff = textwrap.dedent("""\
            diff --git a/A.cpp b/A.cpp
            diff --git a/B.h b/B.h
            diff --git a/C.inl b/C.inl
        """)
        files = parse_diff_files(diff)
        assert files == ["A.cpp", "B.h", "C.inl"]

    def test_no_duplicates(self):
        diff = textwrap.dedent("""\
            diff --git a/A.cpp b/A.cpp
            diff --git a/A.cpp b/A.cpp
        """)
        files = parse_diff_files(diff)
        assert files == ["A.cpp"]

    def test_quoted_diff_header(self):
        """Git emits quoted headers for non-ASCII or special-char filenames."""
        diff = textwrap.dedent("""\
            diff --git "a/Source/MyActor.cpp" "b/Source/MyActor.cpp"
            --- "a/Source/MyActor.cpp"
            +++ "b/Source/MyActor.cpp"
            @@ -1 +1 @@
            -old
            +new
        """)
        files = parse_diff_files(diff)
        assert files == ["Source/MyActor.cpp"]

    def test_quoted_mixed_with_unquoted(self):
        """Mix of quoted and unquoted diff headers."""
        diff = "\n".join([
            'diff --git "a/Source/Korean/Actor.cpp" "b/Source/Korean/Actor.cpp"',
            'diff --git a/Source/Normal.h b/Source/Normal.h',
        ])
        files = parse_diff_files(diff)
        assert files == ["Source/Korean/Actor.cpp", "Source/Normal.h"]

    def test_quoted_octal_utf8_korean(self):
        """Git octal escapes for Korean UTF-8 bytes must decode correctly.

        '한글' = U+D55C U+AE00
          한 = UTF-8 bytes 0xED 0x95 0x9C = octal \\355\\225\\234
          글 = UTF-8 bytes 0xEA 0xB8 0x80 = octal \\352\\270\\200
        """
        # Git would output: diff --git "a/Source/\355\225\234\352\270\200/Actor.cpp" ...
        diff = (
            'diff --git '
            '"a/Source/\\355\\225\\234\\352\\270\\200/Actor.cpp" '
            '"b/Source/\\355\\225\\234\\352\\270\\200/Actor.cpp"'
        )
        files = parse_diff_files(diff)
        assert len(files) == 1
        assert files[0] == "Source/한글/Actor.cpp"

    def test_empty_diff(self):
        assert parse_diff_files("") == []

    def test_fixture_diff(self):
        """Parse the sample_diff.patch fixture."""
        diff_text = (FIXTURES_DIR / "sample_diff.patch").read_text(encoding="utf-8")
        files = parse_diff_files(diff_text)
        assert len(files) == 10
        assert "Source/MyGame/Actors/MyActor.cpp" in files
        assert "ThirdParty/protobuf/src/google/protobuf/message.cc" in files
        assert "Content/Maps/TestMap.umap" in files


# ---------------------------------------------------------------------------
# Unit Tests: filter_files
# ---------------------------------------------------------------------------


class TestFilterFiles:
    def setup_method(self):
        self.config = _load_test_config()
        self.skip_patterns = self.config["skip_patterns"]

    def test_cpp_files_pass(self):
        files = ["Source/MyActor.cpp", "Source/MyActor.h", "Source/Util.inl"]
        reviewable, skipped = filter_files(files, self.skip_patterns)
        assert reviewable == files
        assert skipped == []

    def test_thirdparty_skipped(self):
        files = ["ThirdParty/protobuf/message.cc", "Source/MyActor.cpp"]
        reviewable, skipped = filter_files(files, self.skip_patterns)
        assert reviewable == ["Source/MyActor.cpp"]
        assert len(skipped) == 1
        assert "ThirdParty/" in skipped[0]["reason"]

    def test_generated_h_skipped(self):
        """[STEP2_GATE spec] .generated.h 필터 확인."""
        files = ["Source/MyActor.generated.h", "Source/MyActor.h"]
        reviewable, skipped = filter_files(files, self.skip_patterns)
        assert reviewable == ["Source/MyActor.h"]
        assert len(skipped) == 1
        assert skipped[0]["file"] == "Source/MyActor.generated.h"

    def test_uasset_skipped(self):
        """[STEP2_GATE spec] .uasset 필터 확인."""
        files = ["Content/Texture.uasset", "Source/MyActor.cpp"]
        reviewable, skipped = filter_files(files, self.skip_patterns)
        assert reviewable == ["Source/MyActor.cpp"]
        assert len(skipped) == 1
        assert skipped[0]["file"] == "Content/Texture.uasset"

    def test_umap_skipped(self):
        files = ["Content/Maps/Level.umap"]
        reviewable, skipped = filter_files(files, self.skip_patterns)
        assert reviewable == []
        assert len(skipped) == 1

    def test_intermediate_skipped(self):
        files = ["Intermediate/Build/Win64/MyGame.generated.h"]
        reviewable, skipped = filter_files(files, self.skip_patterns)
        assert reviewable == []
        assert len(skipped) == 1

    def test_non_cpp_skipped(self):
        """Non-C++ files like .cs, .py are skipped as non-reviewable."""
        files = ["Source/MyGame.Build.cs", "Tools/build.py"]
        reviewable, skipped = filter_files(files, self.skip_patterns)
        assert reviewable == []
        assert len(skipped) == 2
        for s in skipped:
            assert "C++ 파일이 아님" in s["reason"]

    def test_binary_files_skipped(self):
        files = [
            "Binaries/Win64/MyGame.exe",
            "Plugins/MyPlugin/Binaries/Win64/MyPlugin.dll",
        ]
        reviewable, skipped = filter_files(files, self.skip_patterns)
        assert reviewable == []
        assert len(skipped) == 2

    def test_plugins_thirdparty_skipped(self):
        files = ["Plugins/OnlineSubsystem/ThirdParty/steam/steam_api.h"]
        reviewable, skipped = filter_files(files, self.skip_patterns)
        assert reviewable == []
        assert len(skipped) == 1


# ---------------------------------------------------------------------------
# Unit Tests: classify_pr
# ---------------------------------------------------------------------------


class TestClassifyPR:
    def test_normal_pr_under_threshold(self):
        is_large, reasons = classify_pr(
            reviewable_count=30,
            max_reviewable_files=50,
            labels=[],
            large_pr_labels=["migration"],
        )
        assert is_large is False
        assert reasons == []

    def test_boundary_at_threshold(self):
        """[STEP2_GATE spec] 라벨 없음 + reviewable 50개 → 일반 PR (경계값)."""
        is_large, reasons = classify_pr(
            reviewable_count=50,
            max_reviewable_files=50,
            labels=[],
            large_pr_labels=["migration"],
        )
        assert is_large is False
        assert reasons == []

    def test_large_pr_over_threshold(self):
        is_large, reasons = classify_pr(
            reviewable_count=51,
            max_reviewable_files=50,
            labels=[],
            large_pr_labels=["migration"],
        )
        assert is_large is True
        assert len(reasons) == 1
        assert "51" in reasons[0]

    def test_large_pr_by_label(self):
        """[STEP2_GATE spec] migration 라벨 + reviewable 5개 → 대규모 PR."""
        is_large, reasons = classify_pr(
            reviewable_count=5,
            max_reviewable_files=50,
            labels=["migration"],
            large_pr_labels=["migration", "large-change", "engine-update"],
        )
        assert is_large is True
        assert any("migration" in r for r in reasons)

    def test_large_pr_by_multiple_labels(self):
        is_large, reasons = classify_pr(
            reviewable_count=5,
            max_reviewable_files=50,
            labels=["migration", "engine-update"],
            large_pr_labels=["migration", "large-change", "engine-update"],
        )
        assert is_large is True

    def test_non_matching_label_normal(self):
        is_large, reasons = classify_pr(
            reviewable_count=10,
            max_reviewable_files=50,
            labels=["bugfix", "feature"],
            large_pr_labels=["migration", "large-change"],
        )
        assert is_large is False

    def test_both_triggers(self):
        """Both file count and label trigger → still large, two reasons."""
        is_large, reasons = classify_pr(
            reviewable_count=60,
            max_reviewable_files=50,
            labels=["migration"],
            large_pr_labels=["migration"],
        )
        assert is_large is True
        assert len(reasons) == 2


# ---------------------------------------------------------------------------
# Unit Tests: determine_allowed_stages
# ---------------------------------------------------------------------------


class TestDetermineAllowedStages:
    def test_normal_pr_all_stages(self):
        allowed, manual = determine_allowed_stages(is_large=False)
        assert allowed == [1, 2, 3]
        assert manual == [1, 2, 3]

    def test_large_pr_no_stage3(self):
        allowed, manual = determine_allowed_stages(is_large=True)
        assert allowed == [1, 2]
        assert manual == [1, 2]
        assert 3 not in allowed
        assert 3 not in manual


# ---------------------------------------------------------------------------
# Integration Tests: run_gate_check
# ---------------------------------------------------------------------------


class TestRunGateCheck:
    def setup_method(self):
        self.config = _load_test_config()

    def test_normal_pr_with_skipped(self):
        """[STEP2_GATE spec] reviewable 3개 + skipped 100개 → 일반 PR (Stage 1~3)."""
        # Build a diff with 3 reviewable C++ files + 100 ThirdParty files
        reviewable = [f"Source/Actor{i}.cpp" for i in range(3)]
        skipped = [f"ThirdParty/lib/file{i}.cpp" for i in range(100)]
        diff = _make_diff(reviewable + skipped)

        result = run_gate_check(diff, self.config, labels=[])

        assert result["is_large_pr"] is False
        assert result["reviewable_count"] == 3
        assert result["skipped_count"] == 100
        assert result["total_changed_files"] == 103
        assert result["allowed_stages"] == [1, 2, 3]
        assert result["reasons"] == []

    def test_large_pr_by_file_count(self):
        """[STEP2_GATE spec] reviewable 60개 → 대규모 PR (Stage 1 only, no Stage 3)."""
        reviewable = [f"Source/Module/Actor{i}.cpp" for i in range(60)]
        diff = _make_diff(reviewable)

        result = run_gate_check(diff, self.config, labels=[])

        assert result["is_large_pr"] is True
        assert result["reviewable_count"] == 60
        assert result["allowed_stages"] == [1, 2]
        assert 3 not in result["allowed_stages"]
        assert len(result["reasons"]) == 1

    def test_large_pr_by_label(self):
        """[STEP2_GATE spec] migration 라벨 + reviewable 5개 → 대규모 PR."""
        reviewable = [f"Source/Actor{i}.cpp" for i in range(5)]
        diff = _make_diff(reviewable)

        result = run_gate_check(diff, self.config, labels=["migration"])

        assert result["is_large_pr"] is True
        assert result["reviewable_count"] == 5
        assert result["allowed_stages"] == [1, 2]
        assert any("migration" in r for r in result["reasons"])

    def test_boundary_50_normal(self):
        """[STEP2_GATE spec] 라벨 없음 + reviewable 50개 → 일반 PR (경계값)."""
        reviewable = [f"Source/Actor{i}.cpp" for i in range(50)]
        diff = _make_diff(reviewable)

        result = run_gate_check(diff, self.config, labels=[])

        assert result["is_large_pr"] is False
        assert result["reviewable_count"] == 50
        assert result["allowed_stages"] == [1, 2, 3]

    def test_all_thirdparty_zero_reviewable(self):
        """[STEP2_GATE spec] 모든 파일이 ThirdParty → reviewable 0개 → 일반 PR."""
        thirdparty = [f"ThirdParty/lib/file{i}.cpp" for i in range(20)]
        diff = _make_diff(thirdparty)

        result = run_gate_check(diff, self.config, labels=[])

        assert result["is_large_pr"] is False
        assert result["reviewable_count"] == 0
        assert result["skipped_count"] == 20
        assert result["allowed_stages"] == [1, 2, 3]

    def test_generated_h_filter(self):
        """[STEP2_GATE spec] .generated.h 필터 확인."""
        files = [
            "Source/MyActor.h",
            "Source/MyActor.generated.h",
            "Source/MyActor.cpp",
        ]
        diff = _make_diff(files)

        result = run_gate_check(diff, self.config, labels=[])

        assert result["reviewable_count"] == 2
        assert "Source/MyActor.h" in result["reviewable_files"]
        assert "Source/MyActor.cpp" in result["reviewable_files"]
        assert result["skipped_count"] == 1
        assert any(
            "Source/MyActor.generated.h" == s["file"]
            for s in result["skipped_files"]
        )

    def test_uasset_filter(self):
        """[STEP2_GATE spec] .uasset 필터 확인."""
        files = [
            "Source/MyActor.cpp",
            "Content/Texture.uasset",
            "Content/Map.umap",
        ]
        diff = _make_diff(files)

        result = run_gate_check(diff, self.config, labels=[])

        assert result["reviewable_count"] == 1
        assert result["reviewable_files"] == ["Source/MyActor.cpp"]
        assert result["skipped_count"] == 2

    def test_fixture_sample_diff(self):
        """Integration test using the sample_diff.patch fixture."""
        diff_text = (FIXTURES_DIR / "sample_diff.patch").read_text(encoding="utf-8")

        result = run_gate_check(diff_text, self.config, labels=[])

        assert result["total_changed_files"] == 10
        # Reviewable: MyActor.cpp, MyActor.h, Helper.cpp, Helper.h = 4 C++ files
        # message.cc is ThirdParty → skipped
        # MyGame.Build.cs → non-C++
        # TestMap.umap → binary
        # T_Default.uasset → binary
        # Intermediate/Build/Win64/MyGame.generated.h → Intermediate/ skip
        # Source/MyGame/MyActor.generated.h → .generated.h skip
        assert result["reviewable_count"] == 4
        assert "Source/MyGame/Actors/MyActor.cpp" in result["reviewable_files"]
        assert "Source/MyGame/Actors/MyActor.h" in result["reviewable_files"]
        assert "Source/MyGame/Utils/Helper.cpp" in result["reviewable_files"]
        assert "Source/MyGame/Utils/Helper.h" in result["reviewable_files"]
        assert result["is_large_pr"] is False

    def test_output_json_schema(self):
        """Verify the output JSON has all required keys."""
        diff = _make_diff(["Source/A.cpp"])
        result = run_gate_check(diff, self.config, labels=[])

        required_keys = {
            "is_large_pr",
            "reasons",
            "allowed_stages",
            "manual_allowed_stages",
            "total_changed_files",
            "reviewable_files",
            "reviewable_count",
            "skipped_files",
            "skipped_count",
        }
        assert set(result.keys()) == required_keys

    def test_empty_diff(self):
        """Empty diff produces zero files, normal PR."""
        result = run_gate_check("", self.config, labels=[])
        assert result["is_large_pr"] is False
        assert result["reviewable_count"] == 0
        assert result["total_changed_files"] == 0


# ---------------------------------------------------------------------------
# Unit Tests: load_config
# ---------------------------------------------------------------------------


class TestLoadConfig:
    def test_load_real_config(self):
        config = load_config(str(CONFIG_PATH))
        assert "skip_patterns" in config
        assert "max_reviewable_files" in config
        assert "large_pr_labels" in config
        assert config["max_reviewable_files"] == 50

    def test_missing_config_file(self):
        with pytest.raises(FileNotFoundError):
            load_config("/nonexistent/path/config.yml")

    def test_missing_required_key(self, tmp_path):
        bad_config = tmp_path / "bad.yml"
        bad_config.write_text("version: '1.0'\nskip_patterns: []\n")
        with pytest.raises(ValueError, match="max_reviewable_files"):
            load_config(str(bad_config))


# ---------------------------------------------------------------------------
# CLI Integration Test
# ---------------------------------------------------------------------------


class TestCLI:
    def test_cli_with_fixture(self, tmp_path):
        """Test gate_checker.py as a CLI tool."""
        output_file = tmp_path / "result.json"
        script = Path(__file__).resolve().parent.parent / "scripts" / "gate_checker.py"
        diff_file = FIXTURES_DIR / "sample_diff.patch"

        import subprocess

        result = subprocess.run(
            [
                sys.executable,
                str(script),
                "--diff", str(diff_file),
                "--config", str(CONFIG_PATH),
                "--output", str(output_file),
            ],
            capture_output=True,
            text=True,
        )

        assert result.returncode == 0, f"CLI failed: {result.stderr}"
        assert output_file.exists()

        data = json.loads(output_file.read_text())
        assert data["is_large_pr"] is False
        assert data["reviewable_count"] == 4

    def test_cli_with_labels(self, tmp_path):
        """Test CLI with --labels flag."""
        output_file = tmp_path / "result.json"
        script = Path(__file__).resolve().parent.parent / "scripts" / "gate_checker.py"
        diff_file = FIXTURES_DIR / "sample_diff.patch"

        import subprocess

        result = subprocess.run(
            [
                sys.executable,
                str(script),
                "--diff", str(diff_file),
                "--config", str(CONFIG_PATH),
                "--output", str(output_file),
                "--labels", "migration",
            ],
            capture_output=True,
            text=True,
        )

        assert result.returncode == 0, f"CLI failed: {result.stderr}"
        data = json.loads(output_file.read_text())
        assert data["is_large_pr"] is True
