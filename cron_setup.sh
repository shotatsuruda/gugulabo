#!/bin/bash
# 毎週月曜9時に実行するCronジョブを設定する

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
CRON_JOB="0 9 * * 1 cd ${SCRIPT_DIR} && /usr/bin/python3 weekly_report.py >> /var/log/gugulabo_weekly.log 2>&1"

(crontab -l 2>/dev/null; echo "$CRON_JOB") | crontab -

echo "✅ Cronジョブを設定しました"
echo "確認: crontab -l"
