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

## 運用ノウハウ / トラブルシューティング

### 目視確認は `manual_check.py`（既存Torに相乗り）
MCPサーバ稼働中に手動で取得結果を目で見たいときは `manual_check.py` を使う。
このスクリプトは**新しい tor.exe を起動せず、MCPが既に動かしている SOCKS
9050 に相乗り**するため、ポート衝突しない。

```bash
# 既定の2URL(check.torproject + DuckDuckGo onion)を順に取得・表示
PYTHONUTF8=1 uv run python -m tor_mcp.manual_check
# 任意URLを目視確認（複数可）。status / final_url / content-type / title / 本文先頭500字
PYTHONUTF8=1 uv run python -m tor_mcp.manual_check https://example.onion/
```

逆に `tor_process --start` や `scratch` は**自前で tor.exe を起動する**ので、
MCPサーバが既にTorを動かしていると 9050/9051 で衝突する。手動の目視確認には
`manual_check.py` を使い分けること。

### `Address already in use [WSAEADDRINUSE]`（起動失敗）
9050/9051 を別の tor.exe が掴んでいる。多くは前回実行で残った孤立プロセス。

```bash
netstat -ano | grep -E "9050|9051"          # 占有PIDを特定
taskkill //PID <該当PID> //F                # vendor/tor 配下のものだけ落とす
```

- **Tor Browser は 9150/9151** を使うため本サーバ（9050/9051）と共存可。
  Tor Browser の tor.exe は落とさないこと。素性は
  `powershell -NoProfile -Command "Get-CimInstance Win32_Process -Filter \"name='tor.exe'\" | Select ProcessId,CommandLine | Format-List"`
  で CommandLine を見て判別する（`tor_mcp/vendor/tor` 配下が本サーバのもの）。

### 文字化け（cp932）
日本語コンソールで実行する際は `PYTHONUTF8=1` を付ける。無いと cp932 で
クラッシュ・文字化けすることがある。

## 倫理・法的方針（重要）

- **受動的な情報収集（閲覧・公開テキストの収集）に限定**する。
- 不正アクセス、違法コンテンツの取得・取引、能動的攻撃には使用しない。
- `onion_search` の結果には未審査・違法な掲載が含まれ得る。
  **結果は検証者が評価する前提**であり、ツールは内容の合法性を保証しない。
- 調査は認可された範囲で、各国法令・所属組織の規程に従って実施すること。

## ライセンス / 著作権

Copyright (c) 2026 segfo

本ソフトウェアは MIT License の下で公開されています。詳細は [LICENSE](LICENSE) を参照。
