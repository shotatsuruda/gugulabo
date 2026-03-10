"""
既存店舗の review_url を口コミ直リンク形式に一括更新するスクリプト。

CID抽出パターン：
  A: ludocid=XXXXXXX が含まれるURL
  B: !1s0x...:0xYYYY 形式のGoogleマップURL（16進→10進変換）
  C: 上記どちらでもない → スキップ
"""
import os
import re
import sqlite3

DATABASE = os.path.join(os.path.dirname(__file__), "review_system.db")
DATABASE_URL = os.environ.get("DATABASE_URL")


def extract_cid(review_url):
    """review_urlからCIDを抽出して返す。取得できない場合はNoneを返す。"""
    # パターンA: ludocid=XXXXXXX
    match = re.search(r'ludocid=(\d+)', review_url)
    if match:
        return match.group(1)

    # パターンB: !1s0xXXXXXXXXXXXXXXXX:0xYYYYYYYYYYYYYYYY
    match = re.search(r'!1s0x[0-9a-fA-F]+:(0x[0-9a-fA-F]+)', review_url)
    if match:
        return str(int(match.group(1), 16))

    return None


def build_review_url(cid):
    return f"https://g.page/r/{cid}/review"


def main():
    updated = []
    skipped = []

    if DATABASE_URL:
        import psycopg2
        conn = psycopg2.connect(DATABASE_URL)
        conn.autocommit = False
        cur = conn.cursor()
        cur.execute("SELECT id, name, review_url FROM shops")
        shops = cur.fetchall()

        for shop_id, name, review_url in shops:
            cid = extract_cid(review_url or "")
            if cid:
                new_url = build_review_url(cid)
                cur.execute("UPDATE shops SET review_url = %s WHERE id = %s", (new_url, shop_id))
                updated.append((name, new_url))
            else:
                skipped.append((name, review_url))

        conn.commit()
        cur.close()
        conn.close()
    else:
        conn = sqlite3.connect(DATABASE)
        conn.row_factory = sqlite3.Row
        shops = conn.execute("SELECT id, name, review_url FROM shops").fetchall()

        for shop in shops:
            cid = extract_cid(shop["review_url"] or "")
            if cid:
                new_url = build_review_url(cid)
                conn.execute("UPDATE shops SET review_url = ? WHERE id = ?", (new_url, shop["id"]))
                updated.append((shop["name"], new_url))
            else:
                skipped.append((shop["name"], shop["review_url"]))

        conn.commit()
        conn.close()

    print("=== 更新完了 ===")
    for name, url in updated:
        print(f"  ✅ {name} → {url}")

    print("\n=== スキップ（CID取得不可）===")
    for name, url in skipped:
        print(f"  ⏭️  {name} : {url}")


if __name__ == "__main__":
    main()
