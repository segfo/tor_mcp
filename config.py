"""Configuration for the Tor MCP server."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

# Package root (…/tor_mcp). Used to resolve bundled tor.exe and data dir.
PACKAGE_DIR = Path(__file__).resolve().parent


def _default_tor_exe() -> Path:
    """Locate the bundled Tor Expert Bundle binary.

    Layout (manually placed, see README):
        tor_mcp/vendor/tor/tor.exe        (Windows)
        tor_mcp/vendor/tor/tor            (Linux/macOS)
    Override with the TOR_MCP_TOR_EXE environment variable.
    """
    override = os.environ.get("TOR_MCP_TOR_EXE")
    if override:
        return Path(override)
    exe_name = "tor.exe" if os.name == "nt" else "tor"
    return PACKAGE_DIR / "vendor" / "tor" / exe_name


@dataclass
class TorConfig:
    """Runtime configuration for the managed Tor process and fetch behavior."""

    # --- Tor process / proxy ---
    tor_exe_path: Path = None  # type: ignore[assignment]  # set in __post_init__
    socks_port: int = 9050
    control_port: int = 9051
    data_dir: Path = None  # type: ignore[assignment]
    bootstrap_timeout: float = 90.0  # seconds to wait for "Bootstrapped 100%"

    # --- Fetch limits (Phase 2) ---
    fetch_timeout: float = 60.0          # per-request timeout (seconds)
    max_response_bytes: int = 5_000_000  # 5 MB hard cap on response body
    max_redirects: int = 5

    # --- Circuit (Phase 3) ---
    new_circuit_wait: float = 5.0  # settle time after NEWNYM (seconds)

    def __post_init__(self) -> None:
        if self.tor_exe_path is None:
            self.tor_exe_path = _default_tor_exe()
        else:
            self.tor_exe_path = Path(self.tor_exe_path)
        if self.data_dir is None:
            self.data_dir = PACKAGE_DIR / "vendor" / "tor-data"
        else:
            self.data_dir = Path(self.data_dir)

    @property
    def socks_proxy_url(self) -> str:
        """SOCKS5h URL (remote DNS) for httpx, e.g. socks5h://127.0.0.1:9050."""
        return f"socks5h://127.0.0.1:{self.socks_port}"

    @classmethod
    def from_env(cls) -> "TorConfig":
        """Build config, allowing port overrides via environment variables."""
        kwargs: dict = {}
        if (v := os.environ.get("TOR_MCP_SOCKS_PORT")):
            kwargs["socks_port"] = int(v)
        if (v := os.environ.get("TOR_MCP_CONTROL_PORT")):
            kwargs["control_port"] = int(v)
        return cls(**kwargs)
