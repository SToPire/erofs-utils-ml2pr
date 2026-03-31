from __future__ import annotations

import os
from dataclasses import dataclass


def _get_int(name: str, default: int) -> int:
    value = os.getenv(name)
    if value is None or value == "":
        return default
    return int(value)


def _get_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None or value == "":
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Config:
    archive_root: str = "https://lists.ozlabs.org/pipermail/linux-erofs/"
    raw_message_root: str = "https://lore.kernel.org/all/"
    github_owner: str = "erofs"
    github_repo: str = "erofs-utils"
    base_branch: str = "experimental"
    poll_interval_hours: int = 2
    lookback_hours: int = 48
    stale_days: int = 14
    bot_branch_prefix: str = "ml"
    clone_dir: str = "/tmp/erofs-cibot-cache"
    git_clone_depth: int = 200
    git_user_name: str = "erofs-cibot"
    git_user_email: str = "erofs-cibot@lists.ozlabs.org"
    request_copilot_review: bool = False
    ignore_existing_prs: bool = False
    close_upstreamed_prs: bool = False
    gh_path: str = "gh"
    copilot_review_token: str | None = None
    github_app_id: str | None = None
    github_app_private_key: str | None = None

    @classmethod
    def from_env(cls) -> "Config":
        private_key = os.getenv("APP_PRIVATE_KEY")
        if private_key:
            private_key = private_key.replace("\\n", "\n")

        return cls(
            archive_root=os.getenv(
                "ARCHIVE_ROOT",
                "https://lists.ozlabs.org/pipermail/linux-erofs/",
            ),
            raw_message_root=os.getenv(
                "RAW_MESSAGE_ROOT",
                "https://lore.kernel.org/all/",
            ),
            github_owner=os.getenv("OWNER", "erofs"),
            github_repo=os.getenv("REPO", "erofs-utils"),
            base_branch=os.getenv("BASE_BRANCH", "experimental"),
            poll_interval_hours=_get_int("POLL_INTERVAL_HOURS", 2),
            lookback_hours=_get_int("LOOKBACK_HOURS", 48),
            stale_days=_get_int("STALE_DAYS", 14),
            bot_branch_prefix=os.getenv("BOT_BRANCH_PREFIX", "ml"),
            clone_dir=os.getenv("CLONE_DIR", "/tmp/erofs-cibot-cache"),
            git_clone_depth=_get_int("GIT_CLONE_DEPTH", 200),
            git_user_name=os.getenv("GIT_USER_NAME", "erofs-cibot"),
            git_user_email=os.getenv(
                "GIT_USER_EMAIL",
                "erofs-cibot@lists.ozlabs.org",
            ),
            request_copilot_review=_get_bool("REQUEST_COPILOT_REVIEW", False),
            ignore_existing_prs=_get_bool("IGNORE_EXISTING_PRS", False),
            close_upstreamed_prs=_get_bool("CLOSE_UPSTREAMED_PRS", False),
            gh_path=os.getenv("GH_PATH", "gh"),
            copilot_review_token=os.getenv("COPILOT_REVIEW_TOKEN"),
            github_app_id=os.getenv("APP_ID"),
            github_app_private_key=private_key,
        )
