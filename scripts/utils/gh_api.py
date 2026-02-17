"""GitHub API utilities for PR metadata and review posting.

This module provides helpers to interact with GitHub (Enterprise Server)
for retrieving PR information and posting pull request reviews.

Used by:
- gate_checker.py: PR labels for large-PR classification
- post_review.py: Posting review comments with suggestions
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional


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


class GitHubClient:
    """HTTP client for GitHub REST API (supports GHES and github.com).

    Uses urllib to avoid external dependencies beyond the standard library.

    Args:
        token: Personal access token for authentication.
        api_url: Base API URL. For GHES: ``https://github.company.com/api/v3``.
                 For github.com: ``https://api.github.com``.
        max_retries: Maximum retry attempts for rate-limited or transient errors.
    """

    def __init__(
        self,
        token: str,
        api_url: str = "https://api.github.com",
        max_retries: int = 3,
    ) -> None:
        self.token = token
        self.api_url = api_url.rstrip("/")
        self.max_retries = max_retries

    def _request(
        self,
        method: str,
        path: str,
        body: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Make an authenticated API request with retry on transient errors.

        Args:
            method: HTTP method (GET, POST, PUT, etc.).
            path: API path (e.g., ``/repos/owner/repo/pulls/1/reviews``).
            body: Request body (will be JSON-encoded).

        Returns:
            Parsed JSON response.

        Raises:
            RuntimeError: If the request fails after retries.
        """
        url = f"{self.api_url}{path}"
        headers = {
            "Authorization": f"token {self.token}",
            "Accept": "application/vnd.github.v3+json",
            "Content-Type": "application/json",
        }

        data = json.dumps(body).encode("utf-8") if body else None

        last_error: Optional[Exception] = None
        for attempt in range(self.max_retries + 1):
            try:
                req = urllib.request.Request(
                    url, data=data, headers=headers, method=method
                )
                with urllib.request.urlopen(req, timeout=60) as resp:
                    resp_body = resp.read().decode("utf-8")
                    if resp_body:
                        return json.loads(resp_body)
                    return {}
            except urllib.error.HTTPError as e:
                last_error = e
                status = e.code
                # Rate limit (403/429) or server error (5xx) → retry
                if status in (403, 429) or status >= 500:
                    if attempt < self.max_retries:
                        wait = 2 ** (attempt + 1)
                        print(
                            f"GitHub API {status} on {method} {path}, "
                            f"retrying in {wait}s (attempt {attempt + 1})...",
                            file=sys.stderr,
                        )
                        time.sleep(wait)
                        continue
                # 422 Unprocessable Entity — GitHub validation error
                error_body = ""
                try:
                    error_body = e.read().decode("utf-8", errors="replace")
                except Exception:
                    pass
                raise RuntimeError(
                    f"GitHub API error {status} on {method} {path}: {error_body}"
                ) from e
            except (urllib.error.URLError, OSError) as e:
                last_error = e
                if attempt < self.max_retries:
                    wait = 2 ** (attempt + 1)
                    print(
                        f"Network error on {method} {path}, "
                        f"retrying in {wait}s (attempt {attempt + 1})...",
                        file=sys.stderr,
                    )
                    time.sleep(wait)
                    continue
                raise RuntimeError(
                    f"GitHub API network error on {method} {path}: {e}"
                ) from e

        raise RuntimeError(
            f"GitHub API request failed after {self.max_retries + 1} attempts: "
            f"{last_error}"
        )

    def create_review(
        self,
        owner: str,
        repo: str,
        pr_number: int,
        commit_sha: str,
        body: str,
        comments: List[Dict[str, Any]],
        event: str = "COMMENT",
    ) -> Dict[str, Any]:
        """Create a pull request review with inline comments.

        Uses the GitHub Pull Request Reviews API to post a single review
        containing multiple inline comments. Each comment can optionally
        include a suggestion block.

        API: POST /repos/{owner}/{repo}/pulls/{pull_number}/reviews

        Args:
            owner: Repository owner.
            repo: Repository name.
            pr_number: Pull request number.
            commit_sha: The SHA of the commit to review.
            body: Top-level review body (summary).
            comments: List of review comment dicts. Each must have:
                - ``path`` (str): File path relative to repo root.
                - ``line`` (int): Line number in the new file.
                - ``body`` (str): Comment body (markdown).
                Optionally:
                - ``start_line`` (int): Start of multi-line comment range.
                - ``side`` (str): ``RIGHT`` for new file (default).
                - ``start_side`` (str): Side for start_line.
            event: Review event type. One of ``COMMENT``, ``APPROVE``,
                   ``REQUEST_CHANGES``.

        Returns:
            API response dict.
        """
        path = f"/repos/{owner}/{repo}/pulls/{pr_number}/reviews"
        payload: Dict[str, Any] = {
            "commit_id": commit_sha,
            "body": body,
            "event": event,
            "comments": comments,
        }
        return self._request("POST", path, payload)

    def get_existing_review_comments(
        self,
        owner: str,
        repo: str,
        pr_number: int,
    ) -> List[Dict[str, Any]]:
        """Fetch existing review comments on a PR to avoid duplicates.

        API: GET /repos/{owner}/{repo}/pulls/{pull_number}/comments

        Returns:
            List of review comment dicts from the API.
        """
        path = f"/repos/{owner}/{repo}/pulls/{pr_number}/comments"
        result = self._request("GET", path)
        if isinstance(result, list):
            return result
        return []

    def get_pull_request(
        self,
        owner: str,
        repo: str,
        pr_number: int,
    ) -> Dict[str, Any]:
        """Fetch PR metadata (head SHA, etc.).

        API: GET /repos/{owner}/{repo}/pulls/{pull_number}

        Returns:
            PR metadata dict from the API.
        """
        path = f"/repos/{owner}/{repo}/pulls/{pr_number}"
        return self._request("GET", path)
