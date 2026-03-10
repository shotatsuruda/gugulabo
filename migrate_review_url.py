"""
既存の review_url を旧形式から新形式に一括更新するマイグレーションスクリプト。

旧: https://search.google.com/local/writereview?placeid=<place_id>
新: https://maps.google.com/?action=reviewbusiness&placeid=<place_id>
"""
import os
import sys

DATABASE = os.path.join(os.path.dirname(__file__), "review_system.db")
DATABASE_URL = os.environ.get("DATABASE_URL")

OLD_PREFIX = "https://search.google.com/local/writereview?placeid="
NEW_PREFIX = "https://maps.google.com/?action=reviewbusiness&placeid="

if DATABASE_URL:
    import psycopg2
    conn = psycopg2.connect(DATABASE_URL)
    conn.autocommit = False
    cur = conn.cursor()
    cur.execute(
        "SELECT id, review_url FROM shops WHERE review_url LIKE %s",
        (OLD_PREFIX + "%",)
    )
    rows = cur.fetchall()
    print(f"更新対象: {len(rows)} 件")
    for row_id, old_url in rows:
        new_url = NEW_PREFIX + old_url[len(OLD_PREFIX):]
        cur.execute("UPDATE shops SET review_url = %s WHERE id = %s", (new_url, row_id))
        print(f"  id={row_id}: {old_url} -> {new_url}")
    conn.commit()
    cur.close()
    conn.close()
else:
    import sqlite3
    conn = sqlite3.connect(DATABASE)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT id, review_url FROM shops WHERE review_url LIKE ?",
        (OLD_PREFIX + "%",)
    ).fetchall()
    print(f"更新対象: {len(rows)} 件")
    for row in rows:
        new_url = NEW_PREFIX + row["review_url"][len(OLD_PREFIX):]
        conn.execute("UPDATE shops SET review_url = ? WHERE id = ?", (new_url, row["id"]))
        print(f"  id={row['id']}: {row['review_url']} -> {new_url}")
    conn.commit()
    conn.close()

print("完了")
