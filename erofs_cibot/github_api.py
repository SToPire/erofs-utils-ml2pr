from __future__ import annotations

import re
import time
from dataclasses import dataclass

import jwt
import requests

from .archive import normalize_message_id
from .config import Config

API_ROOT = "https://api.github.com"
SERIES_MARKER_RE = re.compile(r"^<!--\s*erofs-cibot-series:\s*(.+?)\s*-->$", re.MULTILINE)
VERSION_MARKER_RE = re.compile(r"^<!--\s*erofs-cibot-version:\s*(\d+)\s*-->$", re.MULTILINE)
PATCH_MARKER_RE = re.compile(r"^<!--\s*erofs-cibot-patch:\s*(.+?)\s*-->$", re.MULTILINE)


@dataclass(frozen=True)
class PullRequest:
    number: int
    state: str
    title: str
    body: str
    head_ref: str
    html_url: str

    @property
    def series_key(self) -> str | None:
        match = SERIES_MARKER_RE.search(self.body or "")
        if match is None:
            return None
        return normalize_message_id(match.group(1))

    @property
    def series_version(self) -> int | None:
        match = VERSION_MARKER_RE.search(self.body or "")
        if match is None:
            return None
        return int(match.group(1))

    @property
    def patch_message_ids(self) -> tuple[str, ...]:
        message_ids: list[str] = []
        for raw_value in PATCH_MARKER_RE.findall(self.body or ""):
            message_id = normalize_message_id(raw_value)
            if message_id is not None:
                message_ids.append(message_id)
        return tuple(message_ids)


class GitHubAppClient:
    def __init__(self, app_id: str, private_key_pem: str) -> None:
        self.app_id = app_id
        self.private_key_pem = private_key_pem

    def build_app_jwt(self) -> str:
        now = int(time.time())
        payload = {
            "iat": now - 60,
            "exp": now + 600,
            "iss": self.app_id,
        }
        return jwt.encode(payload, self.private_key_pem, algorithm="RS256")

    def _app_request(self, method: str, url: str, **kwargs) -> requests.Response:
        response = requests.request(
            method,
            url,
            headers={
                "Authorization": f"Bearer {self.build_app_jwt()}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            timeout=30,
            **kwargs,
        )
        response.raise_for_status()
        return response

    def create_installation_token(self, owner: str, repo: str) -> str:
        installation = self._app_request(
            "GET",
            f"{API_ROOT}/repos/{owner}/{repo}/installation",
        ).json()
        installation_id = installation["id"]

        token_response = self._app_request(
            "POST",
            f"{API_ROOT}/app/installations/{installation_id}/access_tokens",
            json={"repositories": [repo]},
        ).json()
        return token_response["token"]


class GitHubClient:
    def __init__(self, owner: str, repo: str, token: str) -> None:
        self.owner = owner
        self.repo = repo
        self.token = token
        self.session = requests.Session()
        self.session.headers.update(
            {
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            }
        )

    @classmethod
    def from_config(cls, config: Config) -> "GitHubClient":
        if config.github_app_id and config.github_app_private_key:
            app_client = GitHubAppClient(
                config.github_app_id,
                config.github_app_private_key,
            )
            token = app_client.create_installation_token(
                config.github_owner,
                config.github_repo,
            )
            return cls(config.github_owner, config.github_repo, token)

        raise ValueError("missing GitHub credentials: set APP_ID and APP_PRIVATE_KEY")

    def _request(self, method: str, path: str, **kwargs) -> requests.Response:
        response = self.session.request(
            method,
            f"{API_ROOT}{path}",
            timeout=30,
            **kwargs,
        )
        response.raise_for_status()
        return response

    def list_pull_requests(self, state: str = "all") -> list[PullRequest]:
        prs: list[PullRequest] = []
        page = 1
        while True:
            response = self._request(
                "GET",
                f"/repos/{self.owner}/{self.repo}/pulls",
                params={
                    "state": state,
                    "per_page": 100,
                    "page": page,
                },
            )
            payload = response.json()
            if not payload:
                break

            prs.extend(
                PullRequest(
                    number=item["number"],
                    state=item["state"],
                    title=item["title"],
                    body=item.get("body") or "",
                    head_ref=item["head"]["ref"],
                    html_url=item["html_url"],
                )
                for item in payload
            )
            if len(payload) < 100:
                break
            page += 1

        return prs

    def create_pull_request(
        self,
        *,
        title: str,
        body: str,
        head: str,
        base: str,
    ) -> PullRequest:
        payload = self._request(
            "POST",
            f"/repos/{self.owner}/{self.repo}/pulls",
            json={
                "title": title,
                "body": body,
                "head": head,
                "base": base,
            },
        ).json()
        return PullRequest(
            number=payload["number"],
            state=payload["state"],
            title=payload["title"],
            body=payload.get("body") or "",
            head_ref=payload["head"]["ref"],
            html_url=payload["html_url"],
        )

    def update_pull_request(
        self,
        pull_number: int,
        *,
        title: str,
        body: str,
    ) -> PullRequest:
        payload = self._request(
            "PATCH",
            f"/repos/{self.owner}/{self.repo}/pulls/{pull_number}",
            json={
                "title": title,
                "body": body,
            },
        ).json()
        return PullRequest(
            number=payload["number"],
            state=payload["state"],
            title=payload["title"],
            body=payload.get("body") or "",
            head_ref=payload["head"]["ref"],
            html_url=payload["html_url"],
        )

    def comment_on_pull_request(self, pull_number: int, *, body: str) -> None:
        self._request(
            "POST",
            f"/repos/{self.owner}/{self.repo}/issues/{pull_number}/comments",
            json={"body": body},
        )

    def close_pull_request(self, pull_number: int) -> None:
        self._request(
            "PATCH",
            f"/repos/{self.owner}/{self.repo}/pulls/{pull_number}",
            json={"state": "closed"},
        )
