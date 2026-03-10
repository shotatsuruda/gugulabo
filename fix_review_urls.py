"""
place_id が登録済みの全店舗の review_url を
https://search.google.com/local/writereview?placeid=XXX 形式に一括更新するスクリプト。
"""
import os
import sqlite3

DATABASE = os.path.join(os.path.dirname(__file__), "review_system.db")
DATABASE_URL = os.environ.get("DATABASE_URL")


def main():
    updated = []
    skipped = []

    if DATABASE_URL:
        import psycopg2
        conn = psycopg2.connect(DATABASE_URL)
        conn.autocommit = False
        cur = conn.cursor()

        cur.execute("SELECT id, name, place_id FROM shops WHERE place_id IS NOT NULL AND place_id != ''")
        shops = cur.fetchall()
        for shop_id, name, place_id in shops:
            new_url = f"https://search.google.com/local/writereview?placeid={place_id}"
            cur.execute("UPDATE shops SET review_url = %s WHERE id = %s", (new_url, shop_id))
            updated.append((name, new_url))

        cur.execute("SELECT name, review_url FROM shops WHERE place_id IS NULL OR place_id = ''")
        for name, url in cur.fetchall():
            skipped.append((name, url))

        conn.commit()
        cur.close()
        conn.close()
    else:
        conn = sqlite3.connect(DATABASE)
        conn.row_factory = sqlite3.Row

        shops = conn.execute(
            "SELECT id, name, place_id FROM shops WHERE place_id IS NOT NULL AND place_id != ''"
        ).fetchall()
        for shop in shops:
            new_url = f"https://search.google.com/local/writereview?placeid={shop['place_id']}"
            conn.execute("UPDATE shops SET review_url = ? WHERE id = ?", (new_url, shop["id"]))
            updated.append((shop["name"], new_url))

        for row in conn.execute(
            "SELECT name, review_url FROM shops WHERE place_id IS NULL OR place_id = ''"
        ).fetchall():
            skipped.append((row["name"], row["review_url"]))

        conn.commit()
        conn.close()

    print("=== 更新完了 ===")
    for name, url in updated:
        print(f"  ✅ {name} → {url}")

    if skipped:
        print("\n=== スキップ（place_id未設定）===")
        for name, url in skipped:
            print(f"  ⏭️  {name} : {url}")


if __name__ == "__main__":
    main()
