# tor_mcp が使用するブラウザ（棚卸し）

本サーバが起動しうるブラウザは **Camoufox** と **ユーザがインストール済みのシステムブラウザ**
の2種類だけ。Playwright のバンドルブラウザ（`playwright install` で入る chromium / firefox /
webkit）は**一切起動しない**。

---

## 接続モデル（2モード）

| 接続 | プロキシ | ブラウザエンジン | 用途 |
|------|----------|------------------|------|
| **Tor**    | SOCKS5 `127.0.0.1:9050` | **Camoufox 固定** | `.onion` |
| **Direct** | なし | 設定で選択（cdp / system / camoufox） | 通常のクリアネット URL |

- **Tor は常に Camoufox**。システムブラウザを Tor 経由にするとフィンガープリント/DNS
  リークの懸念が高いため対応しない。
- **Direct のエンジンは `TOR_MCP_DIRECT_BROWSER` で選択**（下記）。

実装: `BrowserSession`（Camoufox, `browser.py`）/ `SystemBrowserSession`（システムブラウザ,
`browser.py`）/ ルーティング `goto_smart()` / 検出 `system_browser.py`。

---

## Direct エンジンの解決ロジック（`_resolve_direct_engine`）

| `TOR_MCP_DIRECT_BROWSER` | 挙動 |
|--------------------------|------|
| `cdp`      | 実 Chrome に CDP で接続（実プロファイル・拡張・**Bot検知されにくい**）。下記参照 |
| `system`   | Playwright が起動するシステムブラウザ。未インストールなら `BrowserError`（バンドルへフォールバックしない） |
| `camoufox` | 常に Camoufox |
| 空（既定） | システムブラウザがあればそれ、無ければ Camoufox にフォールバック |

> **system と cdp の違い**: `system` は Playwright が `launch_persistent_context` で
> Chrome を起動するため、`--no-sandbox` バナーや `navigator.webdriver=true` などの
> 自動化シグネチャが付き、**Bot検知されやすい**。`cdp` は自分で `chrome.exe` を
> `--remote-debugging-port` 付きで起動して接続するだけなので自動化フラグが付かず、
> 実プロファイルの拡張機能も生きる。検知の強いサイト（pixiv 等）は `cdp` を使う。

---

## CDP エンジン（実 Chrome に接続）

`TOR_MCP_DIRECT_BROWSER=cdp` のとき、`CdpBrowserSession` が次を行う:

1. `http://127.0.0.1:<TOR_MCP_CDP_PORT>`（既定 9222）が応答していなければ、
   `TOR_MCP_CDP_AUTOLAUNCH=1`（既定）なら Chrome を自動起動する:
   `chrome.exe --remote-debugging-port=<port> --user-data-dir=<dir> --profile-directory=<name>`
2. Playwright の `connect_over_cdp()` でそのインスタンスに接続し、実プロファイルの
   コンテキストで新規タブを開く（既存タブは奪わない）。
3. `close`/`shutdown` 時は**接続を切るだけ**で Chrome は閉じない（ログイン状態を維持）。

| 環境変数 | 既定 | 説明 |
|----------|------|------|
| `TOR_MCP_CDP_PORT` | `9222` | リモートデバッグポート |
| `TOR_MCP_CDP_AUTOLAUNCH` | `1` | ポート未起動時に Chrome を自動起動 |
| `TOR_MCP_CHROME_EXE` | `(auto)` | Chrome の実行ファイル（空=自動検出: Chrome→Brave→Edge） |
| `TOR_MCP_CHROME_USER_DATA_DIR` | `vendor/cdp-chrome-profile` | User Data ディレクトリ |
| `TOR_MCP_CHROME_PROFILE_DIR` | `Default` | `--profile-directory`（`Profile 1` 等で特定プロファイル指定可） |

### ⚠️ User Data ディレクトリの排他ロック
Chrome は User Data ディレクトリ単位で SingletonLock を持つ。**同じ User Data の
Chrome が既に起動中だと `--remote-debugging-port` が無視される**（既存プロセスに吸収）。

- **既定（推奨）**: 専用 `vendor/cdp-chrome-profile` を使うので、日常 Chrome と
  共存できる。初回にこの Chrome ウィンドウで対象サイトにログイン・拡張機能を入れれば
  以後永続する。「Default 以外の自分のプロファイル」を使いたい場合は、この専用 User Data
  内に作ったプロファイル名を `TOR_MCP_CHROME_PROFILE_DIR` に指定する。
- **日常の実プロファイルを使う場合**: `TOR_MCP_CHROME_USER_DATA_DIR` を
  `C:\Users\<user>\AppData\Local\Google\Chrome\User Data` に向け、
  `TOR_MCP_CHROME_PROFILE_DIR` に対象プロファイル（`Default` / `Profile 1` …）を指定。
  ただし**使用中は日常 Chrome を全て閉じる**こと（ロック衝突回避）。

### 実機確認（pixiv での検証例）
`navigator.webdriver=false` で作品ページ（JSレンダリング）も 200 取得を確認済み。
`browser_list_profiles` の `direct` エントリに `engine="cdp"`、`cdp_endpoint_alive`、
`user_data_dir`、`profile_directory` が出る。

system エンジン時に「どのシステムブラウザを使うか」は次で決まる:

1. `TOR_MCP_CLEARNET_BROWSER_EXE`（絶対パス）が指定されていればそれを使用
   （種別は `TOR_MCP_CLEARNET_BROWSER_TYPE` = `chromium` / `firefox`）
2. 未指定なら自動検出。優先順は **Chrome → Brave → Edge → Firefox**
   （標準パス → Windows レジストリ `StartMenuInternet` の順、`system_browser.py`）

---

## `browser_open(via=...)` の対応

| `via` | 接続 | 結果の `via` |
|-------|------|--------------|
| `auto`（既定） | `.onion`→Tor、それ以外→Direct（失敗時はエラー） | `tor` / `direct(system)` / `direct(camoufox)` |
| `tor`    | 常に Tor（Camoufox） | `tor` |
| `direct` | クリアネットのみ（`.onion` 拒否）、エンジンは設定依存 | `direct(system)` / `direct(camoufox)` |
| `clearnet` | **非推奨エイリアス**。Direct を system エンジン強制 | `direct(system)` |

---

## Playwright バンドルブラウザを使わない理由と保証

- `SystemBrowserSession.ensure()` は常に `executable_path`（実在するシステムブラウザの
  パス）を指定して起動する。`executable_path` を与えると Playwright はバンドル版を使わない。
- `info`（検出結果）が `None`、または `exe_path` が実在しない場合は `BrowserError` を送出し、
  **バンドル版へフォールバックしない**（明示ガード）。
- Camoufox は独自の Firefox バイナリ（`python -m camoufox fetch` で取得）を使うため、
  Playwright バンドルには依存しない。

→ したがって `playwright install` は不要。`PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=1` を設定して
インストールしてよい（`.mcp.json` / README 参照）。

### 実機確認

`browser_list_profiles` ツールを呼ぶと、`profiles`（`tor` と `direct`）、`direct_engine`、
`detected_system_browsers`、`env_hints` が返る。ここに **Playwright バンドルは現れない**。
これで現在どのエンジンが使われるかを起動前に確認できる。
