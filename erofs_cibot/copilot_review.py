from __future__ import annotations

import os
import subprocess


def request_review(
    *,
    gh_path: str,
    owner: str,
    repo: str,
    pull_number: int,
    token: str,
) -> None:
    env = os.environ.copy()
    env["GITHUB_TOKEN"] = token

    subprocess.run(
        [
            gh_path,
            "pr",
            "edit",
            str(pull_number),
            "--repo",
            f"{owner}/{repo}",
            "--add-reviewer",
            "@copilot",
        ],
        check=True,
        capture_output=True,
        text=True,
        env=env,
    )
