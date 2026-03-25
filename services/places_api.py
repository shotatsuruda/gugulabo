import os
import requests


def _translate_to_japanese(text: str) -> str:
    """OpenRouterを使ってテキストを日本語に翻訳する。"""
    OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY")
    response = requests.post(
        "https://openrouter.ai/api/v1/chat/completions",
        headers={
            "Authorization": f"Bearer {OPENROUTER_API_KEY}",
            "Content-Type": "application/json",
        },
        json={
            "model": "anthropic/claude-haiku-4-5",
            "messages": [{"role": "user", "content": f"次のテキストを日本語に翻訳してください。翻訳文のみ返してください。\n\n{text}"}],
            "max_tokens": 500,
        },
    )
    response.raise_for_status()
    return response.json()["choices"][0]["message"]["content"].strip()


def get_reviews(place_id: str) -> dict:
    """
    Places API（Legacy）でplace_idの口コミを取得する。
    返り値：{ rating: float, reviews: [ {id, author, rating, text, time}, ... ] }
    """
    PLACES_API_KEY = os.environ.get("PLACES_API_KEY")
    url = "https://maps.googleapis.com/maps/api/place/details/json"
    params = {
        "place_id": place_id,
        "fields": "rating,reviews",
        "reviews_sort": "newest",
        "reviews_no_translations": "true",
        "language": "ja",
        "key": PLACES_API_KEY
    }
    response = requests.get(url, params=params)
    response.raise_for_status()
    data = response.json().get("result", {})

    reviews = []
    for r in data.get("reviews", []):
        author = r.get("author_name", "匿名")
        time = r.get("time", "")
        text = r.get("text", "")
        lang = r.get("original_language") or r.get("language", "ja")
        if text and lang != "ja":
            try:
                text = f"(翻訳){_translate_to_japanese(text)}"
            except Exception:
                pass
        reviews.append({
            "id": f"{author}_{time}",
            "author": author,
            "rating": r.get("rating", 0),
            "text": text,
            "time": time
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
