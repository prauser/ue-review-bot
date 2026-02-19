#!/usr/bin/env python3
"""Token budget management for Stage 3 LLM reviewer.

Controls per-PR and per-file token budgets to prevent excessive API costs.
Provides utilities for estimating token usage and chunking diffs by hunk
boundaries.

Budget constants:
    BUDGET_PER_PR  = 100_000 input tokens
    BUDGET_PER_FILE = 20_000 input tokens
    COST_LIMIT_PER_PR = 2.00 USD
"""

from __future__ import annotations

import re
from typing import List

# ---------------------------------------------------------------------------
# Budget constants
# ---------------------------------------------------------------------------

BUDGET_PER_PR: int = 100_000  # max input tokens per PR
BUDGET_PER_FILE: int = 20_000  # max input tokens per file
COST_LIMIT_PER_PR: float = 2.00  # max USD per PR

# Approximate input token cost for claude-sonnet-4-5 ($3 per 1M input tokens).
_INPUT_COST_PER_TOKEN: float = 3.0 / 1_000_000
# Approximate output token cost ($15 per 1M output tokens).
_OUTPUT_COST_PER_TOKEN: float = 15.0 / 1_000_000
# Assumed average output tokens per file review call.
_ESTIMATED_OUTPUT_PER_FILE: int = 1_000
# Worst-case output tokens (matches DEFAULT_MAX_TOKENS in stage3_llm_reviewer).
_MAX_OUTPUT_PER_CALL: int = 4_096

# Skip patterns for files that should never reach Stage 3.
_SKIP_PATTERNS = [
    r"(^|/)ThirdParty/",
    r"\.(generated|gen)\.",
    r"\.generated\.h$",
    r"\.pb\.(h|cc)$",
    r"(^|/)Intermediate/",
]
_SKIP_RE = [re.compile(p, re.IGNORECASE) for p in _SKIP_PATTERNS]


def estimate_tokens(text: str) -> int:
    """Conservatively estimate token count for a text string.

    Uses ~3 characters per token as a conservative estimate (actual ratio
    is typically 3.5-4 for code).

    Args:
        text: Input text to estimate.

    Returns:
        Estimated token count.
    """
    return len(text) // 3


def estimate_cost(input_tokens: int, output_tokens: int = _ESTIMATED_OUTPUT_PER_FILE) -> float:
    """Estimate USD cost for an API call.

    Args:
        input_tokens: Number of input tokens.
        output_tokens: Number of output tokens (default: estimated average).

    Returns:
        Estimated cost in USD.
    """
    return (input_tokens * _INPUT_COST_PER_TOKEN) + (output_tokens * _OUTPUT_COST_PER_TOKEN)


def should_skip_file(file_path: str) -> bool:
    """Check if a file should be skipped by Stage 3.

    Defensive duplicate of gate filtering — catches auto-generated and
    third-party files that shouldn't consume LLM budget.

    Args:
        file_path: File path to check.

    Returns:
        True if the file should be skipped.
    """
    for pattern in _SKIP_RE:
        if pattern.search(file_path):
            return True
    return False


def chunk_diff(file_diff: str, max_tokens: int = BUDGET_PER_FILE) -> List[str]:
    """Split a file diff into chunks that fit within token budget.

    Splits on @@ hunk headers first.  If a single hunk exceeds
    ``max_tokens``, it is further split into smaller line-based chunks.

    Args:
        file_diff: Full unified diff text for a single file.
        max_tokens: Maximum tokens per chunk.

    Returns:
        List of diff text chunks, each within the token budget.
    """
    if estimate_tokens(file_diff) <= max_tokens:
        return [file_diff]

    # Split by hunk headers (@@ ... @@)
    hunk_pattern = re.compile(r"^(@@\s.*?@@.*)", re.MULTILINE)
    parts = hunk_pattern.split(file_diff)

    # parts[0] is the diff header (before first @@).
    # Then alternating: hunk_header, hunk_content, hunk_header, hunk_content ...
    header = parts[0] if parts else ""
    hunks: List[str] = []

    i = 1
    while i < len(parts):
        hunk_header = parts[i]
        hunk_body = parts[i + 1] if i + 1 < len(parts) else ""
        hunks.append(hunk_header + hunk_body)
        i += 2

    if not hunks:
        # No hunk headers found — split by lines as fallback.
        return _split_by_lines(file_diff, max_tokens)

    chunks: List[str] = []
    current = header

    for hunk in hunks:
        combined = current + hunk
        if estimate_tokens(combined) > max_tokens:
            if current.strip() and current != header:
                chunks.append(current)
            # If single hunk exceeds budget, split it further.
            if estimate_tokens(header + hunk) > max_tokens:
                # Extract @@ header line so every sub-chunk retains it.
                hunk_first_nl = hunk.find("\n")
                if hunk_first_nl != -1:
                    hunk_hdr_line = hunk[: hunk_first_nl]  # without newline
                    hunk_body = hunk[hunk_first_nl + 1 :]
                else:
                    hunk_hdr_line = hunk
                    hunk_body = ""

                # Parse original start lines from @@ header.
                hdr_match = re.match(
                    r"@@\s+-(\d+)(?:,\d+)?\s+\+(\d+)(?:,\d+)?\s+@@",
                    hunk_hdr_line,
                )
                old_start = int(hdr_match.group(1)) if hdr_match else 1
                new_start = int(hdr_match.group(2)) if hdr_match else 1

                # Estimate prefix token cost for budget calculation.
                sample_prefix = header + hunk_hdr_line + "\n"
                prefix_tokens = estimate_tokens(sample_prefix)
                body_budget = max(max_tokens - prefix_tokens, 100)
                sub_chunks = _split_by_lines(hunk_body, body_budget)

                # Rewrite @@ header per sub-chunk with correct line ranges.
                for sc in sub_chunks:
                    new_hdr = _rewrite_hunk_header(
                        hunk_hdr_line, old_start, new_start, sc,
                    )
                    chunks.append(header + new_hdr + "\n" + sc)
                    # Advance start lines for the next sub-chunk.
                    for ln in sc.split("\n"):
                        if ln.startswith("+"):
                            new_start += 1
                        elif ln.startswith("-"):
                            old_start += 1
                        elif ln:
                            old_start += 1
                            new_start += 1
                current = header
            else:
                current = header + hunk
        else:
            current = combined

    if current.strip() and current != header:
        chunks.append(current)

    return chunks if chunks else [file_diff]


def _rewrite_hunk_header(
    original_header: str,
    old_start: int,
    new_start: int,
    body: str,
) -> str:
    """Rewrite a ``@@ ... @@`` header to match *body*'s actual line counts.

    Counts context / addition / deletion lines in *body* and produces
    ``@@ -old_start,old_len +new_start,new_len @@`` (preserving any
    trailing function-name annotation from the original header).
    """
    old_len = 0
    new_len = 0
    for ln in body.split("\n"):
        if ln.startswith("+"):
            new_len += 1
        elif ln.startswith("-"):
            old_len += 1
        else:
            # context line (or empty line from split)
            if ln:  # skip truly empty trailing lines
                old_len += 1
                new_len += 1

    # Preserve trailing annotation after the closing @@, e.g. " funcName"
    m = re.match(r"@@\s[^@]*@@(.*)", original_header)
    annotation = m.group(1) if m else ""

    return f"@@ -{old_start},{old_len} +{new_start},{new_len} @@{annotation}"


def _split_by_lines(text: str, max_tokens: int) -> List[str]:
    """Split text into chunks by lines, keeping each under max_tokens.

    Each line is counted as at least 1 token (even if ``estimate_tokens``
    returns 0 for very short lines) plus 1 token for the newline separator
    so that many short lines don't accumulate into an oversized chunk.

    If a single line exceeds ``max_tokens``, it is further split by
    characters so that no chunk exceeds the budget.
    """
    lines = text.split("\n")
    chunks: List[str] = []
    current_lines: List[str] = []
    current_tokens = 0

    for line in lines:
        # Minimum 1 token per line + 1 for the newline join cost
        line_tokens = max(estimate_tokens(line), 1) + 1
        if current_tokens + line_tokens > max_tokens and current_lines:
            chunks.append("\n".join(current_lines))
            current_lines = []
            current_tokens = 0
        # Single line exceeds budget — split by characters.
        # Only the *first* fragment keeps the diff prefix (+/-/ ) so that
        # downstream hunk-header rewriting counts it as one logical line,
        # not N lines.  Continuation fragments are plain text.
        if line_tokens > max_tokens:
            prefix = ""
            content = line
            if line and line[0] in ("+", "-", " "):
                prefix = line[0]
                content = line[1:]
            chars_per_chunk = max(max_tokens * 3 - len(prefix), 1)
            for idx, start in enumerate(range(0, len(content), chars_per_chunk)):
                fragment = content[start : start + chars_per_chunk]
                chunks.append(prefix + fragment if idx == 0 else fragment)
            continue
        current_lines.append(line)
        current_tokens += line_tokens

    if current_lines:
        chunks.append("\n".join(current_lines))

    return chunks


class BudgetTracker:
    """Tracks cumulative token usage and cost across a PR review session.

    Usage:
        tracker = BudgetTracker()
        if tracker.can_review_file(estimated_tokens):
            # ... call API ...
            tracker.record_usage(input_tokens, output_tokens)
        else:
            # skip file — budget exhausted
    """

    def __init__(
        self,
        max_tokens: int = BUDGET_PER_PR,
        max_cost: float = COST_LIMIT_PER_PR,
    ) -> None:
        self.max_tokens = max_tokens
        self.max_cost = max_cost
        self.total_input_tokens = 0
        self.total_output_tokens = 0
        self.total_cost = 0.0
        self.files_reviewed = 0
        self.files_skipped_budget = 0

    def can_review_file(self, estimated_input_tokens: int) -> bool:
        """Check if there is enough budget remaining to review a file.

        Uses the worst-case output token limit (``_MAX_OUTPUT_PER_CALL``)
        for the cost check so that a single long response cannot exceed
        the cost cap.

        Args:
            estimated_input_tokens: Estimated input tokens for the file.

        Returns:
            True if the file can be reviewed within budget.
        """
        if self.total_input_tokens + estimated_input_tokens > self.max_tokens:
            return False
        estimated_cost = self.total_cost + estimate_cost(
            estimated_input_tokens, _MAX_OUTPUT_PER_CALL
        )
        if estimated_cost > self.max_cost:
            return False
        return True

    def record_usage(self, input_tokens: int, output_tokens: int) -> None:
        """Record actual token usage after an API call.

        Also increments ``files_reviewed`` — use for single-call file reviews.
        For chunked reviews, use :meth:`record_chunk_usage` per chunk and
        :meth:`record_file_reviewed` once after all chunks.

        Args:
            input_tokens: Actual input tokens used.
            output_tokens: Actual output tokens used.
        """
        self.total_input_tokens += input_tokens
        self.total_output_tokens += output_tokens
        self.total_cost += estimate_cost(input_tokens, output_tokens)
        self.files_reviewed += 1

    def record_chunk_usage(self, input_tokens: int, output_tokens: int) -> None:
        """Record token usage for a single chunk without incrementing file count.

        Args:
            input_tokens: Actual input tokens used.
            output_tokens: Actual output tokens used.
        """
        self.total_input_tokens += input_tokens
        self.total_output_tokens += output_tokens
        self.total_cost += estimate_cost(input_tokens, output_tokens)

    def record_file_reviewed(self) -> None:
        """Increment the file-reviewed counter by one."""
        self.files_reviewed += 1

    def record_skip(self) -> None:
        """Record that a file was skipped due to budget exhaustion."""
        self.files_skipped_budget += 1

    def summary(self) -> dict:
        """Return a summary dict of budget usage."""
        return {
            "total_input_tokens": self.total_input_tokens,
            "total_output_tokens": self.total_output_tokens,
            "total_cost_usd": round(self.total_cost, 4),
            "files_reviewed": self.files_reviewed,
            "files_skipped_budget": self.files_skipped_budget,
            "budget_remaining_tokens": self.max_tokens - self.total_input_tokens,
            "budget_remaining_usd": round(self.max_cost - self.total_cost, 4),
        }
