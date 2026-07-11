"""Tor MCP server (stdio) — thin proxy to the shared backend.

Exposes tools to reach .onion / Web resources through Tor. The actual Tor
process and Playwright browser live in a single shared backend process
(backend.py) so that every `claude -p` invocation and the interactive console
share ONE browser/login/CAPTCHA-cleared state. This stdio server merely forwards
each tool call to that backend (starting it on first use).

Ethical scope: this server is for **passive** OSINT collection only (viewing and
collecting publicly reachable text). It must not be used for unauthorized access,
acquisition/trade of illegal content, or any active attack.
"""

from __future__ import annotations

import json
import re
from typing import Any

from mcp.server.fastmcp import FastMCP

from .config import TorConfig
from . import backend_client

# Shared config instance (port overrides honored via environment variables).
config = TorConfig.from_env()

mcp = FastMCP("tor-osint")


def _j(obj: Any) -> str:
    """Return tool results as a flat JSON string.

    Some LLMs (LM Studio / OpenAI-compat) reject tool_result.content as an array,
    which is what FastMCP emits for dict/list returns. A plain str becomes a
    single text block those models accept.
    """
    return json.dumps(obj, ensure_ascii=False, indent=2)


def _tor_disabled_error() -> str:
    return _j({"error": "tor_disabled",
               "message": "Tor is disabled for this run (TOR_MCP_DISABLE_TOR). This tool was not invoked."})


@mcp.tool()
async def tor_check() -> str:
    """Verify that outbound traffic is routed through Tor and report the exit IP.

    Starts the shared backend + managed Tor process if not running yet (first
    call may take ~10-40s while Tor bootstraps). Returns whether the connection
    is recognized as Tor and the current exit-node IP address.
    """
    if config.disable_tor:
        return _tor_disabled_error()
    return _j(await backend_client.call(config, "tor_check", {}))


@mcp.tool()
async def tor_fetch(url: str, render: str = "text", max_text_chars: int = 20000,
                    save_path: str = "") -> str:
    """Fetch an http(s) URL (including .onion) through Tor and return its content.

    Args:
      url: The target http/https URL. .onion addresses are supported.
      render: "text" (default) cleans HTML to readable text plus title and
        extracted links; "raw_head" returns the head of the raw decoded body.
      max_text_chars: Cap on returned text length (default 20000).
      save_path: If given (absolute path recommended), the full body is written
        to that file and the response returns only lightweight metadata
        (saved_path, title, links, an 800-char preview) instead of the full text.
        Use this for collection so large pages don't fill the LLM context.

    Binary / non-textual responses are not downloaded; only metadata is reported.
    For passive OSINT collection only.
    """
    if config.disable_tor:
        return _tor_disabled_error()
    return _j(await backend_client.call(
        config, "tor_fetch",
        {"url": url, "render": render, "max_text_chars": max_text_chars, "save_path": save_path},
    ))


@mcp.tool()
async def tor_new_circuit(verify: bool = True) -> str:
    """Request fresh Tor circuits (a new exit node) to vary the egress IP.

    Useful to avoid rate limits / per-IP blocks during investigation. When
    verify=True (default), reports the exit IP before and after and whether it
    changed (a change is likely but not guaranteed on every request).
    """
    if config.disable_tor:
        return _tor_disabled_error()
    return _j(await backend_client.call(config, "tor_new_circuit", {"verify": verify}))


@mcp.tool()
async def onion_search(query: str, max_results: int = 20, engine: str = "tordex") -> str:
    """Search a Tor search engine for .onion sites matching a query (via Tor).

    Args:
      query: Search terms.
      max_results: Max results to return (default 20).
      engine: "tordex" (default) or "torch".

    Returns a list of {title, onion_url, snippet}. Use tor_fetch to retrieve the
    content of any returned onion URL. For passive OSINT discovery only — results
    are unmoderated and must be assessed by the investigator.
    """
    if config.disable_tor:
        return _tor_disabled_error()
    return _j(await backend_client.call(
        config, "onion_search",
        {"query": query, "max_results": max_results, "engine": engine},
    ))


# --- Playwright browser tools (stateful, Tor-routed, shared across processes) ---
#
# These drive a real browser kept alive in the shared backend, so JavaScript
# runs, waiting queues can be ridden out, forms can be filled, and login/CAPTCHA
# state persists ACROSS separate `claude -p` runs and the interactive console.
# The browser is headful by default (set TOR_MCP_HEADLESS=1 to hide it) so a
# human can solve CAPTCHAs / log in.
#
# Ethical scope: authorized investigation only. Log in with your own authorized
# credentials (preferably via the human in the headful window). No auth bypass.


@mcp.tool()
async def browser_open(
    url: str,
    wait_until: str = "load",
    save_path: str = "",
    via: str = "auto",
) -> str:
    """Open a URL in the shared browser and return its state.

    via (connection mode):
      "auto"   — .onion → Tor; clearnet → Direct. Falls back to Tor if Direct fails (default).
      "tor"    — Always route through Tor (Camoufox). Works for clearnet and .onion.
      "direct" — Clearnet connection (no Tor); .onion URLs are rejected. The browser
                 engine is chosen by TOR_MCP_DIRECT_BROWSER: "cdp" (real Chrome over
                 CDP — real profile/extensions, not bot-detected), "system"
                 (Playwright-launched browser), or "camoufox". Run
                 browser_list_profiles() to see which engine is active.
      "clearnet" — Deprecated alias for "direct" forced to the system-browser engine.

    The ``via`` field in the result indicates which path was actually used:
    "tor", "direct(system)", "direct(camoufox)", or "tor_fallback".

    Args:
      url: Target http/https/.onion URL.
      wait_until: Navigation completion signal — "load" (default),
        "domcontentloaded", "networkidle", or "commit".

    Returns the final url, title, readable text, links, and a list of
    interactive elements (with selectors) you can pass to browser_click /
    browser_fill. First use may take ~10-40s (Tor bootstrap + browser launch).

    save_path: If given (absolute path recommended), the page body is written to
    that file and the response returns only lightweight metadata (saved_path,
    title, links, interactive elements, an 800-char preview) instead of the full
    text — use this for collection so large pages don't fill the LLM context.
    """
    if config.disable_tor:
        resolves_to_tor = via == "tor" or (via == "auto" and bool(re.search(r"\.onion(?:[:/]|$)", url, re.I)))
        if resolves_to_tor:
            return _tor_disabled_error()
    return _j(await backend_client.call(
        config, "browser_open",
        {"url": url, "wait_until": wait_until, "save_path": save_path, "mode": via}
    ))


@mcp.tool()
async def browser_state(render: str = "text", save_path: str = "") -> str:
    """Report the current page without navigating (poll a queue, re-read state).

    render: "text" (default) → url/title/text/links/interactive elements;
    "html" → raw page HTML (capped).
    save_path: If given, the page body is saved to that file and only lightweight
    metadata is returned (see browser_open) — use this for collection.
    """
    return _j(await backend_client.call(
        config, "browser_state", {"render": render, "save_path": save_path}
    ))


@mcp.tool()
async def browser_click(selector: str) -> str:
    """Click an element (link, button, queue-advance control) by CSS selector.

    Returns the page state after the click. Use selectors from the
    `interactive` list returned by browser_open / browser_state.
    """
    return _j(await backend_client.call(config, "browser_click", {"selector": selector}))


@mcp.tool()
async def browser_fill(selector: str, value: str) -> str:
    """Type `value` into an input/textarea identified by CSS selector.

    For credentials, prefer browser_login (reads secrets from env so they are
    not written into the conversation transcript).
    """
    return _j(await backend_client.call(
        config, "browser_fill", {"selector": selector, "value": value}
    ))


@mcp.tool()
async def browser_wait(
    kind: str, value: str | None = None, timeout_ms: int | None = None
) -> str:
    """Wait for a condition — designed to ride out waiting queues / CAPTCHAs.

    Args:
      kind: "selector" (wait until `value` visible), "selector_gone" (until
        hidden), "url_change" (until URL differs), "text" (until `value` text
        appears), or "load" (networkidle).
      value: Selector or text, depending on kind.
      timeout_ms: Override the default wait timeout (queues default to minutes).

    Returns the page state plus `timed_out` and `url_changed` flags.
    """
    return _j(await backend_client.call(
        config, "browser_wait", {"kind": kind, "value": value, "timeout_ms": timeout_ms}
    ))


@mcp.tool()
async def browser_screenshot(full_page: bool = False) -> str:
    """Save a PNG screenshot of the current page and return its file path.

    Useful for a human to inspect CAPTCHAs / queue screens, or to record state.
    """
    return _j(await backend_client.call(config, "browser_screenshot", {"full_page": full_page}))


@mcp.tool()
async def browser_solve_captcha() -> str:
    """Attempt to clear the CAPTCHA on the current page (authorized sites only).

    Behavior depends on the server's TOR_MCP_CAPTCHA_MODE:
      - "human" (default): no auto-solve — returns solved=false, mode="human".
      - "ai": checkbox widgets (Turnstile / reCAPTCHA / hCaptcha "I'm not a
        robot") are clicked inside their iframe; classic image/text CAPTCHAs are
        read by the claude.exe vision harness (local VLM first, Anthropic
        fallback) and typed into the answer field. The answer is NOT submitted —
        press the submit/login control yourself afterward.
      - "off": no-op.

    Image-grid / picture-selection CAPTCHAs are never auto-solved.

    Returns {present, kind, mode, solved, message, answer?, image_path?}.
    If solved=false (mode=human, reason=needs_human, or a failed attempt), fall
    back to the human resume flow: emit `ERROR: auth_required` and stop. This is
    NOT an authentication-bypass tool — use it only on sites you may access.
    """
    return _j(await backend_client.call(config, "browser_solve_captcha", {}))


@mcp.tool()
async def browser_eval(js: str) -> str:
    """Run a JavaScript expression in the page and return its JSON result.

    Advanced/escape-hatch use — e.g. read a queue countdown value. The argument
    should be a JS function or expression, e.g. "() => document.title".
    """
    return _j(await backend_client.call(config, "browser_eval", {"js": js}))


@mcp.tool()
async def browser_login(
    url: str,
    user_selector: str,
    user_env: str,
    pass_selector: str,
    pass_env: str,
    submit_selector: str,
) -> str:
    """Fill and submit a login form using credentials read from env variables.

    Credentials are taken (inside the backend) from the environment variables
    named by `user_env` / `pass_env` (e.g. set SITE_USER / SITE_PASS in .env) —
    never passed as plain text — so they don't land in the transcript. For
    authorized accounts only.

    Args:
      url: Login page URL (opened first).
      user_selector / pass_selector: CSS selectors for the username/password fields.
      user_env / pass_env: Names of env vars holding the credentials.
      submit_selector: CSS selector for the submit button.
    """
    return _j(await backend_client.call(config, "browser_login", {
        "url": url,
        "user_selector": user_selector,
        "user_env": user_env,
        "pass_selector": pass_selector,
        "pass_env": pass_env,
        "submit_selector": submit_selector,
    }))


@mcp.tool()
async def browser_list_profiles() -> str:
    """List the two connection modes (Tor / Direct) and the resolved Direct engine.

    Returns:
    - profiles: "tor" (always Camoufox) and "direct" (engine = system or camoufox,
      resolved from TOR_MCP_DIRECT_BROWSER). The direct entry shows which system
      browser is selected when the engine is "system".
    - direct_engine: "system" or "camoufox" — what browser_open(via="direct") uses.
    - detected_system_browsers: Every installed browser found (the selected one is marked).
    - env_hints: Current TOR_MCP_DIRECT_BROWSER / CLEARNET_BROWSER_EXE / TYPE values.

    Playwright's bundled browsers never appear here — they are never launched.
    Run this before your first browser_open(via="direct") call to confirm the engine.
    """
    return _j(await backend_client.call(config, "browser_list_profiles", {}))


@mcp.tool()
async def browser_close() -> str:
    """Close the shared browser session (keeps saved profiles/cookies on disk)."""
    return _j(await backend_client.call(config, "browser_close", {}))


@mcp.tool()
async def browser_reset() -> str:
    """Close the active browser session and wipe its profile (drops all cookies/session).

    Only the currently active session's profile is deleted. Use browser_close to
    close the session without wiping profiles.
    """
    return _j(await backend_client.call(config, "browser_reset", {}))


def run() -> None:
    """Run the MCP server over stdio."""
    mcp.run()
