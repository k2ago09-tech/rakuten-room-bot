# 楽天ROOM自動化ツール(暮らしの鑑定士)

毎日、楽天ウェブサービスAPIで売れ筋商品を収集し、Gemini APIで下書きコメントを生成、
Discordに通知する。実際の投稿(ROOMアプリでの最終クリック)は必ず人間が行う。
検討経緯は [楽天ROOM自動化_検討メモ.md](./楽天ROOM自動化_検討メモ.md) を参照。

## 構成

```
GitHub Actions(毎日21時JST)
  → 楽天ウェブサービスAPIで商品ランキング取得・フィルタ
  → Gemini APIで下書き生成
  → docs/data/today.json に書き込み(今日の2件、GitHub Pagesで公開)
  → data/history.json を更新(重複除外・順位履歴、非公開)
  → リポジトリにコミット&プッシュ
  → Discord webhookで通知(PWAのURLをリンク)
```

## セットアップ手順

### 1. 楽天ウェブサービス アプリ登録

2026年5月のAPI刷新以降、`applicationId` に加えて `accessKey`(`pk_`から始まる)が必須。
既存アプリの編集ではなく、[楽天ウェブサービス](https://webservice.rakuten.co.jp/) で
**新規にアプリケーションを登録**して両方を発行すること。

登録時、Application typeは **「Web Application」を選ぶこと**(「API/Backend Service」は
アクセス元IPを固定リストで指定する必要があり、GitHub Actionsのようにランナーの送信元IPが
毎回変わる環境では運用できないため)。「Web Application」はドメイン(Referrer)制限になるので、
ドメイン欄には `k2ago09-tech.github.io` を入力する。Application URLは
`https://k2ago09-tech.github.io/rakuten-room-bot/` とする。
このドメイン制限は、スクリプト側で送信するHTTPリクエストに同じ`Referer`ヘッダーを
付与することでクリアする(`scripts/generate_drafts.py` の `RAKUTEN_REFERER` で設定済み)。

### 2. Gemini APIキー取得

[Google AI Studio](https://aistudio.google.com/) でAPIキーを発行(無料枠のFlash/Flash-Liteを利用)。

### 3. Discord webhook作成

通知を受け取りたいDiscordチャンネルの「連携サービス」→「ウェブフックを作成」でURLを発行。

### 4. GitHubリポジトリ作成 + Secrets/Variables設定

このフォルダをGitHubリポジトリにpushした上で、
`Settings → Secrets and variables → Actions` で以下を設定する。

**Secrets(非公開)**
- `RAKUTEN_APP_ID`
- `RAKUTEN_ACCESS_KEY`
- `GEMINI_API_KEY`
- `DISCORD_WEBHOOK_URL`

**Variables**
- `PWA_BASE_URL`(例: `https://<ユーザー名>.github.io/<リポジトリ名>/`)

### 5. GitHub Pages設定

`Settings → Pages` で Source を「Deploy from a branch」、Branch を
「main」「/docs」に設定する。

### 6. 動作確認

`Actions` タブから `Daily draft generation` を選び、「Run workflow」で手動実行できる
(毎日21時JSTの自動実行を待たずに確認可能)。

## ローカルでの動作確認

```bash
pip install -r scripts/requirements.txt

export RAKUTEN_APP_ID=xxxx
export RAKUTEN_ACCESS_KEY=pk_xxxx
export GEMINI_API_KEY=xxxx
export DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/xxxx
export PWA_BASE_URL=https://example.github.io/rakuten-room-bot/

python scripts/generate_drafts.py
```

PWA単体のプレビューは `python -m http.server 8420 --directory docs` で確認できる
(`.claude/launch.json` に同設定済み)。

## 既知の制約・要確認事項

- 楽天ウェブサービスAPIのレスポンスのトップレベルキー名(`Items`/`items`)は、
  実際のAPIキーでの動作未検証。初回実行でパースエラーが出た場合は
  `scripts/generate_drafts.py` の `fetch_ranking()` を実際のレスポンス形式に合わせて調整すること
- 具体的なレビュー本文(個別の高評価コメント引用など)は公式APIから取得できないため、
  下書き生成には件数・平均評価・商品説明文などの客観情報のみを渡している
  (実在しない体験談を捏造しないという方針を優先)
- しきい値(レビュー件数10件以上・評価3.5以上)や投稿数(1日2件)は
  `scripts/generate_drafts.py` の先頭で調整可能
