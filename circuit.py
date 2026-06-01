"""Circuit control: request a fresh Tor circuit (new exit node) via the ControlPort.

Uses stem to authenticate (cookie auth, configured in torrc) and send NEWNYM.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from stem import Signal
from stem.control import Controller

# stem logs benign "SocketClosed" errors when the control connection closes
# after NEWNYM; silence them so they don't pollute the MCP stdio channel.
logging.getLogger("stem").setLevel(logging.CRITICAL)

from .config import TorConfig
from .net import check_tor
from .tor_process import get_tor


def new_circuit(config: TorConfig, *, verify: bool = True) -> dict[str, Any]:
    """Signal Tor to build fresh circuits, then optionally report the new exit IP.

    Returns {"signaled": True, "exit_ip_before": ..., "exit_ip_after": ...,
             "changed": bool} (the *_before / *_after / changed keys only when
    verify=True).
    """
    get_tor(config)  # ensure Tor is running/bootstrapped

    before = check_tor(config)["exit_ip"] if verify else None

    with Controller.from_port(port=config.control_port) as controller:
        controller.authenticate()  # cookie auth (CookieAuthentication 1)
        controller.signal(Signal.NEWNYM)

    # Give Tor time to establish new circuits before re-checking.
    time.sleep(config.new_circuit_wait)

    result: dict[str, Any] = {"signaled": True}
    if verify:
        after = check_tor(config)["exit_ip"]
        result.update({
            "exit_ip_before": before,
            "exit_ip_after": after,
            "changed": bool(before and after and before != after),
        })
    return result
