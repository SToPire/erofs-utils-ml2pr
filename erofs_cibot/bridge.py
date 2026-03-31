from __future__ import annotations

import logging
import os
import re
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.parse import unquote, urlparse

from .archive import (
    candidate_archive_months,
    discover_recent_series,
    normalize_message_id,
    resolve_series_by_root_message,
    write_series_mailbox,
)
from .copilot_review import request_review
from .config import Config
from .github_api import GitHubClient, PullRequest
from .gitops import (
    apply_mailbox,
    build_branch_name,
    clone_or_fetch_repo,
    list_recent_commit_messages,
    push_branch,
    reset_repo,
)

LOG = logging.getLogger(__name__)
UPSTREAM_SCAN_COMMITS = 20
LORE_URL_RE = re.compile(r"https://lore\.kernel\.org/[^\s)>\"]+")


def _write_summary(line: str) -> None:
    summary_path = os.getenv("GITHUB_STEP_SUMMARY")
    if not summary_path:
        return
    with open(summary_path, "a", encoding="utf-8") as handle:
        handle.write(line)
        handle.write("\n")


def _sanitize_title(title: str) -> str:
    return title.replace("\r", " ").replace("\n", " ").strip()


def _format_pr_body(series, config: Config) -> str:
    patch_lines = "\n".join(
        f"- `{patch.index}/{patch.total}` {patch.title}" for patch in series.patches
    )
    patch_markers = [
        f"<!-- erofs-cibot-patch: {patch.message.message_id} -->"
        for patch in series.patches
    ]
    lore_link = f"{config.raw_message_root.rstrip('/')}/{series.root_message_id}/"
    return "\n".join(
        [
            f"<!-- erofs-cibot-series: {series.root_message_id} -->",
            f"<!-- erofs-cibot-version: {series.version} -->",
            *patch_markers,
            f"**Message-ID:** `{series.root_message_id}`",
            f"**Lore:** {lore_link}",
            f"**Submitter:** {series.submitter_name} <{series.submitter_addr}>",
            f"**Version:** v{series.version}",
            f"**Patches:** {series.total}",
            f"**Base:** `{config.base_branch}`",
            "",
            "**Patch subjects**",
            patch_lines,
            "",
            "*Automated by erofs-cibot.*",
        ]
    )


def _find_pr_for_series(prs: list[PullRequest], series_key: str) -> PullRequest | None:
    for pr in prs:
        if pr.series_key == series_key:
            return pr
    return None


def _maybe_request_copilot_review(pr: PullRequest, github: GitHubClient, config: Config) -> None:
    if not config.request_copilot_review:
        return

    review_token = config.copilot_review_token or github.token

    try:
        request_review(
            gh_path=config.gh_path,
            owner=github.owner,
            repo=github.repo,
            pull_number=pr.number,
            token=review_token,
        )
    except Exception as exc:
        LOG.warning(
            "copilot review request failed pr=%s url=%s error=%s",
            pr.number,
            pr.html_url,
            exc,
        )
        _write_summary(
            f"| copilot failed | {_sanitize_title(pr.title)} | [PR #{pr.number}]({pr.html_url}) |"
        )
        return

    LOG.info("requested copilot review pr=%s url=%s", pr.number, pr.html_url)
    _write_summary(
        f"| copilot requested | {_sanitize_title(pr.title)} | [PR #{pr.number}]({pr.html_url}) |"
    )


def _extract_lore_message_ids(text: str) -> set[str]:
    message_ids: set[str] = set()
    for raw_url in LORE_URL_RE.findall(text):
        parsed = urlparse(raw_url.rstrip(").,;"))
        path_parts = [part for part in parsed.path.split("/") if part]
        if not path_parts:
            continue

        message_id = normalize_message_id(unquote(path_parts[-1]))
        if message_id is not None:
            message_ids.add(message_id)

    return message_ids


def _load_pr_patch_message_ids(
    pr: PullRequest,
    *,
    config: Config,
    now: datetime,
) -> tuple[str, ...]:
    if pr.patch_message_ids:
        return pr.patch_message_ids

    if pr.series_key is None or pr.series_version is None:
        return ()

    series = resolve_series_by_root_message(
        config.raw_message_root,
        pr.series_key,
        version=pr.series_version,
        now=now,
    )
    return tuple(patch.message.message_id for patch in series.patches)


def _close_upstreamed_prs(
    *,
    prs: list[PullRequest],
    github: GitHubClient,
    repo_dir: Path,
    config: Config,
    now: datetime,
) -> None:
    commit_messages = list_recent_commit_messages(
        repo_dir,
        token=github.token,
        ref=f"origin/{config.base_branch}",
        limit=UPSTREAM_SCAN_COMMITS,
    )
    upstream_message_ids: set[str] = set()
    for message in commit_messages:
        upstream_message_ids.update(_extract_lore_message_ids(message))

    LOG.info(
        "upstream scan commits=%d lore_message_ids=%d",
        len(commit_messages),
        len(upstream_message_ids),
    )
    if not upstream_message_ids:
        return

    for pr in prs:
        if pr.series_key is None:
            continue

        try:
            patch_message_ids = _load_pr_patch_message_ids(pr, config=config, now=now)
        except Exception as exc:
            LOG.warning(
                "upstream close scan failed pr=%s url=%s error=%s",
                pr.number,
                pr.html_url,
                exc,
            )
            continue

        if not patch_message_ids:
            continue

        missing_ids = [message_id for message_id in patch_message_ids if message_id not in upstream_message_ids]
        if missing_ids:
            continue

        try:
            github.comment_on_pull_request(
                pr.number,
                body=(
                    "Closing this PR because the latest synced upstream "
                    "history already references all patch mails from this "
                    "series on lore.kernel.org."
                ),
            )
        except Exception as exc:
            LOG.warning(
                "upstream close comment failed pr=%s url=%s error=%s",
                pr.number,
                pr.html_url,
                exc,
            )

        try:
            github.close_pull_request(pr.number)
        except Exception as exc:
            LOG.warning(
                "upstream close failed pr=%s url=%s error=%s",
                pr.number,
                pr.html_url,
                exc,
            )
            _write_summary(
                f"| close failed | {_sanitize_title(pr.title)} | [PR #{pr.number}]({pr.html_url}) |"
            )
            continue

        LOG.info(
            "closed upstreamed pr=%s url=%s patch_count=%d",
            pr.number,
            pr.html_url,
            len(patch_message_ids),
        )
        _write_summary(
            f"| closed upstreamed | {_sanitize_title(pr.title)} | [PR #{pr.number}]({pr.html_url}) |"
        )


def _process_series(
    *,
    series,
    prs: list[PullRequest],
    github: GitHubClient,
    repo_dir: Path,
    config: Config,
) -> None:
    existing_pr = _find_pr_for_series(prs, series.root_message_id)
    if existing_pr is not None and not config.ignore_existing_prs:
        LOG.info(
            "series already has PR root=%s pr=%s state=%s",
            series.root_message_id,
            existing_pr.number,
            existing_pr.state,
        )
        _write_summary(
            f"| exists | {_sanitize_title(series.title)} | PR #{existing_pr.number} |"
        )
        return

    if existing_pr is not None and config.ignore_existing_prs:
        LOG.info(
            "ignoring existing PR root=%s pr=%s state=%s due to config",
            series.root_message_id,
            existing_pr.number,
            existing_pr.state,
        )

    rerun_suffix = None
    if config.ignore_existing_prs:
        rerun_suffix = datetime.now(tz=UTC).strftime("rerun-%Y%m%d%H%M%S")

    branch_name = build_branch_name(
        config.bot_branch_prefix,
        series.root_message_id,
        series.title,
        config.base_branch,
        unique_suffix=rerun_suffix,
    )

    reset_repo(repo_dir, token=github.token, base_branch=config.base_branch)
    with TemporaryDirectory(prefix="erofs-cibot-series-") as tmpdir:
        mailbox_path = Path(tmpdir) / "series.mbox"
        apply_series = series
        write_series_mailbox(apply_series, mailbox_path)

        try:
            applied = apply_mailbox(
                repo_dir,
                token=github.token,
                base_branch=config.base_branch,
                mailbox_path=mailbox_path,
            )
        except Exception as exc:
            LOG.warning(
                "apply failed root=%s title=%s error=%s",
                series.root_message_id,
                series.title,
                exc,
            )
            _write_summary(
                f"| apply failed | {_sanitize_title(series.title)} | `{series.root_message_id}` |"
            )
            return

    if applied <= 0:
        LOG.warning("series applied zero commits root=%s", series.root_message_id)
        _write_summary(
            f"| empty | {_sanitize_title(series.title)} | `{series.root_message_id}` |"
        )
        return

    push_branch(repo_dir, token=github.token, branch_name=branch_name)
    pr = github.create_pull_request(
        title=_sanitize_title(apply_series.title),
        body=_format_pr_body(apply_series, config),
        head=branch_name,
        base=config.base_branch,
    )
    LOG.info(
        "created pr root=%s pr=%s url=%s",
        series.root_message_id,
        pr.number,
        pr.html_url,
    )
    _write_summary(
        f"| created | {_sanitize_title(series.title)} | [PR #{pr.number}]({pr.html_url}) |"
    )
    _maybe_request_copilot_review(pr, github, config)


def run_once(config: Config) -> int:
    now = datetime.now(tz=UTC)
    months = candidate_archive_months(now, config.lookback_hours)

    LOG.info(
        "bridge start archive_root=%s base=%s lookback_hours=%d months=%s",
        config.archive_root,
        config.base_branch,
        config.lookback_hours,
        ",".join(months),
    )

    github = GitHubClient.from_config(config)
    repo_dir = Path(config.clone_dir)
    clone_or_fetch_repo(
        repo_dir,
        owner=config.github_owner,
        repo=config.github_repo,
        base_branch=config.base_branch,
        token=github.token,
        user_name=config.git_user_name,
        user_email=config.git_user_email,
        clone_depth=config.git_clone_depth,
    )

    _write_summary("## erofs-cibot run")
    _write_summary("")
    _write_summary("| status | series | result |")
    _write_summary("|--------|--------|--------|")

    if config.close_upstreamed_prs:
        open_prs = github.list_pull_requests(state="open")
        LOG.info("loaded open pull requests=%d for upstream close scan", len(open_prs))
        _close_upstreamed_prs(
            prs=[pr for pr in open_prs if pr.state == "open"],
            github=github,
            repo_dir=repo_dir,
            config=config,
            now=now,
        )

    series_list = discover_recent_series(
        config.archive_root,
        raw_message_root=config.raw_message_root,
        lookback_hours=config.lookback_hours,
        now=now,
    )
    LOG.info("discovered complete series=%d", len(series_list))

    prs = github.list_pull_requests(state="all")
    LOG.info("loaded pull requests=%d", len(prs))

    for series in series_list:
        _process_series(
            series=series,
            prs=prs,
            github=github,
            repo_dir=repo_dir,
            config=config,
        )

    reset_repo(repo_dir, token=github.token, base_branch=config.base_branch)
    return 0
