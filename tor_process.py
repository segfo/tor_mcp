"""Managed Tor process: generate torrc, launch tor.exe, wait for bootstrap, stop.

This is the foundation for every tool in the server. It is written to be usable
standalone (see the __main__ block at the bottom) so the Tor lifecycle can be
verified without going through the full MCP handshake.
"""

from __future__ import annotations

import subprocess
import sys
import threading
import time
from collections import deque
from pathlib import Path
from typing import Optional

from .config import TorConfig


class TorError(RuntimeError):
    """Raised when the Tor process fails to start or bootstrap."""


class TorProcess:
    """Launch and supervise a local Tor process for the lifetime of the server."""

    def __init__(self, config: Optional[TorConfig] = None) -> None:
        self.config = config or TorConfig.from_env()
        self._proc: Optional[subprocess.Popen] = None
        self._reader: Optional[threading.Thread] = None
        self._bootstrapped = threading.Event()
        self._stopped = threading.Event()
        # Keep the last log lines for diagnostics on failure.
        self._log_tail: deque[str] = deque(maxlen=50)
        self._lock = threading.Lock()

    # --- public API -------------------------------------------------------

    @property
    def is_running(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    @property
    def is_ready(self) -> bool:
        return self.is_running and self._bootstrapped.is_set()

    def check_binary(self) -> Path:
        """Validate that the configured tor binary exists; return its path."""
        exe = self.config.tor_exe_path
        if not exe.exists():
            raise TorError(
                f"Tor binary not found at {exe}. "
                "Place the Tor Expert Bundle (see tor_mcp/vendor/README.md) "
                "or set TOR_MCP_TOR_EXE."
            )
        return exe

    def start(self) -> None:
        """Start Tor and block until bootstrapped, or raise TorError.

        Idempotent: returns immediately if already ready.
        """
        with self._lock:
            if self.is_ready:
                return
            if self.is_running:
                # Started but not yet bootstrapped; fall through to wait.
                pass
            else:
                self._spawn()

        ok = self._bootstrapped.wait(timeout=self.config.bootstrap_timeout)
        if not ok:
            tail = "\n".join(self._log_tail)
            self.stop()
            raise TorError(
                f"Tor did not bootstrap within {self.config.bootstrap_timeout}s.\n"
                f"--- last tor log ---\n{tail}"
            )
        if not self.is_running:
            tail = "\n".join(self._log_tail)
            raise TorError(f"Tor process exited during startup.\n--- last tor log ---\n{tail}")

    def stop(self, timeout: float = 10.0) -> None:
        """Terminate the Tor process and clean up the torrc file."""
        self._stopped.set()
        proc = self._proc
        if proc is not None and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                proc.kill()
        self._proc = None
        # Best-effort torrc cleanup (data dir is kept for faster re-bootstrap).
        torrc = self.config.data_dir / "torrc"
        try:
            torrc.unlink(missing_ok=True)
        except OSError:
            pass

    # --- internals --------------------------------------------------------

    def _write_torrc(self) -> Path:
        self.config.data_dir.mkdir(parents=True, exist_ok=True)
        torrc = self.config.data_dir / "torrc"
        lines = [
            f"SocksPort 127.0.0.1:{self.config.socks_port}",
            f"ControlPort 127.0.0.1:{self.config.control_port}",
            "CookieAuthentication 1",
            f"DataDirectory {self.config.data_dir}",
            "Log notice stdout",
        ]
        torrc.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return torrc

    def _spawn(self) -> None:
        exe = self.check_binary()
        torrc = self._write_torrc()
        self._bootstrapped.clear()
        self._stopped.clear()
        # CREATE_NO_WINDOW so launching tor.exe doesn't pop a console on Windows.
        creationflags = 0
        if sys.platform == "win32":
            creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        self._proc = subprocess.Popen(
            [str(exe), "-f", str(torrc)],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            creationflags=creationflags,
        )
        self._reader = threading.Thread(target=self._read_output, daemon=True)
        self._reader.start()

    def _read_output(self) -> None:
        assert self._proc is not None and self._proc.stdout is not None
        for line in self._proc.stdout:
            line = line.rstrip("\n")
            if not line:
                continue
            self._log_tail.append(line)
            if "Bootstrapped 100%" in line:
                self._bootstrapped.set()


# Module-level singleton, started lazily / via server lifespan.
_TOR: Optional[TorProcess] = None


def get_tor(config: Optional[TorConfig] = None) -> TorProcess:
    """Return the shared TorProcess, starting it if necessary."""
    global _TOR
    if _TOR is None:
        _TOR = TorProcess(config)
    if not _TOR.is_ready:
        _TOR.start()
    return _TOR


def shutdown_tor() -> None:
    global _TOR
    if _TOR is not None:
        _TOR.stop()
        _TOR = None


if __name__ == "__main__":
    # Standalone helpers for Phase 0/1 verification:
    #   python -m tor_mcp.tor_process --check-binary
    #   python -m tor_mcp.tor_process --start   (start, report, stop)
    import argparse

    parser = argparse.ArgumentParser(description="Tor process helper")
    parser.add_argument("--check-binary", action="store_true", help="Verify tor binary exists")
    parser.add_argument("--start", action="store_true", help="Start Tor, wait for bootstrap, then stop")
    args = parser.parse_args()

    tp = TorProcess()
    if args.check_binary:
        print(f"tor binary OK: {tp.check_binary()}")
    if args.start:
        print("Starting Tor (this may take 10-40s)...")
        t0 = time.time()
        tp.start()
        print(f"Bootstrapped in {time.time() - t0:.1f}s. is_ready={tp.is_ready}")
        tp.stop()
        print("Stopped.")
    if not (args.check_binary or args.start):
        parser.print_help()
