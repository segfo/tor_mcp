"""Entry points for tor_mcp.

  python -m tor_mcp             # stdio MCP server (proxies to the shared backend)
  python -m tor_mcp --backend   # run the shared Tor + browser backend (HTTP)
"""
import sys

if __name__ == "__main__":
    if "--backend" in sys.argv[1:]:
        from .backend import run_backend
        run_backend()
    else:
        from .server import run
        run()
