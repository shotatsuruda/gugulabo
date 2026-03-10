"""
Places API で place_id から CID を取得し、全店舗の review_url を
https://www.google.com/maps?hl=ja#lrd=0x0:[CID_HEX],3 形式に一括更新するスクリプト。

CID取得に失敗した店舗は従来の writereview 形式にフォールバックする。
"""
import os
import sqlite3

import requests
from dotenv import load_dotenv

load_dotenv()

DATABASE = os.path.join(os.path.dirname(__file__), "review_system.db")
DATABASE_URL = os.environ.get("DATABASE_URL")
PLACES_API_KEY = os.environ.get("PLACES_API_KEY")


def get_cid_from_place_id(place_id):
    """Places API で place_id から CID（10進数文字列）を取得する。失敗時は None を返す。"""
    url = f"https://places.googleapis.com/v1/places/{place_id}"
    headers = {
        "X-Goog-Api-Key": PLACES_API_KEY,
        "X-Goog-FieldMask": "id,googleMapsUri",
    }
    try:
        res = requests.get(url, headers=headers, timeout=10)
        data = res.json()
        maps_uri = data.get("googleMapsUri", "")
        if "cid=" in maps_uri:
            return maps_uri.split("cid=")[1].split("&")[0]
    except Exception as e:
        print(f"  APIエラー: {e}")
    return None


def build_lrd_url(cid_decimal):
    cid_hex = hex(int(cid_decimal))[2:]
    return f"https://www.google.com/maps?hl=ja#lrd=0x0:{cid_hex},3"


def main():
    if not PLACES_API_KEY:
        print("エラー: PLACES_API_KEY が設定されていません。")
        return

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
            print(f"処理中: {name} ({place_id})")
            cid = get_cid_from_place_id(place_id)
            if cid:
                new_url = build_lrd_url(cid)
                cur.execute("UPDATE shops SET review_url = %s WHERE id = %s", (new_url, shop_id))
                updated.append((name, new_url))
                print(f"  ✅ {new_url}")
            else:
                fallback = f"https://search.google.com/local/writereview?placeid={place_id}"
                skipped.append((name, place_id))
                print(f"  ⏭️  CID取得失敗 → フォールバック: {fallback}")

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
            print(f"処理中: {shop['name']} ({shop['place_id']})")
            cid = get_cid_from_place_id(shop["place_id"])
            if cid:
                new_url = build_lrd_url(cid)
                conn.execute("UPDATE shops SET review_url = ? WHERE id = ?", (new_url, shop["id"]))
                updated.append((shop["name"], new_url))
                print(f"  ✅ {new_url}")
            else:
                skipped.append((shop["name"], shop["place_id"]))
                print(f"  ⏭️  CID取得失敗")

        conn.commit()
        conn.close()

    print(f"\n=== 完了 ===")
    print(f"更新: {len(updated)}件 / スキップ: {len(skipped)}件")
    if skipped:
        print("スキップされた店舗:")
        for name, place_id in skipped:
            print(f"  - {name}: {place_id}")


if __name__ == "__main__":
    main()
