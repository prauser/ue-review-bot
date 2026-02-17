#!/usr/bin/env python3
"""Stage 2 — clang-tidy fixes YAML → suggestion/comment converter.

Parses the YAML output of ``clang-tidy --export-fixes`` and converts each
diagnostic into a finding dict compatible with Stage 1 output format.

* Diagnostics with a replacement fix → suggestion block
* Diagnostics without a fix → regular comment
* Deduplicates against Stage 1 results (same file + line → skip)

Usage:
    python -m scripts.stage2_tidy_to_suggestions \\
        --tidy-fixes fixes.yaml \\
        --stage1-results findings-stage1.json \\
        --output findings-stage2.json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

import yaml

# ---------------------------------------------------------------------------
# clang-tidy --export-fixes YAML schema (abbreviated):
#
#   MainSourceFile: /path/to/source.cpp
#   Diagnostics:
#     - DiagnosticName: modernize-use-override
#       DiagnosticMessage:
#         Message: "annotate this function with 'override'..."
#         FilePath: /path/to/source.cpp
#         FileOffset: 1234
#         Replacements:
#           - FilePath: /path/to/source.cpp
#             Offset: 1234
#             Length: 0
#             ReplacementText: ' override'
#       Level: Warning
#       BuildDirectory: /path/to/build
#
# When there are no replacements the Replacements key may be absent or [].
# ---------------------------------------------------------------------------

# Mapping from clang-tidy check names to checklist.yml rule_ids.
# Diagnostics not in this map use the clang-tidy check name as rule_id.
_CHECK_TO_RULE: Dict[str, str] = {
    "modernize-use-override": "override_keyword",
    "cppcoreguidelines-virtual-class-destructor": "virtual_destructor",
    "performance-for-range-copy": "unnecessary_copy",
    "performance-unnecessary-copy-initialization": "unnecessary_copy",
}

# Severity mapping: clang-tidy Level → output severity.
# clang-tidy levels: Error, Warning, Note, Remark
_LEVEL_TO_SEVERITY: Dict[str, str] = {
    "Error": "error",
    "Warning": "warning",
    "Note": "info",
    "Remark": "info",
}


def _resolve_path(file_path: str, build_dir: Optional[str] = None) -> str:
    """Normalise an absolute file path to a relative project path.

    clang-tidy emits absolute paths in the YAML.  We strip common build
    directory prefixes and return a forward-slash relative path suitable
    for matching against diff file paths.
    """
    p = Path(file_path)
    # Try to make relative to build_dir first, then to cwd
    if build_dir:
        try:
            return str(p.relative_to(build_dir))
        except ValueError:
            pass
    try:
        return str(p.relative_to(Path.cwd()))
    except ValueError:
        # Already relative or unresolvable — return as-is
        return str(p)


def _offset_to_line(content: str, offset: int) -> int:
    """Convert a byte offset to a 1-based line number.

    clang-tidy emits byte offsets, so we encode to UTF-8 first to get
    accurate line numbers when the file contains multi-byte characters.
    """
    raw = content.encode("utf-8")
    # Clamp to valid range
    offset = min(offset, len(raw))
    return raw[:offset].count(b"\n") + 1


def _apply_replacements(
    content: str,
    replacements: List[Dict[str, Any]],
    target_file: str,
) -> Optional[str]:
    """Apply clang-tidy replacements to source content and return the
    modified text.

    Only processes replacements that target *target_file*.  Returns the
    full modified source text, or ``None`` if no applicable replacements.

    clang-tidy Offset/Length are byte-based, so we operate on the UTF-8
    encoded bytes and decode back to str afterwards.  This ensures correct
    behaviour when source files contain multi-byte characters (e.g. CJK
    comments, Unicode literals).

    When multiple replacements target different offsets we apply them
    back-to-front (highest offset first) so earlier offsets remain valid.
    """
    applicable = [
        r for r in replacements
        if _normalise(r.get("FilePath", "")) == _normalise(target_file)
    ]
    if not applicable:
        return None

    # Sort by offset descending for safe in-place replacement
    applicable.sort(key=lambda r: r.get("Offset", 0), reverse=True)

    # Work in bytes — clang-tidy offsets are byte-based
    modified = content.encode("utf-8")
    for repl in applicable:
        offset = repl.get("Offset", 0)
        length = repl.get("Length", 0)
        text = repl.get("ReplacementText", "").encode("utf-8")
        modified = modified[:offset] + text + modified[offset + length:]

    return modified.decode("utf-8", errors="replace")


def _normalise(path: str) -> str:
    """Normalise a path for comparison (resolve, lower on Windows)."""
    try:
        return str(Path(path).resolve())
    except (OSError, ValueError):
        return path


def parse_tidy_fixes(
    fixes_path: str,
) -> List[Dict[str, Any]]:
    """Parse a clang-tidy ``--export-fixes`` YAML file.

    Args:
        fixes_path: Path to the fixes YAML file.

    Returns:
        List of raw diagnostic dicts extracted from the YAML.
        Empty list if the file is missing, empty, or invalid.
    """
    path = Path(fixes_path)
    if not path.exists():
        return []

    text = path.read_text(encoding="utf-8", errors="replace")
    if not text.strip():
        return []

    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError:
        return []

    if not isinstance(data, dict):
        return []

    return data.get("Diagnostics", []) or []


def _extract_suggestion_span(
    original: str,
    modified: str,
) -> Tuple[Optional[str], int, Optional[int]]:
    """Compare original and modified source to extract the changed span.

    Returns:
        (suggestion_text, start_line_1based, end_line_1based_or_None)
        - suggestion_text: the replacement text (modified lines in the changed
          range), or None if no difference is found.
        - start_line: 1-based first changed line in the *original* file.
        - end_line: 1-based last changed line in the *original* file,
          or None if the change is single-line.
    """
    orig_lines = original.splitlines()
    mod_lines = modified.splitlines()

    # Find first differing line (from top)
    first_diff = 0
    min_len = min(len(orig_lines), len(mod_lines))
    while first_diff < min_len and orig_lines[first_diff] == mod_lines[first_diff]:
        first_diff += 1

    if first_diff == len(orig_lines) == len(mod_lines):
        return None, 1, None  # No difference

    # Find last differing line (from bottom)
    last_orig = len(orig_lines) - 1
    last_mod = len(mod_lines) - 1
    while (
        last_orig > first_diff
        and last_mod > first_diff
        and orig_lines[last_orig] == mod_lines[last_mod]
    ):
        last_orig -= 1
        last_mod -= 1

    # Extract suggestion text (modified lines in the changed range)
    suggestion_lines = mod_lines[first_diff : last_mod + 1]
    suggestion = "\n".join(suggestion_lines)

    # Convert to 1-based line numbers (original file's lines)
    start_line = first_diff + 1
    end_line = last_orig + 1

    if start_line == end_line:
        return suggestion, start_line, None  # single line
    return suggestion, start_line, end_line


def convert_diagnostics(
    diagnostics: List[Dict[str, Any]],
    source_contents: Optional[Dict[str, str]] = None,
    build_dir: Optional[str] = None,
    path_map: Optional[Dict[str, str]] = None,
) -> List[Dict[str, Any]]:
    """Convert clang-tidy diagnostics to Stage-compatible findings.

    Args:
        diagnostics: Raw diagnostics from parse_tidy_fixes().
        source_contents: Optional mapping of absolute file paths to their
            contents.  Required for generating line numbers from byte
            offsets and for producing suggestion text.  When absent,
            offset-to-line conversion falls back to offset // 80.
        build_dir: Optional build directory for path resolution.
        path_map: Optional mapping of original file paths to repo-relative
            paths, as returned by _collect_source_contents().  When
            provided, these paths are preferred over _resolve_path() so
            that findings use the same repo-relative paths as Stage 1.

    Returns:
        List of finding dicts with file, line, rule_id, severity,
        message, and optional suggestion.
    """
    findings: List[Dict[str, Any]] = []

    for diag in diagnostics:
        if not isinstance(diag, dict):
            continue

        check_name = diag.get("DiagnosticName", "")
        msg_info = diag.get("DiagnosticMessage", {})
        if not isinstance(msg_info, dict):
            continue

        message = msg_info.get("Message", "")
        file_path = msg_info.get("FilePath", "")
        file_offset = msg_info.get("FileOffset", 0)
        replacements = msg_info.get("Replacements") or []

        # Resolve level → severity
        level = diag.get("Level", "Warning")
        severity = _LEVEL_TO_SEVERITY.get(level, "warning")

        # Resolve file path — prefer path_map (source_dir-resolved) over
        # _resolve_path to ensure repo-relative paths match Stage 1 / diff.
        if path_map and file_path in path_map:
            rel_path = path_map[file_path]
        else:
            rel_path = _resolve_path(file_path, build_dir)

        # Compute line number
        if source_contents and file_path in source_contents:
            line_num = _offset_to_line(source_contents[file_path], file_offset)
        elif source_contents:
            # Try with resolved path
            for src_path, content in source_contents.items():
                if _normalise(src_path) == _normalise(file_path):
                    line_num = _offset_to_line(content, file_offset)
                    break
            else:
                # Rough estimate: ~80 chars per line
                line_num = max(1, file_offset // 80 + 1)
        else:
            line_num = max(1, file_offset // 80 + 1)

        # Map check name to rule_id
        rule_id = _CHECK_TO_RULE.get(check_name, check_name)

        # Generate suggestion from replacements if available
        suggestion = None
        end_line = None
        if replacements and source_contents:
            abs_path = file_path
            content = source_contents.get(abs_path)
            if content is None:
                for src_path, src_content in source_contents.items():
                    if _normalise(src_path) == _normalise(abs_path):
                        content = src_content
                        break

            if content is not None:
                modified = _apply_replacements(content, replacements, abs_path)
                if modified is not None and modified != content:
                    suggestion, span_start, span_end = _extract_suggestion_span(
                        content, modified
                    )
                    # Use the span's line numbers instead of the diagnostic
                    # offset — the actual changed range may differ.
                    line_num = span_start
                    end_line = span_end

        finding: Dict[str, Any] = {
            "file": rel_path,
            "line": line_num,
            "rule_id": rule_id,
            "severity": severity,
            "message": message,
            "suggestion": suggestion,
        }
        if end_line is not None:
            finding["end_line"] = end_line
        findings.append(finding)

    return findings


def deduplicate(
    stage2_findings: List[Dict[str, Any]],
    stage1_findings: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Remove Stage 2 findings that overlap with Stage 1.

    A Stage 2 finding is considered a duplicate if Stage 1 already has
    a finding on the same file + line.

    Args:
        stage2_findings: Findings from Stage 2 (this module).
        stage1_findings: Findings from Stage 1 (pattern checker).

    Returns:
        Filtered Stage 2 findings with duplicates removed.
    """
    stage1_keys: Set[Tuple[str, int]] = set()
    for f in stage1_findings:
        stage1_keys.add((f.get("file", ""), f.get("line", 0)))

    return [
        f for f in stage2_findings
        if (f.get("file", ""), f.get("line", 0)) not in stage1_keys
    ]


def _collect_source_contents(
    diagnostics: List[Dict[str, Any]],
    source_dir: Optional[str] = None,
) -> Tuple[Dict[str, str], Dict[str, str]]:
    """Read source files referenced in diagnostics.

    Collects unique file paths from diagnostics and reads their contents.
    Tries absolute paths first, then falls back to source_dir-relative paths.

    Args:
        diagnostics: Raw diagnostics from parse_tidy_fixes().
        source_dir: Optional base directory to resolve relative paths.

    Returns:
        A tuple of (contents, path_map):
        - contents: mapping of original file paths to their text contents.
        - path_map: mapping of original file paths to repo-relative paths,
          populated when source_dir suffix-matching is used. Downstream code
          should prefer ``path_map[fp]`` over ``_resolve_path(fp)`` so that
          findings use the same repo-relative paths as Stage 1 / git diff.
    """
    contents: Dict[str, str] = {}
    path_map: Dict[str, str] = {}
    seen_paths: set = set()

    for diag in diagnostics:
        if not isinstance(diag, dict):
            continue
        msg_info = diag.get("DiagnosticMessage", {})
        if not isinstance(msg_info, dict):
            continue

        file_path = msg_info.get("FilePath", "")
        if not file_path or file_path in seen_paths:
            continue
        seen_paths.add(file_path)

        # Also collect paths from replacements
        for repl in msg_info.get("Replacements") or []:
            repl_path = repl.get("FilePath", "")
            if repl_path:
                seen_paths.add(repl_path)

    for file_path in seen_paths:
        if not file_path:
            continue
        p = Path(file_path)
        # Try absolute path first
        if p.is_file():
            try:
                contents[file_path] = p.read_text(encoding="utf-8", errors="replace")
                continue
            except OSError:
                pass
        # Try relative to source_dir
        if source_dir:
            sd = Path(source_dir)
            # Build (relative_suffix, candidate_path) pairs so we can record
            # which suffix matched → becomes the repo-relative path.
            # For absolute paths, try longest suffix first so that
            # Source/A.cpp is preferred over A.cpp when both exist.
            candidates: List[Tuple[str, Path]] = []
            if p.is_absolute():
                parts = p.parts[1:]  # drop root '/'
                for i in range(len(parts)):
                    suffix = str(Path(*parts[i:]))
                    candidates.append((suffix, sd / suffix))
            else:
                candidates.append((str(p), sd / p))
                candidates.append((p.name, sd / p.name))
            # Deduplicate while preserving order
            seen_candidates: set = set()
            for rel_suffix, candidate in candidates:
                resolved = str(candidate)
                if resolved in seen_candidates:
                    continue
                seen_candidates.add(resolved)
                if candidate.is_file():
                    try:
                        contents[file_path] = candidate.read_text(
                            encoding="utf-8", errors="replace"
                        )
                        path_map[file_path] = rel_suffix
                        break
                    except OSError:
                        pass

    return contents, path_map


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Stage 2 — clang-tidy fixes → suggestion converter"
    )
    parser.add_argument(
        "--tidy-fixes",
        required=True,
        help="Path to clang-tidy --export-fixes YAML file",
    )
    parser.add_argument(
        "--stage1-results",
        default=None,
        help="Path to Stage 1 findings JSON (for deduplication)",
    )
    parser.add_argument(
        "--pvs-report",
        default=None,
        help="Path to PVS-Studio report JSON (placeholder — not yet implemented)",
    )
    parser.add_argument(
        "--source-dir",
        default=None,
        help="Base directory for resolving source file paths (for suggestion generation)",
    )
    parser.add_argument(
        "--output",
        default=None,
        help="Output JSON file path (default: stdout)",
    )

    args = parser.parse_args()

    # PVS-Studio placeholder
    if args.pvs_report:
        print(
            "Warning: --pvs-report is not yet implemented. Ignoring.",
            file=sys.stderr,
        )

    # Parse clang-tidy fixes
    diagnostics = parse_tidy_fixes(args.tidy_fixes)

    # Load source files for accurate line numbers and suggestion generation
    source_contents, path_map = _collect_source_contents(diagnostics, args.source_dir)
    if source_contents:
        print(
            f"Loaded {len(source_contents)} source file(s) for suggestion generation.",
            file=sys.stderr,
        )
    elif diagnostics:
        print(
            "Warning: Could not load any source files. "
            "Line numbers will use offset//80 fallback and suggestions will be empty. "
            "Use --source-dir to specify the project root.",
            file=sys.stderr,
        )

    findings = convert_diagnostics(
        diagnostics,
        source_contents=source_contents,
        path_map=path_map,
    )

    # Deduplicate against Stage 1
    if args.stage1_results:
        s1_path = Path(args.stage1_results)
        if s1_path.exists():
            stage1 = json.loads(s1_path.read_text(encoding="utf-8"))
            findings = deduplicate(findings, stage1)
        else:
            print(
                f"Warning: Stage 1 results not found: {args.stage1_results}",
                file=sys.stderr,
            )

    # Output
    output_json = json.dumps(findings, ensure_ascii=False, indent=2)

    if args.output:
        output_path = Path(args.output)
        output_path.write_text(output_json + "\n", encoding="utf-8")
        print(
            f"Stage 2 findings: {len(findings)} issues found. "
            f"Written to: {args.output}"
        )
    else:
        print(output_json)

    sys.exit(0)


if __name__ == "__main__":
    main()
