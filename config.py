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

    # --- Shared backend (Tor + browser daemon shared across processes) ---
    # A single backend process holds the Tor process and the browser session so
    # that multiple `claude -p` invocations (and the interactive console) share
    # one browser/login/CAPTCHA-cleared state instead of each spawning its own.
    backend_host: str = "127.0.0.1"
    backend_port: int = 9100
    backend_startup_timeout: float = 120.0  # wait for backend /health after spawn

    # --- Fetch limits (Phase 2) ---
    fetch_timeout: float = 60.0          # per-request timeout (seconds)
    max_response_bytes: int = 5_000_000  # 5 MB hard cap on response body
    max_redirects: int = 5

    # --- Circuit (Phase 3) ---
    new_circuit_wait: float = 5.0  # settle time after NEWNYM (seconds)

    # --- Playwright browser (Phase 4) ---
    # Headful by default: a human can watch the window and solve CAPTCHAs /
    # log in manually for sites that block automation or use waiting queues.
    headless: bool = False
    browser_width: int = 1024
    browser_height: int = 720
    browser_profile_dir: Path = None  # type: ignore[assignment]  # persistent cookies/session
    screenshot_dir: Path = None  # type: ignore[assignment]
    nav_timeout_ms: int = 60_000          # per-navigation timeout
    browser_wait_timeout_ms: int = 300_000  # waits (queues can take minutes)

    def __post_init__(self) -> None:
        if self.tor_exe_path is None:
            self.tor_exe_path = _default_tor_exe()
        else:
            self.tor_exe_path = Path(self.tor_exe_path)
        if self.data_dir is None:
            self.data_dir = PACKAGE_DIR / "vendor" / "tor-data"
        else:
            self.data_dir = Path(self.data_dir)
        if self.browser_profile_dir is None:
            self.browser_profile_dir = PACKAGE_DIR / "vendor" / "browser-profile"
        else:
            self.browser_profile_dir = Path(self.browser_profile_dir)
        if self.screenshot_dir is None:
            self.screenshot_dir = PACKAGE_DIR / "vendor" / "screenshots"
        else:
            self.screenshot_dir = Path(self.screenshot_dir)

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
        if (v := os.environ.get("TOR_MCP_BOOTSTRAP_TIMEOUT")):
            kwargs["bootstrap_timeout"] = float(v)
        if (v := os.environ.get("TOR_MCP_BACKEND_HOST")):
            kwargs["backend_host"] = v
        if (v := os.environ.get("TOR_MCP_BACKEND_PORT")):
            kwargs["backend_port"] = int(v)
        if (v := os.environ.get("TOR_MCP_HEADLESS")) is not None:
            kwargs["headless"] = v.strip().lower() in ("1", "true", "yes", "on")
        if (v := os.environ.get("TOR_MCP_BROWSER_WIDTH")):
            kwargs["browser_width"] = int(v)
        if (v := os.environ.get("TOR_MCP_BROWSER_HEIGHT")):
            kwargs["browser_height"] = int(v)
        if (v := os.environ.get("TOR_MCP_BROWSER_PROFILE_DIR")):
            kwargs["browser_profile_dir"] = Path(v)
        if (v := os.environ.get("TOR_MCP_SCREENSHOT_DIR")):
            kwargs["screenshot_dir"] = Path(v)
        return cls(**kwargs)
