# erofs-cibot

`erofs-cibot` is an automation bot for `linux-erofs@lists.ozlabs.org`.

Its job is to:

1. poll the OzLabs archive on a schedule
2. discover recent complete `erofs-utils:` patch series
3. try to apply each series onto `erofs/erofs-utils` on the `experimental` branch
4. create a GitHub pull request when the series applies cleanly

## Requirements

- `git`
- Python 3.11 or newer
- `gh` when `REQUEST_COPILOT_REVIEW=1`

Install the project with:

```bash
pip install .
```

## Configuration

Common environment variables:

- `ARCHIVE_ROOT=https://lists.ozlabs.org/pipermail/linux-erofs/`
- `RAW_MESSAGE_ROOT=https://lore.kernel.org/all/`
- `OWNER=erofs`
- `REPO=erofs-utils`
- `BASE_BRANCH=experimental`
- `LOOKBACK_HOURS=48`
- `POLL_INTERVAL_HOURS=2`
- `BOT_BRANCH_PREFIX=ml`
- `CLONE_DIR=/tmp/erofs-cibot-cache`
- `REQUEST_COPILOT_REVIEW=0`
- `IGNORE_EXISTING_PRS=0`
- `GH_PATH=gh`

GitHub authentication:

- `APP_ID` and `APP_PRIVATE_KEY`
- optional `COPILOT_REVIEW_TOKEN` for requesting `@copilot` review as a user

Authentication priority and scope:

- `APP_ID` plus `APP_PRIVATE_KEY` are the main GitHub authentication
  method for cloning, pushing branches, and creating PRs.
- `COPILOT_REVIEW_TOKEN` only affects the `@copilot` review request.
  It does not change which identity creates the PR.
- If `COPILOT_REVIEW_TOKEN` is unset, the Copilot review request falls
  back to the main GitHub token used by the run.

When `REQUEST_COPILOT_REVIEW=1`, `erofs-cibot` requests a Copilot review
for each new pull request with:

```bash
gh pr edit <pr> --repo <owner>/<repo> --add-reviewer @copilot
```

If `COPILOT_REVIEW_TOKEN` is set, only the Copilot review request uses
that token. PR creation still uses `APP_ID` plus `APP_PRIVATE_KEY`.

When `IGNORE_EXISTING_PRS=1`, `erofs-cibot` ignores matching existing PRs
for the same mail thread and opens a fresh PR on a new bot branch.

## Usage

Show the effective configuration:

```bash
erofs-cibot show-config
```

Show which archive months will be scanned:

```bash
erofs-cibot show-months
```

This is useful for checking which OzLabs monthly archives the current
lookback window will touch before running a real bridge cycle.

List complete recent series found in the configured lookback window:

```bash
erofs-cibot list-series
```

Run one bridge cycle:

```bash
erofs-cibot bridge
```

## GitHub Actions

The repository includes a scheduled workflow at
`./.github/workflows/bridge.yml` that runs every two hours by default.
It also supports manual triggering through `workflow_dispatch`.
The manual trigger exposes an `ignore_existing_prs` input, which maps to
`IGNORE_EXISTING_PRS=1` for that run only.
Before each bridge run, the workflow also mirrors the configured
`erofs/erofs-utils:experimental` branch into `OWNER/REPO:BASE_BRANCH`.
When `OWNER=erofs`, `REPO=erofs-utils`, and `BASE_BRANCH=experimental`,
that branch sync step is skipped.

Set these repository secrets or variables before enabling it:

- `APP_ID`
- `APP_PRIVATE_KEY`

Optional repository variables can override the workflow defaults. If a
variable is unset, the workflow keeps its built-in default. Supported
variables are:

- `ARCHIVE_ROOT`
- `RAW_MESSAGE_ROOT`
- `OWNER`
- `REPO`
- `BASE_BRANCH`
- `LOOKBACK_HOURS`
- `POLL_INTERVAL_HOURS`
- `BOT_BRANCH_PREFIX`
- `CLONE_DIR`
- `GIT_CLONE_DEPTH`
- `GIT_USER_NAME`
- `GIT_USER_EMAIL`
- `REQUEST_COPILOT_REVIEW`
- `GH_PATH`
- `APP_ID`

Supported secrets are:

- `COPILOT_REVIEW_TOKEN`
- `APP_PRIVATE_KEY`

## How It Works

`erofs-cibot` first scans the relevant OzLabs monthly `date.html` index
pages to discover recent `erofs-utils:` patch mails.
For each candidate mail, it opens the corresponding OzLabs message page,
extracts the `Message-ID`, fetches the raw lore thread, rebuilds an
mbox from the original mails, tries `git am --3way` on `experimental`,
and opens a PR when the apply succeeds. If
`REQUEST_COPILOT_REVIEW=1`, it also requests `@copilot` review for the
new PR.

## Current Behavior

The current implementation:

- fetches monthly OzLabs `date.html` pages for discovery
- opens matching OzLabs message pages to extract exact timestamps and `Message-ID`
- only tracks patch series whose title starts with `erofs-utils:`
- reconstructs complete patch series from raw lore thread mbox data
- applies series with `git am --3way`
- pushes bot branches and opens pull requests
- can request `@copilot` review on newly created pull requests

Planned follow-up work includes superseded-version handling, stale PR
cleanup, merged-series detection, and optional smoke tests.
