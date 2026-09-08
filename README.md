# tennis-draw-monitor

複数のウェブページの更新（テニス大会の組み合わせ発表など）をGitHub Actionsで定期的にチェックし、変化があればLINEに通知するシステム。

PCを常時起動しておく必要がなく、GitHub Actions（Publicリポジトリなら無料）上で完結する。

## 仕組み

- `config/targets.json` に監視したいページをいくつでも登録できる（URL・チェック間隔・CSSセレクタ・キーワードを個別に設定）
- 指定したキーワードを含む要素（`<li>組合せ</li>`など）を探し、その中の`<a>`タグに`href`属性が付いたかどうかで「更新」を判定する
- GitHub Actionsが `cron` で定期実行し、前回チェック時の状態を `state/state.json` に保存（変化があればActionsが自動でコミット）
- 差分があった場合のみLINE Messaging APIで通知（初回登録時はbaseline保存のみで通知しない）

## 監視対象の追加方法

`config/targets.json` の `monitors` 配列に追加する。

```json
{
  "name": "大会名（state.jsonのキーになる。重複不可）",
  "url": "https://example.com/tournament/",
  "enabled": true,
  "check_interval_minutes": 30,
  "container_selector": "div.entry_items",
  "item_selector": "li",
  "text_keywords": ["組", "合", "わ", "せ"],
  "notification_title": "📋 更新が公開されました！"
}
```

- `container_selector` / `item_selector`: 対象要素を含むコンテナとリストアイテムのCSSセレクタ
- `text_keywords`: これらの文字のいずれかを含む`item_selector`要素をターゲットとする（配列内はOR条件）
- `check_interval_minutes`: このURLをチェックする間隔（分）。GitHub Actions側のcron間隔より短くしても、cronの実行タイミングでしか実際にはチェックされない点に注意

セレクタの調べ方: ブラウザで対象ページを開き、F12の開発者ツールで要素を右クリック→検査し、HTML構造を確認する。

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

`state/state.json` は各監視対象の前回チェック結果を保持する。初回登録時はbaselineとして保存されるのみで通知は送られない。2回目以降のチェックで実際に「未発表→発表済み」の変化があった時だけLINE通知される。

サイトへのアクセスが失敗した場合や、対象要素が見つからなかった場合は、誤って「変化なし」と判定しないよう `has_link` を更新しない。3回連続で失敗すると1度だけ「監視エラー」としてLINE通知する。

## トラブルシューティング

- **通知が来ない**: `gh run view <run-id> --log` でActionsの実行ログを確認。`container_selector`/`item_selector`/`text_keywords`が現在のHTML構造と合っているか確認する
- **cronの実行タイミングがずれる**: GitHub Actionsのscheduled runはbest-effortであり、混雑時は数分〜十数分遅れることがある（公式仕様）
