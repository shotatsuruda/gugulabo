import os
import requests

def get_reviews(place_id: str) -> dict:
    """
    Places API（New）でplace_idの口コミを取得する。
    返り値：{ rating: float, reviews: [ {id, author, rating, text, time}, ... ] }
    """
    PLACES_API_KEY = os.environ.get("PLACES_API_KEY")
    url = f"https://places.googleapis.com/v1/places/{place_id}"
    headers = {
        "X-Goog-Api-Key": PLACES_API_KEY,
        "X-Goog-FieldMask": "rating,reviews"
    }
    params = {"reviews_sort": "newest"}
    response = requests.get(url, headers=headers, params=params)
    response.raise_for_status()
    data = response.json()

    reviews = []
    for r in data.get("reviews", []):
        reviews.append({
            "id": r.get("name", ""),
            "author": r.get("authorAttribution", {}).get("displayName", "匿名"),
            "rating": r.get("rating", 0),
            "text": r.get("text", {}).get("text", ""),
            "time": r.get("publishTime", "")
        })

    return {
        "rating": data.get("rating", 0),
        "reviews": reviews
    }


def get_new_reviews(shop_id: int, place_id: str, db_conn) -> tuple:
    """
    取得した口コミのうち、DBに未登録のもの（新着）だけ返す。
    返り値：(new_reviews: list, current_avg_rating: float)
    """
    data = get_reviews(place_id)
    all_reviews = data["reviews"]

    cursor = db_conn.execute(
        "SELECT review_id FROM fetched_reviews WHERE shop_id = ?", (shop_id,)
    )
    seen_ids = {row[0] for row in cursor.fetchall()}

    new_reviews = [r for r in all_reviews if r["id"] not in seen_ids]
    return new_reviews, data["rating"]
