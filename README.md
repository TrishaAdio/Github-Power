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

  GitHub token  (input hidden — paste and press Enter)
  needs 'repo' scope; add 'delete_repo' to allow deletions
  > ****************************************

  ✓ authenticated as your-name

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

## Tools the AI gets

| Tool | What it does |
| --- | --- |
| `whoami` | Which account the server acts as, plus token scopes |
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
--token TOKEN          skip the prompt (or set GITHUB_TOKEN)
--local-root DIR       allow push_local_path to read files under DIR
--sse                  stream SSE responses instead of JSON
--quiet                stop logging tool calls
```

Environment equivalents: `PORT`, `HOST`, `AI_PASSCODE`, `GITHUB_TOKEN`, `LOCAL_ROOT`.

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

Runs a real MCP client against the server backed by a stub GitHub API: auth gate,
tool schemas, first commit into an empty repo, updates, binary files, deletions,
branches and PRs. No token or internet needed.
