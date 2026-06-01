"""Fetch .onion / Web resources through Tor and render them safely.

Guards: response size cap, request timeout, redirect cap (via client), and a
content-type check so binary/huge payloads are summarized rather than returned.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup

from .config import TorConfig
from .net import tor_client

# Content types we will decode and render as text.
_TEXTUAL_PREFIXES = ("text/", "application/json", "application/xml", "application/xhtml")


def _is_textual(content_type: str) -> bool:
    ct = content_type.split(";", 1)[0].strip().lower()
    return any(ct.startswith(p) for p in _TEXTUAL_PREFIXES) or ct.endswith("+xml") or ct.endswith("+json")


def _read_capped(resp: httpx.Response, cap: int) -> tuple[bytes, bool]:
    """Read up to `cap` bytes from a streamed response. Returns (data, truncated)."""
    chunks: list[bytes] = []
    total = 0
    truncated = False
    for chunk in resp.iter_bytes():
        chunks.append(chunk)
        total += len(chunk)
        if total >= cap:
            truncated = True
            break
    return b"".join(chunks)[:cap], truncated


def _html_to_text(html: str, base_url: str) -> tuple[str, str | None, list[dict[str, str]]]:
    """Extract (text, title, links) from HTML. Links include .onion targets."""
    soup = BeautifulSoup(html, "html.parser")
    title = soup.title.string.strip() if soup.title and soup.title.string else None
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    text = soup.get_text("\n", strip=True)
    # Collapse runs of blank lines.
    lines = [ln for ln in (l.strip() for l in text.splitlines()) if ln]
    text = "\n".join(lines)

    links: list[dict[str, str]] = []
    seen: set[str] = set()
    for a in soup.find_all("a", href=True):
        href = urljoin(base_url, a["href"].strip())
        if href in seen:
            continue
        seen.add(href)
        links.append({"text": a.get_text(strip=True)[:120], "url": href})
        if len(links) >= 200:
            break
    return text, title, links


def fetch(config: TorConfig, url: str, render: str = "text", max_text_chars: int = 20000) -> dict[str, Any]:
    """Fetch `url` via Tor.

    render:
      - "text": for HTML, return cleaned text + title + extracted links.
      - "raw_head": return the first part of the raw decoded body (any textual type).

    Non-textual (binary) responses are not returned; only metadata is reported.
    """
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise ValueError(f"Unsupported URL scheme: {parsed.scheme!r} (use http/https, incl. .onion)")

    with tor_client(config) as client:
        with client.stream("GET", url) as resp:
            content_type = resp.headers.get("content-type", "")
            textual = _is_textual(content_type)
            result: dict[str, Any] = {
                "url": url,
                "final_url": str(resp.url),
                "status": resp.status_code,
                "content_type": content_type,
                "is_onion": parsed.hostname.endswith(".onion") if parsed.hostname else False,
            }
            if not textual:
                # Don't download binaries; report metadata only.
                result.update({
                    "rendered": False,
                    "reason": "non-textual content type; body not retrieved",
                    "content_length": resp.headers.get("content-length"),
                })
                return result

            data, truncated = _read_capped(resp, config.max_response_bytes)

    encoding = resp.encoding or "utf-8"
    try:
        body = data.decode(encoding, errors="replace")
    except (LookupError, TypeError):
        body = data.decode("utf-8", errors="replace")

    result["bytes_read"] = len(data)
    result["size_truncated"] = truncated

    ct_main = content_type.split(";", 1)[0].strip().lower()
    if render == "text" and (ct_main == "text/html" or ct_main.endswith("xhtml+xml")):
        text, title, links = _html_to_text(body, str(resp.url))
        result.update({
            "rendered": True,
            "title": title,
            "text": text[:max_text_chars],
            "text_truncated": len(text) > max_text_chars,
            "links": links,
        })
    else:
        result.update({
            "rendered": True,
            "text": body[:max_text_chars],
            "text_truncated": len(body) > max_text_chars,
        })
    return result
