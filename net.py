"""HTTP access routed through the managed Tor SOCKS5h proxy.

Centralizes client construction so every tool (tor_check, tor_fetch,
onion_search) shares the same proxy settings and limits.
"""

from __future__ import annotations

from typing import Any, Optional

import httpx

from .config import TorConfig
from .tor_process import get_tor

# A browser-like UA reduces trivial blocks; Tor anonymity is handled by Tor itself.
DEFAULT_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; rv:128.0) Gecko/20100101 Firefox/128.0",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

CHECK_URL = "https://check.torproject.org/api/ip"


def tor_client(config: TorConfig, *, follow_redirects: bool = True) -> httpx.Client:
    """Create an httpx.Client whose traffic egresses through Tor.

    Ensures Tor is bootstrapped before returning.
    """
    get_tor(config)  # start/ensure ready (blocking, idempotent)
    timeout = httpx.Timeout(config.fetch_timeout)
    return httpx.Client(
        proxy=config.socks_proxy_url,
        headers=DEFAULT_HEADERS,
        timeout=timeout,
        follow_redirects=follow_redirects,
        max_redirects=config.max_redirects,
    )


def check_tor(config: TorConfig) -> dict[str, Any]:
    """Confirm connectivity is via Tor and report the exit IP.

    Returns {"is_tor": bool, "exit_ip": str|None, "checked_url": str}.
    """
    with tor_client(config) as client:
        resp = client.get(CHECK_URL)
        resp.raise_for_status()
        data = resp.json()
    # check.torproject.org/api/ip returns {"IsTor": true, "IP": "x.x.x.x"}
    return {
        "is_tor": bool(data.get("IsTor")),
        "exit_ip": data.get("IP"),
        "checked_url": CHECK_URL,
    }
