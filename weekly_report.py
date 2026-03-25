import sqlite3
import os
from datetime import datetime, date, timedelta
from dotenv import load_dotenv
from services.places_api import get_new_reviews
from services.ai_reply import generate_reply, generate_advice
from services.meo_advice import generate_meo_advice
from services.line_notify import send_line_message, build_message, build_reply_message

load_dotenv()

DB_PATH = os.path.join(os.path.dirname(__file__), "review_system.db")


def get_this_month_count(db, shop_id):
    """今月の口コミ累計を返す"""
    first_day = date.today().replace(day=1)
    cursor = db.execute(
        "SELECT COUNT(*) FROM fetched_reviews WHERE shop_id = ? AND DATE(fetched_at) >= ?",
        (shop_id, first_day.isoformat())
    )
    return cursor.fetchone()[0]


def get_prev_avg_rating(db, shop_id):
    """先週の平均評価を返す（直近2件目のサマリーから取得）"""
    cursor = db.execute(
        "SELECT avg_rating FROM weekly_summaries WHERE shop_id = ? ORDER BY week_start DESC LIMIT 1 OFFSET 1",
        (shop_id,)
    )
    row = cursor.fetchone()
    return row[0] if row else None


def run():
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row

    shops = db.execute(
        "SELECT * FROM shops WHERE place_id IS NOT NULL AND line_user_id IS NOT NULL"
    ).fetchall()

    today = date.today()
    week_start = today - timedelta(days=today.weekday())

    for shop in shops:
        shop_id = shop["id"]
        shop_name = shop["name"]
        place_id = shop["place_id"]
        line_user_id = shop["line_user_id"]
        business_type = shop["business_type"] or "店舗"
        zero_weeks = shop["zero_review_weeks"] or 0

        try:
            new_reviews, current_avg = get_new_reviews(shop_id, place_id, db)

            replies = []
            for review in new_reviews:
                reply = generate_reply(review, business_type)
                replies.append(reply)

                db.execute(
                    """INSERT OR IGNORE INTO fetched_reviews
                       (shop_id, review_id, author_name, rating, text, review_time, reply_draft)
                       VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (shop_id, review["id"], review["author"],
                     review["rating"], review["text"], review["time"], reply)
                )

            if len(new_reviews) == 0:
                zero_weeks += 1
                db.execute(
                    "UPDATE shops SET zero_review_weeks = ? WHERE id = ?",
                    (zero_weeks, shop_id)
                )
            else:
                db.execute(
                    "UPDATE shops SET zero_review_weeks = 0 WHERE id = ?",
                    (shop_id,)
                )
                zero_weeks = 0

            db.execute(
                """INSERT OR IGNORE INTO weekly_summaries (shop_id, week_start, review_count, avg_rating)
                   VALUES (?, ?, ?, ?)""",
                (shop_id, week_start.isoformat(), len(new_reviews), current_avg)
            )

            prev_avg = get_prev_avg_rating(db, shop_id)
            total_month = get_this_month_count(db, shop_id)
            advice = generate_advice(zero_weeks, current_avg, len(new_reviews))
            meo_advice = generate_meo_advice(business_type)

            message = build_message(
                shop_name, new_reviews,
                current_avg, prev_avg, total_month, advice, meo_advice
            )
            send_line_message(line_user_id, message)

            for i, (review, reply) in enumerate(zip(new_reviews, replies), 1):
                reply_message = build_reply_message(review, reply, i)
                send_line_message(line_user_id, reply_message)

            print(f"✅ {shop_name}：送信完了（新着{len(new_reviews)}件）")

        except Exception as e:
            print(f"❌ {shop_name}：エラー {e}")

    db.commit()
    db.close()


if __name__ == "__main__":
    run()
