from __future__ import annotations

import base64
import hashlib
import re
import subprocess
from pathlib import Path


def _auth_header(token: str) -> str:
    encoded = base64.b64encode(f"x-access-token:{token}".encode()).decode()
    return f"Authorization: basic {encoded}"


def run_git(
    repo: Path,
    token: str | None,
    *args: str,
    input_text: str | None = None,
) -> str:
    cmd = ["git"]
    if token:
        cmd.extend(["-c", f"http.extraheader={_auth_header(token)}"])
    cmd.extend(args)

    completed = subprocess.run(
        cmd,
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
        input=input_text,
    )
    return completed.stdout.strip()


def clone_or_fetch_repo(
    repo_dir: Path,
    *,
    owner: str,
    repo: str,
    base_branch: str,
    token: str,
    user_name: str,
    user_email: str,
    clone_depth: int,
) -> None:
    remote_url = f"https://github.com/{owner}/{repo}.git"
    repo_dir.parent.mkdir(parents=True, exist_ok=True)

    if (repo_dir / ".git").exists():
        run_git(
            repo_dir,
            token,
            "fetch",
            "--depth",
            str(clone_depth),
            "origin",
            f"+refs/heads/{base_branch}:refs/remotes/origin/{base_branch}",
        )
        try:
            run_git(repo_dir, token, "checkout", base_branch)
        except subprocess.CalledProcessError:
            run_git(repo_dir, token, "checkout", "-b", base_branch, f"origin/{base_branch}")
    else:
        subprocess.run(
            [
                "git",
                "-c",
                f"http.extraheader={_auth_header(token)}",
                "clone",
                f"--depth={clone_depth}",
                "--branch",
                base_branch,
                "--single-branch",
                remote_url,
                str(repo_dir),
            ],
            check=True,
            text=True,
        )

    run_git(repo_dir, token, "config", "user.name", user_name)
    run_git(repo_dir, token, "config", "user.email", user_email)
    reset_repo(repo_dir, token=token, base_branch=base_branch)


def reset_repo(repo_dir: Path, *, token: str | None, base_branch: str) -> None:
    try:
        run_git(repo_dir, token, "am", "--abort")
    except subprocess.CalledProcessError:
        pass
    run_git(repo_dir, token, "checkout", base_branch)
    run_git(repo_dir, token, "reset", "--hard", f"origin/{base_branch}")
    run_git(repo_dir, token, "clean", "-fdx")


def apply_mailbox(repo_dir: Path, *, token: str | None, base_branch: str, mailbox_path: Path) -> int:
    try:
        run_git(repo_dir, token, "am", "--3way", str(mailbox_path))
    except subprocess.CalledProcessError:
        try:
            run_git(repo_dir, token, "am", "--abort")
        except subprocess.CalledProcessError:
            pass
        raise

    applied = run_git(
        repo_dir,
        token,
        "rev-list",
        "--count",
        f"origin/{base_branch}..HEAD",
    )
    return int(applied or "0")


def push_branch(repo_dir: Path, *, token: str, branch_name: str) -> None:
    run_git(
        repo_dir,
        token,
        "push",
        "--force",
        "origin",
        f"HEAD:refs/heads/{branch_name}",
    )


def list_recent_commit_messages(
    repo_dir: Path,
    *,
    token: str | None,
    ref: str,
    limit: int,
) -> list[str]:
    output = run_git(
        repo_dir,
        token,
        "log",
        f"--max-count={limit}",
        "--format=%B%x00",
        ref,
    )
    return [message.strip() for message in output.split("\x00") if message.strip()]


def build_branch_name(
    prefix: str,
    root_message_id: str,
    title: str,
    base_branch: str,
    unique_suffix: str | None = None,
) -> str:
    digest = hashlib.sha1(root_message_id.encode(), usedforsecurity=False).hexdigest()[:12]
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
    slug = slug[:40] or "series"
    branch_name = f"{prefix}/{base_branch}/{digest}-{slug}"
    if unique_suffix:
        branch_name = f"{branch_name}-{unique_suffix}"
    return branch_name
