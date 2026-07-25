"""Runtime configuration for the GitHub MCP server."""

from __future__ import annotations

import secrets
from dataclasses import dataclass, field
from pathlib import Path

# Unambiguous alphabet: no 0/o/1/l/i to keep passcodes easy to read out loud.
_ALPHABET = "abcdefghjkmnpqrstuvwxyz23456789"


def generate_passcode(groups: int = 4, size: int = 4) -> str:
    """Return a passcode like ``k7m2-p9qx-4tzr-8fhw`` (URL and header safe)."""
    return "-".join(
        "".join(secrets.choice(_ALPHABET) for _ in range(size)) for _ in range(groups)
    )


@dataclass
class Settings:
    token: str
    passcode: str
    host: str = "0.0.0.0"
    port: int = 5000
    login: str = ""
    scopes: str = ""
    push_token: str = ""
    push_login: str = ""
    push_scopes: str = ""
    local_root: Path | None = None
    json_response: bool = True
    verbose: bool = True
    color: bool = True
    # Extra Host header values to accept (the public IP or domain you serve on).
    allowed_hosts: list[str] = field(default_factory=list)
    # Trust X-Forwarded-* when sitting behind Caddy/nginx.
    behind_proxy: bool = False
    # "any" = passcode via URL path, query or header. "header" = header only.
    auth_mode: str = "any"

    @property
    def loopback_only(self) -> bool:
        return self.host in ("127.0.0.1", "localhost", "::1")

    @property
    def mount_path(self) -> str:
        return "/mcp"
