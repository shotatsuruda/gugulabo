"""
マッサージ店向け Googleレビュー管理システム
Flask + OpenRouter API + qrcode + Pillow を使用
"""

import base64
import io
import os
import re
import secrets
import smtplib
import sqlite3
import string
from datetime import date, datetime, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from functools import wraps

import bcrypt
from dotenv import load_dotenv
from flask import (
    Flask, abort, flash, jsonify, redirect, render_template,
    request, url_for,
)
from flask_login import (
    LoginManager, UserMixin, current_user, login_required,
    login_user, logout_user,
)
from openai import OpenAI
from PIL import Image
import qrcode
from qrcode.image.styledpil import StyledPilImage
from qrcode.image.styles.colormasks import (
    HorizontalGradiantColorMask,
    RadialGradiantColorMask,
    SolidFillColorMask,
    VerticalGradiantColorMask,
)

try:
    from qrcode.image.styles.moduledrawers.pil import (
        CircleModuleDrawer,
        RoundedModuleDrawer,
        SquareModuleDrawer,
    )
except ImportError:
    from qrcode.image.styles.moduledrawers import (  # type: ignore
        CircleModuleDrawer,
        RoundedModuleDrawer,
        SquareModuleDrawer,
    )

load_dotenv()

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY", "change-this-in-production")

# ===== 設定値（.envから読み込む） =====
SHOP_NAME = os.environ.get("SHOP_NAME", "リラクゼーションサロン")
OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY")

# メール送信設定（クーポン送信に使用）
MAIL_SMTP_HOST = os.environ.get("MAIL_SMTP_HOST", "smtp.gmail.com")
MAIL_SMTP_PORT = int(os.environ.get("MAIL_SMTP_PORT", "587"))
MAIL_USERNAME = os.environ.get("MAIL_USERNAME", "")
MAIL_PASSWORD = os.environ.get("MAIL_PASSWORD", "")
MAIL_FROM = os.environ.get("MAIL_FROM", MAIL_USERNAME)

DATABASE = os.path.join(os.path.dirname(__file__), "review_system.db")
DATABASE_URL = os.environ.get("DATABASE_URL")
DB_TYPE = "postgresql" if DATABASE_URL else "sqlite"

if DB_TYPE == "postgresql":
    import psycopg2
    import psycopg2.extras
    DBIntegrityError = psycopg2.IntegrityError
else:
    DBIntegrityError = sqlite3.IntegrityError


# ===== Flask-Login 設定 =====

login_manager = LoginManager(app)
login_manager.login_view = "login"
login_manager.login_message = "ログインが必要です。"
login_manager.login_message_category = "warning"


class User(UserMixin):
    def __init__(self, id, email, name, is_admin):
        self.id = id
        self.email = email
        self.name = name
        self.is_admin = bool(is_admin)


@login_manager.user_loader
def load_user(user_id):
    conn = get_db()
    row = conn.execute(
        "SELECT id, email, name, is_admin FROM users WHERE id = ?", (user_id,)
    ).fetchone()
    conn.close()
    if row is None:
        return None
    return User(row["id"], row["email"], row["name"], row["is_admin"])


def admin_required(f):
    """管理者のみアクセス可能なルートに付けるデコレーター"""
    @wraps(f)
    @login_required
    def decorated(*args, **kwargs):
        if not current_user.is_admin:
            abort(403)
        return f(*args, **kwargs)
    return decorated


# ===== データベース =====

class _PgCursor:
    """psycopg2カーソルをsqlite3カーソルのように扱うラッパー"""

    def __init__(self, cursor, is_insert=False):
        self._cursor = cursor
        self.lastrowid = None
        if is_insert:
            row = cursor.fetchone()
            if row:
                self.lastrowid = row["id"]

    def fetchone(self):
        return self._cursor.fetchone()

    def fetchall(self):
        return self._cursor.fetchall()


class _PgConn:
    """psycopg2接続をsqlite3接続のように扱うラッパー"""

    def __init__(self, conn):
        self._conn = conn

    def execute(self, sql, params=None):
        is_insert = sql.strip().upper().startswith("INSERT")
        pg_sql = sql.replace("?", "%s")
        if is_insert and "RETURNING" not in sql.upper():
            pg_sql = pg_sql.rstrip().rstrip(";") + " RETURNING id"
        cursor = self._conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        if params is not None:
            cursor.execute(pg_sql, params)
        else:
            cursor.execute(pg_sql)
        return _PgCursor(cursor, is_insert=is_insert)

    def commit(self):
        self._conn.commit()

    def close(self):
        self._conn.close()

    def rollback(self):
        self._conn.rollback()


def get_db():
    """データベース接続を返す"""
    if DB_TYPE == "postgresql":
        conn = psycopg2.connect(DATABASE_URL)
        return _PgConn(conn)
    conn = sqlite3.connect(DATABASE)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    """データベースとテーブルを初期化する"""
    conn = get_db()

    # データベース種別に応じた主キー構文
    pk = "SERIAL PRIMARY KEY" if DB_TYPE == "postgresql" else "INTEGER PRIMARY KEY AUTOINCREMENT"

    # ユーザーテーブル
    conn.execute(
        f"""
        CREATE TABLE IF NOT EXISTS users (
            id            {pk},
            email         TEXT NOT NULL UNIQUE,
            password_hash TEXT NOT NULL,
            name          TEXT NOT NULL,
            is_admin      INTEGER NOT NULL DEFAULT 0,
            created_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """
    )

    # 店舗テーブル
    conn.execute(
        f"""
        CREATE TABLE IF NOT EXISTS shops (
            id         {pk},
            name       TEXT NOT NULL,
            review_url TEXT NOT NULL,
            slug       TEXT UNIQUE,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """
    )

    # 既存テーブルへの slug / user_id / business_type カラム追加（マイグレーション）
    if DB_TYPE == "postgresql":
        conn.execute("ALTER TABLE shops ADD COLUMN IF NOT EXISTS slug TEXT")
        conn.execute("ALTER TABLE shops ADD COLUMN IF NOT EXISTS user_id INTEGER REFERENCES users(id)")
        conn.execute("ALTER TABLE shops ADD COLUMN IF NOT EXISTS business_type TEXT DEFAULT ''")
    else:
        try:
            conn.execute("ALTER TABLE shops ADD COLUMN slug TEXT")
        except Exception:
            pass  # カラムが既に存在する場合はスキップ
        try:
            conn.execute("ALTER TABLE shops ADD COLUMN user_id INTEGER REFERENCES users(id)")
        except Exception:
            pass  # カラムが既に存在する場合はスキップ
        try:
            conn.execute("ALTER TABLE shops ADD COLUMN business_type TEXT DEFAULT ''")
        except Exception:
            pass  # カラムが既に存在する場合はスキップ

    # users テーブルへの plan / plan_expires_at / notify_enabled カラム追加（マイグレーション）
    if DB_TYPE == "postgresql":
        conn.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS plan TEXT DEFAULT '月額プラン'")
        conn.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS plan_expires_at TEXT")
        conn.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS notify_enabled INTEGER NOT NULL DEFAULT 1")
    else:
        try:
            conn.execute("ALTER TABLE users ADD COLUMN plan TEXT DEFAULT '月額プラン'")
        except Exception:
            pass
        try:
            conn.execute("ALTER TABLE users ADD COLUMN plan_expires_at TEXT")
        except Exception:
            pass
        try:
            conn.execute("ALTER TABLE users ADD COLUMN notify_enabled INTEGER NOT NULL DEFAULT 1")
        except Exception:
            pass

    # slug のユニークインデックス（既にあればスキップ）
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_shops_slug ON shops(slug)")

    conn.commit()

    # slug が未設定の既存店舗に shop-{id} を自動設定
    shops_without_slug = conn.execute(
        "SELECT id FROM shops WHERE slug IS NULL OR slug = ''"
    ).fetchall()
    for row in shops_without_slug:
        conn.execute(
            "UPDATE shops SET slug = ? WHERE id = ?",
            (f"shop-{row['id']}", row["id"]),
        )

    # クーポン設定テーブル（店舗ごとに1件）
    conn.execute(
        f"""
        CREATE TABLE IF NOT EXISTS coupons (
            id            {pk},
            shop_id       INTEGER NOT NULL UNIQUE,
            coupon_name   TEXT NOT NULL DEFAULT 'ご来店感謝クーポン',
            discount_text TEXT NOT NULL DEFAULT '次回施術10%オフ',
            valid_days    INTEGER NOT NULL DEFAULT 30,
            is_active     INTEGER NOT NULL DEFAULT 1,
            updated_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """
    )

    # お客様のご意見テーブル（★1〜3のフィードバック）
    conn.execute(
        f"""
        CREATE TABLE IF NOT EXISTS feedbacks (
            id           {pk},
            shop_id      INTEGER NOT NULL,
            rating       INTEGER NOT NULL,
            comment      TEXT,
            submitted_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """
    )

    # クーポン送信履歴テーブル
    conn.execute(
        f"""
        CREATE TABLE IF NOT EXISTS coupon_deliveries (
            id            {pk},
            shop_id       INTEGER NOT NULL,
            email         TEXT NOT NULL,
            coupon_code   TEXT NOT NULL,
            coupon_name   TEXT NOT NULL,
            discount_text TEXT NOT NULL,
            expires_at    TEXT NOT NULL,
            sent_at       TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """
    )

    conn.commit()
    conn.close()


with app.app_context():
    init_db()
    # 管理者が存在しない場合のみ初期管理者を自動作成
    _conn = get_db()
    _admin_exists = _conn.execute(
        "SELECT 1 FROM users WHERE is_admin = 1 LIMIT 1"
    ).fetchone()
    if not _admin_exists:
        _hash = bcrypt.hashpw(b"admin1234", bcrypt.gensalt()).decode()
        _conn.execute(
            "INSERT INTO users (email, password_hash, name, is_admin) VALUES (?, ?, ?, ?)",
            ("admin@gugulabo.com", _hash, "管理者", 1),
        )
        _conn.commit()
    _conn.close()


# ===== クーポン関連ユーティリティ =====

def _generate_coupon_code() -> str:
    """
    読みやすいクーポンコードを生成する。
    紛らわしい文字（O, 0, I, 1, L）を除いた英数字で構成。
    例: ABCD-EF23-GH45
    """
    alphabet = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
    raw = "".join(secrets.choice(alphabet) for _ in range(12))
    return f"{raw[:4]}-{raw[4:8]}-{raw[8:]}"


def send_coupon_email(
    to_email: str,
    shop_name: str,
    coupon_name: str,
    discount_text: str,
    coupon_code: str,
    expires_at: str,
) -> tuple[bool, str]:
    """
    クーポン情報をメールで送信する。
    テキスト・HTMLの両形式で送信し、受信メールクライアントが最適な方を表示する。
    """
    if not MAIL_USERNAME or not MAIL_PASSWORD:
        return False, (
            "メール送信設定が未設定です。"
            "環境変数 MAIL_USERNAME・MAIL_PASSWORD を設定してください。"
        )

    try:
        msg = MIMEMultipart("alternative")
        msg["From"] = f"{shop_name} <{MAIL_FROM}>"
        msg["To"] = to_email
        msg["Subject"] = f"【{shop_name}】クーポンのご案内"

        # ===== テキスト版メール本文 =====
        text_body = f"""
この度は{shop_name}にご来店いただき、誠にありがとうございました。

アンケートにお答えいただいた感謝として、クーポンをプレゼントいたします。

━━━━━━━━━━━━━━━━━━━━
  {coupon_name}
  割引内容: {discount_text}
  クーポンコード: {coupon_code}
  有効期限: {expires_at}
━━━━━━━━━━━━━━━━━━━━

ご来店の際にスタッフへクーポンコードをお伝えください。
皆様のご来店を心よりお待ちしております。

{shop_name} スタッフ一同
        """.strip()

        # ===== HTML版メール本文（見やすいデザイン） =====
        html_body = f"""<!DOCTYPE html>
<html lang="ja">
<head><meta charset="UTF-8"></head>
<body style="margin:0;padding:0;background:#f5f0ff;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;">
  <div style="max-width:560px;margin:32px auto;padding:0 16px;">

    <!-- ヘッダー -->
    <div style="background:linear-gradient(135deg,#667eea,#764ba2);border-radius:20px 20px 0 0;padding:32px 24px;text-align:center;">
      <p style="color:rgba(255,255,255,0.85);margin:0 0 8px;font-size:14px;">ご来店ありがとうございました</p>
      <h1 style="color:#fff;margin:0;font-size:22px;font-weight:700;">{shop_name}</h1>
    </div>

    <!-- 本文 -->
    <div style="background:#fff;padding:28px 24px;border-radius:0 0 20px 20px;box-shadow:0 4px 24px rgba(99,102,241,0.1);">
      <p style="color:#374151;line-height:1.7;margin-top:0;">
        アンケートにお答えいただきありがとうございます。<br>
        感謝の気持ちを込めてクーポンをプレゼントいたします。
      </p>

      <!-- クーポンカード -->
      <div style="background:linear-gradient(135deg,#667eea,#764ba2);border-radius:16px;padding:24px;text-align:center;margin:24px 0;">
        <p style="color:rgba(255,255,255,0.85);margin:0 0 4px;font-size:13px;">🎟️ {coupon_name}</p>
        <p style="color:#fff;margin:0 0 16px;font-size:26px;font-weight:800;">{discount_text}</p>

        <!-- クーポンコード枠 -->
        <div style="background:rgba(255,255,255,0.2);border-radius:12px;padding:14px;">
          <p style="color:rgba(255,255,255,0.7);margin:0 0 6px;font-size:11px;">クーポンコード</p>
          <p style="color:#fff;margin:0;font-size:24px;font-weight:800;letter-spacing:3px;">{coupon_code}</p>
        </div>

        <p style="color:rgba(255,255,255,0.75);margin:14px 0 0;font-size:13px;">有効期限: {expires_at}</p>
      </div>

      <p style="color:#6b7280;font-size:14px;line-height:1.7;">
        ご来店の際にスタッフへクーポンコードをお伝えください。<br>
        皆様のご来店を心よりお待ちしております。
      </p>

      <p style="color:#9ca3af;font-size:12px;margin-bottom:0;">{shop_name} スタッフ一同</p>
    </div>

  </div>
</body></html>"""

        msg.attach(MIMEText(text_body, "plain", "utf-8"))
        msg.attach(MIMEText(html_body, "html", "utf-8"))

        with smtplib.SMTP(MAIL_SMTP_HOST, MAIL_SMTP_PORT) as server:
            server.starttls()
            server.login(MAIL_USERNAME, MAIL_PASSWORD)
            server.sendmail(MAIL_FROM, to_email, msg.as_string())

        return True, "送信成功"

    except Exception as e:
        return False, str(e)


# ===== フィードバック通知メール =====

def send_feedback_notification(
    to_email: str,
    shop_name: str,
    rating: int,
    comment: str,
    submitted_at: str,
) -> None:
    """フィードバック受信をオーナーにメールで通知する"""
    if not MAIL_USERNAME or not MAIL_PASSWORD:
        return

    stars_filled = "★" * rating
    stars_empty  = "☆" * (5 - rating)
    stars        = stars_filled + stars_empty

    msg = MIMEMultipart("alternative")
    msg["From"]    = f"গগলাবো <{MAIL_FROM or MAIL_USERNAME}>"
    msg["To"]      = to_email
    msg["Subject"] = "【Gugulabo】新しいフィードバックが届きました"

    text_body = f"""
{shop_name} に新しいフィードバックが届きました。

━━━━━━━━━━━━━━━━━━━━
  店舗名　: {shop_name}
  星評価　: {stars}（{rating}）
  コメント: {comment or 'なし'}
  送信日時: {submitted_at}
━━━━━━━━━━━━━━━━━━━━

গগলাবো 管理画面でご確認ください。
    """.strip()

    html_body = f"""<!DOCTYPE html>
<html lang="ja">
<head><meta charset="UTF-8"></head>
<body style="margin:0;padding:0;background:#f5f0ff;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;">
  <div style="max-width:560px;margin:32px auto;padding:0 16px;">
    <div style="background:linear-gradient(135deg,#667eea,#764ba2);border-radius:20px 20px 0 0;padding:28px 24px;text-align:center;">
      <p style="color:rgba(255,255,255,0.85);margin:0 0 6px;font-size:13px;">新しいフィードバックが届きました</p>
      <h1 style="color:#fff;margin:0;font-size:20px;font-weight:700;">{shop_name}</h1>
    </div>
    <div style="background:#fff;padding:28px 24px;border-radius:0 0 20px 20px;box-shadow:0 4px 24px rgba(99,102,241,0.1);">
      <table style="width:100%;border-collapse:collapse;font-size:14px;color:#374151;">
        <tr style="border-bottom:1px solid #f3f4f6;">
          <td style="padding:10px 0;font-weight:600;width:90px;color:#6b7280;">星評価</td>
          <td style="padding:10px 0;font-size:18px;color:#f59e0b;letter-spacing:2px;">{stars}</td>
        </tr>
        <tr style="border-bottom:1px solid #f3f4f6;">
          <td style="padding:10px 0;font-weight:600;color:#6b7280;">コメント</td>
          <td style="padding:10px 0;">{comment or 'なし'}</td>
        </tr>
        <tr>
          <td style="padding:10px 0;font-weight:600;color:#6b7280;">送信日時</td>
          <td style="padding:10px 0;color:#9ca3af;">{submitted_at}</td>
        </tr>
      </table>
    </div>
  </div>
</body></html>"""

    msg.attach(MIMEText(text_body, "plain", "utf-8"))
    msg.attach(MIMEText(html_body, "html", "utf-8"))

    with smtplib.SMTP(MAIL_SMTP_HOST, MAIL_SMTP_PORT) as server:
        server.starttls()
        server.login(MAIL_USERNAME, MAIL_PASSWORD)
        server.sendmail(MAIL_FROM or MAIL_USERNAME, to_email, msg.as_string())


# ===== Googleレビュークリック通知メール =====

def send_google_click_notification(
    to_email: str,
    shop_name: str,
    clicked_at: str,
) -> None:
    """お客様がGoogleレビューページを開いたことをオーナーに通知する"""
    if not MAIL_USERNAME or not MAIL_PASSWORD:
        return

    msg = MIMEMultipart("alternative")
    msg["From"]    = f"Gugulabo <{MAIL_FROM or MAIL_USERNAME}>"
    msg["To"]      = to_email
    msg["Subject"] = "【Gugulabo】お客様がGoogleレビューページを開きました"

    text_body = f"""
{shop_name} のお客様がGoogleレビューページを開きました。

━━━━━━━━━━━━━━━━━━━━
  店舗名: {shop_name}
  日時　: {clicked_at}
━━━━━━━━━━━━━━━━━━━━

口コミ投稿につながる可能性があります。
Gugulabo 管理画面でフィードバックをご確認ください。
    """.strip()

    html_body = f"""<!DOCTYPE html>
<html lang="ja">
<head><meta charset="UTF-8"></head>
<body style="margin:0;padding:0;background:#f5f0ff;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;">
  <div style="max-width:560px;margin:32px auto;padding:0 16px;">
    <div style="background:linear-gradient(135deg,#667eea,#764ba2);border-radius:20px 20px 0 0;padding:28px 24px;text-align:center;">
      <p style="color:rgba(255,255,255,0.85);margin:0 0 6px;font-size:13px;">Googleレビューページが開かれました</p>
      <h1 style="color:#fff;margin:0;font-size:20px;font-weight:700;">{shop_name}</h1>
    </div>
    <div style="background:#fff;padding:28px 24px;border-radius:0 0 20px 20px;box-shadow:0 4px 24px rgba(99,102,241,0.1);">
      <p style="color:#374151;font-size:14px;line-height:1.7;margin-top:0;">
        お客様が <strong>「Googleレビューを書く」</strong> ボタンをクリックし、<br>
        Googleレビューページを開きました。
      </p>
      <table style="width:100%;border-collapse:collapse;font-size:14px;color:#374151;">
        <tr style="border-bottom:1px solid #f3f4f6;">
          <td style="padding:10px 0;font-weight:600;width:80px;color:#6b7280;">店舗名</td>
          <td style="padding:10px 0;">{shop_name}</td>
        </tr>
        <tr>
          <td style="padding:10px 0;font-weight:600;color:#6b7280;">日時</td>
          <td style="padding:10px 0;color:#9ca3af;">{clicked_at}</td>
        </tr>
      </table>
      <p style="color:#6b7280;font-size:13px;margin-top:1rem;margin-bottom:0;">
        口コミ投稿につながる可能性があります。
      </p>
    </div>
  </div>
</body></html>"""

    msg.attach(MIMEText(text_body, "plain", "utf-8"))
    msg.attach(MIMEText(html_body, "html", "utf-8"))

    with smtplib.SMTP(MAIL_SMTP_HOST, MAIL_SMTP_PORT) as server:
        server.starttls()
        server.login(MAIL_USERNAME, MAIL_PASSWORD)
        server.sendmail(MAIL_FROM or MAIL_USERNAME, to_email, msg.as_string())


# ===== QRコード生成 =====

def hex_to_rgb(hex_color: str) -> tuple:
    """HEXカラー文字列を (R, G, B) タプルに変換する"""
    h = hex_color.lstrip("#")
    return tuple(int(h[i : i + 2], 16) for i in (0, 2, 4))


def build_qr_image(
    url: str,
    fg_color: str,
    fg_color2: str,
    gradient_dir: str,
    bg_color: str,
    corner_style: str,
    size: int = 1024,
) -> Image.Image:
    """QRコード画像を生成して PIL Image (RGBA) を返す"""
    fg_rgb = hex_to_rgb(fg_color)
    bg_rgb = hex_to_rgb(bg_color)
    fg_rgb2 = hex_to_rgb(fg_color2)

    # モジュールの形状を選択
    drawers = {
        "rounded": RoundedModuleDrawer(),
        "dot": CircleModuleDrawer(),
        "square": SquareModuleDrawer(),
    }
    drawer = drawers.get(corner_style, SquareModuleDrawer())

    # カラーマスク（グラデーション or 単色）
    is_gradient = fg_rgb != fg_rgb2
    if is_gradient:
        if gradient_dir == "horizontal":
            color_mask = HorizontalGradiantColorMask(
                back_color=bg_rgb, left_color=fg_rgb, right_color=fg_rgb2
            )
        elif gradient_dir == "vertical":
            color_mask = VerticalGradiantColorMask(
                back_color=bg_rgb, top_color=fg_rgb, bottom_color=fg_rgb2
            )
        else:  # radial（放射状）
            color_mask = RadialGradiantColorMask(
                back_color=bg_rgb, center_color=fg_rgb, edge_color=fg_rgb2
            )
    else:
        color_mask = SolidFillColorMask(back_color=bg_rgb, front_color=fg_rgb)

    # エラー訂正レベルH（30%まで隠れても読める）を使用
    qr = qrcode.QRCode(
        error_correction=qrcode.constants.ERROR_CORRECT_H,
        box_size=10,
        border=4,
    )
    qr.add_data(url)
    qr.make(fit=True)

    styled = qr.make_image(
        image_factory=StyledPilImage,
        module_drawer=drawer,
        color_mask=color_mask,
    )

    # PIL Image (RGBA) に変換してリサイズ
    buf = io.BytesIO()
    styled.save(buf, format="PNG")
    buf.seek(0)
    img = Image.open(buf).copy().convert("RGBA")
    img = img.resize((size, size), Image.LANCZOS)

    return img


# ===== OpenRouter AI返答生成 =====

def generate_review_response(review_text: str, business_type: str = "") -> str:
    """OpenRouter APIを使い、Googleレビューへの返答文を日本語で生成する"""
    client = OpenAI(
        api_key=OPENROUTER_API_KEY,
        base_url="https://openrouter.ai/api/v1",
    )

    if business_type:
        persona  = f"{business_type}のオーナー"
        sign     = f"{business_type} スタッフ一同"
        context  = f"（業種：{business_type}）"
    else:
        persona  = "店舗のオーナー"
        sign     = "スタッフ一同"
        context  = ""

    prompt = f"""あなたは{persona}です{context}。お客様からGoogleにいただいた以下のレビューに対して、丁寧でプロフェッショナルな返答文を日本語で作成してください。

【返答文の条件】
- 丁寧な敬語を使用する
- お客様への感謝の気持ちを伝える
- レビューの内容（良い点・気になった点）に具体的に言及する
- ポジティブな内容は喜びを表現する
- ネガティブな内容は真摯に受け止め、改善への姿勢を示す
- 200〜300文字程度でまとめる
- 署名は「{sign}」とする

【お客様のレビュー】
{review_text}

【返答文のみを出力してください。説明や前置きは不要です。】"""

    response = client.chat.completions.create(
        model="anthropic/claude-sonnet-4-6",
        max_tokens=1024,
        messages=[{"role": "user", "content": prompt}],
    )
    return response.choices[0].message.content


# ===== ルート定義 =====

# ----- 認証 -----

@app.route("/login", methods=["GET", "POST"])
def login():
    if current_user.is_authenticated:
        return redirect(url_for("index"))

    error = None
    if request.method == "POST":
        email = (request.form.get("email") or "").strip().lower()
        password = request.form.get("password") or ""

        conn = get_db()
        row = conn.execute(
            "SELECT id, email, name, is_admin, password_hash FROM users WHERE email = ?",
            (email,),
        ).fetchone()
        conn.close()

        if row and bcrypt.checkpw(password.encode(), row["password_hash"].encode()):
            user = User(row["id"], row["email"], row["name"], row["is_admin"])
            login_user(user)
            return redirect(url_for("index"))
        else:
            error = "メールアドレスまたはパスワードが正しくありません。"

    return render_template("login.html", error=error)


@app.route("/register", methods=["GET", "POST"])
def register():
    if current_user.is_authenticated:
        return redirect(url_for("index"))

    errors = {}
    form = {}
    if request.method == "POST":
        form["name"]  = (request.form.get("name") or "").strip()
        form["email"] = (request.form.get("email") or "").strip().lower()
        password      = request.form.get("password") or ""
        password_conf = request.form.get("password_conf") or ""

        if not form["name"]:
            errors["name"] = "名前を入力してください。"
        if not form["email"] or "@" not in form["email"]:
            errors["email"] = "正しいメールアドレスを入力してください。"
        if len(password) < 6:
            errors["password"] = "パスワードは6文字以上で入力してください。"
        elif password != password_conf:
            errors["password_conf"] = "パスワードが一致しません。"

        if not errors:
            password_hash = bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()
            try:
                conn = get_db()
                cursor = conn.execute(
                    "INSERT INTO users (email, password_hash, name, is_admin) VALUES (?, ?, ?, 0)",
                    (form["email"], password_hash, form["name"]),
                )
                user_id = cursor.lastrowid
                conn.commit()
                conn.close()
                user = User(user_id, form["email"], form["name"], False)
                login_user(user)
                return redirect(url_for("qr_form"))
            except DBIntegrityError:
                errors["email"] = "そのメールアドレスはすでに登録されています。"

    return render_template("register.html", errors=errors, form=form)


@app.route("/logout")
@login_required
def logout():
    logout_user()
    return redirect(url_for("login"))


# ----- ホーム / ランディングページ -----

@app.route("/")
def index():
    if current_user.is_authenticated:
        conn = get_db()
        feedbacks = conn.execute(
            """
            SELECT f.rating, f.comment, f.submitted_at, s.name AS shop_name
            FROM feedbacks f
            JOIN shops s ON s.id = f.shop_id
            WHERE s.user_id = ? AND f.rating <= 3
            ORDER BY f.submitted_at DESC
            LIMIT 20
            """,
            (current_user.id,),
        ).fetchall()
        conn.close()
        feedbacks = [dict(fb) for fb in feedbacks]
        for fb in feedbacks:
            sa = fb.get("submitted_at")
            if sa and hasattr(sa, "strftime"):
                fb["submitted_at"] = sa.strftime("%Y-%m-%d %H:%M")
            elif sa and isinstance(sa, str):
                fb["submitted_at"] = sa[:16]
        return render_template("index.html", feedbacks=feedbacks)
    return render_template("landing.html")


@app.route("/contact", methods=["POST"])
def contact():
    """お問い合わせフォームの送信を受け付け、shotatsuruda0819@gmail.com にメール通知する"""
    data = request.get_json(silent=True) or {}
    name    = (data.get("name")    or "").strip()
    email   = (data.get("email")   or "").strip()
    message = (data.get("message") or "").strip()

    if not name or not email or not message:
        return jsonify({"success": False, "error": "全ての項目を入力してください"})

    if MAIL_USERNAME and MAIL_PASSWORD:
        try:
            msg = MIMEMultipart()
            msg["From"]    = MAIL_FROM or MAIL_USERNAME
            msg["To"]      = "shotatsuruda0819@gmail.com"
            msg["Subject"] = f"【গগुলाবো お問い合わせ】{name}様より"
            body = (
                f"お名前　　: {name}\n"
                f"メール　　: {email}\n\n"
                f"お問い合わせ内容:\n{message}"
            )
            msg.attach(MIMEText(body, "plain", "utf-8"))
            with smtplib.SMTP(MAIL_SMTP_HOST, MAIL_SMTP_PORT) as server:
                server.starttls()
                server.login(MAIL_USERNAME, MAIL_PASSWORD)
                server.sendmail(msg["From"], "shotatsuruda0819@gmail.com", msg.as_string())
        except Exception:
            pass  # メール送信失敗しても受付完了として返す

    return jsonify({"success": True})


# ----- 顧客向けアンケート（ログイン不要） -----

@app.route("/shop/<slug>")
def survey(slug):
    """
    顧客がQRコードを読み取った際に最初に表示されるアンケートページ。
    店舗情報とクーポン設定を取得してテンプレートに渡す。
    """
    conn = get_db()
    shop = conn.execute("SELECT * FROM shops WHERE slug = ?", (slug,)).fetchone()
    if not shop:
        conn.close()
        return "店舗が見つかりません", 404

    # is_active=1（有効）のクーポンのみ取得
    coupon = conn.execute(
        "SELECT * FROM coupons WHERE shop_id = ? AND is_active = 1", (shop["id"],)
    ).fetchone()
    conn.close()

    return render_template(
        "survey.html",
        shop=dict(shop),
        coupon=dict(coupon) if coupon else None,
    )


@app.route("/shop/<slug>/feedback", methods=["POST"])
def submit_feedback(slug):
    """
    お客様のご意見をDBに保存し、オーナーに通知メールを送る（AJAX用）。
    """
    conn = get_db()
    shop = conn.execute(
        "SELECT s.id, s.name, u.email AS owner_email, u.notify_enabled "
        "FROM shops s LEFT JOIN users u ON u.id = s.user_id "
        "WHERE s.slug = ?",
        (slug,),
    ).fetchone()
    conn.close()
    if not shop:
        return jsonify({"success": False, "error": "店舗が見つかりません"}), 404

    data = request.get_json()
    rating  = data.get("rating")
    comment = (data.get("comment") or "").strip()
    submitted_at = datetime.now().strftime("%Y年%m月%d日 %H:%M")

    conn = get_db()
    conn.execute(
        "INSERT INTO feedbacks (shop_id, rating, comment) VALUES (?, ?, ?)",
        (shop["id"], rating, comment),
    )
    conn.commit()
    conn.close()

    # オーナーへ通知メールを送信（notify_enabled が True の場合のみ）
    if shop["owner_email"] and shop["notify_enabled"]:
        try:
            send_feedback_notification(
                to_email=shop["owner_email"],
                shop_name=shop["name"],
                rating=rating,
                comment=comment,
                submitted_at=submitted_at,
            )
        except Exception:
            pass

    return jsonify({"success": True})


@app.route("/shop/<slug>/coupon", methods=["POST"])
def request_coupon(slug):
    """
    お客様がクーポンをリクエストする。
    クーポンコードを生成してメールで送信し、送信履歴をDBに保存する（AJAX用）。
    """
    data = request.get_json()
    email = (data.get("email") or "").strip()

    # メールアドレスの簡易バリデーション
    if not email or "@" not in email:
        return jsonify({"success": False, "error": "メールアドレスが正しくありません"})

    conn = get_db()
    shop = conn.execute("SELECT * FROM shops WHERE slug = ?", (slug,)).fetchone()
    if not shop:
        conn.close()
        return jsonify({"success": False, "error": "店舗が見つかりません"})
    shop_id = shop["id"]
    coupon = conn.execute(
        "SELECT * FROM coupons WHERE shop_id = ? AND is_active = 1", (shop_id,)
    ).fetchone()
    conn.close()

    if not coupon:
        return jsonify({"success": False, "error": "クーポンが設定されていません"})

    # クーポンコードを生成
    coupon_code = _generate_coupon_code()

    # 有効期限を計算（今日 + valid_days 日後）
    expires_at = (date.today() + timedelta(days=coupon["valid_days"])).strftime(
        "%Y年%m月%d日"
    )

    # クーポンをメールで送信
    success, message = send_coupon_email(
        to_email=email,
        shop_name=shop["name"],
        coupon_name=coupon["coupon_name"],
        discount_text=coupon["discount_text"],
        coupon_code=coupon_code,
        expires_at=expires_at,
    )

    if not success:
        return jsonify({"success": False, "error": f"メール送信に失敗しました: {message}"})

    # 送信履歴をDBに保存
    conn = get_db()
    conn.execute(
        """
        INSERT INTO coupon_deliveries
            (shop_id, email, coupon_code, coupon_name, discount_text, expires_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            shop_id,
            email,
            coupon_code,
            coupon["coupon_name"],
            coupon["discount_text"],
            expires_at,
        ),
    )
    conn.commit()
    conn.close()

    return jsonify(
        {
            "success": True,
            "coupon_code": coupon_code,
            "coupon_name": coupon["coupon_name"],
            "discount_text": coupon["discount_text"],
            "expires_at": expires_at,
        }
    )


# ----- 管理画面: QRコード生成 -----

@app.route("/manual")
@login_required
def manual():
    return render_template("manual.html")


@app.route("/settings", methods=["GET", "POST"])
@login_required
def settings():
    """通知設定ページ"""
    conn = get_db()
    if request.method == "POST":
        notify_enabled = 1 if request.form.get("notify_enabled") else 0
        conn.execute(
            "UPDATE users SET notify_enabled = ? WHERE id = ?",
            (notify_enabled, current_user.id),
        )
        conn.commit()
        conn.close()
        flash("設定を保存しました。", "success")
        return redirect(url_for("settings"))

    user = conn.execute(
        "SELECT notify_enabled, email FROM users WHERE id = ?", (current_user.id,)
    ).fetchone()
    conn.close()
    notify_enabled = bool(user["notify_enabled"]) if user else True
    current_email = user["email"] if user else ""
    return render_template("settings.html", notify_enabled=notify_enabled, current_email=current_email)


@app.route("/settings/email", methods=["POST"])
@login_required
def settings_email():
    """メールアドレス変更処理"""
    new_email = request.form.get("new_email", "").strip()
    confirm_password = request.form.get("confirm_password", "")

    if not new_email or not confirm_password:
        flash("新しいメールアドレスとパスワードを入力してください。", "error")
        return redirect(url_for("settings"))

    conn = get_db()
    user = conn.execute(
        "SELECT email, password_hash FROM users WHERE id = ?", (current_user.id,)
    ).fetchone()

    if not user or not bcrypt.checkpw(confirm_password.encode(), user["password_hash"].encode()):
        conn.close()
        flash("パスワードが正しくありません。", "error")
        return redirect(url_for("settings"))

    if new_email == user["email"]:
        conn.close()
        flash("新しいメールアドレスが現在と同じです。", "error")
        return redirect(url_for("settings"))

    existing = conn.execute(
        "SELECT id FROM users WHERE email = ? AND id != ?", (new_email, current_user.id)
    ).fetchone()
    if existing:
        conn.close()
        flash("そのメールアドレスはすでに使用されています。", "error")
        return redirect(url_for("settings"))

    conn.execute(
        "UPDATE users SET email = ? WHERE id = ?", (new_email, current_user.id)
    )
    conn.commit()
    conn.close()
    flash("メールアドレスを変更しました。", "success")
    return redirect(url_for("settings"))


@app.route("/qr")
@login_required
def qr_form():
    return render_template("qr.html", shop_name=SHOP_NAME)


@app.route("/qr/shops", methods=["GET"])
@login_required
def get_shops():
    """ログイン中ユーザーの店舗一覧をJSON形式で返す"""
    conn = get_db()
    shops = conn.execute(
        "SELECT id, name, review_url, slug, business_type, created_at FROM shops WHERE user_id = ? ORDER BY created_at DESC",
        (current_user.id,),
    ).fetchall()
    conn.close()
    return jsonify([dict(s) for s in shops])


@app.route("/qr/shops", methods=["POST"])
@login_required
def add_shop():
    """店舗を追加する（ログインユーザーに紐づける）"""
    conn = get_db()
    existing = conn.execute(
        "SELECT COUNT(*) as cnt FROM shops WHERE user_id = ?", (current_user.id,)
    ).fetchone()
    conn.close()
    if existing and existing["cnt"] >= 1:
        return jsonify({"error": "店舗は1アカウントにつき1店舗までご登録いただけます。"}), 400

    data = request.get_json()
    name          = (data.get("name")          or "").strip()
    review_url    = (data.get("review_url")    or "").strip()
    slug_input    = (data.get("slug")          or "").strip()
    business_type = (data.get("business_type") or "").strip()
    if not name or not review_url:
        return jsonify({"error": "name と review_url は必須です"}), 400

    conn = get_db()
    if slug_input:
        slug = re.sub(r"[^a-z0-9\-_]", "-", slug_input.lower())
        slug = re.sub(r"-{2,}", "-", slug).strip("-") or None
    else:
        slug = None

    cursor = conn.execute(
        "INSERT INTO shops (name, review_url, slug, user_id, business_type) VALUES (?, ?, ?, ?, ?)",
        (name, review_url, slug, current_user.id, business_type),
    )
    shop_id = cursor.lastrowid
    if not slug:
        conn.execute("UPDATE shops SET slug = ? WHERE id = ?", (f"shop-{shop_id}", shop_id))
    conn.commit()
    conn.close()
    return jsonify({"success": True}), 201


@app.route("/qr/shops/<int:shop_id>", methods=["DELETE"])
@login_required
def delete_shop(shop_id):
    """店舗と紐づくクーポン設定を削除する（所有者のみ）"""
    conn = get_db()
    shop = conn.execute(
        "SELECT id FROM shops WHERE id = ? AND user_id = ?", (shop_id, current_user.id)
    ).fetchone()
    if not shop:
        conn.close()
        return jsonify({"error": "店舗が見つかりません"}), 404
    conn.execute("DELETE FROM shops WHERE id = ?", (shop_id,))
    conn.execute("DELETE FROM coupons WHERE shop_id = ?", (shop_id,))
    conn.commit()
    conn.close()
    return jsonify({"success": True})


@app.route("/qr/shops/<int:shop_id>/coupon", methods=["GET"])
@login_required
def get_coupon_settings(shop_id):
    """店舗のクーポン設定をJSON形式で返す（所有者のみ）"""
    conn = get_db()
    shop = conn.execute(
        "SELECT id FROM shops WHERE id = ? AND user_id = ?", (shop_id, current_user.id)
    ).fetchone()
    if not shop:
        conn.close()
        return jsonify({"error": "店舗が見つかりません"}), 404

    coupon = conn.execute(
        "SELECT * FROM coupons WHERE shop_id = ?", (shop_id,)
    ).fetchone()
    conn.close()

    if coupon:
        return jsonify(dict(coupon))

    return jsonify(
        {
            "shop_id": shop_id,
            "coupon_name": "",
            "discount_text": "",
            "valid_days": 30,
            "is_active": 0,
        }
    )


@app.route("/qr/shops/<int:shop_id>/coupon", methods=["POST"])
@login_required
def save_coupon_settings(shop_id):
    """店舗のクーポン設定を保存する（所有者のみ）"""
    conn = get_db()
    shop = conn.execute(
        "SELECT id FROM shops WHERE id = ? AND user_id = ?", (shop_id, current_user.id)
    ).fetchone()
    if not shop:
        conn.close()
        return jsonify({"error": "店舗が見つかりません"}), 404

    data = request.get_json()
    coupon_name = (data.get("coupon_name") or "").strip()
    discount_text = (data.get("discount_text") or "").strip()
    valid_days = int(data.get("valid_days") or 30)
    is_active = 1 if data.get("is_active") else 0

    if is_active and (not coupon_name or not discount_text):
        conn.close()
        return jsonify({"error": "クーポンを有効にするにはクーポン名と割引内容が必須です"}), 400

    existing = conn.execute(
        "SELECT id FROM coupons WHERE shop_id = ?", (shop_id,)
    ).fetchone()

    if existing:
        conn.execute(
            """
            UPDATE coupons
            SET coupon_name=?, discount_text=?, valid_days=?, is_active=?,
                updated_at=CURRENT_TIMESTAMP
            WHERE shop_id=?
            """,
            (coupon_name, discount_text, valid_days, is_active, shop_id),
        )
    else:
        conn.execute(
            """
            INSERT INTO coupons (shop_id, coupon_name, discount_text, valid_days, is_active)
            VALUES (?, ?, ?, ?, ?)
            """,
            (shop_id, coupon_name, discount_text, valid_days, is_active),
        )

    conn.commit()
    conn.close()
    return jsonify({"success": True})


@app.route("/qr/generate", methods=["POST"])
@login_required
def qr_generate():
    """QRコード画像を生成してBase64エンコードしたPNGをJSONで返す"""
    url = (request.form.get("url") or "").strip()
    if not url:
        return jsonify({"success": False, "error": "URLが必要です"})

    try:
        img = build_qr_image(
            url=url,
            fg_color=request.form.get("fg_color", "#000000"),
            fg_color2=request.form.get("fg_color2", "#000000"),
            gradient_dir=request.form.get("gradient_dir", "radial"),
            bg_color=request.form.get("bg_color", "#ffffff"),
            corner_style=request.form.get("corner_style", "square"),
            size=1024,
        )
        buf = io.BytesIO()
        img.save(buf, format="PNG", optimize=True)
        buf.seek(0)
        encoded = base64.b64encode(buf.getvalue()).decode("utf-8")
        return jsonify({"success": True, "image": encoded})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)})


# ----- 管理画面: AI返答生成 -----

@app.route("/review")
@login_required
def review_form():
    return render_template("review.html", shop_name=SHOP_NAME)


@app.route("/review/generate", methods=["POST"])
@login_required
def generate_response():
    """AI返答文生成処理（Ajax用JSONレスポンス）"""
    review_text   = (request.form.get("review_text")   or "").strip()
    business_type = (request.form.get("business_type") or "").strip()
    if not review_text:
        return jsonify({"success": False, "error": "レビュー内容を入力してください。"})
    try:
        response_text = generate_review_response(review_text, business_type)
        return jsonify({"success": True, "response": response_text})
    except Exception as e:
        return jsonify({"success": False, "error": f"AI返答生成に失敗しました: {str(e)}"})


# ----- 管理者: ユーザー管理 -----

@app.route("/admin")
@admin_required
def admin_users():
    """管理者向けユーザー管理画面"""
    conn = get_db()
    rows = conn.execute(
        "SELECT id, email, name, is_admin, created_at, plan, plan_expires_at FROM users ORDER BY id"
    ).fetchall()
    conn.close()
    users = []
    for row in rows:
        d = dict(row)
        ca = d.get("created_at")
        if ca and hasattr(ca, "strftime"):
            d["created_at"] = ca.strftime("%Y-%m-%d %H:%M:%S")
        if not d.get("plan"):
            d["plan"] = "月額プラン"
        users.append(d)
    return render_template("admin.html", users=users)


@app.route("/admin/users", methods=["POST"])
@admin_required
def admin_create_user():
    """新規ユーザーを作成する"""
    email = (request.form.get("email") or "").strip().lower()
    name = (request.form.get("name") or "").strip()
    password = request.form.get("password") or ""
    is_admin = 1 if request.form.get("is_admin") else 0

    if not email or not name or not password:
        flash("メールアドレス・名前・パスワードはすべて必須です。", "danger")
        return redirect(url_for("admin_users"))

    if len(password) < 6:
        flash("パスワードは6文字以上で入力してください。", "danger")
        return redirect(url_for("admin_users"))

    password_hash = bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()

    try:
        conn = get_db()
        conn.execute(
            "INSERT INTO users (email, password_hash, name, is_admin) VALUES (?, ?, ?, ?)",
            (email, password_hash, name, is_admin),
        )
        conn.commit()
        conn.close()
        flash(f"ユーザー「{name}」を作成しました。", "success")
    except DBIntegrityError:
        flash("そのメールアドレスはすでに登録されています。", "danger")

    return redirect(url_for("admin_users"))


@app.route("/admin/users/<int:user_id>", methods=["POST"])
@admin_required
def admin_delete_user(user_id):
    """ユーザーを削除する（自分自身の削除は禁止）"""
    if user_id == current_user.id:
        flash("自分自身のアカウントは削除できません。", "danger")
        return redirect(url_for("admin_users"))

    conn = get_db()
    conn.execute("DELETE FROM users WHERE id = ?", (user_id,))
    conn.commit()
    conn.close()
    flash("ユーザーを削除しました。", "success")
    return redirect(url_for("admin_users"))


@app.route("/admin/users/<int:user_id>/reset-password", methods=["POST"])
@admin_required
def admin_reset_password(user_id):
    """管理者がユーザーのパスワードをリセットする"""
    new_password = request.form.get("new_password") or ""

    if len(new_password) < 6:
        flash("パスワードは6文字以上で入力してください。", "danger")
        return redirect(url_for("admin_users"))

    password_hash = bcrypt.hashpw(new_password.encode(), bcrypt.gensalt()).decode()
    conn = get_db()
    conn.execute(
        "UPDATE users SET password_hash = ? WHERE id = ?",
        (password_hash, user_id),
    )
    conn.commit()
    conn.close()
    flash("パスワードを変更しました。", "success")
    return redirect(url_for("admin_users"))


@app.route("/notify_google_click/<slug>", methods=["POST"])
def notify_google_click(slug):
    """お客様がGoogleレビューボタンをクリックしたときにオーナーへ通知する（AJAX用）"""
    conn = get_db()
    shop = conn.execute(
        "SELECT s.name, u.email AS owner_email, u.notify_enabled "
        "FROM shops s LEFT JOIN users u ON u.id = s.user_id "
        "WHERE s.slug = ?",
        (slug,),
    ).fetchone()
    conn.close()
    if not shop:
        return jsonify({"success": False}), 404

    if shop["owner_email"] and shop["notify_enabled"]:
        try:
            send_google_click_notification(
                to_email=shop["owner_email"],
                shop_name=shop["name"],
                clicked_at=datetime.now().strftime("%Y年%m月%d日 %H:%M"),
            )
        except Exception:
            pass

    return jsonify({"success": True})


@app.route("/admin/users/<int:user_id>/plan", methods=["POST"])
@admin_required
def admin_update_plan(user_id):
    """管理者がユーザーの契約プランと有効期限を更新する"""
    plan = (request.form.get("plan") or "月額プラン").strip()
    plan_expires_at = (request.form.get("plan_expires_at") or "").strip() or None
    conn = get_db()
    conn.execute(
        "UPDATE users SET plan = ?, plan_expires_at = ? WHERE id = ?",
        (plan, plan_expires_at, user_id),
    )
    conn.commit()
    conn.close()
    flash("プラン情報を更新しました。", "success")
    return redirect(url_for("admin_users"))


if __name__ == "__main__":
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 5000)), debug=False)
