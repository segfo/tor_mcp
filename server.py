"""Tor MCP server.

Exposes tools to reach .onion / Web resources through a locally managed Tor
process (SOCKS5h proxy). Tor is started lazily on first tool use and torn down
when the server exits.

Ethical scope: this server is for **passive** OSINT collection only (viewing and
collecting publicly reachable text). It must not be used for unauthorized access,
acquisition/trade of illegal content, or any active attack.
"""

from __future__ import annotations

import atexit
from typing import Any

from mcp.server.fastmcp import FastMCP

from .config import TorConfig
from . import net
from . import fetch as _fetch
from . import circuit as _circuit
from . import search as _search
from .tor_process import shutdown_tor

# Shared config instance (port overrides honored via environment variables).
config = TorConfig.from_env()

mcp = FastMCP("tor-osint")

# Ensure the managed Tor process is stopped when the server process exits.
atexit.register(shutdown_tor)


@mcp.tool()
def tor_check() -> dict[str, Any]:
    """Verify that outbound traffic is routed through Tor and report the exit IP.

    Starts the managed Tor process if it isn't running yet (first call may take
    ~10-40s while Tor bootstraps). Returns whether the connection is recognized
    as Tor and the current exit-node IP address.
    """
    return net.check_tor(config)


@mcp.tool()
def tor_fetch(url: str, render: str = "text", max_text_chars: int = 20000) -> dict[str, Any]:
    """Fetch an http(s) URL (including .onion) through Tor and return its content.

    Args:
      url: The target http/https URL. .onion addresses are supported.
      render: "text" (default) cleans HTML to readable text plus title and
        extracted links; "raw_head" returns the head of the raw decoded body.
      max_text_chars: Cap on returned text length (default 20000).

    Binary / non-textual responses are not downloaded; only metadata is reported.
    For passive OSINT collection only.
    """
    return _fetch.fetch(config, url, render=render, max_text_chars=max_text_chars)


@mcp.tool()
def tor_new_circuit(verify: bool = True) -> dict[str, Any]:
    """Request fresh Tor circuits (a new exit node) to vary the egress IP.

    Useful to avoid rate limits / per-IP blocks during investigation. When
    verify=True (default), reports the exit IP before and after and whether it
    changed (a change is likely but not guaranteed on every request).
    """
    return _circuit.new_circuit(config, verify=verify)


@mcp.tool()
def onion_search(query: str, max_results: int = 20, engine: str = "tordex") -> dict[str, Any]:
    """Search a Tor search engine for .onion sites matching a query (via Tor).

    Args:
      query: Search terms.
      max_results: Max results to return (default 20).
      engine: "tordex" (default) or "torch".

    Returns a list of {title, onion_url, snippet}. Use tor_fetch to retrieve the
    content of any returned onion URL. For passive OSINT discovery only — results
    are unmoderated and must be assessed by the investigator.
    """
    return _search.onion_search(config, query, max_results=max_results, engine=engine)


def run() -> None:
    """Run the MCP server over stdio."""
    mcp.run()
