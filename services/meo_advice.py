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

    # 業種別の特性ヒント
    type_hints = {
        "メンズサロン":    "男性客向け・スピード感・清潔感・コスパを重視した集客",
        "女性専用サロン":  "プライベート感・安心感・丁寧なカウンセリングを重視した集客",
        "ヘッドスパ専門":  "リラクゼーション・頭皮ケア・癒し体験を重視した集客",
        "縮毛矯正専門":    "技術の専門性・ダメージレス・仕上がりの自然さを重視した集客",
        "美容院":          "幅広いメニュー・スタイリスト指名・再来店率向上を重視した集客",
        "総合サロン":      "多様なメニュー展開・ファミリー層・地域密着を重視した集客",
    }
    hint = type_hints.get(business_type, "サロンとしての専門性と顧客満足を重視した集客")

    prompt = f"""あなたはMEO（Googleマップ集客）の専門家です。
{month}月の{business_type}向けに、以下のトピックについて実践的なアドバイスを200文字以内で書いてください。

トピック：{topic}
この店舗の特性：{hint}

条件：
- 具体的で今すぐ実践できる内容にする
- {business_type}ならではの強みや特性に絡めた内容にする
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
