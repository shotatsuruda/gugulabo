"""
collect_reviews.py
salons.json の全 place_id を対象に口コミを収集するスクリプト

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

SALONS_FILE  = os.path.join(os.path.dirname(__file__), "salons.json")
OUTPUT_FILE  = os.path.join(os.path.dirname(__file__), "reviews_collected.json")
MAX_REVIEWS  = 5    # Places API の上限
MIN_RATING   = 4.0  # 評価フィルタ
MIN_LENGTH   = 50   # 文字数フィルタ


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
    # salons.json から place_id を読み込む
    if not os.path.exists(SALONS_FILE):
        print(f"salons.json が見つかりません: {SALONS_FILE}")
        return

    with open(SALONS_FILE, "r", encoding="utf-8") as f:
        salons = json.load(f)

    place_ids = [s["place_id"] for s in salons if s.get("place_id")]
    print(f"=== collect_reviews.py 開始: {len(place_ids)}件の店舗を処理 ===")

    # 既存データ読み込み（重複回避）
    existing = []
    if os.path.exists(OUTPUT_FILE):
        with open(OUTPUT_FILE, "r", encoding="utf-8") as f:
            existing = json.load(f)

    existing_keys = {(r["place_id"], r["review_text"]) for r in existing}
    new_records = []

    for i, place_id in enumerate(place_ids, 1):
        print(f"\n[{i}/{len(place_ids)}] 取得中: {place_id}")
        try:
            result = fetch_reviews(place_id)
        except Exception as e:
            print(f"  エラー: {e}")
            time.sleep(1)
            continue

        shop_name = result.get("name", "不明")
        reviews = result.get("reviews", [])
        print(f"  店舗名: {shop_name} / 取得件数: {len(reviews)}")

        count = 0
        for r in reviews[:MAX_REVIEWS]:
            lang   = r.get("original_language") or r.get("language", "ja")
            text   = r.get("text", "").strip()
            rating = r.get("rating", 0)

            # 日本語・評価4以上・50文字以上のみ
            if lang != "ja":
                continue
            if not text:
                continue
            if rating < MIN_RATING:
                continue
            if len(text) < MIN_LENGTH:
                continue

            key = (place_id, text)
            if key in existing_keys:
                continue

            record = {
                "place_id":    place_id,
                "shop_name":   shop_name,
                "review_text": text,
                "rating":      rating,
            }
            new_records.append(record)
            existing_keys.add(key)
            count += 1
            print(f"  [{rating}★] {text[:40]}...")

        print(f"  → 新規追加: {count}件")
        time.sleep(0.5)  # API レート制限対策

    all_records = existing + new_records
    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(all_records, f, ensure_ascii=False, indent=2)

    print(f"\n✅ 完了: 新規 {len(new_records)}件 / 累計 {len(all_records)}件")
    print(f"   保存先: {OUTPUT_FILE}")


if __name__ == "__main__":
    collect()
