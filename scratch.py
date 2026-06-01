"""End-to-end smoke test for tor_mcp (exercises all tools through Tor).

Run with a constant command so it only needs to be permitted once:
    PYTHONUTF8=1 uv run python -m tor_mcp.scratch
"""

import asyncio

from tor_mcp import circuit, fetch, net, search
from tor_mcp.config import TorConfig
from tor_mcp.server import mcp
from tor_mcp.tor_process import shutdown_tor

cfg = TorConfig.from_env()


def main() -> None:
    # 0) MCP tool registration
    tools = asyncio.run(mcp.list_tools())
    print("registered tools:", sorted(t.name for t in tools))

    # 1) tor_check
    chk = net.check_tor(cfg)
    print(f"tor_check: is_tor={chk['is_tor']} exit_ip={chk['exit_ip']}")

    # 2) tor_fetch (.onion) — DuckDuckGo onion (reliably up)
    ddg = "https://duckduckgogg42xjoc72x3sjasowoarfbgcmvfimaftt6twagswzczad.onion/"
    f = fetch.fetch(cfg, ddg)
    print(f"tor_fetch: status={f['status']} is_onion={f['is_onion']} title={f.get('title')!r}")

    # 3) onion_search (Tordex)
    s = search.onion_search(cfg, "bitcoin", max_results=3)
    print(f"onion_search: engine={s['engine']} count={s['count']}")
    for r in s["results"]:
        print("   -", r["onion_url"][:60])

    # 4) tor_new_circuit
    nc = circuit.new_circuit(cfg)
    print(f"tor_new_circuit: {nc['exit_ip_before']} -> {nc['exit_ip_after']} changed={nc['changed']}")


if __name__ == "__main__":
    try:
        main()
    finally:
        shutdown_tor()
        print("shutdown done")
