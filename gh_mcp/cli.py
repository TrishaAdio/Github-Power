"""Terminal entrypoint: ask for the GitHub token, print the AI passcode, serve on :5000."""

from __future__ import annotations

import argparse
import getpass
import json
import os
import socket
import sys
from pathlib import Path

import httpx
import uvicorn

from . import __version__
from .auth import PasscodeGate
from .config import Settings, generate_passcode
from .github import GitHubClient
from .server import build_server

COLOR = sys.stdout.isatty() and os.environ.get("NO_COLOR") is None


def c(text: str, code: str) -> str:
    return f"\033[{code}m{text}\033[0m" if COLOR else text


def bold(t: str) -> str:
    return c(t, "1")


def dim(t: str) -> str:
    return c(t, "90")


def green(t: str) -> str:
    return c(t, "32")


def red(t: str) -> str:
    return c(t, "31")


def cyan(t: str) -> str:
    return c(t, "36")


def yellow(t: str) -> str:
    return c(t, "33")


def verify_token(token: str) -> tuple[dict, str]:
    """Synchronously validate a token before the event loop starts."""
    response = httpx.get(
        "https://api.github.com/user",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": f"github-mcp/{__version__}",
        },
        timeout=20.0,
    )
    if response.status_code == 401:
        raise ValueError("token was rejected by GitHub (bad, expired or revoked)")
    if response.status_code == 403:
        raise ValueError("token is forbidden (rate limited or blocked)")
    response.raise_for_status()
    return response.json(), response.headers.get("x-oauth-scopes", "")


def prompt_token(preset: str | None) -> tuple[str, dict, str]:
    token = preset
    for attempt in range(4):
        if not token:
            print(bold("  GitHub token") + dim("  (input hidden — paste and press Enter)"))
            print(dim("  needs 'repo' scope; add 'delete_repo' to allow deletions"))
            try:
                token = getpass.getpass("  > ").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                sys.exit(130)
            print()
        if not token:
            print(red("  no token entered"))
            continue
        try:
            user, scopes = verify_token(token)
            return token, user, scopes
        except Exception as exc:
            print(red(f"  {exc}"))
            print()
            token = None
            if attempt >= 2:
                break
    print(red("  giving up after too many failed attempts"))
    sys.exit(1)


def lan_ip() -> str | None:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.settimeout(0.4)
            sock.connect(("8.8.8.8", 80))
            return sock.getsockname()[0]
    except OSError:
        return None


def port_free(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind(("" if host == "0.0.0.0" else host, port))
            return True
        except OSError:
            return False


def print_handoff(settings: Settings, tool_names: list[str]) -> None:
    """Everything the operator needs to copy over to the AI."""
    code = settings.passcode
    hosts: list[tuple[str, str]] = [("local", "127.0.0.1")]
    if settings.host == "0.0.0.0":
        ip = lan_ip()
        if ip:
            hosts.append(("network", ip))

    rule = dim("─" * 62)
    print(rule)
    print(f"  {bold('AI PASSCODE')}   {green(bold(code))}")
    print(dim("  share this with your AI — anyone holding it can use your token"))
    print(rule)
    print()
    print(bold("  Endpoint") + dim("  (passcode in the URL — works with any MCP client)"))
    for label, host in hosts:
        print(f"    {dim(label + ':'):<12} {cyan(f'http://{host}:{settings.port}/{code}/mcp')}")
    print()
    print(bold("  Or send it as a header"))
    print(f"    {cyan(f'http://{hosts[0][1]}:{settings.port}/mcp')}")
    print(f"    {dim('Authorization: Bearer')} {code}")
    print()

    url = f"http://{hosts[-1][1]}:{settings.port}/{code}/mcp"
    snippet = {
        "mcpServers": {
            "github": {
                "url": url,
                "headers": {"Authorization": f"Bearer {code}"},
            }
        }
    }
    print(bold("  MCP client config"))
    for line in json.dumps(snippet, indent=2).splitlines():
        print(f"    {dim(line)}")
    print()
    print(
        bold("  Account   ")
        + f"{settings.login}  {dim('scopes: ' + (settings.scopes or 'fine-grained'))}"
    )
    print(bold("  Tools     ") + dim(f"{len(tool_names)}: " + ", ".join(tool_names)))
    if settings.local_root:
        print(bold("  Local push") + f"  enabled from {settings.local_root}")
    else:
        print(bold("  Local push") + dim("  disabled (start with --local-root <dir> to enable)"))
    if settings.host == "0.0.0.0" and len(hosts) > 1:
        print(
            bold("  Remote AI ")
            + dim(" not on your LAN? expose it: ")
            + dim(f"cloudflared tunnel --url http://localhost:{settings.port}")
        )
    print()
    print(dim("  waiting for the AI to connect… Ctrl-C to stop"))
    print(rule)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="github-mcp",
        description="Passcode-protected GitHub MCP server for AI agents.",
    )
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", 5000)))
    parser.add_argument(
        "--host",
        default=os.environ.get("HOST", "0.0.0.0"),
        help="bind address (default 0.0.0.0; use 127.0.0.1 to stay local-only)",
    )
    parser.add_argument(
        "--passcode",
        default=os.environ.get("AI_PASSCODE"),
        help="use a fixed passcode instead of generating one",
    )
    parser.add_argument(
        "--token",
        default=os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN"),
        help="GitHub token (otherwise prompted for, hidden)",
    )
    parser.add_argument(
        "--local-root",
        default=os.environ.get("LOCAL_ROOT"),
        help="allow push_local_path to read files under this directory",
    )
    parser.add_argument("--sse", action="store_true", help="stream SSE responses instead of JSON")
    parser.add_argument("--quiet", action="store_true", help="do not log tool calls")
    parser.add_argument("--version", action="version", version=f"github-mcp {__version__}")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)

    print()
    print(f"  {bold('GitHub MCP')} {dim('v' + __version__)}")
    print()

    if not port_free(args.host, args.port):
        print(red(f"  port {args.port} is already in use — pass --port <other>"))
        sys.exit(1)

    token, user, scopes = prompt_token(args.token)
    print(green("  ✓") + f" authenticated as {bold(user.get('login', '?'))}")
    if scopes and "repo" not in scopes:
        print(yellow("  !") + dim(f" token scopes are '{scopes}' — repo writes may fail"))
    print()

    local_root = Path(args.local_root).expanduser().resolve() if args.local_root else None
    if local_root and not local_root.is_dir():
        print(red(f"  --local-root is not a directory: {local_root}"))
        sys.exit(1)

    settings = Settings(
        token=token,
        passcode=args.passcode or generate_passcode(),
        host=args.host,
        port=args.port,
        login=user.get("login", ""),
        scopes=scopes,
        local_root=local_root,
        json_response=not args.sse,
        verbose=not args.quiet,
        color=COLOR,
    )

    github = GitHubClient(token)
    github.login = settings.login
    github.scopes = scopes

    mcp = build_server(settings, github)
    app = PasscodeGate(
        mcp.streamable_http_app(),
        settings.passcode,
        on_reject=lambda info: print(f"  {red('denied')} {dim(info)}", flush=True),
    )

    print_handoff(settings, sorted(t.name for t in mcp._tool_manager.list_tools()))

    try:
        uvicorn.run(app, host=settings.host, port=settings.port, log_level="warning")
    except KeyboardInterrupt:
        pass
    finally:
        print()
        print(dim("  stopped"))


if __name__ == "__main__":
    main()
