#!/usr/bin/env python3
"""
scheduler/reminder_scheduler.py  ―  来店リマインダー日次通知
crontab: 0 0 * * * /var/www/gugulabo/venv/bin/python /var/www/gugulabo/scheduler/reminder_scheduler.py
         ^^^ UTC 0:00 = JST 9:00
"""
import os
import sys
import sqlite3
import logging
from datetime import date, datetime, timedelta

import requests
from dotenv import load_dotenv

# .env 読み込み（VPS上の /var/www/gugulabo/.env）
load_dotenv(os.path.join(os.path.dirname(os.path.dirname(__file__)), ".env"))

REMINDER_DB = os.path.join(os.path.dirname(os.path.dirname(__file__)), "reminder.db")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [reminder_scheduler] %(levelname)s %(message)s",
)
log = logging.getLogger(__name__)


# ------------------------------------------------------------------ #
#  DB ヘルパー                                                         #
# ------------------------------------------------------------------ #

def get_db():
    conn = sqlite3.connect(REMINDER_DB)
    conn.row_factory = sqlite3.Row
    return conn


def ensure_store_settings(db):
    """store_settings テーブルがなければ作成する"""
    db.execute("""
        CREATE TABLE IF NOT EXISTS store_settings (
            store_id         INTEGER PRIMARY KEY,
            line_notify_token TEXT
        )
    """)
    db.commit()


# ------------------------------------------------------------------ #
#  LINE Notify 送信                                                    #
# ------------------------------------------------------------------ #

def send_line_notify(token: str, message: str):
    resp = requests.post(
        "https://notify-api.line.me/api/notify",
        headers={"Authorization": f"Bearer {token}"},
        data={"message": message},
        timeout=10,
    )
    resp.raise_for_status()


# ------------------------------------------------------------------ #
#  アラートロジック（GET /reminder/alerts と同一）                      #
# ------------------------------------------------------------------ #

def fetch_alerts(db, store_id: int) -> list[dict]:
    today = date.today().isoformat()
    rows = db.execute(
        """
        SELECT
            c.name            AS customer_name,
            v.menu_name,
            MAX(v.visited_at) AS last_visited,
            mt.cycle_days,
            mt.message_body
        FROM visits v
        JOIN customers c        ON c.id        = v.customer_id
        JOIN message_templates mt
                                ON mt.store_id = c.store_id
                               AND mt.menu_name = v.menu_name
        WHERE c.store_id = ?
        GROUP BY c.id, v.menu_name, mt.id
        HAVING DATE(last_visited, '+' || mt.cycle_days || ' days') <= ?
        ORDER BY last_visited ASC
        """,
        (store_id, today),
    ).fetchall()

    alerts = []
    for r in rows:
        last_dt = datetime.strptime(r["last_visited"], "%Y-%m-%d").date()
        due_date = last_dt + timedelta(days=r["cycle_days"])
        days_overdue = (date.today() - due_date).days
        alerts.append({
            "customer_name": r["customer_name"],
            "menu_name":     r["menu_name"],
            "days_overdue":  days_overdue,
        })
    return alerts


# ------------------------------------------------------------------ #
#  メッセージ組み立て                                                   #
# ------------------------------------------------------------------ #

def build_message(alerts: list[dict]) -> str:
    lines = ["【リマインダー】本日の連絡候補"]
    for a in alerts:
        lines.append(
            f"・{a['customer_name']}さん（{a['menu_name']}）{a['days_overdue']}日超過"
        )
    lines.append("")
    lines.append("管理画面で文章を確認: https://gugulabo.com/reminder/alerts")
    return "\n".join(lines)


# ------------------------------------------------------------------ #
#  メイン処理                                                          #
# ------------------------------------------------------------------ #

def run():
    db = get_db()
    try:
        ensure_store_settings(db)

        # store_settings に登録されているすべての店舗を対象にする
        # 登録がなければ LINE_NOTIFY_TOKEN 環境変数を store_id=0 として扱う
        store_rows = db.execute(
            "SELECT store_id, line_notify_token FROM store_settings"
            " WHERE line_notify_token IS NOT NULL AND line_notify_token != ''"
        ).fetchall()

        token_map: dict[int, str] = {r["store_id"]: r["line_notify_token"] for r in store_rows}

        # 環境変数フォールバック（単一店舗運用時）
        env_token = os.environ.get("LINE_NOTIFY_TOKEN", "").strip()
        if not token_map and env_token:
            # store_id=0 をデフォルト店舗として扱う
            # ※ 実運用では登録済み store_id に差し替えてください
            log.warning("store_settings が空のため LINE_NOTIFY_TOKEN 環境変数を使用します（store_id=0）")
            token_map[0] = env_token

        if not token_map:
            log.warning("送信先トークンが見つかりません。終了します。")
            return

        for store_id, token in token_map.items():
            try:
                alerts = fetch_alerts(db, store_id)
                if not alerts:
                    log.info("store_id=%s: 対象顧客なし", store_id)
                    continue

                message = build_message(alerts)
                send_line_notify(token, message)
                log.info("store_id=%s: %d件を通知しました", store_id, len(alerts))

            except Exception as e:
                log.error("store_id=%s: 通知失敗 %s", store_id, e)

    finally:
        db.close()


if __name__ == "__main__":
    run()
