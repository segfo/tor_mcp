"""Playwright-driven browser with smart Tor/Direct routing.

Two connection modes are exposed:
  - Tor connection   : all traffic via SOCKS5 127.0.0.1:9050. Always Camoufox.
                       Used for .onion and as a fallback when Direct fails.
  - Direct connection: no proxy (ordinary clearnet URLs). The engine is chosen
                       by TOR_MCP_DIRECT_BROWSER (see _resolve_direct_engine):
                         "system"  → Playwright-launched installed browser
                         "cdp"     → real Chrome attached over CDP (real profile,
                                     extensions, not bot-detected)
                         "camoufox"→ Camoufox
                         ""(auto)  → system browser if installed, else Camoufox.

Routing (goto_smart):
  1. .onion URL    → Tor (always).
  2. Clearnet URL  → Direct (resolved engine); in "auto" mode, a failing Direct
                     connection falls back to Tor.

The "active session" (whichever was last successfully navigated) is returned by
get_session(), so browser_click / browser_fill / browser_state all operate on
the same session the agent just navigated to.

Browser engines used: Camoufox (Firefox-based, C++-level anti-fingerprinting)
and the user's system-installed browser via an explicit executable_path.
Playwright's own bundled browsers are never launched (see docs/BROWSERS.md).

Async by design: Playwright's sync API is thread-affine and FastMCP runs sync
tools on a worker-thread pool, which would break any shared browser object.
The async API + async tools run on FastMCP's single event loop, so singletons
can be shared safely.

Ethical scope: authorized investigation only. Logins must use your own
authorized credentials. No authentication bypass / unauthorized access.
"""

from __future__ import annotations

import asyncio
import json
import os
import socket
import subprocess
import urllib.request
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlparse

import anyio
from camoufox.async_api import AsyncCamoufox
from playwright.async_api import (
    Browser,
    BrowserContext,
    Page,
    async_playwright,
)
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from .config import TorConfig
from .fetch import _html_to_text
from .tor_process import TorError, get_tor


def _port_listening(port: int, host: str = "127.0.0.1") -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        return s.connect_ex((host, port)) == 0


def _is_onion(url: str) -> bool:
    host = urlparse(url).hostname or ""
    return host.endswith(".onion")


class BrowserError(RuntimeError):
    """Raised when the browser session cannot be created or driven."""


class BrowserSession:
    """A single long-lived Camoufox/Firefox session (Tor-proxied or direct)."""

    engine_label = "camoufox"

    def __init__(self, config: Optional[TorConfig] = None, use_tor: bool = True) -> None:
        self.config = config or TorConfig.from_env()
        self.use_tor = use_tor
        self._camoufox: Optional[AsyncCamoufox] = None
        self._browser: Optional[Browser] = None
        self._context: Optional[BrowserContext] = None
        self._page: Optional[Page] = None
        self._lock = asyncio.Lock()

    @property
    def is_open(self) -> bool:
        return self._context is not None and self._page is not None

    async def ensure(self) -> Page:
        async with self._lock:
            if self.is_open:
                return self._page  # type: ignore[return-value]

            if self.use_tor:
                try:
                    await anyio.to_thread.run_sync(get_tor, self.config)
                except TorError:
                    if not _port_listening(self.config.socks_port):
                        raise

            proxy = (
                {"server": f"socks5://127.0.0.1:{self.config.socks_port}"}
                if self.use_tor
                else None
            )

            try:
                kwargs: dict[str, Any] = dict(
                    headless=self.config.headless,
                    os="windows",
                    window=(self.config.browser_width, self.config.browser_height),
                )
                if proxy:
                    kwargs["proxy"] = proxy
                    kwargs["geoip"] = True

                # Ensure visible window in headful mode on Windows
                launch_args: list[str] | None = None
                if not self.config.headless:
                    launch_args = ["--new-window", "--no-remote"]

                self._camoufox = AsyncCamoufox(**kwargs, args=launch_args)
                self._browser = await self._camoufox.__aenter__()
                self._context = await self._browser.new_context(
                    ignore_https_errors=True,
                    viewport={"width": self.config.browser_width, "height": self.config.browser_height},
                )
            except Exception as exc:  # noqa: BLE001
                await self._teardown()
                raise BrowserError(f"Failed to launch Camoufox: {exc}") from exc

            # Restore cookies/localStorage saved from previous session
            storage_file = self._storage_state_path()
            if storage_file.exists():
                try:
                    state = json.loads(storage_file.read_text(encoding="utf-8"))
                    cookies = state.get("cookies", [])
                    if cookies:
                        await self._context.add_cookies(cookies)
                except Exception:  # noqa: BLE001
                    pass  # Ignore corrupt file; start fresh

            self._context.set_default_navigation_timeout(self.config.nav_timeout_ms)
            self._context.set_default_timeout(self.config.nav_timeout_ms)
            self._page = await self._context.new_page()
            return self._page

    def _storage_state_path(self) -> Path:
        suffix = "tor" if self.use_tor else "direct"
        return self.config.browser_profile_dir / f"storage-state-{suffix}.json"

    async def _save_storage_state(self) -> None:
        if self._context is None:
            return
        try:
            # Use cookies() instead of storage_state() for Camoufox/Firefox compatibility
            cookies = await self._context.cookies()
            state = {"cookies": cookies}
            storage_file = self._storage_state_path()
            storage_file.parent.mkdir(parents=True, exist_ok=True)
            storage_file.write_text(json.dumps(state), encoding="utf-8")
        except Exception as exc:  # noqa: BLE001
            import sys
            print(f"[tor_mcp] _save_storage_state failed: {exc}", file=sys.stderr)

    async def close(self) -> None:
        async with self._lock:
            await self._save_storage_state()
            await self._teardown()

    async def reset(self) -> None:
        async with self._lock:
            # Wipe saved state as well
            for f in (self._storage_state_path(),):
                try:
                    f.unlink(missing_ok=True)
                except Exception:  # noqa: BLE001
                    pass
            await self._teardown()

    async def _teardown(self) -> None:
        try:
            if self._context is not None:
                await self._context.close()
        except Exception:  # noqa: BLE001
            pass
        try:
            if self._camoufox is not None:
                await self._camoufox.__aexit__(None, None, None)
        except Exception:  # noqa: BLE001
            pass
        self._context = None
        self._page = None
        self._camoufox = None
        self._browser = None

    # --- actions ----------------------------------------------------------

    async def goto(self, url: str, wait_until: str = "load") -> dict[str, Any]:
        page = await self.ensure()
        try:
            resp = await page.goto(url, wait_until=wait_until)  # type: ignore[arg-type]
        except Exception as exc:
            # Window was closed externally (user closed it, crash, etc.)
            # Tear down stale state and retry once with a fresh window.
            if "closed" in str(exc).lower():
                await self._teardown()
                page = await self.ensure()
                resp = await page.goto(url, wait_until=wait_until)  # type: ignore[arg-type]
            else:
                raise
        out = await self._snapshot(page, render="text")
        out["status"] = resp.status if resp else None
        # Persist cookies after each navigation so they survive process termination
        await self._save_storage_state()
        return out

    async def state(self, render: str = "text") -> dict[str, Any]:
        page = await self.ensure()
        return await self._snapshot(page, render=render)

    async def click(self, selector: str) -> dict[str, Any]:
        page = await self.ensure()
        await page.click(selector, timeout=self.config.nav_timeout_ms)
        out = await self._snapshot(page, render="text")
        # Save after click: may trigger form submit / redirect that sets session cookies
        await self._save_storage_state()
        return out

    async def fill(self, selector: str, value: str) -> dict[str, Any]:
        page = await self.ensure()
        await page.fill(selector, value, timeout=self.config.nav_timeout_ms)
        return {"ok": True, "selector": selector, "filled_chars": len(value)}

    async def press(self, selector: str, key: str) -> dict[str, Any]:
        page = await self.ensure()
        await page.press(selector, key, timeout=self.config.nav_timeout_ms)
        return await self._snapshot(page, render="text")

    async def wait_for(
        self, kind: str, value: Optional[str] = None, timeout_ms: Optional[int] = None
    ) -> dict[str, Any]:
        """Wait for a condition.

        kind: "selector" / "selector_gone" / "url_change" / "text" / "load"
        """
        page = await self.ensure()
        timeout = timeout_ms or self.config.browser_wait_timeout_ms
        before_url = page.url
        try:
            if kind == "selector":
                await page.wait_for_selector(value, state="visible", timeout=timeout)  # type: ignore[arg-type]
            elif kind == "selector_gone":
                await page.wait_for_selector(value, state="hidden", timeout=timeout)  # type: ignore[arg-type]
            elif kind == "url_change":
                await page.wait_for_url(lambda u: u != before_url, timeout=timeout)  # noqa: B023
            elif kind == "text":
                await page.wait_for_selector(f"text={value}", timeout=timeout)
            elif kind == "load":
                await page.wait_for_load_state("networkidle", timeout=timeout)
            else:
                raise BrowserError(f"Unknown wait kind: {kind!r}")
            timed_out = False
        except PlaywrightTimeoutError:
            timed_out = True
        out = await self._snapshot(page, render="text")
        out["timed_out"] = timed_out
        out["url_changed"] = page.url != before_url
        return out

    async def screenshot(self, full_page: bool = False) -> dict[str, Any]:
        page = await self.ensure()
        self.config.screenshot_dir.mkdir(parents=True, exist_ok=True)
        from datetime import datetime
        name = datetime.now().strftime("shot-%Y%m%d-%H%M%S-%f.png")
        path = self.config.screenshot_dir / name
        await page.screenshot(path=str(path), full_page=full_page)
        return {"ok": True, "path": str(path), "url": page.url}

    async def evaluate(self, js: str) -> dict[str, Any]:
        page = await self.ensure()
        result = await page.evaluate(js)
        return {"ok": True, "result": result}

    # --- helpers ----------------------------------------------------------

    async def _snapshot(self, page: Page, render: str) -> dict[str, Any]:
        url = page.url
        try:
            title = await page.title()
        except Exception:  # noqa: BLE001
            title = ""
        out: dict[str, Any] = {"url": url, "title": title}
        html = await page.content()
        if render == "html":
            out["html"] = html[: self.config.max_response_bytes]
            return out
        text, _title, links = _html_to_text(html, url)
        out["text"] = text[:20000]
        out["text_truncated"] = len(text) > 20000
        out["links"] = links
        out["interactive"] = await self._interactive_elements(page)
        return out

    async def _interactive_elements(self, page: Page) -> list[dict[str, Any]]:
        js = r"""
        () => {
          const out = [];
          const sel = (el) => {
            if (el.id) return '#' + CSS.escape(el.id);
            if (el.name) return el.tagName.toLowerCase() + '[name="' + el.name + '"]';
            return null;
          };
          const nodes = document.querySelectorAll(
            'input, textarea, select, button, a[href], [role=button]'
          );
          for (const el of nodes) {
            if (out.length >= 80) break;
            const rect = el.getBoundingClientRect();
            if (rect.width === 0 && rect.height === 0) continue;
            out.push({
              tag: el.tagName.toLowerCase(),
              type: el.getAttribute('type') || null,
              name: el.getAttribute('name') || null,
              id: el.id || null,
              text: (el.innerText || el.value || el.placeholder || '').trim().slice(0, 80),
              selector: sel(el),
            });
          }
          return out;
        }
        """
        try:
            return await page.evaluate(js)
        except Exception:  # noqa: BLE001
            return []


# ---------------------------------------------------------------------------
# System browser session (clearnet surface-web profile)
# ---------------------------------------------------------------------------


class SystemBrowserSession:
    """Drive a system-installed browser (Chrome/Edge/Brave/Firefox) via Playwright.

    Unlike BrowserSession (Camoufox), this uses the real browser the user has
    installed, which handles JS-heavy SPAs and strict UA-check sites that
    Camoufox sometimes fails on. No Tor routing — clearnet only.
    """

    engine_label = "system"

    def __init__(self, config: Optional[TorConfig] = None) -> None:
        self.config = config or TorConfig.from_env()
        self._pw = None                        # Playwright instance
        self._browser: Optional[Browser] = None  # Only used for Firefox
        self._context: Optional[BrowserContext] = None
        self._page: Optional[Page] = None
        self._lock = asyncio.Lock()
        self._browser_info = None              # BrowserInfo set on first launch

    @property
    def is_open(self) -> bool:
        return self._context is not None and self._page is not None

    async def ensure(self) -> Page:
        async with self._lock:
            if self.is_open:
                return self._page  # type: ignore[return-value]

            from .system_browser import pick_best_browser
            info = pick_best_browser(
                exe_override=self.config.clearnet_browser_exe,
                type_override=self.config.clearnet_browser_type,
            )
            if info is None or info.exe_path is None or not Path(info.exe_path).exists():
                # Hard guard: we always drive a real, installed browser via an
                # explicit executable_path. We never fall back to a Playwright
                # bundled browser (chromium/firefox/webkit). See docs/BROWSERS.md.
                raise BrowserError(
                    "No system browser found. "
                    "Install Chrome, Edge, Brave, or Firefox, "
                    "or set TOR_MCP_CLEARNET_BROWSER_EXE to the full path."
                )
            self._browser_info = info

            profile_dir = self.config.clearnet_profile_dir
            profile_dir.mkdir(parents=True, exist_ok=True)

            try:
                self._pw = await async_playwright().start()

                if info.browser_type == "chromium":
                    # executable_path is always set → Playwright's bundled
                    # browser is never launched (intentional, see docs/BROWSERS.md).
                    # launch_persistent_context keeps cookies/session automatically.
                    self._context = await self._pw.chromium.launch_persistent_context(
                        user_data_dir=str(profile_dir),
                        executable_path=str(info.exe_path),
                        headless=self.config.headless,
                        viewport={"width": self.config.browser_width,
                                  "height": self.config.browser_height},
                        ignore_https_errors=True,
                        args=["--no-first-run", "--no-default-browser-check"],
                    )
                    pages = self._context.pages
                    self._page = pages[0] if pages else await self._context.new_page()
                else:
                    # Firefox: manual cookie persistence (same pattern as BrowserSession).
                    # executable_path is always set → bundled Firefox is never used.
                    self._browser = await self._pw.firefox.launch(
                        executable_path=str(info.exe_path),
                        headless=self.config.headless,
                    )
                    self._context = await self._browser.new_context(
                        ignore_https_errors=True,
                        viewport={"width": self.config.browser_width,
                                  "height": self.config.browser_height},
                    )
                    storage_file = self._storage_state_path()
                    if storage_file.exists():
                        try:
                            state = json.loads(storage_file.read_text(encoding="utf-8"))
                            cookies = state.get("cookies", [])
                            if cookies:
                                await self._context.add_cookies(cookies)
                        except Exception:  # noqa: BLE001
                            pass
                    self._page = await self._context.new_page()
            except Exception as exc:  # noqa: BLE001
                await self._teardown()
                raise BrowserError(f"Failed to launch system browser: {exc}") from exc

            self._context.set_default_navigation_timeout(self.config.nav_timeout_ms)
            self._context.set_default_timeout(self.config.nav_timeout_ms)
            return self._page  # type: ignore[return-value]

    def _storage_state_path(self) -> Path:
        return self.config.clearnet_profile_dir / "storage-state-clearnet.json"

    async def _save_storage_state(self) -> None:
        if self._context is None:
            return
        if self._browser_info and self._browser_info.browser_type == "firefox":
            try:
                cookies = await self._context.cookies()
                state = {"cookies": cookies}
                self._storage_state_path().write_text(json.dumps(state), encoding="utf-8")
            except Exception as exc:  # noqa: BLE001
                import sys
                print(f"[tor_mcp] SystemBrowserSession save failed: {exc}", file=sys.stderr)
        # Chromium persistent context handles storage automatically

    async def close(self) -> None:
        async with self._lock:
            await self._save_storage_state()
            await self._teardown()

    async def reset(self) -> None:
        async with self._lock:
            try:
                self._storage_state_path().unlink(missing_ok=True)
            except Exception:  # noqa: BLE001
                pass
            await self._teardown()

    async def _teardown(self) -> None:
        for obj, method in (
            (self._context, "close"),
            (self._browser, "close"),
        ):
            if obj is not None:
                try:
                    await getattr(obj, method)()
                except Exception:  # noqa: BLE001
                    pass
        if self._pw is not None:
            try:
                await self._pw.stop()
            except Exception:  # noqa: BLE001
                pass
        self._context = None
        self._page = None
        self._browser = None
        self._pw = None

    # --- actions (same interface as BrowserSession) ---

    async def goto(self, url: str, wait_until: str = "load") -> dict[str, Any]:
        page = await self.ensure()
        try:
            resp = await page.goto(url, wait_until=wait_until)  # type: ignore[arg-type]
        except Exception as exc:
            if "closed" in str(exc).lower():
                await self._teardown()
                page = await self.ensure()
                resp = await page.goto(url, wait_until=wait_until)  # type: ignore[arg-type]
            else:
                raise
        out = await self._snapshot(page, render="text")
        out["status"] = resp.status if resp else None
        await self._save_storage_state()
        return out

    async def state(self, render: str = "text") -> dict[str, Any]:
        page = await self.ensure()
        return await self._snapshot(page, render=render)

    async def click(self, selector: str) -> dict[str, Any]:
        page = await self.ensure()
        await page.click(selector, timeout=self.config.nav_timeout_ms)
        out = await self._snapshot(page, render="text")
        await self._save_storage_state()
        return out

    async def fill(self, selector: str, value: str) -> dict[str, Any]:
        page = await self.ensure()
        await page.fill(selector, value, timeout=self.config.nav_timeout_ms)
        return {"ok": True, "selector": selector, "filled_chars": len(value)}

    async def press(self, selector: str, key: str) -> dict[str, Any]:
        page = await self.ensure()
        await page.press(selector, key, timeout=self.config.nav_timeout_ms)
        return await self._snapshot(page, render="text")

    async def wait_for(
        self, kind: str, value: Optional[str] = None, timeout_ms: Optional[int] = None
    ) -> dict[str, Any]:
        page = await self.ensure()
        timeout = timeout_ms or self.config.browser_wait_timeout_ms
        before_url = page.url
        try:
            if kind == "selector":
                await page.wait_for_selector(value, state="visible", timeout=timeout)  # type: ignore[arg-type]
            elif kind == "selector_gone":
                await page.wait_for_selector(value, state="hidden", timeout=timeout)  # type: ignore[arg-type]
            elif kind == "url_change":
                await page.wait_for_url(lambda u: u != before_url, timeout=timeout)  # noqa: B023
            elif kind == "text":
                await page.wait_for_selector(f"text={value}", timeout=timeout)
            elif kind == "load":
                await page.wait_for_load_state("networkidle", timeout=timeout)
            else:
                raise BrowserError(f"Unknown wait kind: {kind!r}")
            timed_out = False
        except PlaywrightTimeoutError:
            timed_out = True
        out = await self._snapshot(page, render="text")
        out["timed_out"] = timed_out
        out["url_changed"] = page.url != before_url
        return out

    async def screenshot(self, full_page: bool = False) -> dict[str, Any]:
        page = await self.ensure()
        self.config.screenshot_dir.mkdir(parents=True, exist_ok=True)
        from datetime import datetime
        name = datetime.now().strftime("shot-%Y%m%d-%H%M%S-%f.png")
        path = self.config.screenshot_dir / name
        await page.screenshot(path=str(path), full_page=full_page)
        return {"ok": True, "path": str(path), "url": page.url}

    async def evaluate(self, js: str) -> dict[str, Any]:
        page = await self.ensure()
        result = await page.evaluate(js)
        return {"ok": True, "result": result}

    async def _snapshot(self, page: Page, render: str) -> dict[str, Any]:
        url = page.url
        try:
            title = await page.title()
        except Exception:  # noqa: BLE001
            title = ""
        out: dict[str, Any] = {"url": url, "title": title}
        html = await page.content()
        if render == "html":
            out["html"] = html[: self.config.max_response_bytes]
            return out
        text, _title, links = _html_to_text(html, url)
        out["text"] = text[:20000]
        out["text_truncated"] = len(text) > 20000
        out["links"] = links
        out["interactive"] = await self._interactive_elements(page)
        return out

    async def _interactive_elements(self, page: Page) -> list[dict[str, Any]]:
        js = r"""
        () => {
          const out = [];
          const sel = (el) => {
            if (el.id) return '#' + CSS.escape(el.id);
            if (el.name) return el.tagName.toLowerCase() + '[name="' + el.name + '"]';
            return null;
          };
          const nodes = document.querySelectorAll(
            'input, textarea, select, button, a[href], [role=button]'
          );
          for (const el of nodes) {
            if (out.length >= 80) break;
            const rect = el.getBoundingClientRect();
            if (rect.width === 0 && rect.height === 0) continue;
            out.push({
              tag: el.tagName.toLowerCase(),
              type: el.getAttribute('type') || null,
              name: el.getAttribute('name') || null,
              id: el.id || null,
              text: (el.innerText || el.value || el.placeholder || '').trim().slice(0, 80),
              selector: sel(el),
            });
          }
          return out;
        }
        """
        try:
            return await page.evaluate(js)
        except Exception:  # noqa: BLE001
            return []


# ---------------------------------------------------------------------------
# CDP session (attach to a real Chrome over remote debugging)
# ---------------------------------------------------------------------------


def _cdp_endpoint(port: int) -> str:
    return f"http://127.0.0.1:{port}"


def _cdp_alive(port: int) -> bool:
    """True if a Chrome DevTools endpoint is answering on the port."""
    try:
        with urllib.request.urlopen(f"{_cdp_endpoint(port)}/json/version", timeout=1) as r:
            return r.status == 200
    except Exception:  # noqa: BLE001
        return False


class CdpBrowserSession(SystemBrowserSession):
    """Attach to a real Chrome over CDP (remote debugging).

    Drives the user's actual Chrome with a real profile (extensions, logins) and
    NO automation flags injected by Playwright — navigator.webdriver is unset and
    there is no "--no-sandbox"/automation banner, so it is far less bot-detectable
    than launch_persistent_context. Chrome is auto-launched with
    --remote-debugging-port (a dedicated user-data-dir by default) if not already up.

    Reuses SystemBrowserSession's action methods (goto/state/click/…); only the
    session lifecycle differs (attach vs launch; never wipe/close the real profile).
    """

    engine_label = "cdp"

    def __init__(self, config: Optional[TorConfig] = None) -> None:
        super().__init__(config)
        self._launched: Optional[subprocess.Popen] = None

    async def ensure(self) -> Page:
        async with self._lock:
            if self.is_open:
                return self._page  # type: ignore[return-value]

            port = self.config.cdp_port
            if not _cdp_alive(port):
                if not self.config.cdp_autolaunch:
                    raise BrowserError(
                        f"No Chrome with remote debugging on port {port}. "
                        f"Launch Chrome with --remote-debugging-port={port} or set "
                        f"TOR_MCP_CDP_AUTOLAUNCH=1."
                    )
                await anyio.to_thread.run_sync(self._launch_chrome)
                for _ in range(60):  # wait up to ~30s for the endpoint
                    if _cdp_alive(port):
                        break
                    await asyncio.sleep(0.5)
                else:
                    raise BrowserError(
                        f"Chrome did not expose CDP on port {port} within 30s. "
                        f"Another Chrome may already own user-data-dir "
                        f"'{self.config.cdp_user_data_dir}' — close it or use a dedicated dir."
                    )

            try:
                self._pw = await async_playwright().start()
                self._browser = await self._pw.chromium.connect_over_cdp(_cdp_endpoint(port))
                # The default context is the real profile; open a fresh tab so we
                # don't hijack the user's existing tabs.
                self._context = (
                    self._browser.contexts[0]
                    if self._browser.contexts
                    else await self._browser.new_context()
                )
                self._page = await self._context.new_page()
            except Exception as exc:  # noqa: BLE001
                await self._teardown()
                raise BrowserError(f"Failed to attach over CDP on port {port}: {exc}") from exc

            self._context.set_default_navigation_timeout(self.config.nav_timeout_ms)
            self._context.set_default_timeout(self.config.nav_timeout_ms)
            return self._page  # type: ignore[return-value]

    def _launch_chrome(self) -> None:
        from .system_browser import pick_best_browser
        exe = self.config.cdp_chrome_exe
        if not exe:
            info = pick_best_browser(type_override="chromium")
            if info is None:
                raise BrowserError(
                    "No Chromium-based browser found for CDP. Set TOR_MCP_CHROME_EXE."
                )
            exe = str(info.exe_path)
        user_data = Path(self.config.cdp_user_data_dir)
        user_data.mkdir(parents=True, exist_ok=True)
        args = [
            exe,
            f"--remote-debugging-port={self.config.cdp_port}",
            f"--user-data-dir={user_data}",
            f"--profile-directory={self.config.cdp_profile_dir}",
            "--no-first-run",
            "--no-default-browser-check",
            "about:blank",
        ]
        # Detach so the Chrome survives this backend process (logins persist).
        creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) if os.name == "nt" else 0
        self._launched = subprocess.Popen(args, creationflags=creationflags)

    async def _save_storage_state(self) -> None:
        # Real Chrome persists its own profile to disk; nothing to do.
        return

    async def _teardown(self) -> None:
        # Detach only — never close the user's Chrome tabs or kill the process,
        # so logins/queues survive across tool calls.
        try:
            if self._browser is not None:
                await self._browser.close()  # CDP: disconnects, leaves Chrome running
        except Exception:  # noqa: BLE001
            pass
        try:
            if self._pw is not None:
                await self._pw.stop()
        except Exception:  # noqa: BLE001
            pass
        self._context = None
        self._page = None
        self._browser = None
        self._pw = None

    async def reset(self) -> None:
        # Do NOT wipe the real profile; just detach.
        async with self._lock:
            await self._teardown()


# ---------------------------------------------------------------------------
# Session singletons and smart routing
# ---------------------------------------------------------------------------

_TOR_SESSION: Optional[BrowserSession] = None
_DIRECT_CAMOUFOX_SESSION: Optional[BrowserSession] = None
_DIRECT_SYSTEM_SESSION: Optional[SystemBrowserSession] = None
_DIRECT_CDP_SESSION: Optional[CdpBrowserSession] = None
# Points to the session most recently used for navigation; get_session() returns it
# so that click / fill / state all operate on the same tab the agent just opened.
_ACTIVE_SESSION: Optional["BrowserSession | SystemBrowserSession"] = None


def _get_tor_session(config: Optional[TorConfig] = None) -> BrowserSession:
    global _TOR_SESSION
    if _TOR_SESSION is None:
        _TOR_SESSION = BrowserSession(config, use_tor=True)
    return _TOR_SESSION


def _get_direct_camoufox_session(config: Optional[TorConfig] = None) -> BrowserSession:
    global _DIRECT_CAMOUFOX_SESSION
    if _DIRECT_CAMOUFOX_SESSION is None:
        _DIRECT_CAMOUFOX_SESSION = BrowserSession(config, use_tor=False)
    return _DIRECT_CAMOUFOX_SESSION


def _get_direct_system_session(config: Optional[TorConfig] = None) -> SystemBrowserSession:
    global _DIRECT_SYSTEM_SESSION
    if _DIRECT_SYSTEM_SESSION is None:
        _DIRECT_SYSTEM_SESSION = SystemBrowserSession(config)
    return _DIRECT_SYSTEM_SESSION


def _get_direct_cdp_session(config: Optional[TorConfig] = None) -> CdpBrowserSession:
    global _DIRECT_CDP_SESSION
    if _DIRECT_CDP_SESSION is None:
        _DIRECT_CDP_SESSION = CdpBrowserSession(config)
    return _DIRECT_CDP_SESSION


def _resolve_direct_engine(config: Optional[TorConfig] = None) -> str:
    """Decide which engine the direct (clearnet) connection should use.

    Returns "system", "camoufox", or "cdp" based on TOR_MCP_DIRECT_BROWSER and
    availability of a system-installed browser:
      "camoufox" → always Camoufox.
      "system"   → always the Playwright-launched system browser (raises if none).
      "cdp"      → attach to a real Chrome over CDP (real profile, not bot-detected).
      ""  (auto) → system browser if one is installed, else Camoufox.
    """
    from .system_browser import pick_best_browser
    cfg = config or TorConfig.from_env()
    choice = (cfg.direct_browser or "").strip().lower()
    if choice in ("camoufox", "system", "cdp"):
        return choice
    # auto: prefer the real system browser, fall back to Camoufox
    if pick_best_browser(cfg.clearnet_browser_exe, cfg.clearnet_browser_type) is not None:
        return "system"
    return "camoufox"


def _get_direct_session(
    config: Optional[TorConfig] = None,
) -> "BrowserSession | SystemBrowserSession":
    """Return the direct (no-Tor) session for the configured engine.

    Engine = system   → SystemBrowserSession (Playwright-launched installed browser).
    Engine = cdp      → CdpBrowserSession (attach to a real Chrome over CDP).
    Engine = camoufox → Camoufox direct session.
    Never falls back to a Playwright-bundled browser.
    """
    engine = _resolve_direct_engine(config)
    if engine == "system":
        return _get_direct_system_session(config)
    if engine == "cdp":
        return _get_direct_cdp_session(config)
    return _get_direct_camoufox_session(config)


def get_session(config: Optional[TorConfig] = None) -> "BrowserSession | SystemBrowserSession":
    """Return the active session (last navigated), or the Tor session if none yet."""
    if _ACTIVE_SESSION is not None:
        return _ACTIVE_SESSION
    return _get_tor_session(config)


async def goto_smart(
    url: str,
    config: Optional[TorConfig] = None,
    wait_until: str = "load",
    mode: str = "auto",
) -> dict[str, Any]:
    """Navigate with Tor/Direct routing.

    Two connection modes:
      "auto"   — .onion → Tor; clearnet → Direct. If Direct fails, fall back to Tor.
      "tor"    — Always route through Tor (Camoufox). Works for clearnet and .onion.
      "direct" — Clearnet connection (no Tor); .onion URLs are rejected.
                 The browser engine (system browser or Camoufox) is chosen by
                 TOR_MCP_DIRECT_BROWSER; see _resolve_direct_engine().
      "clearnet" — Deprecated alias for direct with the system-browser engine.

    The returned ``via`` field reports the path actually used: "tor",
    "direct(system)", "direct(camoufox)", or "tor_fallback".
    Closed windows are re-launched automatically on the first retry (in goto()).
    """
    global _ACTIVE_SESSION

    if mode == "tor" or (mode == "auto" and _is_onion(url)):
        session = _get_tor_session(config)
        result = await session.goto(url, wait_until=wait_until)
        result["via"] = "tor"
        _ACTIVE_SESSION = session
        return result

    if _is_onion(url):
        raise BrowserError(".onion URL cannot be opened in direct mode (use Tor)")

    # "clearnet" is a deprecated alias: force the system-browser engine.
    if mode == "clearnet":
        direct = _get_direct_system_session(config)
    else:
        direct = _get_direct_session(config)
    engine = getattr(direct, "engine_label", "camoufox")

    try:
        result = await direct.goto(url, wait_until=wait_until)
        result["via"] = f"direct({engine})"
        _ACTIVE_SESSION = direct
        return result
    except Exception:
        # In auto mode a flaky direct connection falls back to Tor; explicit
        # direct/clearnet requests surface the error to the caller.
        if mode != "auto":
            raise

    tor = _get_tor_session(config)
    result = await tor.goto(url, wait_until=wait_until)
    result["via"] = "tor_fallback"
    _ACTIVE_SESSION = tor
    return result


def list_profiles(config: Optional[TorConfig] = None) -> dict[str, Any]:
    """Return the two connection modes (Tor / Direct) and the resolved direct engine.

    Only Camoufox and system-installed browsers ever appear here — Playwright's
    bundled browsers are never used (see docs/BROWSERS.md).
    """
    from .system_browser import detect_system_browsers, pick_best_browser
    cfg = config or TorConfig.from_env()
    selected = pick_best_browser(cfg.clearnet_browser_exe, cfg.clearnet_browser_type)
    direct_engine = _resolve_direct_engine(cfg)

    # Tor connection: always Camoufox.
    profiles: list[dict[str, Any]] = [
        {
            "name": "tor",
            "via": "tor",
            "engine": "camoufox",
            "status": "open" if (_TOR_SESSION and _TOR_SESSION.is_open) else "available",
        },
    ]

    # Direct connection: engine resolved from TOR_MCP_DIRECT_BROWSER.
    if direct_engine == "system":
        sys_open = bool(_DIRECT_SYSTEM_SESSION and _DIRECT_SYSTEM_SESSION.is_open)
        profiles.append({
            "name": "direct",
            "via": "direct",
            "engine": "system",
            "exe": str(selected.exe_path) if selected else None,
            "browser": selected.name if selected else None,
            "browser_type": selected.browser_type if selected else None,
            "status": "open" if sys_open else "available",
        })
    elif direct_engine == "cdp":
        cdp_open = bool(_DIRECT_CDP_SESSION and _DIRECT_CDP_SESSION.is_open)
        profiles.append({
            "name": "direct",
            "via": "direct",
            "engine": "cdp",
            "cdp_port": cfg.cdp_port,
            "cdp_endpoint_alive": _cdp_alive(cfg.cdp_port),
            "user_data_dir": str(cfg.cdp_user_data_dir),
            "profile_directory": cfg.cdp_profile_dir,
            "chrome_exe": cfg.cdp_chrome_exe or (str(selected.exe_path) if selected else None),
            "autolaunch": cfg.cdp_autolaunch,
            "status": "open" if cdp_open else "available",
        })
    else:
        cam_open = bool(_DIRECT_CAMOUFOX_SESSION and _DIRECT_CAMOUFOX_SESSION.is_open)
        profiles.append({
            "name": "direct",
            "via": "direct",
            "engine": "camoufox",
            "status": "open" if cam_open else "available",
        })

    # Surface every detected system browser for visibility / override hints.
    detected = [
        {"name": b.name, "exe": str(b.exe_path), "browser_type": b.browser_type,
         "selected": selected is not None and str(b.exe_path) == str(selected.exe_path)}
        for b in detect_system_browsers()
    ]

    return {
        "profiles": profiles,
        "direct_engine": direct_engine,
        "detected_system_browsers": detected,
        "env_hints": {
            "TOR_MCP_DIRECT_BROWSER": cfg.direct_browser or "(auto: system→camoufox)",
            "TOR_MCP_CLEARNET_BROWSER_EXE": cfg.clearnet_browser_exe or "(auto)",
            "TOR_MCP_CLEARNET_BROWSER_TYPE": cfg.clearnet_browser_type or "(auto)",
            "TOR_MCP_CDP_PORT": cfg.cdp_port,
            "TOR_MCP_CHROME_USER_DATA_DIR": str(cfg.cdp_user_data_dir),
            "TOR_MCP_CHROME_PROFILE_DIR": cfg.cdp_profile_dir,
        },
    }


async def shutdown_session_async() -> None:
    global _TOR_SESSION, _DIRECT_CAMOUFOX_SESSION, _DIRECT_SYSTEM_SESSION
    global _DIRECT_CDP_SESSION, _ACTIVE_SESSION
    for s in (_TOR_SESSION, _DIRECT_CAMOUFOX_SESSION, _DIRECT_SYSTEM_SESSION, _DIRECT_CDP_SESSION):
        if s is not None:
            try:
                await s.close()
            except Exception:  # noqa: BLE001
                pass
    _TOR_SESSION = None
    _DIRECT_CAMOUFOX_SESSION = None
    _DIRECT_SYSTEM_SESSION = None
    _DIRECT_CDP_SESSION = None
    _ACTIVE_SESSION = None


if __name__ == "__main__":
    # Smoke test:
    #   PYTHONUTF8=1 uv run python -m tor_mcp.browser --smoke --headless
    #   PYTHONUTF8=1 uv run python -m tor_mcp.browser --headless --url https://example.com
    import argparse

    parser = argparse.ArgumentParser(description="Browser smoke test")
    parser.add_argument("--smoke", action="store_true")
    parser.add_argument("--url", default="https://check.torproject.org/")
    parser.add_argument("--headless", action="store_true")
    parser.add_argument("--keep-open", action="store_true", help="Keep browser open until Enter is pressed")
    args = parser.parse_args()

    async def _main() -> None:
        cfg = TorConfig.from_env()
        if args.headless:
            cfg.headless = True
        print(f"Opening {args.url} via smart routing...")
        result = await goto_smart(args.url, cfg)
        print(f"via={result.get('via')} status={result.get('status')} title={result.get('title')!r}")
        if args.smoke:
            text = result.get("text", "")
            ok = "Congratulations. This browser is configured to use Tor." in text
            print(f"Tor-confirmed: {ok}")
        if args.keep_open or not cfg.headless:
            await anyio.to_thread.run_sync(lambda: input("Press Enter to close browser..."))
        await shutdown_session_async()

    if args.smoke or args.url:
        anyio.run(_main)
    else:
        parser.print_help()
