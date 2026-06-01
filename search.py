"""Search for .onion sites via Tor-network search engines (over Tor).

Default engine is Tordex, which renders results server-side (no JavaScript), so
they can be parsed directly. Ahmia was dropped because its current frontend
requires JavaScript and no longer serves a non-JS results page.

Note: onion search engines surface unmoderated and sometimes illegal listings.
This tool is for *passive discovery* during authorized OSINT only; results are
returned verbatim and must be assessed by the investigator.
"""

from __future__ import annotations

import re
from typing import Any

from bs4 import BeautifulSoup

from .config import TorConfig
from .net import tor_client

# Full onion URL (v3 = 56 base32 chars) with optional path.
_ONION_URL_RE = re.compile(r"https?://[a-z2-7]{56}\.onion[^\s\"'<>]*", re.IGNORECASE)

# Server-rendered onion search engines. `query` is appended as a query param.
ENGINES: dict[str, dict[str, str]] = {
    "tordex": {
        "base": "http://tordexu73joywapk2txdr54jed4imqledpcvcuf75qsas2gwdgksvnyd.onion/search",
        "param": "query",
    },
    "torch": {
        "base": "http://torchdeedp3i2jigzjdmfpn5ttjhthh5wbmda2rr3jvqjg5p77c54dqd.onion/search",
        "param": "query",
    },
}
DEFAULT_ENGINE = "tordex"


def _parse_results(html: str, max_results: int) -> list[dict[str, str]]:
    """Parse Tordex-style 'div.result' blocks (title / link / description)."""
    soup = BeautifulSoup(html, "html.parser")
    results: list[dict[str, str]] = []
    seen: set[str] = set()

    for block in soup.select("div.result"):
        link_el = block.select_one("#link") or block.select_one("h6")
        # The real onion URL is the *text* of the link element (the href is an
        # ad-tracking redirect on some engines).
        link_text = link_el.get_text(strip=True) if link_el else block.get_text(" ", strip=True)
        m = _ONION_URL_RE.search(link_text)
        if not m:
            continue
        onion_url = m.group(0)
        if onion_url in seen:
            continue
        seen.add(onion_url)

        title_el = block.select_one("#title") or block.select_one("h5")
        desc_el = block.select_one("#desc") or block.select_one("p")
        results.append({
            "title": (title_el.get_text(strip=True) if title_el else "")[:200],
            "onion_url": onion_url,
            "snippet": (desc_el.get_text(" ", strip=True) if desc_el else "")[:300],
        })
        if len(results) >= max_results:
            break

    # Fallback: scrape raw onion URLs if the result structure changed.
    if not results:
        for m in _ONION_URL_RE.finditer(html):
            u = m.group(0)
            if u in seen:
                continue
            seen.add(u)
            results.append({"title": "", "onion_url": u, "snippet": ""})
            if len(results) >= max_results:
                break
    return results


def onion_search(
    config: TorConfig,
    query: str,
    max_results: int = 20,
    engine: str = DEFAULT_ENGINE,
) -> dict[str, Any]:
    """Search a Tor search engine for .onion sites matching `query`.

    Returns {"query", "engine", "count", "results": [{title, onion_url, snippet}]}.
    """
    if engine not in ENGINES:
        raise ValueError(f"Unknown engine {engine!r}. Available: {', '.join(ENGINES)}")
    spec = ENGINES[engine]

    with tor_client(config) as client:
        resp = client.get(spec["base"], params={spec["param"]: query})
        resp.raise_for_status()
        html = resp.text

    results = _parse_results(html, max_results)
    return {"query": query, "engine": engine, "count": len(results), "results": results}
