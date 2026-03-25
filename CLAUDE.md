# Project: গগラボ (gugulabo)

Googleレビュー管理SaaS。整流デザイン運営。対象顧客：マッサージ店・整骨院・美容院・歯科医院等の中小店舗。
ドメイン: gugulabo.com / VPS: 162.43.76.18

## Tech Stack
- Python / Flask / SQLite / Gunicorn / Nginx
- Google Places API (New) / Stripe / LINE Notify / OpenRouter (Claude claude-haiku-4-5)
- VPS Ubuntu on シン VPS

## Commands
# ローカル開発
python app.py

# デプロイ（必ずこの手順）
git add . && git commit -m "メッセージ"
git push origin main
ssh root@162.43.76.18 "cd /var/www/gugulabo && git pull && systemctl restart gunicorn"

# ※ 正規サービス名は gunicorn.service（gugulabo.service は廃止・disabled）
# ※ 2つのサービスが同じポート5000を取り合っていた問題を解消済み（2026-03-24）

# ※ Mac再起動後は必ず先に実行
ssh-add ~/.ssh/id_ed25519

## Architecture
app.py              → Flaskメインエントリ
services/
  places_api.py     → Google Places API（APIキーは関数内で取得すること）
  ai_reply.py       → OpenRouter AI返信生成（APIキーは関数内で取得すること）
  line_notify.py    → LINE週次レポート（APIキーは関数内で取得すること）
templates/          → Jinja2テンプレート
static/             → CSS/JS/画像
.env                → 環境変数（git管理外）

## Gotchas（重要な落とし穴）
- APIキーは必ず関数内で os.getenv() すること（モジュール読み込み時に取得するとgit pull後に死ぬ）
- VPSでgit競合が出たら: git stash && git pull してから systemctl stop && systemctl start
- systemctl restart は古いGunicornワーカーが残存することがある。必ず stop→start の2ステップを使う
- 変更が反映されない場合はまず curl -s http://127.0.0.1:5000/ でサーバー側の実際のHTMLを確認する
- ファイルをVPS上で直接編集しないこと。必ずローカルでコミット→プッシュ→プル
- SQLiteのマイグレーションは手動DDL。alembicは使っていない
- Stripeは本番キーを.envで管理（テスト時はtest_キーに切り替え）
- レビューURL形式: search.google.com/local/writereview?placeid=XXXX

## Review URL
get_review_url_from_place_id(place_id) → search.google.com/local/writereview?placeid={place_id}

## Key External APIs
- Google Places API (New): reviews_sort=newest で最新5件取得
- Stripe: /subscribe エンドポイントで課金処理
- LINE Notify: 週次レビューレポート送信

## Workflows
### 新機能追加
1. ローカルでブランチ不要（solo開発）、mainに直接コミット
2. python app.py でローカル確認
3. deploy.sh or 上記デプロイコマンドで本番反映

### デバッグ
- VPSログ: sudo journalctl -u gugulabo -n 100 --no-pager
- Nginxログ: sudo tail -f /var/log/nginx/error.log
