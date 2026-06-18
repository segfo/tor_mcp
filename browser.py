"""Playwright-driven browser with smart Tor/direct routing.

Two Camoufox (Firefox) sessions are managed:
  - Tor session   : all traffic via SOCKS5 127.0.0.1:9050 (used for .onion and
                    as fallback when direct fails).
  - Direct session: no proxy (used first for ordinary clearnet URLs).

Routing (goto_smart):
  1. .onion URL         → Tor session always.
  2. Clearnet URL       → try direct; if it raises/times-out → retry via Tor.

The "active session" (whichever was last successfully navigated) is returned by
get_session(), so browser_click / browser_fill / browser_state all operate on
the same session the agent just navigated to.

Camoufox replaces Chromium: fingerprinting is patched at the C++ level
(navigator, WebGL, fonts, screen, WebRTC), making automation much harder to
detect than JS-injection approaches.

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
import socket
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlparse

import anyio
from camoufox.async_api import AsyncCamoufox
from playwright.async_api import (
    Browser,
    BrowserContext,
    Page,
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
# Session singletons and smart routing
# ---------------------------------------------------------------------------

_TOR_SESSION: Optional[BrowserSession] = None
_DIRECT_SESSION: Optional[BrowserSession] = None
# Points to the session most recently used for navigation; get_session() returns it
# so that click / fill / state all operate on the same tab the agent just opened.
_ACTIVE_SESSION: Optional[BrowserSession] = None


def _get_tor_session(config: Optional[TorConfig] = None) -> BrowserSession:
    global _TOR_SESSION
    if _TOR_SESSION is None:
        _TOR_SESSION = BrowserSession(config, use_tor=True)
    return _TOR_SESSION


def _get_direct_session(config: Optional[TorConfig] = None) -> BrowserSession:
    global _DIRECT_SESSION
    if _DIRECT_SESSION is None:
        _DIRECT_SESSION = BrowserSession(config, use_tor=False)
    return _DIRECT_SESSION


def get_session(config: Optional[TorConfig] = None) -> BrowserSession:
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
    """Navigate with Tor/direct routing.

    mode:
      "auto"   — .onion → Tor; clearnet → Direct first.
                 Closed windows are re-launched automatically on the first retry.
                 If Direct ultimately fails, traffic falls back to Tor.
      "tor"    — Always use Tor (clearnet and .onion alike).
      "direct" — Always use Direct; .onion URLs are rejected.
                 Closed window is re-launched automatically on the first retry.
    """
    global _ACTIVE_SESSION

    if mode == "tor" or (mode == "auto" and _is_onion(url)):
        session = _get_tor_session(config)
        result = await session.goto(url, wait_until=wait_until)
        result["via"] = "tor"
        _ACTIVE_SESSION = session
        return result

    if mode == "direct":
        if _is_onion(url):
            raise BrowserError(".onion URL cannot be opened in direct mode")
        direct = _get_direct_session(config)
        result = await direct.goto(url, wait_until=wait_until)
        result["via"] = "direct"
        _ACTIVE_SESSION = direct
        return result

    # mode == "auto", clearnet: try Direct first, fallback to Tor on any error
    direct = _get_direct_session(config)
    try:
        result = await direct.goto(url, wait_until=wait_until)
        result["via"] = "direct"
        _ACTIVE_SESSION = direct
        return result
    except Exception:
        pass

    tor = _get_tor_session(config)
    result = await tor.goto(url, wait_until=wait_until)
    result["via"] = "tor"
    _ACTIVE_SESSION = tor
    return result


async def shutdown_session_async() -> None:
    global _TOR_SESSION, _DIRECT_SESSION, _ACTIVE_SESSION
    for s in (_TOR_SESSION, _DIRECT_SESSION):
        if s is not None:
            try:
                await s.close()
            except Exception:  # noqa: BLE001
                pass
    _TOR_SESSION = None
    _DIRECT_SESSION = None
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
