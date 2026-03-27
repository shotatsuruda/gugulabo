"""
collect_reviews.py
大阪・神戸エリアの美容室口コミ収集スクリプト

使い方:
    cd /var/www/gugulabo
    source venv/bin/activate
    python collect_reviews.py

結果: reviews_collected.json に追記保存
"""

import json
import os
import time
import requests
from dotenv import load_dotenv

load_dotenv()

# ----------------------------------------------------------------
# 収集対象の place_id リスト（手動で追加・編集してください）
# Google Maps で店舗を開き URL に含まれる ChIJ... の文字列
# ----------------------------------------------------------------
PLACE_IDS = [
    # 大阪エリア
    # "ChIJ_________OSAKA_1",  # 店舗名（コメントに記載推奨）
    # "ChIJ_________OSAKA_2",

    # 神戸エリア
    # "ChIJ_________KOBE_1",
    # "ChIJ_________KOBE_2",
]

OUTPUT_FILE = os.path.join(os.path.dirname(__file__), "reviews_collected.json")
MAX_REVIEWS = 5  # Places API の上限


def fetch_reviews(place_id: str) -> dict:
    """Places API（Legacy）で口コミを取得する。"""
    api_key = os.environ.get("PLACES_API_KEY")
    if not api_key:
        raise RuntimeError("環境変数 PLACES_API_KEY が設定されていません")

    url = "https://maps.googleapis.com/maps/api/place/details/json"
    params = {
        "place_id": place_id,
        "fields": "name,rating,reviews",
        "reviews_sort": "newest",
        "reviews_no_translations": "true",
        "language": "ja",
        "key": api_key,
    }
    resp = requests.get(url, params=params, timeout=10)
    resp.raise_for_status()
    return resp.json().get("result", {})


def collect():
    if not PLACE_IDS:
        print("PLACE_IDS が空です。スクリプト上部にplace_idを追加してください。")
        return

    # 既存データ読み込み（重複回避）
    existing = []
    if os.path.exists(OUTPUT_FILE):
        with open(OUTPUT_FILE, "r", encoding="utf-8") as f:
            existing = json.load(f)

    existing_keys = {(r["place_id"], r["review_text"]) for r in existing}
    new_records = []

    for place_id in PLACE_IDS:
        print(f"\n▶ 取得中: {place_id}")
        try:
            result = fetch_reviews(place_id)
        except Exception as e:
            print(f"  エラー: {e}")
            continue

        shop_name = result.get("name", "不明")
        reviews = result.get("reviews", [])
        print(f"  店舗名: {shop_name} / 取得件数: {len(reviews)}")

        count = 0
        for r in reviews[:MAX_REVIEWS]:
            lang = r.get("original_language") or r.get("language", "ja")
            text = r.get("text", "").strip()

            # 日本語レビューのみ・空テキスト除外
            if lang != "ja" or not text:
                continue

            key = (place_id, text)
            if key in existing_keys:
                print(f"  スキップ（重複）: {text[:30]}...")
                continue

            record = {
                "place_id": place_id,
                "shop_name": shop_name,
                "review_text": text,
                "rating": r.get("rating", 0),
            }
            new_records.append(record)
            existing_keys.add(key)
            count += 1
            print(f"  [{r.get('rating')}★] {text[:40]}...")

        print(f"  → 新規追加: {count}件")
        time.sleep(0.5)  # API レート制限対策

    all_records = existing + new_records
    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(all_records, f, ensure_ascii=False, indent=2)

    print(f"\n✅ 完了: 新規 {len(new_records)}件 / 累計 {len(all_records)}件")
    print(f"   保存先: {OUTPUT_FILE}")


if __name__ == "__main__":
    collect()
