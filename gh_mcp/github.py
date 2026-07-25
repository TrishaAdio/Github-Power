"""Minimal async GitHub REST client with atomic multi-file commits."""

from __future__ import annotations

import base64
from typing import Any, Iterable, Sequence

import httpx

API_BASE = "https://api.github.com"


class GitHubError(RuntimeError):
    """A GitHub API call failed."""

    def __init__(self, status: int, message: str, url: str = "") -> None:
        self.status = status
        self.url = url
        super().__init__(f"GitHub API {status}: {message}" + (f" ({url})" if url else ""))


class GitHubClient:
    def __init__(self, token: str, timeout: float = 30.0) -> None:
        self._token = token
        self._client = httpx.AsyncClient(
            base_url=API_BASE,
            timeout=timeout,
            follow_redirects=True,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "github-mcp/1.0",
            },
        )
        self.login: str = ""
        self.scopes: str = ""

    async def aclose(self) -> None:
        await self._client.aclose()

    # ------------------------------------------------------------------ core

    async def request(
        self,
        method: str,
        path: str,
        *,
        ok404: bool = False,
        **kwargs: Any,
    ) -> Any:
        response = await self._client.request(method, path, **kwargs)

        if response.status_code == 404 and ok404:
            return None
        if response.status_code >= 400:
            try:
                payload = response.json()
                message = payload.get("message", response.text)
                errors = payload.get("errors")
                if errors:
                    details = "; ".join(
                        e.get("message") or f"{e.get('field')}: {e.get('code')}"
                        for e in errors
                        if isinstance(e, dict)
                    )
                    if details:
                        message = f"{message} ({details})"
            except Exception:
                message = response.text[:400] or response.reason_phrase
            raise GitHubError(response.status_code, message, str(response.url))

        if response.status_code == 204 or not response.content:
            return {}
        return response.json()

    async def paginate(self, path: str, *, limit: int = 100, **kwargs: Any) -> list[dict]:
        """Follow ``Link: rel="next"`` headers until ``limit`` items are collected."""
        items: list[dict] = []
        params = dict(kwargs.pop("params", {}) or {})
        params.setdefault("per_page", min(100, max(1, limit)))
        url: str | None = path

        while url and len(items) < limit:
            response = await self._client.request("GET", url, params=params, **kwargs)
            if response.status_code >= 400:
                raise GitHubError(
                    response.status_code,
                    response.json().get("message", response.text)
                    if response.content
                    else response.reason_phrase,
                    str(response.url),
                )
            batch = response.json()
            if not isinstance(batch, list):
                batch = batch.get("items", [])
            items.extend(batch)
            url = response.links.get("next", {}).get("url")
            params = {}  # already encoded in the next URL

        return items[:limit]

    # ------------------------------------------------------------------ auth

    async def authenticate(self) -> dict:
        """Verify the token and cache the login + granted scopes."""
        response = await self._client.get("/user")
        if response.status_code == 401:
            raise GitHubError(401, "token rejected (bad or revoked)", "/user")
        if response.status_code >= 400:
            raise GitHubError(
                response.status_code,
                response.json().get("message", response.text)
                if response.content
                else response.reason_phrase,
                "/user",
            )
        user = response.json()
        self.login = user.get("login", "")
        self.scopes = response.headers.get("x-oauth-scopes", "")
        return user

    async def resolve_repo(self, repo: str) -> str:
        """Accept ``owner/name`` or a bare ``name`` (owned by the token user)."""
        repo = repo.strip().strip("/")
        if repo.startswith("https://github.com/"):
            repo = repo[len("https://github.com/") :]
        if repo.endswith(".git"):
            repo = repo[:-4]
        if "/" in repo:
            return repo
        if not self.login:
            await self.authenticate()
        return f"{self.login}/{repo}"

    # ----------------------------------------------------------------- repos

    async def create_repo(
        self,
        name: str,
        *,
        description: str = "",
        private: bool = True,
        org: str | None = None,
        auto_init: bool = False,
        gitignore_template: str | None = None,
        license_template: str | None = None,
        homepage: str | None = None,
    ) -> dict:
        payload: dict[str, Any] = {
            "name": name,
            "private": private,
            "auto_init": auto_init,
        }
        if description:
            payload["description"] = description
        if homepage:
            payload["homepage"] = homepage
        if gitignore_template:
            payload["gitignore_template"] = gitignore_template
        if license_template:
            payload["license_template"] = license_template

        path = f"/orgs/{org}/repos" if org else "/user/repos"
        return await self.request("POST", path, json=payload)

    async def get_repo(self, repo: str) -> dict:
        return await self.request("GET", f"/repos/{await self.resolve_repo(repo)}")

    async def delete_repo(self, repo: str) -> None:
        await self.request("DELETE", f"/repos/{await self.resolve_repo(repo)}")

    async def list_repos(
        self, *, visibility: str = "all", affiliation: str = "owner", limit: int = 30
    ) -> list[dict]:
        return await self.paginate(
            "/user/repos",
            limit=limit,
            params={
                "visibility": visibility,
                "affiliation": affiliation,
                "sort": "updated",
            },
        )

    # -------------------------------------------------------------- branches

    async def list_branches(self, repo: str, *, limit: int = 100) -> list[dict]:
        full = await self.resolve_repo(repo)
        return await self.paginate(f"/repos/{full}/branches", limit=limit)

    async def get_ref(self, full: str, branch: str) -> dict | None:
        return await self.request(
            "GET", f"/repos/{full}/git/ref/heads/{branch}", ok404=True
        )

    async def create_branch(self, repo: str, branch: str, from_branch: str | None = None) -> dict:
        full = await self.resolve_repo(repo)
        info = await self.request("GET", f"/repos/{full}")
        source = from_branch or info.get("default_branch") or "main"

        if await self.get_ref(full, branch):
            raise GitHubError(422, f"branch '{branch}' already exists", full)

        base = await self.get_ref(full, source)
        if not base:
            raise GitHubError(404, f"source branch '{source}' not found", full)

        ref = await self.request(
            "POST",
            f"/repos/{full}/git/refs",
            json={"ref": f"refs/heads/{branch}", "sha": base["object"]["sha"]},
        )
        return {
            "repo": full,
            "branch": branch,
            "from_branch": source,
            "sha": ref["object"]["sha"],
        }

    # ----------------------------------------------------------------- files

    async def commit_files(
        self,
        repo: str,
        files: Sequence[dict],
        message: str,
        *,
        branch: str | None = None,
        create_branch: bool = True,
        delete_paths: Iterable[str] = (),
    ) -> dict:
        """Write/delete many paths in a single commit (Git data API)."""
        full = await self.resolve_repo(repo)
        info = await self.request("GET", f"/repos/{full}")
        default_branch = info.get("default_branch") or "main"
        target = branch or default_branch

        ref = await self.get_ref(full, target)
        branch_existed = ref is not None
        parent_sha: str | None = ref["object"]["sha"] if ref else None

        if parent_sha is None:
            base = await self.get_ref(full, default_branch)
            if base:
                if not create_branch:
                    raise GitHubError(
                        404,
                        f"branch '{target}' does not exist (pass create_branch=true)",
                        full,
                    )
                parent_sha = base["object"]["sha"]
            # else: repository has no commits yet -> this becomes the root commit

        base_tree: str | None = None
        if parent_sha:
            commit = await self.request("GET", f"/repos/{full}/git/commits/{parent_sha}")
            base_tree = commit["tree"]["sha"]

        tree: list[dict[str, Any]] = []
        written: list[str] = []

        for entry in files:
            path = str(entry["path"]).lstrip("/")
            raw = entry.get("content", "")
            encoding = (entry.get("encoding") or "text").lower()
            if encoding == "base64":
                data = base64.b64decode(raw)
            else:
                data = str(raw).encode("utf-8")

            blob = await self.request(
                "POST",
                f"/repos/{full}/git/blobs",
                json={
                    "content": base64.b64encode(data).decode(),
                    "encoding": "base64",
                },
            )
            tree.append(
                {
                    "path": path,
                    "mode": entry.get("mode") or "100644",
                    "type": "blob",
                    "sha": blob["sha"],
                }
            )
            written.append(path)

        removed: list[str] = []
        for path in delete_paths or ():
            path = str(path).lstrip("/")
            if not base_tree:
                raise GitHubError(422, "cannot delete files: branch has no commits", full)
            tree.append({"path": path, "mode": "100644", "type": "blob", "sha": None})
            removed.append(path)

        if not tree:
            raise GitHubError(422, "nothing to commit: no files and no deletions", full)

        tree_payload: dict[str, Any] = {"tree": tree}
        if base_tree:
            tree_payload["base_tree"] = base_tree
        new_tree = await self.request("POST", f"/repos/{full}/git/trees", json=tree_payload)

        new_commit = await self.request(
            "POST",
            f"/repos/{full}/git/commits",
            json={
                "message": message,
                "tree": new_tree["sha"],
                "parents": [parent_sha] if parent_sha else [],
            },
        )

        if branch_existed:
            await self.request(
                "PATCH",
                f"/repos/{full}/git/refs/heads/{target}",
                json={"sha": new_commit["sha"], "force": False},
            )
        else:
            await self.request(
                "POST",
                f"/repos/{full}/git/refs",
                json={"ref": f"refs/heads/{target}", "sha": new_commit["sha"]},
            )

        return {
            "repo": full,
            "branch": target,
            "branch_created": not branch_existed,
            "commit_sha": new_commit["sha"],
            "commit_url": f"https://github.com/{full}/commit/{new_commit['sha']}",
            "files_written": written,
            "files_deleted": removed,
            "message": message,
        }

    async def read_file(self, repo: str, path: str, ref: str | None = None) -> dict:
        full = await self.resolve_repo(repo)
        params = {"ref": ref} if ref else None
        data = await self.request(
            "GET", f"/repos/{full}/contents/{path.lstrip('/')}", params=params
        )

        if isinstance(data, list):
            return {
                "repo": full,
                "path": path,
                "type": "directory",
                "entries": [
                    {"path": e["path"], "type": e["type"], "size": e.get("size", 0)}
                    for e in data
                ],
            }

        if data.get("content"):
            raw = base64.b64decode(data["content"])
        else:  # files > 1 MB come back empty; fetch the blob instead
            blob = await self.request("GET", f"/repos/{full}/git/blobs/{data['sha']}")
            raw = base64.b64decode(blob.get("content", ""))

        try:
            text, binary = raw.decode("utf-8"), False
        except UnicodeDecodeError:
            text, binary = base64.b64encode(raw).decode(), True

        return {
            "repo": full,
            "path": data.get("path", path),
            "type": "file",
            "size": data.get("size", len(raw)),
            "sha": data.get("sha"),
            "encoding": "base64" if binary else "text",
            "content": text,
            "html_url": data.get("html_url"),
        }

    async def list_files(
        self, repo: str, path: str = "", ref: str | None = None, recursive: bool = False
    ) -> dict:
        full = await self.resolve_repo(repo)

        if recursive:
            info = await self.request("GET", f"/repos/{full}")
            target = ref or info.get("default_branch") or "main"
            tree = await self.request(
                "GET", f"/repos/{full}/git/trees/{target}", params={"recursive": "1"}
            )
            prefix = path.strip("/")
            entries = [
                {
                    "path": e["path"],
                    "type": "file" if e["type"] == "blob" else "dir",
                    "size": e.get("size", 0),
                }
                for e in tree.get("tree", [])
                if not prefix or e["path"].startswith(prefix)
            ]
            return {
                "repo": full,
                "ref": target,
                "truncated": tree.get("truncated", False),
                "entries": entries,
            }

        params = {"ref": ref} if ref else None
        data = await self.request(
            "GET", f"/repos/{full}/contents/{path.strip('/')}", params=params
        )
        if isinstance(data, dict):
            data = [data]
        return {
            "repo": full,
            "ref": ref,
            "entries": [
                {"path": e["path"], "type": e["type"], "size": e.get("size", 0)}
                for e in data
            ],
        }

    # ------------------------------------------------------- pulls / issues

    async def create_pull_request(
        self,
        repo: str,
        title: str,
        head: str,
        base: str | None = None,
        body: str = "",
        draft: bool = False,
    ) -> dict:
        full = await self.resolve_repo(repo)
        if not base:
            info = await self.request("GET", f"/repos/{full}")
            base = info.get("default_branch") or "main"
        pr = await self.request(
            "POST",
            f"/repos/{full}/pulls",
            json={
                "title": title,
                "head": head,
                "base": base,
                "body": body,
                "draft": draft,
            },
        )
        return {
            "repo": full,
            "number": pr["number"],
            "url": pr["html_url"],
            "head": head,
            "base": base,
            "state": pr["state"],
            "draft": pr.get("draft", False),
        }

    async def create_issue(
        self, repo: str, title: str, body: str = "", labels: Sequence[str] = ()
    ) -> dict:
        full = await self.resolve_repo(repo)
        payload: dict[str, Any] = {"title": title, "body": body}
        if labels:
            payload["labels"] = list(labels)
        issue = await self.request("POST", f"/repos/{full}/issues", json=payload)
        return {
            "repo": full,
            "number": issue["number"],
            "url": issue["html_url"],
            "state": issue["state"],
        }
