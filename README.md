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

- `GH_TOKEN`
- or `APP_ID` and `APP_PRIVATE_KEY`

When `REQUEST_COPILOT_REVIEW=1`, `erofs-cibot` requests a Copilot review
for each new pull request with:

```bash
gh pr edit <pr> --repo <owner>/<repo> --add-reviewer @copilot
```

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

- `GH_TOKEN`
- `APP_PRIVATE_KEY`

## How It Works

`erofs-cibot` first scans the relevant OzLabs monthly `txt.gz` archives
to discover recent complete `erofs-utils:` patch series. After a series
is selected, it fetches the raw lore thread by `Message-ID`, rebuilds an
mbox from the original mails, tries `git am --3way` on `experimental`,
and opens a PR when the apply succeeds. If
`REQUEST_COPILOT_REVIEW=1`, it also requests `@copilot` review for the
new PR.

## Current Behavior

The current implementation:

- fetches monthly OzLabs `txt.gz` archives for discovery
- only tracks patch series whose title starts with `erofs-utils:`
- reconstructs complete patch series from recent messages
- fetches raw thread mbox data by `Message-ID` before applying
- applies series with `git am --3way`
- pushes bot branches and opens pull requests
- can request `@copilot` review on newly created pull requests

Planned follow-up work includes superseded-version handling, stale PR
cleanup, merged-series detection, and optional smoke tests.
