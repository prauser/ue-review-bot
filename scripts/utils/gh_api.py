"""GitHub API utilities for PR metadata retrieval.

This module provides helpers to fetch PR information from GitHub Enterprise Server.
Currently used by gate_checker.py to retrieve PR labels for large-PR classification.
"""

from __future__ import annotations

import json
import subprocess
from typing import List


def get_pr_labels(pr_number: int) -> List[str]:
    """Fetch labels for a given PR number using the gh CLI.

    Args:
        pr_number: The pull request number.

    Returns:
        A list of label name strings.

    Raises:
        RuntimeError: If the gh CLI command fails.
    """
    try:
        result = subprocess.run(
            ["gh", "pr", "view", str(pr_number), "--json", "labels", "--jq", ".labels[].name"],
            capture_output=True,
            text=True,
            check=True,
            timeout=30,
        )
        labels = [line.strip() for line in result.stdout.strip().splitlines() if line.strip()]
        return labels
    except FileNotFoundError:
        raise RuntimeError("gh CLI is not installed or not in PATH")
    except subprocess.CalledProcessError as e:
        raise RuntimeError(f"Failed to fetch PR labels: {e.stderr.strip()}")
    except subprocess.TimeoutExpired:
        raise RuntimeError("Timed out fetching PR labels")
