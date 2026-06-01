"""手動・目視確認用スクリプト（MCPが動かしている既存のTor 9050に相乗り）。

新しいtor.exeを起動しないので、MCPサーバ稼働中でもポート衝突しない。

実行:
    PYTHONUTF8=1 uv run python -m tor_mcp.manual_check
    PYTHONUTF8=1 uv run python -m tor_mcp.manual_check https://example.onion/
"""

import sys

import httpx
from bs4 import BeautifulSoup

PROXY = "socks5h://127.0.0.1:9050"  # MCPが起動済みのTor SOCKS
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; rv:128.0) Gecko/20100101 Firefox/128.0"}

# 引数が無ければ、危険度の低い定番URLを順にチェック
DEFAULT_URLS = [
    "https://check.torproject.org/api/ip",  # ① Tor経路の確認(JSON)
    "https://duckduckgogg42xjoc72x3sjasowoarfbgcmvfimaftt6twagswzczad.onion/",  # ② .onion到達性
]


def fetch(url: str) -> None:
    print("=" * 70)
    print(f"GET {url}")
    with httpx.Client(proxy=PROXY, headers=HEADERS, timeout=60, follow_redirects=True) as c:
        r = c.get(url)
    print(f"status      : {r.status_code}")
    print(f"final_url   : {r.url}")
    print(f"content-type: {r.headers.get('content-type')}")
    ct = r.headers.get("content-type", "")
    if "html" in ct:
        soup = BeautifulSoup(r.text, "html.parser")
        title = soup.title.string.strip() if soup.title and soup.title.string else None
        print(f"title       : {title!r}")
        for tag in soup(["script", "style", "noscript"]):
            tag.decompose()
        text = "\n".join(l.strip() for l in soup.get_text("\n").splitlines() if l.strip())
        print("--- body (先頭500字) ---")
        print(text[:500])
    else:
        print("--- body (先頭500字) ---")
        print(r.text[:500])


def main() -> None:
    urls = sys.argv[1:] or DEFAULT_URLS
    for url in urls:
        try:
            fetch(url)
        except Exception as e:  # noqa: BLE001  目視確認用なので握りつぶして次へ
            print(f"ERROR: {type(e).__name__}: {e}")
    print("=" * 70)


if __name__ == "__main__":
    main()
