"""Shared Tor + browser backend (HTTP).

A single long-lived process that owns the managed Tor process and the Playwright
browser session, exposing every tool over a tiny HTTP API. Multiple stdio MCP
servers (one per `claude -p` invocation, plus the interactive console) all talk
to this one backend, so the browser window, login cookies and CAPTCHA-cleared
state are shared instead of each process spawning its own Tor + browser.

Started lazily by backend_client.ensure_backend() — the first process to use a
browser/tor tool spawns it; everyone else reuses it. Port-bind contention means
only one backend can own the port; redundant spawns exit immediately.

API:
  GET  /health        -> {"ok": true}
  POST /call          -> body {"tool": name, "args": {...}}
                         reply {"ok": true, "result": <json>} | {"ok": false, "error": str}
"""

from __future__ import annotations

import atexit
import os
from pathlib import Path
from typing import Any, Awaitable, Callable

import anyio
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from .config import TorConfig
from . import net
from . import fetch as _fetch
from . import circuit as _circuit
from . import search as _search
from . import browser as _browser
from .tor_process import shutdown_tor


def _load_dotenv(path: Path) -> None:
    """Minimal .env loader so browser_login can read SITE_USER / SITE_PASS.

    Does not override variables already present in the environment.
    """
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, val = line.split("=", 1)
        os.environ.setdefault(key.strip(), val.strip().strip('"').strip("'"))


# --- tool implementations (run inside the backend process) -----------------
#
# Sync tools (Tor httpx calls) are offloaded to a worker thread so they don't
# block the event loop; async browser tools are awaited directly.


def _maybe_save(result: Any, save_path: str) -> Any:
    """save_path 指定時、本文(text/html)をファイルに書き、LLMには軽量メタだけ返す。

    収集エージェントのコンテキストに本文全文が乗らないようにするための仕組み。
    save_path 未指定なら result をそのまま返す（後方互換）。
    links / interactive は次の遷移・操作の判断に必要なので残す。
    """
    if not save_path or not isinstance(result, dict):
        return result
    body = result.get("text") or result.get("html") or ""
    p = Path(save_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    header = (
        f"<!-- url: {result.get('final_url') or result.get('url', '')}\n"
        f"     title: {result.get('title', '')}  status: {result.get('status', '')} -->\n\n"
    )
    p.write_text(header + body, encoding="utf-8")
    light = {k: v for k, v in result.items() if k not in ("text", "html")}
    light.update({
        "saved_path": str(p),
        "bytes_saved": len(body),
        "preview": body[:800],
    })
    return light


def _build_dispatch(config: TorConfig) -> dict[str, Callable[[dict[str, Any]], Awaitable[Any]]]:
    async def _to_thread(fn, *a, **kw):
        return await anyio.to_thread.run_sync(lambda: fn(*a, **kw))

    async def tor_check(_: dict[str, Any]) -> Any:
        return await _to_thread(net.check_tor, config)

    async def tor_fetch(a: dict[str, Any]) -> Any:
        result = await _to_thread(
            _fetch.fetch, config, a["url"],
            render=a.get("render", "text"),
            max_text_chars=a.get("max_text_chars", 20000),
        )
        return _maybe_save(result, a.get("save_path", ""))

    async def tor_new_circuit(a: dict[str, Any]) -> Any:
        return await _to_thread(_circuit.new_circuit, config, verify=a.get("verify", True))

    async def onion_search(a: dict[str, Any]) -> Any:
        return await _to_thread(
            _search.onion_search, config, a["query"],
            max_results=a.get("max_results", 20),
            engine=a.get("engine", "tordex"),
        )

    async def browser_open(a: dict[str, Any]) -> Any:
        result = await _browser.goto_smart(
            a["url"], config,
            wait_until=a.get("wait_until", "load"),
            mode=a.get("mode", "auto"),
        )
        return _maybe_save(result, a.get("save_path", ""))

    async def browser_state(a: dict[str, Any]) -> Any:
        result = await _browser.get_session(config).state(render=a.get("render", "text"))
        return _maybe_save(result, a.get("save_path", ""))

    async def browser_click(a: dict[str, Any]) -> Any:
        return await _browser.get_session(config).click(a["selector"])

    async def browser_fill(a: dict[str, Any]) -> Any:
        return await _browser.get_session(config).fill(a["selector"], a["value"])

    async def browser_wait(a: dict[str, Any]) -> Any:
        return await _browser.get_session(config).wait_for(
            a["kind"], value=a.get("value"), timeout_ms=a.get("timeout_ms")
        )

    async def browser_screenshot(a: dict[str, Any]) -> Any:
        return await _browser.get_session(config).screenshot(full_page=a.get("full_page", False))

    async def browser_eval(a: dict[str, Any]) -> Any:
        return await _browser.get_session(config).evaluate(a["js"])

    async def browser_login(a: dict[str, Any]) -> Any:
        user = os.environ.get(a["user_env"])
        password = os.environ.get(a["pass_env"])
        if not user or not password:
            missing = [n for n, v in ((a["user_env"], user), (a["pass_env"], password)) if not v]
            return {"ok": False, "error": f"missing env credential(s): {', '.join(missing)}"}
        await _browser.goto_smart(a["url"], config, wait_until="load")
        session = _browser.get_session(config)
        await session.fill(a["user_selector"], user)
        await session.fill(a["pass_selector"], password)
        return await session.click(a["submit_selector"])

    async def browser_close(_: dict[str, Any]) -> Any:
        await _browser.shutdown_session_async()
        return {"ok": True, "closed": True}

    async def browser_reset(_: dict[str, Any]) -> Any:
        await _browser.get_session(config).reset()
        return {"ok": True, "reset": True}

    async def browser_list_profiles(_: dict[str, Any]) -> Any:
        return await anyio.to_thread.run_sync(lambda: _browser.list_profiles(config))

    return {
        "tor_check": tor_check,
        "tor_fetch": tor_fetch,
        "tor_new_circuit": tor_new_circuit,
        "onion_search": onion_search,
        "browser_open": browser_open,
        "browser_state": browser_state,
        "browser_click": browser_click,
        "browser_fill": browser_fill,
        "browser_wait": browser_wait,
        "browser_screenshot": browser_screenshot,
        "browser_eval": browser_eval,
        "browser_login": browser_login,
        "browser_close": browser_close,
        "browser_reset": browser_reset,
        "browser_list_profiles": browser_list_profiles,
    }


def build_app(config: TorConfig) -> Starlette:
    dispatch = _build_dispatch(config)

    async def health(_: Request) -> JSONResponse:
        return JSONResponse({"ok": True})

    async def call(request: Request) -> JSONResponse:
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001
            return JSONResponse({"ok": False, "error": "invalid JSON body"}, status_code=400)
        tool = body.get("tool")
        args = body.get("args") or {}
        handler = dispatch.get(tool)
        if handler is None:
            return JSONResponse({"ok": False, "error": f"unknown tool: {tool!r}"}, status_code=400)
        try:
            result = await handler(args)
            return JSONResponse({"ok": True, "result": result})
        except Exception as exc:  # noqa: BLE001
            return JSONResponse({"ok": False, "error": f"{type(exc).__name__}: {exc}"})

    async def shutdown(request: Request) -> JSONResponse:
        # Gracefully stop uvicorn; atexit then closes the browser and Tor.
        server = getattr(request.app.state, "server", None)
        if server is not None:
            server.should_exit = True
        return JSONResponse({"ok": True, "shutting_down": True})

    return Starlette(routes=[
        Route("/health", health, methods=["GET"]),
        Route("/call", call, methods=["POST"]),
        Route("/shutdown", shutdown, methods=["POST"]),
    ])


def _shutdown_browser() -> None:
    import asyncio
    try:
        asyncio.run(_browser.shutdown_session_async())
    except Exception:  # noqa: BLE001
        pass


def run_backend(config: TorConfig | None = None) -> None:
    """Run the shared backend over HTTP (blocking). Exits if the port is taken."""
    import uvicorn

    config = config or TorConfig.from_env()
    # Load credentials for browser_login (project-root .env).
    _load_dotenv(Path(__file__).resolve().parent.parent / ".env")

    # Browser closes first (registered last → LIFO), then Tor.
    atexit.register(shutdown_tor)
    atexit.register(_shutdown_browser)

    app = build_app(config)
    server_config = uvicorn.Config(
        app, host=config.backend_host, port=config.backend_port, log_level="warning"
    )
    server = uvicorn.Server(server_config)
    app.state.server = server  # let /shutdown signal a graceful stop
    try:
        server.run()
    except OSError as exc:
        # Another backend already owns the port — that's fine, just exit quietly.
        print(f"[tor_mcp.backend] port {config.backend_port} unavailable ({exc}); "
              "another backend is likely already running.", flush=True)


if __name__ == "__main__":
    run_backend()
