"""Client side of the shared backend.

Each stdio MCP server (server.py) uses this to talk to the one shared backend
(backend.py). ``ensure_backend`` implements the "use it if it's up, otherwise
start it" behaviour the user asked for: the first process to need a tool spawns
the backend detached; everyone else just connects.

Spawn races are guarded by a spawn lock file so only ONE process launches the
backend; the rest just wait for /health. (Previously many backends could pile up
when the Tor port was contended and spawns retried.)
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import anyio
import httpx

from .config import TorConfig


def _base_url(config: TorConfig) -> str:
    return f"http://{config.backend_host}:{config.backend_port}"


def _acquire_spawn_lock(lock_path: Path, stale_sec: float = 90.0) -> bool:
    """Try to acquire the spawn lock (atomic create). True if we may spawn.

    A stale lock (older than stale_sec, e.g. a holder that died) is taken over so
    spawning can never deadlock permanently.
    """
    try:
        fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.write(fd, str(time.time()).encode())
        os.close(fd)
        return True
    except FileExistsError:
        try:
            if time.time() - lock_path.stat().st_mtime > stale_sec:
                lock_path.unlink(missing_ok=True)
                fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.close(fd)
                return True
        except (FileNotFoundError, FileExistsError, OSError):
            pass
        return False


def _is_healthy_sync(config: TorConfig, timeout: float = 1.0) -> bool:
    try:
        r = httpx.get(_base_url(config) + "/health", timeout=timeout)
        return r.status_code == 200
    except Exception:  # noqa: BLE001
        return False


async def _is_healthy(config: TorConfig, timeout: float = 1.0) -> bool:
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            r = await client.get(_base_url(config) + "/health")
            return r.status_code == 200
    except Exception:  # noqa: BLE001
        return False


def ensure_backend(config: TorConfig) -> None:
    """Ensure the shared backend is running; spawn it detached if not.

    Blocks until the backend answers /health or backend_startup_timeout elapses.
    Safe to call from many processes concurrently.
    """
    if _is_healthy_sync(config):
        return

    base = config.screenshot_dir.parent
    base.mkdir(parents=True, exist_ok=True)
    log_path = base / "backend.log"
    lock_path = base / "backend.spawn.lock"

    # Only the process that wins the spawn lock launches the backend; the others
    # just wait for /health. This prevents the backend from being spawned many
    # times when several stdio servers start at once.
    holder = _acquire_spawn_lock(lock_path)
    try:
        if holder and not _is_healthy_sync(config):
            creationflags = 0
            if sys.platform == "win32":
                creationflags = (
                    getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
                    | getattr(subprocess, "DETACHED_PROCESS", 0)
                )
            # Detach fully: the backend must outlive this stdio server. Its
            # stdout/stderr go to a log file (never to our stdout, which carries
            # the MCP protocol).
            with open(log_path, "ab") as log:
                subprocess.Popen(
                    [sys.executable, "-m", "tor_mcp", "--backend"],
                    stdout=log,
                    stderr=log,
                    stdin=subprocess.DEVNULL,
                    creationflags=creationflags,
                    close_fds=True,
                )

        deadline = time.time() + config.backend_startup_timeout
        while time.time() < deadline:
            if _is_healthy_sync(config):
                return
            time.sleep(1.0)
        raise RuntimeError(
            f"shared backend did not become healthy within "
            f"{config.backend_startup_timeout}s (log: {log_path})"
        )
    finally:
        if holder:
            lock_path.unlink(missing_ok=True)


def _call_timeout(tool: str, args: dict) -> float:
    """Per-call HTTP timeout. browser_wait can block for minutes (queues/CAPTCHA)."""
    if tool == "browser_wait":
        ms = args.get("timeout_ms") or 300_000
        return ms / 1000.0 + 60.0
    return 400.0


async def call(config: TorConfig, tool: str, args: dict) -> object:
    """Invoke a tool on the shared backend, starting it if necessary."""
    if not await _is_healthy(config):
        await anyio.to_thread.run_sync(ensure_backend, config)

    timeout = _call_timeout(tool, args)
    async with httpx.AsyncClient(timeout=timeout) as client:
        r = await client.post(_base_url(config) + "/call", json={"tool": tool, "args": args})
        data = r.json()
    if not data.get("ok"):
        raise RuntimeError(data.get("error", "backend call failed"))
    return data["result"]


def shutdown_backend(config: TorConfig) -> bool:
    """Ask the shared backend to stop (closes browser + Tor). Returns True if signalled.

    No-op if no backend is running. Note the backend is shared — only shut it
    down when no other session needs it.
    """
    if not _is_healthy_sync(config):
        return False
    try:
        httpx.post(_base_url(config) + "/shutdown", timeout=5.0)
        return True
    except Exception:  # noqa: BLE001
        return False
