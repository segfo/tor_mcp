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

    # --- Direct (clearnet) browser engine selection ---
    # The "direct" connection (no Tor) can be driven by the user's system browser,
    # Camoufox, or a real Chrome attached over CDP (remote debugging).
    #   ""        → auto: system browser if found, else fall back to Camoufox
    #   "system"  → Playwright-launched system browser (BrowserError if none)
    #   "camoufox"→ always Camoufox (anti-fingerprint Firefox)
    #   "cdp"     → attach to a real Chrome over CDP (real profile/extensions, not
    #               bot-detected; auto-launches Chrome with --remote-debugging-port)
    # The Tor connection always uses Camoufox regardless of this setting.
    direct_browser: str = ""          # "" | "system" | "camoufox" | "cdp"

    # --- CDP engine (attach to a real Chrome over remote debugging) ---
    # Drives the user's actual Chrome: real profile, extensions, no automation
    # flags (no --no-sandbox / navigator.webdriver) → not bot-detected.
    # NOTE: Chrome's --remote-debugging-port is ignored if another Chrome is
    # already running on the SAME user-data-dir. The default below is a dedicated
    # OSINT user-data-dir so it can coexist with your daily Chrome. Point
    # cdp_user_data_dir at your real "...\Google\Chrome\User Data" to use your
    # everyday profile, but then close all daily Chrome windows first.
    cdp_port: int = 9222
    cdp_autolaunch: bool = True        # auto-start Chrome if the port is not up
    cdp_chrome_exe: str = ""           # empty = auto-detect (Chrome → Brave → Edge)
    cdp_user_data_dir: Path = None     # type: ignore[assignment]  # default: vendor/cdp-chrome-profile
    cdp_profile_dir: str = "Default"   # --profile-directory (e.g., "Profile 1")

    # When the direct engine is "system" (or auto picks system), these select
    # *which* installed browser to drive. Auto-detected if left empty.
    clearnet_browser_exe: str = ""    # absolute path; empty = auto-detect
    clearnet_browser_type: str = ""   # "chromium" | "firefox"; empty = auto
    clearnet_profile_dir: Path = None  # type: ignore[assignment]  # vendor/clearnet-profile/

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
        if self.clearnet_profile_dir is None:
            self.clearnet_profile_dir = PACKAGE_DIR / "vendor" / "clearnet-profile"
        else:
            self.clearnet_profile_dir = Path(self.clearnet_profile_dir)
        if self.cdp_user_data_dir is None:
            self.cdp_user_data_dir = PACKAGE_DIR / "vendor" / "cdp-chrome-profile"
        else:
            self.cdp_user_data_dir = Path(self.cdp_user_data_dir)
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
        if (v := os.environ.get("TOR_MCP_DIRECT_BROWSER")):
            kwargs["direct_browser"] = v.strip().lower()
        if (v := os.environ.get("TOR_MCP_CDP_PORT")):
            kwargs["cdp_port"] = int(v)
        if (v := os.environ.get("TOR_MCP_CDP_AUTOLAUNCH")) is not None:
            kwargs["cdp_autolaunch"] = v.strip().lower() in ("1", "true", "yes", "on")
        if (v := os.environ.get("TOR_MCP_CHROME_EXE")):
            kwargs["cdp_chrome_exe"] = v
        if (v := os.environ.get("TOR_MCP_CHROME_USER_DATA_DIR")):
            kwargs["cdp_user_data_dir"] = Path(v)
        if (v := os.environ.get("TOR_MCP_CHROME_PROFILE_DIR")):
            kwargs["cdp_profile_dir"] = v
        if (v := os.environ.get("TOR_MCP_CLEARNET_BROWSER_EXE")):
            kwargs["clearnet_browser_exe"] = v
        if (v := os.environ.get("TOR_MCP_CLEARNET_BROWSER_TYPE")):
            kwargs["clearnet_browser_type"] = v
        if (v := os.environ.get("TOR_MCP_CLEARNET_PROFILE_DIR")):
            kwargs["clearnet_profile_dir"] = Path(v)
        return cls(**kwargs)
