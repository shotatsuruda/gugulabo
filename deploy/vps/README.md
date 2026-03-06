# シンVPS への移行手順（Ubuntu想定）

## 0. 事前準備
- DNS を新サーバーに向ける前に、サーバー構築とアプリ起動確認まで実施
- 旧環境のバックアップ取得（DB、.env）

## 1. サーバー初期設定
```bash
sudo apt update && sudo apt -y upgrade
sudo apt -y install git python3-venv python3-pip nginx ufw postgresql
sudo ufw allow OpenSSH
sudo ufw allow 80/tcp
sudo ufw allow 443/tcp
sudo ufw enable
```

## 2. アプリ配置
```bash
cd /home/ubuntu
git clone <REPO_URL> massage-review-system
cd massage-review-system
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
```

## 3. 環境変数
```bash
sudo bash -c 'cat >/etc/gugulabo.env' <<'ENV'
FLASK_ENV=production
FLASK_SECRET_KEY=<random>
SHOP_NAME=<your shop>
OPENROUTER_API_KEY=<sk-or-...>
MAIL_SMTP_HOST=smtp.gmail.com
MAIL_SMTP_PORT=587
MAIL_USERNAME=<your@gmail.com>
MAIL_PASSWORD=<app-password>
MAIL_FROM=<your@gmail.com>
STRIPE_SECRET_KEY=
STRIPE_PUBLIC_KEY=
STRIPE_WEBHOOK_SECRET=
DATABASE_URL=postgresql://gugulabo:<password>@127.0.0.1:5432/gugulabo
ENV
```

## 4. PostgreSQL セットアップ
```bash
sudo -u postgres psql <<'SQL'
CREATE USER gugulabo WITH PASSWORD '<password>';
CREATE DATABASE gugulabo OWNER gugulabo;
GRANT ALL PRIVILEGES ON DATABASE gugulabo TO gugulabo;
SQL
```

既存が PostgreSQL の場合:
```bash
pg_dump -Fc -h <old_host> -U <old_user> <old_db> > dump.dump
pg_restore -h 127.0.0.1 -U gugulabo -d gugulabo --no-owner --no-privileges -c dump.dump
```

既存が SQLite の場合:
- 新サーバーで一旦アプリを起動し DB スキーマを生成
- 必要テーブルのデータを CSV でエクスポートし、psql \COPY で取り込み

## 5. Gunicorn（systemd）
```bash
sudo cp /home/ubuntu/massage-review-system/deploy/vps/gunicorn.service /etc/systemd/system/gugulabo.service
sudo systemctl daemon-reload
sudo systemctl enable --now gugulabo
sudo systemctl status gugulabo
```

## 6. Nginx（リバースプロキシ）
```bash
sudo cp /home/ubuntu/massage-review-system/deploy/vps/nginx_gugulabo.conf /etc/nginx/sites-available/gugulabo.conf
sudo ln -s /etc/nginx/sites-available/gugulabo.conf /etc/nginx/sites-enabled/gugulabo.conf
sudo nginx -t
sudo systemctl reload nginx
```

TLS は certbot 等で取得し、サーバーブロックを 443 化。

## 7. 動作確認
- http://<server>/health が 200 を返すことを確認
- /qr, /bulk-create, /shop/<slug> を確認

## 8. DNS 切替
- A レコードを新サーバーの IP へ向ける
- 切替直後はキャッシュの都合で旧表示が混在

## 9. ログと再起動
```bash
journalctl -u gugulabo -f
sudo systemctl restart gugulabo
```

## 付録: よくあるエラー
- ImportError: psycopg2 → requirements の再インストール
- 502/504 → Nginx upstream/SELinux/UFW を確認
- 503 on /health → DATABASE_URL/権限/pg 起動状態を確認

