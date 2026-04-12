import os
import requests

# サロンタイプ別ペルソナ定義
_SALON_PERSONA = {
    "美容院": {
        "role": "美容室のオーナースタイリスト",
        "style": "丁寧で温かみがあり、スタイリストとしての専門性をさりげなく伝える",
        "closing_examples": [
            "またのご来店をスタッフ一同お待ちしております。",
            "次回もご希望のスタイルを一緒に作り上げましょう。",
            "またいつでもお気軽にご来店ください。",
        ],
        "keywords": ["スタイル", "仕上がり", "スタイリスト", "ヘアケア"],
    },
    "メンズサロン": {
        "role": "メンズ美容室のオーナー",
        "style": "すっきりとしたテンポで、男性客に寄り添う誠実な口調",
        "closing_examples": [
            "またのご来店をお待ちしています。",
            "次回もベストな仕上がりをご提供できるよう努めます。",
            "またぜひお立ち寄りください。",
        ],
        "keywords": ["カット", "フェード", "清潔感", "スタイル"],
    },
    "女性専用サロン": {
        "role": "女性専用美容室のオーナー",
        "style": "安心感とプライベート感を大切にした、やさしく丁寧な口調",
        "closing_examples": [
            "またリラックスしてご来店いただければ幸いです。",
            "次回もゆったりとした時間をご提供できるよう努めます。",
            "またのご来店を心よりお待ちしております。",
        ],
        "keywords": ["プライベート感", "安心", "丁寧なカウンセリング", "リラックス"],
    },
    "ヘッドスパ専門": {
        "role": "ヘッドスパ専門サロンのオーナー",
        "style": "癒しと頭皮ケアの専門家としての温かみのある口調",
        "closing_examples": [
            "またリフレッシュしにお越しください。",
            "次回も心地よい時間をご提供できるよう努めます。",
            "頭皮ケアのことはいつでもご相談ください。",
        ],
        "keywords": ["頭皮ケア", "リラクゼーション", "ヘッドスパ", "癒し"],
    },
    "縮毛矯正専門": {
        "role": "縮毛矯正専門サロンのオーナー",
        "style": "技術への自信と誠実さを持ち、髪の悩みに寄り添う専門家らしい口調",
        "closing_examples": [
            "またお髪のことはお気軽にご相談ください。",
            "次回も自然でキレイな仕上がりを目指します。",
            "縮毛矯正のご相談はいつでもお待ちしております。",
        ],
        "keywords": ["縮毛矯正", "自然な仕上がり", "ダメージケア", "くせ毛"],
    },
    "総合サロン": {
        "role": "総合美容サロンのオーナー",
        "style": "幅広いお客様に対応する親しみやすく丁寧な口調",
        "closing_examples": [
            "またのご来店をスタッフ一同心よりお待ちしております。",
            "次回もご満足いただけるよう精一杯努めます。",
            "またいつでもお気軽にご来店ください。",
        ],
        "keywords": ["トータルビューティー", "スタイル", "ヘアケア", "サービス"],
    },
}

_DEFAULT_PERSONA = {
    "role": "美容室のオーナースタイリスト",
    "style": "丁寧で温かみがあり、スタイリストとしての専門性をさりげなく伝える",
    "closing_examples": ["またのご来店をスタッフ一同お待ちしております。"],
    "keywords": ["スタイル", "仕上がり", "ヘアケア"],
}


def generate_reply(review: dict, business_type: str, style_texts: list = None) -> str:
    """
    口コミ1件に対して、サロンタイプ別のAI返答文を生成する。
    style_texts: 過去の返答文テキストのリスト（文体・トーン参照用）
    """
    OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY")
    rating = review["rating"]
    text = review["text"]
    author = review["author"]

    persona = _SALON_PERSONA.get(business_type, _DEFAULT_PERSONA)

    if rating >= 4:
        tone = "感謝の気持ちを丁寧に伝え、再来店を自然に促す"
    elif rating == 3:
        tone = "感謝しつつ、より良いサービスを提供できるよう改善に努める姿勢を示す"
    else:
        tone = "誠実にお詫びし、具体的な改善意思を示す。防御的にならず真摯に受け止める"

    # 低評価（星1〜2）は140〜180文字、それ以外は80〜120文字
    if rating <= 2:
        min_chars, max_chars = 140, 180
    else:
        min_chars, max_chars = 80, 120

    closing = "／".join(persona["closing_examples"])

    style_section = ""
    if style_texts:
        combined = "\n---\n".join(style_texts)
        style_section = f"\n【過去の返答例（文体・トーン・言い回しを必ず踏襲すること）】\n{combined}\n"

    prompt = f"""あなたは{persona["role"]}です。
以下のGoogleレビューに対して返答文を{min_chars}〜{max_chars}文字で作成してください。

【投稿者】{author}
【評価】★{rating}
【内容】{text if text else "（テキストなし・星のみの投稿）"}

【返答の方向性】{tone}
【文体・トーン】{persona["style"]}
【クロージングの参考例】{closing}
{style_section}
条件：
- 丁寧な敬語を使う
- 書き出しのバリエーションを意識する（「ありがとうございます」「嬉しいお言葉」「貴重なご意見」「ご来店いただき」「{persona["keywords"][0]}についてお褒めの言葉」など）
- 口コミの内容に具体的に触れる（テキストなしの場合はスターへの感謝を）
- 店名・個人名は含めない
- 定型文にならないよう自然に
- 他店・競合との比較表現は一切使わない
- 過去の返答例がある場合はその文体・トーン・言い回しを必ず踏襲する
- 文字数：{min_chars}〜{max_chars}文字
- 返答文のみを出力すること（前置き・説明・注釈は不要）
"""

    response = requests.post(
        "https://openrouter.ai/api/v1/chat/completions",
        headers={"Authorization": f"Bearer {OPENROUTER_API_KEY}"},
        json={
            "model": "anthropic/claude-haiku-4.5",
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": 300,
        },
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
