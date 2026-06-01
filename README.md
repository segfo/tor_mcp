# tor_mcp — Tor接続 MCPサーバ（ダークウェブOSINT支援）

ローカルで管理する Tor プロセス（SOCKS5h プロキシ）経由で .onion / Web に
アクセスする MCP サーバ。**受動的な OSINT 情報収集**を目的とする。

```
Claude ──(MCP/stdio)──▶ tor_mcp ──(SOCKS5h)──▶ tor.exe ──▶ .onion / Web
                            └──(ControlPort/stem)──▶ tor.exe（回路制御）
```

## 提供ツール

| ツール | 用途 |
|--------|------|
| `tor_check` | Tor 経由かを検証し、出口ノードIPを返す（初回は tor 起動で10〜40秒） |
| `tor_fetch` | .onion / Web ページを取得（HTML→テキスト整形・タイトル・リンク抽出） |
| `tor_new_circuit` | 新しい回路（別の出口IP）を要求。前後の出口IPと変化有無を返す |
| `onion_search` | Tor 検索エンジン（Tordex / Torch）で .onion を検索 |

## セットアップ

### 1. 依存パッケージ

```bash
uv pip install -r tor_mcp/requirements.txt
```

（`mcp`, `httpx[socks]`, `stem`, `beautifulsoup4`）

### 2. Tor 本体（Tor Expert Bundle）の配置

リポジトリにはバイナリを含めない。各自で配置する（[vendor/README.md](vendor/README.md) 参照）。

- 取得: https://www.torproject.org/download/tor/ → **Windows Expert Bundle**
- 配置: 展開した `tor` フォルダの中身を `tor_mcp/vendor/tor/` へ
  （`tor_mcp/vendor/tor/tor.exe` になるように）
- 確認: `uv run python -m tor_mcp.tor_process --check-binary`

`tor.exe` のパスは環境変数 `TOR_MCP_TOR_EXE` で上書き可能。

### 3. MCP サーバとして登録

プロジェクトルートの [.mcp.json](../.mcp.json) で `tor-osint` サーバが定義済み。
Claude Code をこのディレクトリで起動すると認識される。

## 設定（環境変数）

| 変数 | 既定 | 説明 |
|------|------|------|
| `TOR_MCP_TOR_EXE` | `vendor/tor/tor.exe` | tor バイナリのパス |
| `TOR_MCP_SOCKS_PORT` | `9050` | SOCKS5 ポート（Tor Browser 併用時は競合に注意） |
| `TOR_MCP_CONTROL_PORT` | `9051` | ControlPort（回路制御用） |

Tor のライフサイクルはサーバが管理する（初回ツール使用時に起動、終了時に停止）。

## 動作確認（スタンドアロン）

```bash
# tor バイナリ検出
uv run python -m tor_mcp.tor_process --check-binary
# tor 起動 → bootstrap → 停止
uv run python -m tor_mcp.tor_process --start
# 全ツールのスモークテスト（tor 経由通信あり、数十秒）
PYTHONUTF8=1 uv run python -m tor_mcp.scratch
```

## 倫理・法的方針（重要）

- **受動的な情報収集（閲覧・公開テキストの収集）に限定**する。
- 不正アクセス、違法コンテンツの取得・取引、能動的攻撃には使用しない。
- `onion_search` の結果には未審査・違法な掲載が含まれ得る。
  **結果は検証者が評価する前提**であり、ツールは内容の合法性を保証しない。
- 調査は認可された範囲で、各国法令・所属組織の規程に従って実施すること。
