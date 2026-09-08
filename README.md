# tennis-draw-monitor

複数のウェブページの更新（テニス大会の組み合わせ発表など）をGitHub Actionsで定期的にチェックし、変化があればLINEに通知するシステム。

PCを常時起動しておく必要がなく、GitHub Actions（Publicリポジトリなら無料）上で完結する。

## 仕組み

- `config/targets.json` に監視したいページをいくつでも登録できる（URL・チェック間隔・検知方式を個別に設定）
- 検知方式（`watch_type`）は3種類から選べる（後述）
- GitHub Actionsが `cron` で定期実行し、前回チェック時の状態（`signature`）を `state/state.json` に保存（変化があればActionsが自動でコミット）
- 差分があった場合のみLINE Messaging APIで通知（初回登録時はbaseline保存のみで通知しない）

## 監視対象の追加方法

### ウィザードで追加（推奨）

```bash
python scripts/setup_wizard.py
```

URLとキーワードを入力するだけで、ページを取得してセレクタ候補を自動検出・提示してくれる。生成された設定は`config/targets.json`に自動追記される（自動コミットはしないので、`git diff`で内容を確認してから手動でコミットすること）。

### 手動で追加

`config/targets.json` の `monitors` 配列に追加する。

```json
{
  "name": "大会名（state.jsonのキーになる。重複不可）",
  "url": "https://example.com/tournament/",
  "enabled": true,
  "check_interval_minutes": 30,
  "watch_type": "link_href",
  "container_selector": "div.entry_items",
  "item_selector": "li",
  "text_keywords": ["組合せ"],
  "notification_title": "📋 更新が公開されました！"
}
```

- `check_interval_minutes`: このURLをチェックする間隔（分）。GitHub Actions側のcron間隔より短くしても、cronの実行タイミングでしか実際にはチェックされない点に注意
- `watch_type`: 省略時は`link_href`扱い（後方互換）。3種類から選べる：

| watch_type | 用途 | 追加で必要なフィールド | 通知タイミング |
|---|---|---|---|
| `link_href`（デフォルト） | リンクが有効化されたら通知（大会組み合わせ等） | `container_selector`, `item_selector`, `text_keywords` | リンク無し→有りに変化した時 |
| `selector_hash` | 特定要素のHTML変化を検知（価格・在庫等） | `selector` | 前回と値が変化した時 |
| `full_text_hash` | ページ全体のテキスト変化を検知 | なし | 前回と値が変化した時 |

`link_href`の`container_selector`/`item_selector`はコンテナとリストアイテムのCSSセレクタ、`text_keywords`はこれらの文字のいずれかを含む`item_selector`要素をターゲットとする（配列内はOR条件）。`selector_hash`の`selector`は監視したい要素1つを指すCSSセレクタ。

`notification_title`には`{name}`というプレースホルダーを書ける。通知送信時にその監視対象の`name`（例: `"秋季ジュニアリーグテニス大会"`）へ置換される（例: `"📋 {name}の組み合わせが公開されました！"` → `"📋 秋季ジュニアリーグテニス大会の組み合わせが公開されました！"`）。同じ`notification_title`のテンプレートを複数の監視対象で使い回したい場合に便利。

セレクタの調べ方: ブラウザで対象ページを開き、F12の開発者ツールで要素を右クリック→検査し、HTML構造を確認する（ウィザードを使えばこの手間は不要）。

### 対象追加時にcronの間隔を見直す

`.github/workflows/watch.yml` の `cron: '*/30 * * * *'` は、現在登録されている監視対象のうち最も短い `check_interval_minutes` に合わせてある。より短い間隔の対象を追加したら、この式も合わせて変更すること。

## セットアップ

### 1. LINE Messaging APIの準備

1. [LINE Developers](https://developers.line.biz/) でプロバイダー・チャネル（Messaging API）を作成
2. チャネルアクセストークン（長期）を発行
3. 発行された公式アカウントを自分のLINEアプリで友だち追加
4. 自分の `user_id` を確認（Webhookを一時的に有効にして取得するか、LINE Developersのチャネル設定内の「あなたのユーザーID」欄で確認できる場合がある）

### 2. GitHub Secretsの登録

```bash
gh secret set LINE_CHANNEL_ACCESS_TOKEN
gh secret set LINE_USER_ID
```

### 3. ローカルでの動作確認（任意）

```bash
pip install -r requirements.txt
cp .env.example .env
# .envにLINE_CHANNEL_ACCESS_TOKENとLINE_USER_IDを設定

# LINE送信・state書き込みなしで安全に試す場合
DRY_RUN=true FORCE_RUN_ALL=true python -c "from dotenv import load_dotenv; load_dotenv(); exec(open('src/monitor.py').read())"
```

PowerShellの場合:
```powershell
$env:DRY_RUN = "true"
$env:FORCE_RUN_ALL = "true"
python src/monitor.py
```
（`.env`を自動読み込みしたい場合は事前に `pip install python-dotenv` 済みの上、環境変数を手動で設定するか、`python-dotenv`のCLI経由で読み込む）

### 4. GitHub Actionsでの手動テスト

```bash
gh workflow run watch.yml -f forceRunAll=true
gh run list --workflow=watch.yml --limit 1
gh run view <run-id> --log
```

## 状態管理について

`state/state.json` は各監視対象の前回チェック結果を`signature`（文字列 or null、`watch_type`によらず共通のフォーマット）として保持する。初回登録時はbaselineとして保存されるのみで通知は送られない。2回目以降のチェックで実際に変化があった時だけLINE通知される。

旧バージョン（`has_link`/`link_url`フィールド）のstate.jsonも自動的に新フォーマットへ変換される（`load_state()`が読み込み時にマイグレーションする）。既存の監視対象の動作は変わらない。

### watch_type=link_href は通知後、自動的にチェックを止める

`link_href`方式は「未発表→発表」という一度きりのイベント検知なので、通知が送信されたらその監視対象の`state.json`エントリに`completed: true`が記録され、**以降は`enabled: true`のままでも自動的にスキップされる**（相手サーバーへの無駄なアクセスやGitHub Actionsの無駄な実行を防ぐため）。

もう一度チェックさせたい場合（発表内容が差し替わった場合など）は、`state/state.json`の該当エントリの`completed`を`false`に戻すか、削除する。動作確認等で`completed`を無視して強制的にチェックしたい場合は`workflow_dispatch`の`forceRunAll`（`FORCE_RUN_ALL=true`）を使う。

`selector_hash`/`full_text_hash`方式は継続的な変化を追い続ける用途なので、この「一度きりで停止」の挙動は適用されない（何度でも通知され続ける）。

## エラー時の挙動

サイトへのアクセスが失敗した場合や、対象要素が見つからなかった場合は、誤って「変化なし」と判定しないよう `signature` を更新しない。エラーは原因別に区別してカウント・通知する：

- **フェッチ失敗**（ネットワーク不調・タイムアウト等）: 3回連続で失敗すると1度だけ「監視エラー」としてLINE通知する（一時的な障害の可能性があるため猶予を持たせている）
- **構造変化**（`container_selector`/`item_selector`/`selector`に一致する要素が見つからない）: 1回で即座に「構造変化を検知した可能性」として通知する（サイトのリニューアル等でセレクタが合わなくなった場合、早く気づけるようにするため）。通知にはデバッグ情報（見つかったitem数など）が含まれる

## トラブルシューティング

- **通知が来ない**: `gh run view <run-id> --log` でActionsの実行ログを確認。`container_selector`/`item_selector`/`text_keywords`/`selector`が現在のHTML構造と合っているか確認する
- **「構造変化を検知した可能性」の通知が来た**: サイトのHTML構造が変わった可能性が高い。ブラウザの開発者ツールで現在の構造を確認し、`config/targets.json`のセレクタを更新する（`python scripts/setup_wizard.py`で再検出するのが手軽）
- **cronの実行タイミングがずれる**: GitHub Actionsのscheduled runはbest-effortであり、混雑時は数分〜十数分遅れることがある（公式仕様）
