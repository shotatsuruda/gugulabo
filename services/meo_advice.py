import os
import requests
from datetime import date

def get_week_of_month(target_date: date) -> int:
    """月内の第何週かを返す（1〜4）"""
    day = target_date.day
    if day <= 7:
        return 1
    elif day <= 14:
        return 2
    elif day <= 21:
        return 3
    else:
        return 4

TOPIC_MAP = {
    1: "季節トレンド・需要予測",
    2: "その月にやるべきMEO施策",
    3: "口コミ返答のコツ",
    4: "Googleプロフィール改善Tips",
}

# 第2〜4週のローテーション（季節トレンドは第1週のみ）
TOPIC_MAP_NON_FIRST = {
    0: "その月にやるべきMEO施策",
    1: "口コミ返答のコツ",
    2: "Googleプロフィール改善Tips",
}

def generate_meo_advice(business_type: str, target_date: date = None) -> str:
    """
    業種と週番号に応じたMEOアドバイスをAIで生成して返す。
    """
    if target_date is None:
        target_date = date.today()

    week = get_week_of_month(target_date)
    if week == 1:
        topic = TOPIC_MAP[1]
    else:
        topic = TOPIC_MAP_NON_FIRST[(week - 2) % 3]
    month = target_date.month

    OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY")

    prompt = f"""あなたはMEO（Googleマップ集客）の専門家です。
{month}月の{business_type}向けに、以下のトピックについて実践的なアドバイスを200文字以内で書いてください。

トピック：{topic}

条件：
- 具体的で今すぐ実践できる内容にする
- {business_type}に特化した内容にする
- 親しみやすい口調で書く
- 絵文字を適度に使う
"""

    response = requests.post(
        "https://openrouter.ai/api/v1/chat/completions",
        headers={
            "Authorization": f"Bearer {OPENROUTER_API_KEY}",
            "Content-Type": "application/json",
        },
        json={
            "model": "anthropic/claude-sonnet-4-5",
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": 400,
        },
    )
    response.raise_for_status()
    return response.json()["choices"][0]["message"]["content"].strip()
