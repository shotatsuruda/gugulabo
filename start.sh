#!/bin/bash

# ==========================================================
# start.sh - 本番環境向け起動スクリプト (Gunicorn)
# ==========================================================

# 1. 環境変数の読み込み (必要に応じて)
# .env ファイルが存在する場合は読み込む (Renderなどでは環境変数をダッシュボードで設定するため不要な場合もあります)
if [ -f .env ]; then
    export $(cat .env | grep -v '^#' | xargs)
fi

# 2. データベースのアップグレード等が必要な場合はここに追記
# 例: flask db upgrade

# 3. Gunicorn でアプリを起動
# worker数はサーバーのCPUコア数に応じて適宜変更してください (例: 2~4)
# --preload: メモリ使用量を抑える
echo "Starting production server with Gunicorn..."
exec gunicorn app:app --workers 2 --threads 4 --bind 0.0.0.0:${PORT:-5000} --access-logfile - --error-logfile - --preload
