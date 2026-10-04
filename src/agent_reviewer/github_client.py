from __future__ import annotations

from dataclasses import dataclass

import httpx

from agent_reviewer.config import Settings
from agent_reviewer.heuristics import is_supported_path, should_skip_path


@dataclass
class RepoFile:
    path: str
    sha: str
    content: str


class GitHubError(RuntimeError):
    pass


class GitHubClient:
    def __init__(self, settings: Settings):
        if not settings.github_token:
            raise GitHubError(
                "GITHUB_TOKEN is not set. Create a token with repo scope and put it in .env."
            )
        self.settings = settings
        self._headers = {
            "Authorization": f"Bearer {settings.github_token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }

    def _client(self) -> httpx.Client:
        return httpx.Client(
            base_url=self.settings.github_api_base.rstrip("/"),
            headers=self._headers,
            timeout=60.0,
        )

    def get_repo(self, owner: str, repo: str) -> dict:
        with self._client() as client:
            response = client.get(f"/repos/{owner}/{repo}")
            self._raise(response, f"Could not load {owner}/{repo}")
            return response.json()

    def get_ref_sha(self, owner: str, repo: str, ref: str) -> str:
        ref_name = ref.removeprefix("refs/").removeprefix("heads/")
        with self._client() as client:
            response = client.get(f"/repos/{owner}/{repo}/git/ref/heads/{ref_name}")
            if response.status_code == 404:
                response = client.get(f"/repos/{owner}/{repo}/commits/{ref_name}")
                self._raise(response, f"Unknown ref {ref_name}")
                return response.json()["sha"]
            self._raise(response, f"Unknown ref {ref_name}")
            return response.json()["object"]["sha"]

    def list_supported_files(self, owner: str, repo: str, ref: str) -> list[dict]:
        with self._client() as client:
            response = client.get(
                f"/repos/{owner}/{repo}/git/trees/{ref}",
                params={"recursive": "1"},
            )
            self._raise(response, "Could not list repository tree")
            payload = response.json()
            files = []
            for item in payload.get("tree", []):
                if item.get("type") != "blob":
                    continue
                path = item.get("path", "")
                if should_skip_path(path) or not is_supported_path(path):
                    continue
                if item.get("size", 0) > self.settings.max_file_bytes:
                    continue
                files.append(item)
                if len(files) >= self.settings.max_files:
                    break
            return files

    def get_file_text(self, owner: str, repo: str, path: str, ref: str) -> str:
        with self._client() as client:
            response = client.get(
                f"/repos/{owner}/{repo}/contents/{path}",
                params={"ref": ref},
                headers={**self._headers, "Accept": "application/vnd.github.raw"},
            )
            self._raise(response, f"Could not fetch {path}")
            return response.text

    def fetch_supported_files(
        self, owner: str, repo: str, ref: str
    ) -> list[RepoFile]:
        items = self.list_supported_files(owner, repo, ref)
        files: list[RepoFile] = []
        for item in items:
            path = item["path"]
            try:
                content = self.get_file_text(owner, repo, path, ref)
            except GitHubError:
                continue
            files.append(RepoFile(path=path, sha=item.get("sha", ""), content=content))
        return files

    def create_pull_request(
        self,
        owner: str,
        repo: str,
        *,
        title: str,
        body: str,
        head: str,
        base: str,
    ) -> dict:
        with self._client() as client:
            response = client.post(
                f"/repos/{owner}/{repo}/pulls",
                json={"title": title, "body": body, "head": head, "base": base},
            )
            self._raise(response, "Could not create pull request")
            return response.json()

    def create_blob(self, owner: str, repo: str, content: str) -> str:
        with self._client() as client:
            response = client.post(
                f"/repos/{owner}/{repo}/git/blobs",
                json={"content": content, "encoding": "utf-8"},
            )
            self._raise(response, "Could not create git blob")
            return response.json()["sha"]

    def get_commit(self, owner: str, repo: str, sha: str) -> dict:
        with self._client() as client:
            response = client.get(f"/repos/{owner}/{repo}/git/commits/{sha}")
            self._raise(response, "Could not load commit")
            return response.json()

    def create_tree(
        self,
        owner: str,
        repo: str,
        base_tree: str,
        entries: list[dict],
    ) -> str:
        with self._client() as client:
            response = client.post(
                f"/repos/{owner}/{repo}/git/trees",
                json={"base_tree": base_tree, "tree": entries},
            )
            self._raise(response, "Could not create git tree")
            return response.json()["sha"]

    def create_commit(
        self,
        owner: str,
        repo: str,
        *,
        message: str,
        tree: str,
        parents: list[str],
    ) -> str:
        with self._client() as client:
            response = client.post(
                f"/repos/{owner}/{repo}/git/commits",
                json={"message": message, "tree": tree, "parents": parents},
            )
            self._raise(response, "Could not create commit")
            return response.json()["sha"]

    def create_ref(self, owner: str, repo: str, branch: str, sha: str) -> None:
        with self._client() as client:
            response = client.post(
                f"/repos/{owner}/{repo}/git/refs",
                json={"ref": f"refs/heads/{branch}", "sha": sha},
            )
            self._raise(response, f"Could not create branch {branch}")

    @staticmethod
    def _raise(response: httpx.Response, message: str) -> None:
        if response.is_success:
            return
        detail = response.text[:500]
        raise GitHubError(f"{message}: HTTP {response.status_code} {detail}")


def parse_repo(repo: str) -> tuple[str, str]:
    cleaned = repo.strip().removeprefix("https://github.com/").removesuffix(".git")
    parts = [part for part in cleaned.split("/") if part]
    if len(parts) != 2:
        raise ValueError("Repo must look like owner/name")
    return parts[0], parts[1]
