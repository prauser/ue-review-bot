#!/usr/bin/env python3
"""Stage 3 — LLM-based semantic code reviewer for UE5 C++ projects.

Uses the Anthropic API (Claude) to perform semantic code review covering:
- Items migrated from Stage 1 (coding conventions, file-level checks)
- clang-tidy fallback checks when compile_commands.json is absent
- GC safety, thread safety, networking, performance, UE5 patterns
- Design, comments, security

Each reviewable file is sent to the API individually with the system prompt
and diff context.  Findings from Stage 1/2 are excluded to avoid duplicates.

Usage:
    python -m scripts.stage3_llm_reviewer \\
        --diff pr.diff \\
        --checklist configs/checklist.yml \\
        --exclude-findings findings-stage1.json findings-stage2.json \\
        --has-compile-commands false \\
        --output findings-stage3.json

    # With source directory for full file context:
    python -m scripts.stage3_llm_reviewer \\
        --diff pr.diff \\
        --source-dir /path/to/repo \\
        --output findings-stage3.json
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.utils.diff_parser import parse_diff
from scripts.utils.token_budget import (
    BUDGET_PER_FILE,
    BudgetTracker,
    chunk_diff,
    estimate_tokens,
    should_skip_file,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Model configuration
# ---------------------------------------------------------------------------

DEFAULT_MODEL = "claude-sonnet-4-5-20250929"
DEFAULT_MAX_TOKENS = 4096
DEFAULT_TEMPERATURE = 0

# Retry configuration for rate limits / transient errors
MAX_RETRIES = 3
RETRY_BASE_DELAY = 2.0  # seconds

# C++ file extensions eligible for LLM review.
_CPP_EXTENSIONS = {".cpp", ".h", ".inl", ".hpp", ".cc", ".cxx", ".hxx"}

# ---------------------------------------------------------------------------
# System prompt
# ---------------------------------------------------------------------------

_SYSTEM_PROMPT_BASE = """\
당신은 UE5 C++ 시니어 코드 리뷰어입니다.

## Stage 1에서 이관된 검사 (반드시 체크)

### 코딩 컨벤션
- auto 사용 금지 (람다 함수를 변수에 담을 때만 허용)
- 요다 컨디션 금지: `if (false == bFlag)` → `if (bFlag == false)`
- 조건문에 !(not) 연산자 금지: `if (!bFlag)` → `if (bFlag == false)`
- Sandwich inequality 금지: `if (0 < x && x < 10)` → `if (x > 0 && x < 10)`
- FSimpleDelegateGraphTask 사용 금지

### 파일 단위 검사
- `#define LOCTEXT_NAMESPACE` 이후 `#undef` 누락
- ConstructorHelpers를 생성자 외부에서 사용
"""

_CLANG_TIDY_FALLBACK_SECTION = """
## clang-tidy 대체 검사 (compile_commands.json 없을 때 Stage 2 대신)

- override 키워드 누락
- 다형성 최상위 클래스 소멸자에 virtual 누락
- 생성자/소멸자에서 virtual 함수 호출
- 불필요한 복사 초기화
- range-for에서 불필요한 복사
- else-after-return (Guard Clause 스타일)
"""

_SYSTEM_PROMPT_LLM_ITEMS = """
## 기존 LLM 검토 항목

### GC 안전성
- UObject 파생 포인터 멤버에 UPROPERTY/TWeakObjectPtr 누락
- NewObject<> Outer가 nullptr
- USTRUCT 멤버 중 GC 대상인데 UPROPERTY 누락
- 순환 참조 가능성

### GameThread 안전성
- UObject를 다른 스레드에서 접근
- 람다 캡처에서 UObject raw pointer + 비동기 실행

### 네트워크 효율
- 불필요한 Replication, 매 Tick RPC, Reliable 남용
- DOREPLIFETIME 조건 미설정, 큰 구조체 RPC 파라미터

### 성능 (Tick / Memory / Hitch)
- 비어있는 Tick, 이벤트 기반 대체 가능, TickInterval 미설정
- 소량 데이터에 TMap, Reserve 없는 루프 Add, 풀링 미사용
- 런타임 동기 로딩, 런타임 Rigid Body 동적 생성

### UE5 패턴
- UCLASS 생성자에서 게임 로직, Transient 누락, DisableNativeTick 누락
- GetWorld() null 체크, UFUNCTION Category 누락

### 설계
- 캡슐화 위반, 과도한 함수 책임, Guard Clause 미사용, 중복 코드

### 주석
- 외부 수식 출처 누락, #todo-ovdr 태그 누락

### 보안
- 클라이언트 RPC 권한 검증 누락
"""

_SYSTEM_PROMPT_OUTPUT = """
## 출력 형식
JSON 배열만 반환. 이슈 없으면 [].

[{
  "file": "Source/MyActor.cpp",
  "line": 42,
  "end_line": 45,
  "severity": "warning|error|suggestion",
  "category": "convention|gc_safety|network|tick_perf|...",
  "message": "한국어 지적 메시지",
  "suggestion": "수정 코드 또는 null"
}]

## 규칙
- 확실하지 않은 지적은 하지 마세요.
- diff에 보이는 코드만으로 판단. 보이지 않는 코드 추측 금지.
- message는 한국어로, 이유 + 수정 방향 간결하게.
- Stage 1에서 이관된 항목은 반드시 빠짐없이 체크하세요.
"""


def build_system_prompt(has_compile_commands: bool) -> str:
    """Build the full system prompt for the LLM reviewer.

    Args:
        has_compile_commands: Whether compile_commands.json is available.
            If False, clang-tidy fallback checks are included.

    Returns:
        Complete system prompt string.
    """
    parts = [_SYSTEM_PROMPT_BASE]
    if not has_compile_commands:
        parts.append(_CLANG_TIDY_FALLBACK_SECTION)
    parts.append(_SYSTEM_PROMPT_LLM_ITEMS)
    parts.append(_SYSTEM_PROMPT_OUTPUT)
    return "\n".join(parts)


def build_user_message(
    file_path: str,
    diff_text: str,
    full_source: Optional[str] = None,
) -> str:
    """Build the user message for a single file review.

    Args:
        file_path: Path of the file being reviewed.
        diff_text: Unified diff text for this file.
        full_source: Optional full file source (provides more context).

    Returns:
        User message string.
    """
    parts = [f"## 파일: `{file_path}`\n"]

    if full_source is not None:
        parts.append("### 전체 소스\n```cpp\n")
        parts.append(full_source)
        parts.append("\n```\n")

    parts.append("### Diff (변경 사항)\n```diff\n")
    parts.append(diff_text)
    parts.append("\n```\n")

    parts.append("위 diff를 코드 리뷰하고 JSON 배열로 결과를 반환하세요.")
    return "\n".join(parts)


def load_exclude_findings(file_paths: List[str]) -> Set[Tuple[str, int]]:
    """Load findings from Stage 1/2 to exclude from Stage 3 review.

    Args:
        file_paths: Paths to JSON finding files from earlier stages.

    Returns:
        Set of (file, line) tuples to exclude.
    """
    excluded: Set[Tuple[str, int]] = set()

    for fp in file_paths:
        path = Path(fp)
        if not path.exists():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8", errors="replace"))
            if not isinstance(data, list):
                continue
            for finding in data:
                file = finding.get("file", "")
                try:
                    line = int(finding.get("line", 0))
                except (TypeError, ValueError):
                    continue
                if file and line > 0:
                    excluded.add((file, line))
        except (json.JSONDecodeError, OSError) as e:
            logger.warning("Failed to load exclude findings from %s: %s", fp, e)

    return excluded


def filter_excluded(
    findings: List[Dict[str, Any]],
    excluded: Set[Tuple[str, int]],
) -> List[Dict[str, Any]]:
    """Remove findings that overlap with Stage 1/2 results.

    Args:
        findings: Raw findings from LLM response.
        excluded: Set of (file, line) tuples from earlier stages.

    Returns:
        Filtered list of findings.
    """
    result = []
    for f in findings:
        file = f.get("file", "")
        try:
            line = int(f.get("line", 0))
        except (TypeError, ValueError):
            line = 0
        if (file, line) not in excluded:
            result.append(f)
    return result


def parse_llm_response(response_text: str) -> List[Dict[str, Any]]:
    """Parse the LLM response text into a list of findings.

    Handles cases where the response contains markdown code fences or
    extra text around the JSON array.  The parser tries multiple
    strategies to extract the JSON array robustly:

    1. Extract content inside markdown code fences first (highest priority).
    2. Iterate over every ``[`` position and attempt ``json.loads`` from
       there, guarding against false matches like ``[주의]``.

    Args:
        response_text: Raw text response from the LLM.

    Returns:
        List of finding dicts.  Empty list on parse failure.
    """
    text = response_text.strip()

    # Strategy 1: Extract content inside markdown code fences.
    fence_content = _extract_fenced_content(text)
    if fence_content is not None:
        result = _try_parse_json_array(fence_content)
        if result is not None:
            return result

    # Strategy 2: Try every '[' position to find a valid JSON array.
    # This avoids false matches like "[주의]" before the real array.
    pos = 0
    while True:
        start = text.find("[", pos)
        if start == -1:
            break
        try:
            data = json.loads(text[start:])
            if isinstance(data, list):
                return data
        except json.JSONDecodeError:
            pass
        # Also try with trailing text trimmed at the matching ']'
        end = text.rfind("]", start)
        if end > start:
            try:
                data = json.loads(text[start : end + 1])
                if isinstance(data, list):
                    return data
            except json.JSONDecodeError:
                pass
        pos = start + 1

    logger.warning("No JSON array found in LLM response")
    return []


def _extract_fenced_content(text: str) -> Optional[str]:
    """Extract text inside the first markdown code fence, if present."""
    if "```" not in text:
        return None
    lines = text.split("\n")
    inside = False
    content_lines: List[str] = []
    for line in lines:
        stripped = line.strip()
        if not inside and stripped.startswith("```"):
            inside = True
            continue
        if inside and stripped == "```":
            break
        if inside:
            content_lines.append(line)
    return "\n".join(content_lines).strip() if content_lines else None


def _try_parse_json_array(text: str) -> Optional[List[Dict[str, Any]]]:
    """Try to parse text as a JSON array. Returns None on failure."""
    text = text.strip()
    try:
        data = json.loads(text)
        if isinstance(data, list):
            return data
    except json.JSONDecodeError:
        pass
    return None


def validate_finding(finding: Dict[str, Any], file_path: str) -> Dict[str, Any]:
    """Normalize and validate a single finding from LLM output.

    Ensures required fields are present and types are correct.
    Sets the ``stage`` field to ``"stage3"`` and ``rule_id`` from ``category``.

    Args:
        finding: Raw finding dict from LLM.
        file_path: Expected file path (used as fallback).

    Returns:
        Normalized finding dict.
    """
    normalized: Dict[str, Any] = {}

    # File — use LLM-provided or fallback to expected file
    normalized["file"] = finding.get("file") or file_path

    # Line numbers — coerce to int
    try:
        normalized["line"] = int(finding.get("line", 0))
    except (TypeError, ValueError):
        normalized["line"] = 0

    end_line = finding.get("end_line")
    if end_line is not None:
        try:
            normalized["end_line"] = int(end_line)
        except (TypeError, ValueError):
            pass

    # Severity — validate against known values
    severity = finding.get("severity", "warning")
    if severity not in ("error", "warning", "info", "suggestion"):
        severity = "warning"
    normalized["severity"] = severity

    # Category / rule_id
    category = finding.get("category", "general")
    normalized["category"] = category
    normalized["rule_id"] = category  # post_review uses rule_id or category

    # Message
    normalized["message"] = finding.get("message", "")

    # Suggestion (optional)
    suggestion = finding.get("suggestion")
    if suggestion:
        normalized["suggestion"] = suggestion

    # Stage tag
    normalized["stage"] = "stage3"

    return normalized


def call_anthropic_api(
    system_prompt: str,
    user_message: str,
    *,
    model: str = DEFAULT_MODEL,
    max_tokens: int = DEFAULT_MAX_TOKENS,
    temperature: int = DEFAULT_TEMPERATURE,
    api_key: Optional[str] = None,
    api_url: Optional[str] = None,
) -> Tuple[str, int, int]:
    """Call the Anthropic Messages API.

    Args:
        system_prompt: System message content.
        user_message: User message content.
        model: Model ID.
        max_tokens: Max output tokens.
        temperature: Sampling temperature.
        api_key: API key (defaults to ANTHROPIC_API_KEY env var).
        api_url: Optional base URL override.

    Returns:
        Tuple of (response_text, input_tokens, output_tokens).

    Raises:
        RuntimeError: On API errors after retries are exhausted.
    """
    import urllib.error
    import urllib.request

    key = api_key or os.environ.get("ANTHROPIC_API_KEY", "")
    if not key:
        raise RuntimeError(
            "ANTHROPIC_API_KEY environment variable is not set. "
            "Set it or pass --api-key."
        )

    base_url = (api_url or "https://api.anthropic.com").rstrip("/")
    url = f"{base_url}/v1/messages"

    payload = {
        "model": model,
        "max_tokens": max_tokens,
        "temperature": temperature,
        "system": system_prompt,
        "messages": [{"role": "user", "content": user_message}],
    }

    headers = {
        "Content-Type": "application/json",
        "x-api-key": key,
        "anthropic-version": "2023-06-01",
    }

    data = json.dumps(payload).encode("utf-8")

    last_error: Optional[Exception] = None
    for attempt in range(MAX_RETRIES + 1):
        try:
            req = urllib.request.Request(url, data=data, headers=headers, method="POST")
            with urllib.request.urlopen(req, timeout=120) as resp:
                body = json.loads(resp.read().decode("utf-8"))

            # Extract text from response
            text = ""
            for block in body.get("content", []):
                if block.get("type") == "text":
                    text += block.get("text", "")

            usage = body.get("usage", {})
            input_tokens = usage.get("input_tokens", 0)
            output_tokens = usage.get("output_tokens", 0)

            return text, input_tokens, output_tokens

        except urllib.error.HTTPError as e:
            last_error = e
            status = e.code
            # Rate limit (429) or server error (5xx) — retry
            if status == 429 or status >= 500:
                if attempt < MAX_RETRIES:
                    delay = RETRY_BASE_DELAY * (2**attempt)
                    logger.warning(
                        "API error %d on attempt %d, retrying in %.1fs...",
                        status,
                        attempt + 1,
                        delay,
                    )
                    time.sleep(delay)
                    continue
            # Client error — don't retry
            error_body = ""
            try:
                error_body = e.read().decode("utf-8", errors="replace")
            except Exception:
                pass
            raise RuntimeError(
                f"Anthropic API error {status}: {error_body}"
            ) from e

        except (urllib.error.URLError, OSError) as e:
            last_error = e
            if attempt < MAX_RETRIES:
                delay = RETRY_BASE_DELAY * (2**attempt)
                logger.warning(
                    "Network error on attempt %d, retrying in %.1fs: %s",
                    attempt + 1,
                    delay,
                    e,
                )
                time.sleep(delay)
                continue
            raise RuntimeError(f"Network error after {MAX_RETRIES + 1} attempts") from e

    raise RuntimeError(f"API call failed after {MAX_RETRIES + 1} attempts") from last_error


def review_file(
    file_path: str,
    diff_text: str,
    system_prompt: str,
    excluded: Set[Tuple[str, int]],
    budget: BudgetTracker,
    *,
    full_source: Optional[str] = None,
    model: str = DEFAULT_MODEL,
    api_key: Optional[str] = None,
    api_url: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Review a single file using the LLM.

    Args:
        file_path: Path of the file.
        diff_text: Unified diff text for the file.
        system_prompt: System prompt to use.
        excluded: Set of (file, line) tuples from earlier stages.
        budget: Budget tracker instance.
        full_source: Optional full file source.
        model: Model ID.
        api_key: API key.
        api_url: API base URL.

    Returns:
        List of validated findings for this file.
    """
    # Check budget
    user_msg = build_user_message(file_path, diff_text, full_source)
    system_tokens = estimate_tokens(system_prompt)
    user_tokens = estimate_tokens(user_msg)
    total_input = system_tokens + user_tokens

    if total_input > BUDGET_PER_FILE:
        # Try chunking the diff.
        # Estimate wrapper overhead: build_user_message adds file header, code
        # fences, and instruction text around the diff content.  Measure this
        # with an empty diff so we subtract it from the per-chunk budget.
        wrapper_overhead = estimate_tokens(build_user_message(file_path, ""))
        chunk_budget = max(BUDGET_PER_FILE - system_tokens - wrapper_overhead, 1000)
        chunks = chunk_diff(diff_text, chunk_budget)
        all_findings: List[Dict[str, Any]] = []
        for i, chunk in enumerate(chunks):
            # Include full source only in first chunk, and only if it fits
            chunk_source = full_source if i == 0 else None
            if chunk_source is not None:
                tentative_msg = build_user_message(file_path, chunk, chunk_source)
                if system_tokens + estimate_tokens(tentative_msg) > BUDGET_PER_FILE:
                    # Full source too large — drop it to stay within per-file limit
                    chunk_source = None
            chunk_msg = build_user_message(file_path, chunk, chunk_source)
            chunk_tokens = system_tokens + estimate_tokens(chunk_msg)
            if chunk_tokens > BUDGET_PER_FILE:
                logger.warning(
                    "Chunk %d for %s exceeds per-file budget (%d > %d), skipping",
                    i, file_path, chunk_tokens, BUDGET_PER_FILE,
                )
                continue
            if not budget.can_review_file(chunk_tokens):
                logger.warning(
                    "Budget exhausted, skipping remaining chunks for %s", file_path
                )
                budget.record_skip()
                break
            try:
                resp_text, actual_input, actual_output = call_anthropic_api(
                    system_prompt,
                    chunk_msg,
                    model=model,
                    api_key=api_key,
                    api_url=api_url,
                )
                budget.record_usage(actual_input, actual_output)
                findings = parse_llm_response(resp_text)
                findings = [validate_finding(f, file_path) for f in findings if isinstance(f, dict)]
                findings = filter_excluded(findings, excluded)
                all_findings.extend(findings)
            except RuntimeError as e:
                logger.error("API error reviewing %s chunk %d: %s", file_path, i, e)
        return all_findings

    if not budget.can_review_file(total_input):
        logger.warning("Budget exhausted, skipping file: %s", file_path)
        budget.record_skip()
        return []

    try:
        resp_text, actual_input, actual_output = call_anthropic_api(
            system_prompt,
            user_msg,
            model=model,
            api_key=api_key,
            api_url=api_url,
        )
        budget.record_usage(actual_input, actual_output)
    except RuntimeError as e:
        logger.error("API error reviewing %s: %s", file_path, e)
        return []

    findings = parse_llm_response(resp_text)
    findings = [validate_finding(f, file_path) for f in findings if isinstance(f, dict)]
    findings = filter_excluded(findings, excluded)

    return findings


def review_pr(
    diff_text: str,
    *,
    has_compile_commands: bool = False,
    exclude_files: Optional[List[str]] = None,
    source_dir: Optional[str] = None,
    model: str = DEFAULT_MODEL,
    api_key: Optional[str] = None,
    api_url: Optional[str] = None,
) -> Tuple[List[Dict[str, Any]], dict]:
    """Review all files in a PR diff.

    Args:
        diff_text: Full PR unified diff text.
        has_compile_commands: Whether compile_commands.json exists.
        exclude_files: Paths to Stage 1/2 finding JSON files.
        source_dir: Path to the source directory for full file context.
        model: Model ID.
        api_key: API key.
        api_url: API base URL.

    Returns:
        Tuple of (all_findings, budget_summary).
    """
    system_prompt = build_system_prompt(has_compile_commands)
    excluded = load_exclude_findings(exclude_files or [])
    budget = BudgetTracker()

    parsed = parse_diff(diff_text)
    all_findings: List[Dict[str, Any]] = []

    for file_path, file_diff in sorted(parsed.items()):
        # Skip non-C++ files
        ext = Path(file_path).suffix.lower()
        if ext not in _CPP_EXTENSIONS:
            continue

        # Skip auto-generated / third-party files
        if should_skip_file(file_path):
            continue

        # Reconstruct diff text for this file
        file_diff_text = _reconstruct_file_diff(file_diff)
        if not file_diff_text.strip():
            continue

        # Try to load full source
        full_source = None
        if source_dir:
            source_path = Path(source_dir) / file_path
            if source_path.exists():
                try:
                    full_source = source_path.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    pass

        findings = review_file(
            file_path,
            file_diff_text,
            system_prompt,
            excluded,
            budget,
            full_source=full_source,
            model=model,
            api_key=api_key,
            api_url=api_url,
        )
        all_findings.extend(findings)

    return all_findings, budget.summary()


def _reconstruct_file_diff(file_diff) -> str:
    """Reconstruct a unified diff text from a FileDiff object.

    Rebuilds ``@@ ... @@`` hunk headers from the stored start/end
    metadata so that the LLM receives line-number context.

    Args:
        file_diff: FileDiff dataclass instance from diff_parser.

    Returns:
        Unified diff text string showing hunks with headers.
    """
    lines = []
    for hunk in file_diff.hunks:
        content = hunk.get("content", "")
        if not content:
            continue
        start = hunk.get("start", 0)
        end = hunk.get("end", start)
        length = end - start + 1 if end >= start else 1
        # Reconstruct @@ header so the LLM knows the line range.
        lines.append(f"@@ -{start},{length} +{start},{length} @@")
        lines.append(content)
    return "\n".join(lines)


def main(argv: Optional[List[str]] = None) -> int:
    """CLI entry point."""
    parser = argparse.ArgumentParser(
        description="Stage 3 — LLM semantic code reviewer for UE5 C++",
    )
    parser.add_argument(
        "--diff",
        required=True,
        help="Path to the PR unified diff file",
    )
    parser.add_argument(
        "--checklist",
        default="configs/checklist.yml",
        help="Path to checklist.yml (currently unused but reserved)",
    )
    parser.add_argument(
        "--exclude-findings",
        nargs="*",
        default=[],
        help="Paths to Stage 1/2 finding JSON files for deduplication",
    )
    parser.add_argument(
        "--has-compile-commands",
        type=str,
        default="false",
        help="Whether compile_commands.json exists (true/false)",
    )
    parser.add_argument(
        "--source-dir",
        help="Path to source directory for full file context",
    )
    parser.add_argument(
        "--output",
        default="findings-stage3.json",
        help="Output JSON file path",
    )
    parser.add_argument(
        "--model",
        default=DEFAULT_MODEL,
        help=f"Anthropic model ID (default: {DEFAULT_MODEL})",
    )
    parser.add_argument(
        "--api-key",
        help="Anthropic API key (defaults to ANTHROPIC_API_KEY env var)",
    )
    parser.add_argument(
        "--api-url",
        help="Anthropic API base URL override",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print system prompt and exit without API calls",
    )

    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(levelname)s: %(message)s",
    )

    has_cc = args.has_compile_commands.lower() in ("true", "1", "yes")

    if args.dry_run:
        prompt = build_system_prompt(has_cc)
        print("=== System Prompt ===")
        print(prompt)
        print(f"\n=== Prompt tokens (estimated): {estimate_tokens(prompt)} ===")
        return 0

    # Load diff
    diff_path = Path(args.diff)
    if not diff_path.exists():
        print(f"Error: Diff file not found: {args.diff}", file=sys.stderr)
        return 1

    diff_text = diff_path.read_text(encoding="utf-8", errors="replace")

    findings, budget_summary = review_pr(
        diff_text,
        has_compile_commands=has_cc,
        exclude_files=args.exclude_findings,
        source_dir=args.source_dir,
        model=args.model,
        api_key=args.api_key,
        api_url=args.api_url,
    )

    # Write output
    output = {
        "findings": findings,
        "budget": budget_summary,
        "total_findings": len(findings),
    }

    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    # Write findings array (compatible with post_review.py load_findings)
    output_path.write_text(
        json.dumps(findings, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    # Write budget summary to a separate file
    budget_path = output_path.with_suffix(".budget.json")
    budget_path.write_text(
        json.dumps(output, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    logger.info(
        "Stage 3 complete: %d findings, %d files reviewed, %d files skipped (budget)",
        len(findings),
        budget_summary["files_reviewed"],
        budget_summary["files_skipped_budget"],
    )
    logger.info(
        "Budget: %d/%d tokens used, $%.4f/$%.2f spent",
        budget_summary["total_input_tokens"],
        budget_summary["total_input_tokens"] + budget_summary["budget_remaining_tokens"],
        budget_summary["total_cost_usd"],
        budget_summary["total_cost_usd"] + budget_summary["budget_remaining_usd"],
    )

    return 0


if __name__ == "__main__":
    sys.exit(main())
