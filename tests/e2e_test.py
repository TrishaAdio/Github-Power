"""End-to-end test: a real MCP client -> this server -> a stub GitHub API.

Run with:  ./.venv/bin/python tests/e2e_test.py
No GitHub token or network access required.
"""

import asyncio
import hashlib
import json
import sys
import threading
from pathlib import Path

import httpx
import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

STUB_PORT = 8765
MCP_PORT = 8766

# --------------------------------------------------------------- stub GitHub
STATE = {"repos": {}, "blobs": {}, "trees": {}, "commits": {}, "refs": {}}


def sha(data: str) -> str:
    return hashlib.sha1(data.encode()).hexdigest()


OWNER_TOKEN = "owner-token"
PUSH_TOKEN = "push-token"
# Records "<identity> <METHOD> <path>" for every stub call, so the test can assert
# which token was used for which operation.
CALLS: list[str] = []
COLLABORATORS: set[tuple[str, str]] = set()
INVITATIONS: list[dict] = []


def identity(request: Request) -> str:
    auth = request.headers.get("authorization", "")
    if auth.endswith(PUSH_TOKEN):
        return "pusher"
    if auth.endswith(OWNER_TOKEN):
        return "owner"
    return "unknown"


async def stub(request: Request):
    path = "/" + request.path_params["rest"]
    method = request.method
    who = identity(request)
    CALLS.append(f"{who} {method} {path}")
    body = {}
    if method in ("POST", "PATCH", "PUT"):
        try:
            body = await request.json()
        except Exception:
            body = {}
    parts = [p for p in path.split("/") if p]

    if path == "/user":
        login = "octobot" if who == "pusher" else "octotest"
        return JSONResponse(
            {"login": login, "name": login.title(), "type": "User", "html_url": "u"},
            headers={"x-oauth-scopes": "repo, delete_repo"},
        )

    if path == "/user/repository_invitations":
        if method == "GET":
            return JSONResponse(INVITATIONS)

    if path.startswith("/user/repository_invitations/") and method == "PATCH":
        invitation_id = int(parts[-1])
        remaining = [i for i in INVITATIONS if i["id"] != invitation_id]
        accepted = [i for i in INVITATIONS if i["id"] == invitation_id]
        INVITATIONS[:] = remaining
        for i in accepted:
            COLLABORATORS.add((i["repository"]["full_name"], "octobot"))
        return Response(status_code=204)

    if path == "/user/repos" and method == "POST":
        full = f"octotest/{body['name']}"
        STATE["repos"][full] = {
            "full_name": full,
            "private": body.get("private", True),
            "default_branch": "main",
            "description": body.get("description", ""),
            "html_url": f"https://github.com/{full}",
            "clone_url": f"https://github.com/{full}.git",
            "ssh_url": f"git@github.com:{full}.git",
            "size": 0,
        }
        return JSONResponse(STATE["repos"][full], status_code=201)

    if path == "/user/repos" and method == "GET":
        return JSONResponse(list(STATE["repos"].values()))

    if parts and parts[0] == "repos":
        full = f"{parts[1]}/{parts[2]}"
        rest = parts[3:]
        if full not in STATE["repos"]:
            return JSONResponse({"message": "Not Found"}, status_code=404)

        # Owner invites the push account as a collaborator.
        if rest[:1] == ["collaborators"] and method == "PUT":
            invitee = rest[1]
            if (full, invitee) in COLLABORATORS:
                return Response(status_code=204)
            invitation = {
                "id": 42,
                "repository": {"full_name": full},
                "invitee": {"login": invitee},
                "permissions": body.get("permission", "push"),
            }
            INVITATIONS.append(invitation)
            return JSONResponse(invitation, status_code=201)

        # The push account may only write where it has been granted access.
        if (
            who == "pusher"
            and method in ("POST", "PATCH", "PUT", "DELETE")
            and (full, "octobot") not in COLLABORATORS
        ):
            return JSONResponse({"message": "Not Found"}, status_code=404)

        if not rest:
            if method == "DELETE":
                STATE["repos"].pop(full)
                return Response(status_code=204)
            return JSONResponse(STATE["repos"][full])

        if rest[:2] == ["git", "ref"]:  # git/ref/heads/<branch>
            branch = "/".join(rest[3:])
            if (full, branch) not in STATE["refs"]:
                return JSONResponse({"message": "Not Found"}, status_code=404)
            return JSONResponse(
                {"ref": f"refs/heads/{branch}", "object": {"sha": STATE["refs"][(full, branch)]}}
            )

        if rest[:2] == ["git", "refs"]:
            if method == "POST":
                branch = body["ref"].split("refs/heads/")[1]
                STATE["refs"][(full, branch)] = body["sha"]
                return JSONResponse({"object": {"sha": body["sha"]}}, status_code=201)
            if method == "PATCH":
                STATE["refs"][(full, "/".join(rest[3:]))] = body["sha"]
                return JSONResponse({"object": {"sha": body["sha"]}})

        if rest[:2] == ["git", "blobs"]:
            if method == "POST":
                s = sha("blob" + body["content"])
                STATE["blobs"][s] = body["content"]
                return JSONResponse({"sha": s}, status_code=201)
            return JSONResponse({"sha": rest[2], "content": STATE["blobs"][rest[2]]})

        if rest[:2] == ["git", "trees"]:
            if method == "POST":
                merged = dict(STATE["trees"].get(body.get("base_tree"), {}))
                for entry in body["tree"]:
                    if entry.get("sha") is None:
                        merged.pop(entry["path"], None)
                    else:
                        merged[entry["path"]] = entry["sha"]
                s = sha("tree" + json.dumps(merged, sort_keys=True))
                STATE["trees"][s] = merged
                return JSONResponse({"sha": s}, status_code=201)
            commit = STATE["commits"].get(STATE["refs"].get((full, rest[2]), ""), {})
            tree = STATE["trees"].get(commit.get("tree", {}).get("sha"), {})
            return JSONResponse(
                {
                    "sha": commit.get("tree", {}).get("sha"),
                    "truncated": False,
                    "tree": [{"path": p, "type": "blob", "size": 1} for p in tree],
                }
            )

        if rest[:2] == ["git", "commits"]:
            if method == "POST":
                s = sha("commit" + json.dumps(body, sort_keys=True))
                STATE["commits"][s] = {
                    "sha": s,
                    "message": body["message"],
                    "tree": {"sha": body["tree"]},
                    "parents": [{"sha": p} for p in body.get("parents", [])],
                }
                return JSONResponse(STATE["commits"][s], status_code=201)
            return JSONResponse(STATE["commits"][rest[2]])

        if rest[0] == "contents":
            target = "/".join(rest[1:])
            branch = request.query_params.get("ref") or "main"
            commit = STATE["commits"].get(STATE["refs"].get((full, branch), ""), {})
            tree = STATE["trees"].get(commit.get("tree", {}).get("sha"), {})
            if target in tree:
                return JSONResponse(
                    {
                        "path": target,
                        "sha": tree[target],
                        "size": 1,
                        "content": STATE["blobs"][tree[target]],
                        "html_url": f"https://github.com/{full}/blob/{branch}/{target}",
                    }
                )
            children = [
                {"path": p, "type": "file", "size": 1}
                for p in tree
                if not target or p.startswith(target.rstrip("/") + "/")
            ]
            if children:
                return JSONResponse(children)
            return JSONResponse({"message": "Not Found"}, status_code=404)

        if rest[0] == "branches":
            return JSONResponse(
                [
                    {"name": b, "commit": {"sha": s}, "protected": False}
                    for (r, b), s in STATE["refs"].items()
                    if r == full
                ]
            )

        if rest[0] == "pulls" and method == "POST":
            return JSONResponse(
                {
                    "number": 1,
                    "html_url": f"https://github.com/{full}/pull/1",
                    "state": "open",
                    "draft": body.get("draft", False),
                },
                status_code=201,
            )

    return JSONResponse({"message": f"stub: no route for {method} {path}"}, status_code=404)


stub_app = Starlette(
    routes=[Route("/{rest:path}", stub, methods=["GET", "POST", "PATCH", "PUT", "DELETE"])]
)


def serve(app, port):
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
    threading.Thread(target=server.run, daemon=True).start()


# ----------------------------------------------------------------- our server
import gh_mcp.github as ghmod  # noqa: E402

ghmod.API_BASE = f"http://127.0.0.1:{STUB_PORT}"

from gh_mcp.auth import PasscodeGate  # noqa: E402
from gh_mcp.config import Settings, generate_passcode  # noqa: E402
from gh_mcp.server import build_server  # noqa: E402
from mcp import ClientSession  # noqa: E402
from mcp.client.streamable_http import streamablehttp_client  # noqa: E402

PASSCODE = generate_passcode()

settings = Settings(
    token=OWNER_TOKEN,
    passcode=PASSCODE,
    port=MCP_PORT,
    login="octotest",
    push_token=PUSH_TOKEN,
    push_login="octobot",
    verbose=False,
)
gh = ghmod.GitHubClient(OWNER_TOKEN, push_token=PUSH_TOKEN)
gh.login = "octotest"
gh.push_login = "octobot"
app = PasscodeGate(build_server(settings, gh).streamable_http_app(), PASSCODE)

failures: list[str] = []


def check(name, cond, extra=""):
    print(("  PASS  " if cond else "  FAIL  ") + name + (f"   {extra}" if extra else ""))
    if not cond:
        failures.append(name)


async def call(session, tool, args):
    result = await session.call_tool(tool, args)
    text = result.content[0].text if result.content else ""
    if result.isError:
        return {"__error__": text}
    try:
        return json.loads(text)
    except Exception:
        return {"__raw__": text}


async def main():
    serve(stub_app, STUB_PORT)
    serve(app, MCP_PORT)
    await asyncio.sleep(1.5)
    base = f"http://127.0.0.1:{MCP_PORT}"

    async with httpx.AsyncClient() as h:
        r = await h.get(f"{base}/healthz")
        check("healthz open without passcode", r.status_code == 200, r.text.strip())

        r = await h.post(
            f"{base}/mcp",
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
            headers={"Accept": "application/json, text/event-stream"},
        )
        check("no passcode -> 401", r.status_code == 401, r.text[:70])

        r = await h.post(
            f"{base}/mcp",
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
            headers={"Authorization": "Bearer wrong-code", "Accept": "application/json"},
        )
        check("wrong passcode -> 401", r.status_code == 401)

    async with streamablehttp_client(
        f"{base}/mcp", headers={"Authorization": f"Bearer {PASSCODE}"}
    ) as (reader, writer, _):
        async with ClientSession(reader, writer) as session:
            await session.initialize()
            tools = (await session.list_tools()).tools
            check("header passcode accepted", True)
            check("tools registered", len(tools) == 15,
                  f"{len(tools)}: {', '.join(sorted(t.name for t in tools))}")

            schema = next(t for t in tools if t.name == "push_files").inputSchema
            props = schema["properties"]
            check("push_files params", {"repo", "files", "message", "branch", "delete_paths"} <= set(props),
                  ", ".join(sorted(props)))
            check("push_files required", set(schema["required"]) == {"repo", "files", "message"},
                  str(schema.get("required")))
            check("FileEntry schema inlined", "$ref" in props["files"]["items"],
                  json.dumps(props["files"]["items"]))
            check("docstring became description",
                  "ONE commit" in (next(t for t in tools if t.name == "push_files").description or ""))

    # passcode embedded in the URL path
    async with streamablehttp_client(f"{base}/{PASSCODE}/mcp") as (reader, writer, _):
        async with ClientSession(reader, writer) as session:
            await session.initialize()

            me = await call(session, "whoami", {})
            check(
                "whoami reports both identities",
                me.get("owner", {}).get("login") == "octotest"
                and me.get("pusher", {}).get("login") == "octobot"
                and me.get("split_identity") is True,
                json.dumps(me)[:120],
            )

            repo = await call(session, "create_repo",
                              {"name": "demo", "private": True, "description": "hi", "auto_init": False})
            check("create_repo", repo.get("full_name") == "octotest/demo", json.dumps(repo)[:110])
            check(
                "repo created by OWNER token",
                "owner POST /user/repos" in CALLS and "pusher POST /user/repos" not in CALLS,
            )

            # Before access is granted the push account must fail with a clear message.
            denied = await call(session, "push_files", {
                "repo": "demo", "message": "too early",
                "files": [{"path": "x.txt", "content": "x"}]})
            check(
                "push blocked until access granted",
                "__error__" in denied and "grant_push_access" in denied["__error__"],
                json.dumps(denied)[:150],
            )

            granted = await call(session, "grant_push_access", {"repo": "demo"})
            check(
                "grant_push_access invites and accepts",
                granted.get("push_account") == "octobot"
                and granted.get("invitation_sent") is True
                and granted.get("invitation_accepted") is True
                and granted.get("status") == "ready",
                json.dumps(granted)[:170],
            )
            check(
                "invite sent by owner, accepted by pusher",
                "owner PUT /repos/octotest/demo/collaborators/octobot" in CALLS
                and "pusher PATCH /user/repository_invitations/42" in CALLS,
            )

            push = await call(session, "push_files", {
                "repo": "demo",
                "message": "init",
                "files": [
                    {"path": "README.md", "content": "# demo\n"},
                    {"path": "src/app.py", "content": "print('hi')\n"},
                    {"path": "run.sh", "content": "echo hi\n", "mode": "100755"},
                ],
            })
            check("push_files into empty repo",
                  push.get("branch") == "main" and push.get("branch_created") is True
                  and sorted(push.get("files_written", [])) == ["README.md", "run.sh", "src/app.py"],
                  json.dumps(push)[:150])

            got = await call(session, "read_file", {"repo": "demo", "path": "src/app.py"})
            check("read_file roundtrip", got.get("content") == "print('hi')\n", json.dumps(got)[:90])

            await call(session, "push_files", {
                "repo": "demo", "message": "add binary",
                "files": [{"path": "logo.png", "content": "iVBORw0KGgo=", "encoding": "base64"}],
            })
            binary = await call(session, "read_file", {"repo": "demo", "path": "logo.png"})
            check("binary roundtrip", binary.get("encoding") == "base64"
                  and binary.get("content") == "iVBORw0KGgo=", json.dumps(binary)[:120])

            second = await call(session, "push_files", {
                "repo": "demo", "message": "update",
                "files": [{"path": "src/app.py", "content": "print('bye')\n"}],
            })
            check("second commit reuses branch", second.get("branch_created") is False)
            kept = await call(session, "read_file", {"repo": "demo", "path": "README.md"})
            check("untouched files survive", kept.get("content") == "# demo\n")
            updated = await call(session, "read_file", {"repo": "demo", "path": "src/app.py"})
            check("file overwritten", updated.get("content") == "print('bye')\n")

            branch = await call(session, "push_files", {
                "repo": "demo", "branch": "feature", "message": "feature work",
                "files": [{"path": "feature.txt", "content": "new\n"}],
            })
            check("branch auto-created", branch.get("branch_created") is True
                  and branch.get("branch") == "feature")

            deleted = await call(session, "delete_files", {
                "repo": "demo", "paths": ["README.md"], "message": "drop readme", "branch": "feature"})
            check("delete_files", deleted.get("files_deleted") == ["README.md"], json.dumps(deleted)[:110])
            gone = await call(session, "read_file",
                              {"repo": "demo", "path": "README.md", "ref": "feature"})
            check("deleted file is gone", "__error__" in gone, json.dumps(gone)[:80])

            tree = await call(session, "list_files", {"repo": "demo", "recursive": True})
            paths = sorted(e["path"] for e in tree.get("entries", []))
            check("list_files recursive", "src/app.py" in paths, ", ".join(paths))

            pr = await call(session, "create_pull_request",
                            {"repo": "demo", "title": "Feature", "head": "feature"})
            check("create_pull_request", pr.get("number") == 1 and pr.get("base") == "main",
                  json.dumps(pr)[:110])

            branches = await call(session, "list_branches", {"repo": "demo"})
            check("list_branches", len(branches.get("branches", [])) == 2, json.dumps(branches)[:110])

            guard = await call(session, "delete_repo", {"repo": "demo"})
            check("delete_repo needs confirm", "__error__" in guard, json.dumps(guard)[:80])
            local = await call(session, "push_local_path",
                              {"repo": "demo", "local_path": "/etc", "message": "x"})
            check("push_local_path off by default", "__error__" in local, json.dumps(local)[:100])
            missing = await call(session, "read_file", {"repo": "nope", "path": "x"})
            check("missing repo -> clean error", "__error__" in missing, json.dumps(missing)[:100])
            nobranch = await call(session, "push_files", {
                "repo": "demo", "branch": "ghost", "create_branch": False, "message": "x",
                "files": [{"path": "a.txt", "content": "a"}]})
            check("create_branch=false respected", "__error__" in nobranch, json.dumps(nobranch)[:120])

    # ---- identity routing over the whole session
    writes = [c for c in CALLS if c.split()[1] in ("POST", "PATCH", "DELETE")]
    git_writes = [c for c in writes if "/git/" in c or "/pulls" in c]
    check(
        "all git/PR writes used the PUSH token",
        git_writes and all(c.startswith("pusher ") for c in git_writes),
        f"{len(git_writes)} calls, offenders: "
        + (", ".join(c for c in git_writes if not c.startswith("pusher ")) or "none"),
    )
    # The pusher token may only touch /user (its own identity) and its invitations.
    allowed_pusher_reads = ("/user", "/user/repository_invitations")
    stray = [
        c
        for c in CALLS
        if c.startswith("pusher ") and c.split()[1] == "GET" and c.split()[2] not in allowed_pusher_reads
    ]
    check("pusher token never reads repo data", not stray, ", ".join(stray) or "none")

    print()
    if failures:
        print(f"  {len(failures)} FAILED: {failures}")
        sys.exit(1)
    print("  all checks passed")


asyncio.run(main())
