import os
import requests

OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY")


def generate_reply(review: dict, business_type: str) -> str:
    """
    口コミ1件に対してAI返答文を生成する。
    """
    rating = review["rating"]
    text = review["text"]
    author = review["author"]

    if rating >= 4:
        tone_instruction = "感謝の気持ちを丁寧に伝え、また来店を促す返答"
    elif rating == 3:
        tone_instruction = "感謝しつつ、改善に努める姿勢を示す返答"
    else:
        tone_instruction = "誠実にお詫びし、改善の意思を示す丁寧な返答"

    prompt = f"""あなたは{business_type}の店舗オーナーです。
以下のGoogleレビューに対して、{tone_instruction}を80〜120文字で作成してください。

投稿者：{author}
評価：★{rating}
内容：{text if text else "（テキストなし）"}

条件：
- 丁寧な敬語を使う
- 「この度は」で書き出す
- 店名は含めない
- 定型文にならないよう自然に
- 文字数：80〜120文字
"""

    response = requests.post(
        "https://openrouter.ai/api/v1/chat/completions",
        headers={"Authorization": f"Bearer {OPENROUTER_API_KEY}"},
        json={
            "model": "anthropic/claude-sonnet-4-5",
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": 300
        }
    )
    response.raise_for_status()
    return response.json()["choices"][0]["message"]["content"].strip()


def generate_advice(zero_weeks: int, avg_rating: float, review_count: int) -> str:
    """
    状況に応じたアドバイスメッセージを返す。
    """
    if review_count > 0:
        if avg_rating >= 4.5:
            return "✨ 今週も高評価が続いています！この調子でQRを渡し続けましょう。"
        elif avg_rating < 3.5:
            return "⚠️ 低評価の口コミがありました。早めに返答することで印象が改善します。"
        else:
            return "📊 今週も口コミが届きました。返答文をコピーしてGoogleに貼り付けましょう。"
    else:
        if zero_weeks == 1:
            return "💡 今週の口コミは0件でした。QRコードは設置できていますか？お会計時にひと声添えると効果的です。"
        elif zero_weeks == 2:
            return "📍 2週連続で口コミが0件です。QRコードの設置場所を変えてみましょう。レジ横・出口付近が特に効果的です。"
        elif zero_weeks >= 3:
            return f"🔄 {zero_weeks}週連続で口コミが来ていません。POPのデザインを変えてみませんか？\n新しいテンプレートはこちら→ https://gugulabo.com/qr"
        return ""
