# GitHub MCP — passcode edition

An MCP server you run in your terminal. It asks for your GitHub token once, prints a
short **AI passcode**, and listens on port **5000**. You hand the passcode to any AI
that speaks MCP, and it can create repos, push files, open branches and PRs on your
behalf. The token never leaves your machine.

## Start it

```bash
./start.sh                       # first run also creates .venv and installs deps
```

```
  GitHub MCP v1.0.0

  1. Owner token   your account — reads your repos
     needs 'repo'; add 'delete_repo' to allow deletions
     input is hidden — paste and press Enter
  > ****************************************

  ✓ owner: your-name

  2. Push token    machine account — makes the commits
     needs 'repo'; leave blank to commit as the owner account
  > ****************************************

  ✓ pusher: your-bot

──────────────────────────────────────────────────────────────
  AI PASSCODE   ph48-errj-czza-xw4r
  share this with your AI — anyone holding it can use your token
──────────────────────────────────────────────────────────────

  Endpoint  (passcode in the URL — works with any MCP client)
    local:       http://127.0.0.1:5000/ph48-errj-czza-xw4r/mcp
    network:     http://192.168.1.20:5000/ph48-errj-czza-xw4r/mcp
```

Every tool call the AI makes is logged in that terminal, so you can watch what it does.

Get a token at <https://github.com/settings/tokens> — classic token with `repo`
(add `delete_repo` if you want `delete_repo` to work), or a fine-grained token with
Contents, Metadata, Pull requests and Issues read/write on the target repos.

## Two accounts: read as you, commit as a bot

Give it a second token and the two identities are kept strictly separate:

| Operation | Token used |
| --- | --- |
| `whoami`, `list_repos`, `get_repo`, `read_file`, `list_files`, `list_branches` | **owner** |
| `create_repo`, `delete_repo` (repos must belong to you) | **owner** |
| `push_files`, `delete_files`, `create_branch` | **push** |
| `create_pull_request`, `create_issue` | **push** |

So your repos stay yours and the AI can see all of them, while every commit, branch
and PR is authored by the machine account. Nothing is written with the owner token
except creating/deleting a repository.

The push account needs write access to a repo before it can commit there. Run this
once per repo and it invites *and* accepts for you:

```
grant_push_access(repo="my-app")
->  { "push_account": "your-bot", "invitation_sent": true,
      "invitation_accepted": true, "status": "ready" }
```

If it isn't set up, writes fail with a message that says exactly that rather than a
bare `404`.

Use a **machine account** for the push token. GitHub allows one bot account per
person for automation; a second *personal* free account is against their Terms.
Splitting tokens changes attribution, not behaviour — if the activity itself looks
abusive, both accounts get flagged.

## Give the passcode to the AI

Any of these work — the server accepts all four:

| How | Value |
| --- | --- |
| URL path (easiest) | `http://HOST:5000/<passcode>/mcp` |
| Header | `Authorization: Bearer <passcode>` |
| Header | `X-AI-Passcode: <passcode>` |
| Query string | `http://HOST:5000/mcp?code=<passcode>` |

MCP client config:

```json
{
  "mcpServers": {
    "github": {
      "url": "http://127.0.0.1:5000/ph48-errj-czza-xw4r/mcp"
    }
  }
}
```

If the AI is not on your network, tunnel the port and give it the public URL:

```bash
cloudflared tunnel --url http://localhost:5000
```

`GET /healthz` is the only unauthenticated route. Everything else returns `401`
without the passcode.

The four methods are alternatives, not layers — if the passcode is already in the
URL path, an `Authorization` header adds nothing. To make the header the *only*
accepted credential, so the passcode never lands in a URL, a browser history or a
proxy access log:

```bash
./start.sh --auth-mode header
```

The URL then becomes plain `/mcp` and `/<passcode>/mcp` returns `401`.

## Exposing it on a public IP or domain

Serving it publicly over plain HTTP means the passcode and all repo content cross
the internet in the clear. Put it behind the TLS proxy you already run:

```
mcp.example.com {
    reverse_proxy 127.0.0.1:5000
}
```

```bash
./start.sh --host 127.0.0.1 --behind-proxy --allowed-host mcp.example.com --auth-mode header
```

`--allowed-host` also switches the Host check to a strict allowlist (localhost plus
what you list). With no `--allowed-host` and a non-loopback bind, any Host header is
accepted — necessary, because the server cannot guess the IP or domain a client will
use, and the passcode is the real credential either way.

## Tools the AI gets

| Tool | What it does |
| --- | --- |
| `whoami` | Both identities (owner + pusher) and their token scopes |
| `grant_push_access` | Invite the push account to a repo and auto-accept it |
| `list_repos` | Repos for the account, newest activity first |
| `create_repo` | New repo (private by default, optional org, gitignore, license) |
| `get_repo` | Default branch, visibility, size, topics |
| `delete_repo` | Deletes a repo — needs `confirm=true` |
| `push_files` | Create/update **many files in one commit**; creates the branch if missing |
| `read_file` | File contents (binary comes back base64) |
| `list_files` | Directory listing, or the whole tree with `recursive=true` |
| `delete_files` | Remove paths in a single commit |
| `create_branch` | Branch off another branch |
| `list_branches` | Branches with head SHAs |
| `create_pull_request` | Open a PR (base defaults to the default branch) |
| `create_issue` | Open an issue |
| `push_local_path` | Upload a file/folder from **your** disk — off unless `--local-root` is set |

`repo` accepts `owner/name` or a bare `name` owned by the connected account.

`push_files` uses the Git data API: one atomic commit, no force-pushes, works on a
brand-new empty repo, and leaves files it wasn't told about alone.

```json
{
  "repo": "my-app",
  "branch": "feature/login",
  "message": "add login form",
  "files": [
    { "path": "src/login.tsx", "content": "export const Login = () => null\n" },
    { "path": "scripts/dev.sh", "content": "npm run dev\n", "mode": "100755" },
    { "path": "public/logo.png", "content": "iVBORw0KGgo=", "encoding": "base64" }
  ],
  "delete_paths": ["src/old-login.tsx"]
}
```

## Options

```
--port 5000            port to listen on
--host 0.0.0.0         bind address (127.0.0.1 to stay local-only)
--passcode CODE        fixed passcode instead of a generated one
--token TOKEN          owner token, skips prompt 1 (or set GITHUB_TOKEN)
--push-token TOKEN     machine-account token, skips prompt 2 (or GITHUB_PUSH_TOKEN)
--no-push-token        don't ask for a push token; commit as the owner
--local-root DIR       allow push_local_path to read files under DIR
--allowed-host HOST    Host header to accept (repeatable); enables strict checking
--behind-proxy         trust X-Forwarded-* from Caddy/nginx
--auth-mode any|header where the passcode may appear (default: any)
--sse                  stream SSE responses instead of JSON
--quiet                stop logging tool calls
```

Environment equivalents: `PORT`, `HOST`, `AI_PASSCODE`, `GITHUB_TOKEN`,
`GITHUB_PUSH_TOKEN`, `LOCAL_ROOT`, `ALLOWED_HOSTS` (comma-separated), `AUTH_MODE`.

## Notes on safety

- Nothing is written to disk — the token lives in memory for the life of the process.
- A new passcode is generated on every start unless you pass `--passcode`.
- The token's scopes are the real limit on what the AI can do. Scope it down if you
  only want it touching one repo.
- Local filesystem reads are off by default.

## Tests

```bash
./.venv/bin/python tests/e2e_test.py
```

42 checks against a stub GitHub API, driven by a real MCP client: the auth gate,
header-only mode, Host header handling (public IP, private IP and domain — the
`421 Invalid Host header` regression), tool schemas, first commit into an empty
repo, updates, binary files, deletions, branches, PRs, and the two-token routing
(the stub records which token made every call and the test asserts no git write
ever used the owner token). No token or internet needed.
