import os
import requests

LINE_CHANNEL_ACCESS_TOKEN = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN")


def send_line_message(line_user_id: str, message: str):
    """
    LINE Messaging APIで個別ユーザーにメッセージを送信する。
    """
    url = "https://api.line.me/v2/bot/message/push"
    headers = {
        "Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}",
        "Content-Type": "application/json"
    }
    payload = {
        "to": line_user_id,
        "messages": [{"type": "text", "text": message}]
    }
    response = requests.post(url, headers=headers, json=payload)
    response.raise_for_status()


def build_message(shop_name: str, reviews: list, replies: list,
                  avg_rating: float, prev_avg_rating: float,
                  total_month: int, advice: str) -> str:
    """
    LINEに送るメッセージ本文を組み立てる。
    """
    lines = []

    lines.append(f"📊 {shop_name} 週次レポート")
    lines.append("━━━━━━━━━━━━━━━")

    rating_diff = avg_rating - prev_avg_rating if prev_avg_rating else 0
    diff_str = (
        f"（先週比 {'↑' if rating_diff > 0 else '↓' if rating_diff < 0 else '→'}"
        f"{abs(rating_diff):.1f}）"
        if prev_avg_rating else ""
    )
    lines.append(f"⭐ 今週の平均評価：{avg_rating:.1f} {diff_str}")
    lines.append(f"📝 今週の口コミ数：{len(reviews)}件")
    lines.append(f"📅 今月の累計：{total_month}件")
    lines.append("")

    if reviews:
        lines.append("【今週の返答案】")
        for i, (review, reply) in enumerate(zip(reviews, replies), 1):
            stars = "⭐" * review["rating"]
            lines.append(f"\n{i}. {stars}")
            if review["text"]:
                preview = review["text"][:30] + "..." if len(review["text"]) > 30 else review["text"]
                lines.append(f"「{preview}」")
            lines.append(f"\n返答案：\n{reply}")
            lines.append("──────────────")

    if advice:
        lines.append("")
        lines.append(advice)

    lines.append("")
    lines.append("▶ Googleマップで返答する")
    lines.append("https://business.google.com")

    return "\n".join(lines)
