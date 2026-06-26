"""System browser detection for clearnet (surface-web) browsing.

Scans standard Windows installation paths (and the registry) to find installed
browsers that Playwright can drive directly. Priority order: Chrome → Brave →
Edge → Firefox.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass
class BrowserInfo:
    name: str
    exe_path: Path
    browser_type: str  # "chromium" | "firefox"


# (display_name, absolute_path, playwright_type)
_CANDIDATE_PATHS: list[tuple[str, str, str]] = [
    ("Chrome",         r"C:\Program Files\Google\Chrome\Application\chrome.exe",                  "chromium"),
    ("Chrome",         r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",            "chromium"),
    ("Brave",          r"C:\Program Files\BraveSoftware\Brave-Browser\Application\brave.exe",     "chromium"),
    ("Brave",          r"C:\Program Files (x86)\BraveSoftware\Brave-Browser\Application\brave.exe","chromium"),
    ("Edge",           r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",                  "chromium"),
    ("Edge",           r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",            "chromium"),
    ("Firefox",        r"C:\Program Files\Mozilla Firefox\firefox.exe",                            "firefox"),
    ("Firefox",        r"C:\Program Files (x86)\Mozilla Firefox\firefox.exe",                     "firefox"),
]


def detect_system_browsers() -> list[BrowserInfo]:
    """Return installed system browsers ordered by priority (Chrome first).

    Scans standard paths first, then falls back to the Windows registry.
    Deduplicates by executable path.
    """
    found: list[BrowserInfo] = []
    seen: set[str] = set()

    for name, path_str, browser_type in _CANDIDATE_PATHS:
        p = Path(path_str)
        key = str(p).lower()
        if key in seen:
            continue
        if p.exists():
            seen.add(key)
            found.append(BrowserInfo(name=name, exe_path=p, browser_type=browser_type))

    # Supplement with registry entries (catches non-standard install locations)
    try:
        import winreg
        with winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE,
            r"SOFTWARE\Clients\StartMenuInternet",
        ) as base:
            i = 0
            while True:
                try:
                    sub_name = winreg.EnumKey(base, i)
                    i += 1
                    try:
                        with winreg.OpenKey(
                            base, sub_name + r"\shell\open\command"
                        ) as cmd_key:
                            cmd_val, _ = winreg.QueryValueEx(cmd_key, "")
                            raw = cmd_val.strip().strip('"')
                            # Handle quoted paths like: "C:\...\chrome.exe" --args
                            exe_str = raw.split('"')[0] if '"' in cmd_val else raw.split()[0]
                            exe_path = Path(exe_str)
                            key = str(exe_path).lower()
                            if key not in seen and exe_path.exists():
                                seen.add(key)
                                btype = "firefox" if "firefox" in key else "chromium"
                                found.append(
                                    BrowserInfo(name=sub_name, exe_path=exe_path, browser_type=btype)
                                )
                    except (OSError, IndexError, ValueError):
                        pass
                except OSError:
                    break
    except (ImportError, OSError):
        pass  # Non-Windows or registry unavailable

    return found


def pick_best_browser(
    exe_override: str = "",
    type_override: str = "",
) -> BrowserInfo | None:
    """Return the best available browser, respecting any explicit overrides.

    Args:
      exe_override: Absolute path to a browser executable (from env var).
      type_override: "chromium" or "firefox" to filter candidates.

    Returns None if no browser is found.
    """
    if exe_override:
        p = Path(exe_override)
        if p.exists():
            btype = type_override or (
                "firefox" if "firefox" in exe_override.lower() else "chromium"
            )
            return BrowserInfo(name=p.stem, exe_path=p, browser_type=btype)

    browsers = detect_system_browsers()
    if not browsers:
        return None

    if type_override:
        for b in browsers:
            if b.browser_type == type_override:
                return b

    return browsers[0]
