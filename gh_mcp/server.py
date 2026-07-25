"""MCP tool surface: repositories, files/commits, branches, PRs, issues."""

from __future__ import annotations

import base64
import fnmatch
import functools
import inspect
import logging
import time
from pathlib import Path
from typing import Any, Literal

from mcp.server.fastmcp import FastMCP
from pydantic import BaseModel, Field

from .config import Settings
from .github import GitHubClient, GitHubError

SKIP_DIRS = {
    ".git",
    "node_modules",
    "__pycache__",
    ".venv",
    "venv",
    ".mypy_cache",
    ".pytest_cache",
    ".next",
    "dist",
    "build",
    ".DS_Store",
}
MAX_LOCAL_FILE_BYTES = 5 * 1024 * 1024


class FileEntry(BaseModel):
    """One file to write in a commit."""

    path: str = Field(description="Path inside the repo, e.g. 'src/main.py'")
    content: str = Field(description="File contents (base64 when encoding='base64')")
    encoding: Literal["text", "base64"] = Field(
        default="text", description="Use 'base64' for binary files such as images"
    )
    mode: Literal["100644", "100755"] = Field(
        default="100644", description="'100755' marks the file executable"
    )


def build_server(settings: Settings, github: GitHubClient) -> FastMCP:
    mcp = FastMCP(
        "github",
        instructions=(
            f"Create and manage GitHub repositories owned by {settings.login or 'the owner account'}. "
            "Write or delete multiple files in one atomic commit with push_files. "
            "Repo arguments accept 'owner/name' or a bare 'name' owned by the owner account."
            + (
                f" Commits, branches, PRs and issues are made by the separate push "
                f"account {settings.push_login or '(machine account)'}; it needs "
                f"collaborator write access on a repo before it can commit there, "
                f"which grant_push_access sets up."
                if settings.push_token
                else ""
            )
        ),
        stateless_http=True,
        json_response=settings.json_response,
        log_level="WARNING",  # FastMCP configures the root logger; keep it quiet
    )
    for noisy in ("httpx", "httpcore", "mcp", "uvicorn.access"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    def paint(text: str, code: str) -> str:
        return f"\033[{code}m{text}\033[0m" if settings.color else text

    def log(line: str) -> None:
        if settings.verbose:
            print(f"  {paint(time.strftime('%H:%M:%S'), '90')} {line}", flush=True)

    def tool(fn):
        """Register a tool, logging each call and flattening GitHub errors."""

        @functools.wraps(fn)
        async def wrapper(*args, **kwargs):
            shown = ", ".join(
                f"{k}={v!r}" if not isinstance(v, (list, dict)) else f"{k}=[{len(v)}]"
                for k, v in list(kwargs.items())[:3]
            )
            log(f"{paint(fn.__name__, '36')} {shown}")
            try:
                result = await fn(*args, **kwargs)
            except GitHubError as exc:
                log(f"  {paint('failed', '31')} {exc}")
                raise ValueError(str(exc)) from None
            except Exception as exc:
                log(f"  {paint('failed', '31')} {type(exc).__name__}: {exc}")
                raise
            log(f"  {paint('ok', '32')}")
            return result

        # eval_str resolves the PEP 563 string annotations so FastMCP can build
        # the JSON schema from the wrapper.
        wrapper.__signature__ = inspect.signature(fn, eval_str=True)
        return mcp.tool()(wrapper)

    # --------------------------------------------------------------- account

    @tool
    async def whoami() -> dict:
        """Show the identities this server uses.

        'owner' is the account that owns the repos and performs all reads.
        'pusher' is the account that makes commits, branches, PRs and issues —
        when a separate push token is configured, commits are authored by it.
        """
        user = await github.authenticate()
        owner = {
            "login": user.get("login"),
            "name": user.get("name"),
            "type": user.get("type"),
            "public_repos": user.get("public_repos"),
            "private_repos": user.get("total_private_repos"),
            "token_scopes": github.scopes or "fine-grained token (no classic scopes)",
            "profile": user.get("html_url"),
        }
        if not github.split_identity:
            return {"owner": owner, "pusher": owner, "split_identity": False}

        bot = await github.authenticate_push()
        return {
            "owner": owner,
            "pusher": {
                "login": (bot or {}).get("login"),
                "name": (bot or {}).get("name"),
                "type": (bot or {}).get("type"),
                "token_scopes": github.push_scopes
                or "fine-grained token (no classic scopes)",
                "profile": (bot or {}).get("html_url"),
            },
            "split_identity": True,
            "note": (
                "Commits, branches, PRs and issues are made by the pusher account; "
                "reads and repo creation/deletion by the owner account. The pusher "
                "needs collaborator write access — use grant_push_access."
            ),
        }

    @tool
    async def grant_push_access(repo: str, permission: str = "push") -> dict:
        """Give the push account write access to one of the owner's repos.

        Invites it as a collaborator using the owner token, then accepts the
        invitation using the push token, so no manual step is needed. Run this once
        per repository before the push account can commit to it.

        Args:
            repo: 'owner/name' or a bare repo name owned by the owner account.
            permission: 'push' (write, default), 'maintain', 'triage' or 'admin'.
        """
        if not github.split_identity:
            raise ValueError(
                "no separate push token configured; start the server with "
                "--push-token to use a machine account"
            )
        return await github.grant_push_access(repo, permission)

    @tool
    async def list_repos(
        limit: int = 30,
        visibility: Literal["all", "public", "private"] = "all",
        affiliation: str = "owner",
    ) -> dict:
        """List repositories for the connected account, most recently updated first."""
        repos = await github.list_repos(
            visibility=visibility, affiliation=affiliation, limit=limit
        )
        return {
            "count": len(repos),
            "repos": [
                {
                    "full_name": r["full_name"],
                    "private": r["private"],
                    "default_branch": r.get("default_branch"),
                    "description": r.get("description"),
                    "url": r["html_url"],
                    "updated_at": r.get("updated_at"),
                }
                for r in repos
            ],
        }

    # ------------------------------------------------------------------ repo

    @tool
    async def create_repo(
        name: str,
        description: str = "",
        private: bool = True,
        org: str | None = None,
        auto_init: bool = True,
        gitignore_template: str | None = None,
        license_template: str | None = None,
    ) -> dict:
        """Create a new repository.

        Args:
            name: Repository name (no owner prefix).
            description: Short description.
            private: Create it private (default) or public.
            org: Create under this organization instead of the personal account.
            auto_init: Seed an initial commit with a README so the repo is not empty.
            gitignore_template: e.g. 'Python', 'Node'.
            license_template: e.g. 'mit', 'apache-2.0'.
        """
        repo = await github.create_repo(
            name,
            description=description,
            private=private,
            org=org,
            auto_init=auto_init,
            gitignore_template=gitignore_template,
            license_template=license_template,
        )
        return {
            "full_name": repo["full_name"],
            "private": repo["private"],
            "default_branch": repo.get("default_branch", "main"),
            "url": repo["html_url"],
            "clone_url": repo["clone_url"],
            "ssh_url": repo["ssh_url"],
        }

    @tool
    async def get_repo(repo: str) -> dict:
        """Get details for a repository: default branch, visibility, size, topics."""
        r = await github.get_repo(repo)
        return {
            "full_name": r["full_name"],
            "private": r["private"],
            "default_branch": r.get("default_branch"),
            "description": r.get("description"),
            "url": r["html_url"],
            "clone_url": r.get("clone_url"),
            "size_kb": r.get("size"),
            "open_issues": r.get("open_issues_count"),
            "topics": r.get("topics", []),
            "pushed_at": r.get("pushed_at"),
        }

    @tool
    async def delete_repo(repo: str, confirm: bool = False) -> dict:
        """Permanently delete a repository. Requires confirm=true and a token with
        the 'delete_repo' scope. This cannot be undone."""
        if not confirm:
            raise ValueError("refusing to delete: call again with confirm=true")
        full = await github.resolve_repo(repo)
        await github.delete_repo(full)
        return {"deleted": full}

    # -------------------------------------------------------------- branches

    @tool
    async def list_branches(repo: str, limit: int = 100) -> dict:
        """List branches in a repository with their head commit SHAs."""
        branches = await github.list_branches(repo, limit=limit)
        return {
            "repo": await github.resolve_repo(repo),
            "branches": [
                {"name": b["name"], "sha": b["commit"]["sha"], "protected": b.get("protected", False)}
                for b in branches
            ],
        }

    @tool
    async def create_branch(repo: str, branch: str, from_branch: str | None = None) -> dict:
        """Create a branch from another branch (defaults to the repo's default branch)."""
        return await github.create_branch(repo, branch, from_branch)

    # ------------------------------------------------------- files & commits

    @tool
    async def push_files(
        repo: str,
        files: list[FileEntry],
        message: str,
        branch: str | None = None,
        create_branch: bool = True,
        delete_paths: list[str] | None = None,
    ) -> dict:
        """Create or update files (and optionally delete paths) in ONE commit.

        Handles empty repositories, creates the branch if it does not exist, and
        never force-pushes. Existing files are overwritten, everything else is left
        untouched. The commit is made by the push account when one is configured.

        Args:
            repo: 'owner/name' or a bare repo name owned by the owner account.
            files: Files to write. Use encoding='base64' for binary content.
            message: Commit message.
            branch: Target branch. Defaults to the repository's default branch.
            create_branch: Create the branch if missing (branched off the default).
            delete_paths: Paths to remove in the same commit.
        """
        if not files and not delete_paths:
            raise ValueError("provide at least one file to write or path to delete")
        return await github.commit_files(
            repo,
            [f.model_dump() for f in files],
            message,
            branch=branch,
            create_branch=create_branch,
            delete_paths=delete_paths or [],
        )

    @tool
    async def read_file(repo: str, path: str, ref: str | None = None) -> dict:
        """Read a file from a repository. Binary files come back base64-encoded.

        Args:
            repo: 'owner/name' or a bare repo name.
            path: Path inside the repository.
            ref: Branch, tag or commit SHA. Defaults to the default branch.
        """
        return await github.read_file(repo, path, ref)

    @tool
    async def list_files(
        repo: str, path: str = "", ref: str | None = None, recursive: bool = False
    ) -> dict:
        """List files in a repository directory, or the whole tree with recursive=true."""
        return await github.list_files(repo, path, ref, recursive)

    @tool
    async def delete_files(
        repo: str, paths: list[str], message: str, branch: str | None = None
    ) -> dict:
        """Delete one or more files from a repository in a single commit."""
        if not paths:
            raise ValueError("paths must not be empty")
        return await github.commit_files(
            repo, [], message, branch=branch, create_branch=False, delete_paths=paths
        )

    # ------------------------------------------------------ pull requests etc

    @tool
    async def create_pull_request(
        repo: str,
        title: str,
        head: str,
        base: str | None = None,
        body: str = "",
        draft: bool = False,
    ) -> dict:
        """Open a pull request from `head` into `base` (base defaults to the default branch)."""
        return await github.create_pull_request(repo, title, head, base, body, draft)

    @tool
    async def create_issue(
        repo: str, title: str, body: str = "", labels: list[str] | None = None
    ) -> dict:
        """Open an issue on a repository."""
        return await github.create_issue(repo, title, body, labels or [])

    # ---------------------------------------------- local disk -> repo (opt-in)

    @tool
    async def push_local_path(
        repo: str,
        local_path: str,
        message: str,
        dest_prefix: str = "",
        branch: str | None = None,
        exclude: list[str] | None = None,
    ) -> dict:
        """Upload a file or folder from the machine running this server into a repo.

        Disabled unless the operator started the server with --local-root. Skips
        .git, node_modules, virtualenvs and files larger than 5 MB.

        Args:
            repo: Target repository.
            local_path: File or directory path, absolute or relative to the allowed root.
            message: Commit message.
            dest_prefix: Optional destination directory inside the repo.
            branch: Target branch (defaults to the repository default branch).
            exclude: Extra glob patterns to skip, e.g. ['*.env', 'secrets/*'].
        """
        root = settings.local_root
        if root is None:
            raise ValueError(
                "local filesystem access is disabled; restart the server with "
                "--local-root <dir> to allow it"
            )

        candidate = Path(local_path).expanduser()
        if not candidate.is_absolute():
            candidate = root / candidate
        candidate = candidate.resolve()
        root = root.resolve()
        if candidate != root and root not in candidate.parents:
            raise ValueError(f"path escapes the allowed root {root}")
        if not candidate.exists():
            raise ValueError(f"path not found: {candidate}")

        patterns = exclude or []
        prefix = dest_prefix.strip("/")
        sources: list[tuple[Path, str]] = []

        if candidate.is_file():
            sources.append((candidate, candidate.name))
        else:
            for item in sorted(candidate.rglob("*")):
                if not item.is_file():
                    continue
                rel = item.relative_to(candidate)
                if any(part in SKIP_DIRS for part in rel.parts):
                    continue
                if any(fnmatch.fnmatch(str(rel), pat) for pat in patterns):
                    continue
                sources.append((item, str(rel).replace("\\", "/")))

        if not sources:
            raise ValueError("no files matched after filtering")

        entries: list[dict[str, Any]] = []
        skipped: list[str] = []
        for item, rel in sources:
            data = item.read_bytes()
            if len(data) > MAX_LOCAL_FILE_BYTES:
                skipped.append(rel)
                continue
            entries.append(
                {
                    "path": f"{prefix}/{rel}" if prefix else rel,
                    "content": base64.b64encode(data).decode(),
                    "encoding": "base64",
                    "mode": "100755" if item.stat().st_mode & 0o111 else "100644",
                }
            )

        if not entries:
            raise ValueError("every matched file exceeded the 5 MB limit")

        result = await github.commit_files(repo, entries, message, branch=branch)
        result["skipped_too_large"] = skipped
        result["source"] = str(candidate)
        return result

    return mcp
