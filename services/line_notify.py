import os
import requests


def send_line_message(line_user_id: str, message: str):
    """
    LINE Messaging APIで個別ユーザーにメッセージを送信する。
    """
    LINE_CHANNEL_ACCESS_TOKEN = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN")
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


def build_message(shop_name: str, reviews: list,
                  avg_rating: float, prev_avg_rating: float,
                  total_month: int, advice: str, meo_advice: str = None) -> str:
    """
    週次サマリーメッセージを組み立てる（返答案は含まない）。
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
    lines.append(f"📝 今週の口コミ数：{len(reviews)}件")
    lines.append(f"📅 今月の累計：{total_month}件")

    if advice:
        lines.append("")
        lines.append(advice)

    lines.append("")
    lines.append("▶ Googleマップで返答する")
    lines.append("https://business.google.com")

    if meo_advice:
        lines.append("")
        lines.append("━━━━━━━━━━━━━━━")
        lines.append("📌 今週のMEOアドバイス")
        lines.append("")
        lines.append(meo_advice)

    return "\n".join(lines)


def build_reply_message(review: dict, reply: str, index: int) -> str:
    """
    口コミ1件分の返答案メッセージを組み立てる。
    """
    lines = []
    stars = "⭐" * review["rating"] + "☆" * (5 - review["rating"])
    lines.append("────────────")
    lines.append(f"【返答案 {index}】")
    lines.append(stars)
    if review["text"]:
        preview = review["text"][:30] + "..." if len(review["text"]) > 30 else review["text"]
        lines.append(f"「{preview}」")
    lines.append("")
    lines.append(reply)
    lines.append("────────────")
    return "\n".join(lines)
