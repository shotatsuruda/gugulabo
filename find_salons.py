"""
find_salons.py
Google Places API Text Search で美容室を検索し place_id を収集するスクリプト

使い方:
    cd /var/www/gugulabo
    source venv/bin/activate
    python find_salons.py

結果: salons.json に保存
"""

import json
import os
import time
import requests
from dotenv import load_dotenv

load_dotenv()

# ----------------------------------------------------------------
# 検索設定
# ----------------------------------------------------------------
KEYWORDS = ["美容室", "美容院", "ヘアサロン"]
AREAS    = ["大阪", "神戸", "京都"]
MIN_RATING = 4.0
MAX_PER_QUERY = 20   # Places API 1リクエストあたりの上限

OUTPUT_FILE = os.path.join(os.path.dirname(__file__), "salons.json")


def text_search(query: str, api_key: str, page_token: str = None) -> dict:
    """Places API Legacy Text Search を呼び出す。"""
    url = "https://maps.googleapis.com/maps/api/place/textsearch/json"
    params = {
        "query": query,
        "language": "ja",
        "key": api_key,
    }
    if page_token:
        params["pagetoken"] = page_token

    resp = requests.get(url, params=params, timeout=10)
    resp.raise_for_status()
    return resp.json()


def collect_salons() -> list:
    api_key = os.environ.get("PLACES_API_KEY") or os.environ.get("GOOGLE_API_KEY")
    if not api_key:
        raise RuntimeError("環境変数 PLACES_API_KEY が設定されていません")

    seen_ids = set()
    results = []

    for area in AREAS:
        for keyword in KEYWORDS:
            query = f"{area} {keyword}"
            print(f"\n▶ 検索: 「{query}」")

            page_token = None
            page_count = 0

            while page_count < 2:   # 最大2ページ（20件×2=40件）まで取得
                if page_token:
                    time.sleep(2)   # next_page_token は2秒待機が必要

                data = text_search(query, api_key, page_token)
                status = data.get("status")

                if status not in ("OK", "ZERO_RESULTS"):
                    print(f"  APIエラー: {status} - {data.get('error_message', '')}")
                    break

                candidates = data.get("results", [])
                print(f"  取得: {len(candidates)}件 (ページ {page_count + 1})")

                for place in candidates:
                    place_id = place.get("place_id")
                    rating   = place.get("rating", 0)
                    name     = place.get("name", "")
                    address  = place.get("formatted_address", "")

                    if place_id in seen_ids:
                        continue
                    if rating < MIN_RATING:
                        continue

                    seen_ids.add(place_id)
                    results.append({
                        "place_id": place_id,
                        "name":     name,
                        "rating":   rating,
                        "address":  address,
                    })
                    print(f"  ✅ [{rating}★] {name} / {address[:30]}")

                page_token = data.get("next_page_token")
                page_count += 1
                if not page_token:
                    break

                time.sleep(0.3)

    # 評価の高い順にソート
    results.sort(key=lambda x: x["rating"], reverse=True)
    return results


def main():
    print("=== find_salons.py 開始 ===")
    salons = collect_salons()

    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(salons, f, ensure_ascii=False, indent=2)

    print(f"\n✅ 完了: {len(salons)}件を保存 → {OUTPUT_FILE}")

    # サンプル表示（上位10件）
    print("\n--- 結果サンプル（上位10件）---")
    for s in salons[:10]:
        print(f"  [{s['rating']}★] {s['name']}")
        print(f"    place_id: {s['place_id']}")
        print(f"    住所: {s['address']}")


if __name__ == "__main__":
    main()
