"""
マッサージ店向け Googleレビュー管理システム
Flask + OpenRouter API + qrcode + Pillow を使用
"""

import base64
import hashlib
import hmac
import io
import os
import re
import secrets
import csv
import smtplib
import sqlite3
import string
import zipfile
from datetime import date, datetime, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from functools import wraps

import bcrypt
import stripe
from dotenv import load_dotenv
from flask import (
    Flask, abort, flash, jsonify, redirect, render_template,
    request, url_for, send_file
)
from flask_login import (
    LoginManager, UserMixin, current_user, login_required,
    login_user, logout_user,
)
from openai import OpenAI
from PIL import Image, ImageDraw, ImageFilter
import qrcode
from qrcode.image.styledpil import StyledPilImage
from qrcode.image.styles.colormasks import (
    HorizontalGradiantColorMask,
    RadialGradiantColorMask,
    SolidFillColorMask,
    VerticalGradiantColorMask,
)

import pykakasi

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

if os.environ.get("FLASK_ENV") == "production":
    app.config.update(
        SESSION_COOKIE_SECURE=True,
        REMEMBER_COOKIE_SECURE=True,
        SESSION_COOKIE_SAMESITE="Lax",
    )

# メール送信設定（クーポン送信に使用）
MAIL_SMTP_HOST = os.environ.get("MAIL_SMTP_HOST", "smtp.gmail.com")
MAIL_SMTP_PORT = int(os.environ.get("MAIL_SMTP_PORT", "587"))
MAIL_USERNAME = os.environ.get("MAIL_USERNAME", "")
MAIL_PASSWORD = os.environ.get("MAIL_PASSWORD", "")
MAIL_FROM = os.environ.get("MAIL_FROM", MAIL_USERNAME)

# ===== LINE 設定 =====
LINE_CHANNEL_SECRET      = os.environ.get("LINE_CHANNEL_SECRET", "")
LINE_CHANNEL_ACCESS_TOKEN = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN", "")
LINE_ADD_FRIEND_URL      = os.environ.get("LINE_ADD_FRIEND_URL", "")

# ===== Stripe 設定 =====
STRIPE_SECRET_KEY    = os.environ.get("STRIPE_SECRET_KEY", "")
STRIPE_PUBLIC_KEY    = os.environ.get("STRIPE_PUBLIC_KEY", "pk_live_51T5hfRI0UveP0nntiIpAIEgEV0Y7l1IKDrRogRQj4jbvypaHpoxPoI6ouFxLG10po9LHh3STXiSESu5sSqwtoB4U0055zkbWIL")
STRIPE_PRICE_ID         = os.environ.get("STRIPE_PRICE_ID", "price_1T67E9I0UveP0nntM4yqbZ9q")
STRIPE_PRICE_ID_YEARLY  = os.environ.get("STRIPE_PRICE_ID_YEARLY", "price_1T67bmI0UveP0nntQVL8Ieen")
STRIPE_WEBHOOK_SECRET = os.environ.get("STRIPE_WEBHOOK_SECRET", "")
if STRIPE_SECRET_KEY:
    stripe.api_key = STRIPE_SECRET_KEY

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
    def __init__(self, id, email, name, is_admin, plan=None, trial_ends_at=None):
        self.id = id
        self.email = email
        self.name = name
        self.is_admin = bool(is_admin)
        self.plan = plan
        self.trial_ends_at = trial_ends_at

    @property
    def is_paid(self):
        """管理者 or 有料プラン契約中 or トライアル期間中ならアクセス可能"""
        if self.is_admin or bool(self.plan):
            return True
        if self.trial_ends_at:
            try:
                trial_end = (
                    self.trial_ends_at if hasattr(self.trial_ends_at, "date")
                    else datetime.fromisoformat(str(self.trial_ends_at))
                )
                return trial_end > datetime.now()
            except Exception:
                return False
        return False

    @property
    def trial_days_remaining(self):
        """トライアル残り日数。有料プラン契約中・管理者・トライアル外は None を返す"""
        if self.is_admin or bool(self.plan) or not self.trial_ends_at:
            return None
        try:
            trial_end = (
                self.trial_ends_at if hasattr(self.trial_ends_at, "date")
                else datetime.fromisoformat(str(self.trial_ends_at))
            )
            remaining = (trial_end.date() - date.today()).days
            return remaining if remaining >= 0 else None
        except Exception:
            return None


@login_manager.user_loader
def load_user(user_id):
    conn = get_db()
    row = conn.execute(
        "SELECT id, email, name, is_admin, plan, trial_ends_at FROM users WHERE id = ?",
        (user_id,),
    ).fetchone()
    conn.close()
    if row is None:
        return None
    return User(
        row["id"], row["email"], row["name"], row["is_admin"],
        row["plan"], row["trial_ends_at"],
    )


def admin_required(f):
    """管理者のみアクセス可能なルートに付けるデコレーター"""
    @wraps(f)
    @login_required
    def decorated(*args, **kwargs):
        if not current_user.is_admin:
            abort(403)
        return f(*args, **kwargs)
    return decorated


def payment_required(f):
    """ログイン済み かつ 支払い済みのみアクセス可能なデコレーター"""
    @wraps(f)
    @login_required
    def decorated(*args, **kwargs):
        if not current_user.is_paid:
            if request.is_json:
                return jsonify({"error": "ご利用にはトライアルまたは有料プランへの登録が必要です。"}), 402
            return redirect(url_for("subscribe"))
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
            place_id   TEXT UNIQUE,
            status     TEXT NOT NULL DEFAULT 'trial',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """
    )

    # 既存テーブルへの slug / user_id / business_type カラム追加（マイグレーション）
    if DB_TYPE == "postgresql":
        conn.execute("ALTER TABLE shops ADD COLUMN IF NOT EXISTS slug TEXT")
        conn.execute("ALTER TABLE shops ADD COLUMN IF NOT EXISTS user_id INTEGER REFERENCES users(id)")
        conn.execute("ALTER TABLE shops ADD COLUMN IF NOT EXISTS business_type TEXT DEFAULT ''")
        conn.execute("ALTER TABLE shops ADD COLUMN IF NOT EXISTS place_id TEXT")
        conn.execute("ALTER TABLE shops ADD COLUMN IF NOT EXISTS status TEXT NOT NULL DEFAULT 'trial'")
        conn.execute("ALTER TABLE shops ADD COLUMN IF NOT EXISTS unique_id TEXT")
        conn.execute("ALTER TABLE shops ADD COLUMN IF NOT EXISTS address TEXT")
        conn.execute("ALTER TABLE shops ADD COLUMN IF NOT EXISTS line_user_id TEXT")
        conn.execute("ALTER TABLE shops ADD COLUMN IF NOT EXISTS zero_review_weeks INTEGER DEFAULT 0")
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
        try:
            conn.execute("ALTER TABLE shops ADD COLUMN place_id TEXT")
        except Exception:
            pass
        try:
            conn.execute("ALTER TABLE shops ADD COLUMN status TEXT NOT NULL DEFAULT 'trial'")
        except Exception:
            pass
        try:
            conn.execute("ALTER TABLE shops ADD COLUMN unique_id TEXT")
        except Exception:
            pass
        try:
            conn.execute("ALTER TABLE shops ADD COLUMN address TEXT")
        except Exception:
            pass
        try:
            conn.execute("ALTER TABLE shops ADD COLUMN place_id TEXT")
        except Exception:
            pass  # カラムが既に存在する場合はスキップ
        try:
            conn.execute("ALTER TABLE shops ADD COLUMN status TEXT NOT NULL DEFAULT 'trial'")
        except Exception:
            pass  # カラムが既に存在する場合はスキップ
        try:
            conn.execute("ALTER TABLE shops ADD COLUMN line_user_id TEXT")
        except Exception:
            pass
        try:
            conn.execute("ALTER TABLE shops ADD COLUMN zero_review_weeks INTEGER DEFAULT 0")
        except Exception:
            pass

    # users テーブルへの各カラム追加（マイグレーション）
    if DB_TYPE == "postgresql":
        conn.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS plan TEXT DEFAULT '月額プラン'")
        conn.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS plan_expires_at TEXT")
        conn.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS notify_enabled INTEGER NOT NULL DEFAULT 1")
        conn.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS stripe_customer_id TEXT")
        conn.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS trial_ends_at TIMESTAMP")
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
        try:
            conn.execute("ALTER TABLE users ADD COLUMN stripe_customer_id TEXT")
        except Exception:
            pass
        try:
            conn.execute("ALTER TABLE users ADD COLUMN trial_ends_at TEXT")
        except Exception:
            pass

    # slug のユニークインデックス（既にあればスキップ）
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_shops_slug ON shops(slug)")
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_shops_place_id ON shops(place_id)")

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

    # お客様のご意見テーブル（★1〜5のフィードバック）
    conn.execute(
        f"""
        CREATE TABLE IF NOT EXISTS feedbacks (
            id           {pk},
            shop_id      INTEGER NOT NULL,
            rating       INTEGER NOT NULL,
            rating2      INTEGER,
            rating3      INTEGER,
            rating4      INTEGER,
            rating5      INTEGER,
            comment      TEXT,
            submitted_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """
    )

    # feedbacks テーブルへの連携カラム追加（マイグレーション）
    if DB_TYPE == "postgresql":
        conn.execute("ALTER TABLE feedbacks ADD COLUMN IF NOT EXISTS is_featured INTEGER NOT NULL DEFAULT 0")
        conn.execute("ALTER TABLE feedbacks ADD COLUMN IF NOT EXISTS guest_type TEXT")
        conn.execute("ALTER TABLE feedbacks ADD COLUMN IF NOT EXISTS positive_points TEXT")
        conn.execute("ALTER TABLE feedbacks ADD COLUMN IF NOT EXISTS negative_points TEXT")
        conn.execute("ALTER TABLE feedbacks ADD COLUMN IF NOT EXISTS ai_draft TEXT")
        conn.execute("ALTER TABLE feedbacks ADD COLUMN IF NOT EXISTS rating2 INTEGER")
        conn.execute("ALTER TABLE feedbacks ADD COLUMN IF NOT EXISTS rating3 INTEGER")
        conn.execute("ALTER TABLE feedbacks ADD COLUMN IF NOT EXISTS rating4 INTEGER")
        conn.execute("ALTER TABLE feedbacks ADD COLUMN IF NOT EXISTS rating5 INTEGER")
    else:
        try:
            conn.execute("ALTER TABLE feedbacks ADD COLUMN is_featured INTEGER NOT NULL DEFAULT 0")
        except Exception:
            pass
        try:
            conn.execute("ALTER TABLE feedbacks ADD COLUMN guest_type TEXT")
            conn.execute("ALTER TABLE feedbacks ADD COLUMN positive_points TEXT")
            conn.execute("ALTER TABLE feedbacks ADD COLUMN negative_points TEXT")
            conn.execute("ALTER TABLE feedbacks ADD COLUMN ai_draft TEXT")
        except Exception:
            pass
        try:
            conn.execute("ALTER TABLE feedbacks ADD COLUMN rating2 INTEGER")
            conn.execute("ALTER TABLE feedbacks ADD COLUMN rating3 INTEGER")
            conn.execute("ALTER TABLE feedbacks ADD COLUMN rating4 INTEGER")
            conn.execute("ALTER TABLE feedbacks ADD COLUMN rating5 INTEGER")
        except Exception:
            pass

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

    # LINE友だち追加時の一時保存テーブル
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS line_pending (
            line_user_id TEXT PRIMARY KEY,
            created_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """
    )

    # 取得済み口コミ管理テーブル（重複送信防止）
    conn.execute(
        f"""
        CREATE TABLE IF NOT EXISTS fetched_reviews (
            id          {pk},
            shop_id     INTEGER NOT NULL,
            review_id   TEXT NOT NULL UNIQUE,
            author_name TEXT,
            rating      INTEGER,
            text        TEXT,
            review_time TEXT,
            reply_draft TEXT,
            fetched_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (shop_id) REFERENCES shops(id)
        )
        """
    )

    # 週次サマリー履歴テーブル
    conn.execute(
        f"""
        CREATE TABLE IF NOT EXISTS weekly_summaries (
            id           {pk},
            shop_id      INTEGER NOT NULL,
            week_start   DATE NOT NULL,
            review_count INTEGER DEFAULT 0,
            avg_rating   REAL,
            sent_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (shop_id) REFERENCES shops(id)
        )
        """
    )

    # デモ用店舗を作成（なければ）
    conn.execute("""
        INSERT INTO shops (user_id, name, slug, review_url)
        SELECT 1, 'デモ店舗', 'demo', 'https://google.com'
        WHERE NOT EXISTS (SELECT 1 FROM shops WHERE slug = 'demo')
    """)

    # デモ用フィードバックを挿入（なければ）
    demo_feedbacks = [
        (4, 'スタッフの方がとても親切で、また来たいと思いました。'),
        (5, '施術が丁寧で、体がとても楽になりました。'),
        (5, '雰囲気も良く、リラックスできました。次回も予約したいです。'),
        (4, '料金もリーズナブルで大満足です。'),
        (5, '初めての利用でしたが、丁寧に説明してくれて安心できました。'),
    ]
    for rating, comment in demo_feedbacks:
        conn.execute("""
            INSERT INTO feedbacks (shop_id, rating, comment, is_featured)
            SELECT s.id, ?, ?, 1
            FROM shops s
            WHERE s.slug = 'demo'
            AND NOT EXISTS (
                SELECT 1 FROM feedbacks f2
                JOIN shops s2 ON s2.id = f2.shop_id
                WHERE s2.slug = 'demo' AND f2.comment = ?
            )
        """, (rating, comment, comment))

    conn.commit()
    conn.close()


DB_INIT_OK = False
DB_INIT_ERR = None
with app.app_context():
    try:
        init_db()
        DB_INIT_OK = True
    except Exception as e:
        DB_INIT_ERR = str(e)
    if os.environ.get("AUTO_CREATE_ADMIN"):
        try:
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
        except Exception:
            pass
        finally:
            try:
                _conn.close()
            except Exception:
                pass


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
  有効期限: {expires_at}
━━━━━━━━━━━━━━━━━━━━

このメールを店員にご提示ください。
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
        <p style="color:#fff;margin:0;font-size:26px;font-weight:800;">{discount_text}</p>

        <p style="color:rgba(255,255,255,0.75);margin:14px 0 0;font-size:13px;">有効期限: {expires_at}</p>
      </div>

      <p style="color:#6b7280;font-size:14px;line-height:1.7;">
        このメールを店員にご提示ください。<br>
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


def predict_business_type_from_name(shop_name: str) -> str:
    """店舗名から業種を予測する（マッサージ、エステ、美容室、整体院、飲食店など）"""
    client = OpenAI(
        api_key=OPENROUTER_API_KEY,
        base_url="https://openrouter.ai/api/v1",
    )
    prompt = f"""以下の店舗名から、最も当てはまる業種を以下の選択肢から1つだけ選んで出力してください。
選択肢: マッサージ, 整体, 整骨院, エステ, 歯科医院, 美容室, ネイルサロン, 脱毛サロン, 飲食店, カフェ, その他
店舗名だけで判断が難しい場合は「その他」と出力してください。出力は選択した業種名（1単語）のみとし、説明などは一切不要です。

店舗名: {shop_name}"""

    try:
        response = client.chat.completions.create(
            model="google/gemini-2.5-flash",
            max_tokens=20,
            messages=[{"role": "user", "content": prompt}],
        )
        result = response.choices[0].message.content.strip()
        # カッコなどがついている場合をケア
        result = result.replace("「", "").replace("」", "").strip()
        if not result or len(result) > 20: 
            return "その他"
        return result
    except Exception as e:
        print(f"Failed to predict business type for {shop_name}: {e}")
        return "その他"


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
                trial_ends_at = (datetime.now() + timedelta(days=30)).strftime("%Y-%m-%d %H:%M:%S")
                conn = get_db()
                cursor = conn.execute(
                    "INSERT INTO users (email, password_hash, name, is_admin, plan, trial_ends_at)"
                    " VALUES (?, ?, ?, 0, NULL, ?)",
                    (form["email"], password_hash, form["name"], trial_ends_at),
                )
                user_id = cursor.lastrowid
                conn.commit()
                conn.close()
                user = User(user_id, form["email"], form["name"], False,
                            plan=None, trial_ends_at=trial_ends_at)
                login_user(user)
                return redirect(url_for("index"))
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
        if not current_user.is_paid:
            return redirect(url_for("subscribe"))
        conn = get_db()
        feedbacks = conn.execute(
            """
            SELECT f.id, f.rating, f.comment, f.submitted_at, f.is_featured, s.name AS shop_name
            FROM feedbacks f
            JOIN shops s ON s.id = f.shop_id
            WHERE s.user_id = ? AND f.rating <= 3
            ORDER BY f.submitted_at DESC
            LIMIT 20
            """,
            (current_user.id,),
        ).fetchall()

        # 星評価の分布（1〜5星それぞれの件数）
        rating_rows = conn.execute(
            """
            SELECT rating, COUNT(*) as count
            FROM feedbacks f
            JOIN shops s ON s.id = f.shop_id
            WHERE s.user_id = ?
            GROUP BY rating
            """,
            (current_user.id,),
        ).fetchall()
        rating_dist = {row["rating"]: row["count"] for row in rating_rows}

        # 過去30日間の日別件数推移
        if DB_TYPE == "postgresql":
            daily_rows = conn.execute(
                """
                SELECT submitted_at::date as date, COUNT(*) as count
                FROM feedbacks f
                JOIN shops s ON s.id = f.shop_id
                WHERE s.user_id = ?
                AND submitted_at >= CURRENT_DATE - INTERVAL '30 days'
                GROUP BY submitted_at::date
                ORDER BY date ASC
                """,
                (current_user.id,),
            ).fetchall()
        else:
            daily_rows = conn.execute(
                """
                SELECT date(submitted_at) as date, COUNT(*) as count
                FROM feedbacks f
                JOIN shops s ON s.id = f.shop_id
                WHERE s.user_id = ?
                AND date(submitted_at) >= date('now', '-30 days')
                GROUP BY date(submitted_at)
                ORDER BY date ASC
                """,
                (current_user.id,),
            ).fetchall()
        daily_trend = [{"date": row["date"], "count": row["count"]} for row in daily_rows]

        # クーポン送信総数
        coupon_row = conn.execute(
            """
            SELECT COUNT(*) as total
            FROM coupon_deliveries cd
            JOIN shops s ON s.id = cd.shop_id
            WHERE s.user_id = ?
            """,
            (current_user.id,),
        ).fetchone()
        coupon_total = coupon_row["total"] if coupon_row else 0

        # 今月の口コミ投稿数
        if DB_TYPE == "postgresql":
            monthly_count = conn.execute(
                """
                SELECT COUNT(*) as count
                FROM feedbacks f
                JOIN shops s ON s.id = f.shop_id
                WHERE s.user_id = ?
                AND submitted_at >= date_trunc('month', CURRENT_DATE)
                """,
                (current_user.id,),
            ).fetchone()["count"]
        else:
            monthly_count = conn.execute(
                """
                SELECT COUNT(*) as count
                FROM feedbacks f
                JOIN shops s ON s.id = f.shop_id
                WHERE s.user_id = ?
                AND date(submitted_at) >= date('now', 'start of month')
                """,
                (current_user.id,),
            ).fetchone()["count"]

        conn.close()
        feedbacks = [dict(fb) for fb in feedbacks]
        for fb in feedbacks:
            sa = fb.get("submitted_at")
            if sa and hasattr(sa, "strftime"):
                fb["submitted_at"] = sa.strftime("%Y-%m-%d %H:%M")
            elif sa and isinstance(sa, str):
                fb["submitted_at"] = sa[:16]
        return render_template(
            "index.html",
            feedbacks=feedbacks,
            trial_days_remaining=current_user.trial_days_remaining,
            rating_dist=rating_dist,
            daily_trend=daily_trend,
            coupon_total=coupon_total,
            monthly_count=monthly_count,
        )
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


# ===== 業種ごとのアンケート項目設定 =====
SURVEY_OPTIONS = {
    "default": {
        "q1": "総合的な満足度",
        "q2": "接客・スタッフの対応",
        "q3": "サービス・技術の質",
        "q4": "店内の雰囲気・清潔感",
        "q5": "また利用したいか",
        "q6": {"label": "来店理由", "choices": ["近所", "口コミ", "紹介", "SNS", "その他"]},
        "q7": {"label": "特によかった点", "choices": ["接客", "技術", "雰囲気", "価格", "清潔感"]},
    },
    "マッサージ": {
        "q1": "総合的な満足度",
        "q2": "接客・スタッフの対応",
        "q3": "施術・技術の質",
        "q4": "店内の雰囲気・静かさ",
        "q5": "また利用したいか",
        "q6": {"label": "来院理由", "choices": ["肩こり", "腰痛", "疲労回復", "頭痛", "その他"]},
        "q7": {"label": "特によかった点", "choices": ["技術", "接客", "雰囲気", "説明の丁寧さ", "価格"]},
    },
    "整骨院": {
        "q1": "総合的な満足度",
        "q2": "問診・説明の分かりやすさ",
        "q3": "施術の効果・技術",
        "q4": "院内の雰囲気・清潔感",
        "q5": "また通院したいか",
        "q6": {"label": "来院理由", "choices": ["肩こり", "腰痛", "疲労回復", "頭痛", "その他"]},
        "q7": {"label": "特によかった点", "choices": ["技術", "接客", "雰囲気", "説明の丁寧さ", "価格"]},
    },
    "エステ": {
        "q1": "総合的な満足度",
        "q2": "カウンセリング・提案の丁寧さ",
        "q3": "施術の効果・技術",
        "q4": "サロンの雰囲気・清潔感",
        "q5": "また利用したいか",
        "q6": {"label": "利用メニュー", "choices": ["フェイシャル", "ボディケア", "脱毛", "リラクゼーション", "その他"]},
        "q7": {"label": "特によかった点", "choices": ["効果", "接客", "雰囲気", "技術", "価格"]},
    },
    "歯科医院": {
        "q1": "総合的な満足度",
        "q2": "問診・説明の分かりやすさ",
        "q3": "施術・治療の丁寧さ",
        "q4": "院内の雰囲気・清潔感",
        "q5": "またこちらに来たいか",
        "q6": {"label": "来院理由", "choices": ["定期検診", "虫歯治療", "ホワイトニング", "矯正", "その他"]},
        "q7": {"label": "特によかった点", "choices": ["丁寧な説明", "痛みが少ない", "清潔感", "スタッフ対応", "待ち時間"]},
    },
    "整体": {
        "q1": "総合的な満足度",
        "q2": "問診・説明の分かりやすさ",
        "q3": "施術の効果・技術",
        "q4": "院内の雰囲気・清潔感",
        "q5": "また通院したいか",
        "q6": {"label": "来院理由", "choices": ["肩こり", "腰痛", "疲労回復", "頭痛", "その他"]},
        "q7": {"label": "特によかった点", "choices": ["技術", "接客", "雰囲気", "説明の丁寧さ", "価格"]},
    },
    "美容院": {
        "q1": "総合的な満足度",
        "q2": "接客・スタッフの対応",
        "q3": "施術・技術の質",
        "q4": "サロンの雰囲気・清潔感",
        "q5": "また来店したいか",
        "q6": {"label": "利用メニュー", "choices": ["カット", "カラー", "パーマ", "トリートメント", "その他"]},
        "q7": {"label": "特によかった点", "choices": ["技術", "接客", "仕上がり", "雰囲気", "価格"]},
    },
    "飲食店": {
        "q1": "総合的な満足度",
        "q2": "接客・スタッフの対応",
        "q3": "料理・ドリンクの味",
        "q4": "店内の雰囲気・清潔感",
        "q5": "また来店したいか",
        "q6": {"label": "来店理由", "choices": ["近所", "口コミ", "記念日", "仕事", "その他"]},
        "q7": {"label": "特によかった点", "choices": ["料理", "接客", "雰囲気", "価格", "清潔感"]},
    },
    "カフェ": {
        "q1": "総合的な満足度",
        "q2": "接客・スタッフの対応",
        "q3": "ドリンク・フードの味",
        "q4": "居心地の良さ・空間",
        "q5": "また来店したいか",
        "q6": {"label": "来店理由", "choices": ["近所", "口コミ", "作業", "休憩", "その他"]},
        "q7": {"label": "特によかった点", "choices": ["ドリンク", "接客", "雰囲気", "価格", "居心地"]},
    },
}
# "その他" も default を使う


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

    # 業種に応じてアンケート項目を取得
    shop_dict = dict(shop)
    b_type = shop_dict.get("business_type") or "default"
    opts = SURVEY_OPTIONS.get(b_type, SURVEY_OPTIONS["default"])

    return render_template(
        "survey.html",
        shop=shop_dict,
        coupon=dict(coupon) if coupon else None,
        survey_options=opts,
    )


@app.route("/shop/demo")
def shop_demo():
    if not request.args.get("demo"):
        return redirect(url_for("shop_demo", demo="1", business_type=request.args.get("business_type", "マッサージ")))
    b_type = request.args.get("business_type", "マッサージ")
    demo_names = {
        "マッサージ": "サンプルマッサージ店",
        "整体":     "サンプル整体院",
        "美容院":   "サンプル美容院",
        "歯科医院": "サンプル歯科医院",
        "エステ":   "サンプルエステサロン",
    }
    shop_dict = {
        "slug": "demo",
        "name": demo_names.get(b_type, f"サンプル{b_type}"),
        "review_url": "https://google.com",
        "business_type": b_type,
    }
    opts = SURVEY_OPTIONS.get(b_type, SURVEY_OPTIONS["default"])
    return render_template(
        "survey.html",
        shop=shop_dict,
        coupon=None,
        survey_options=opts
    )


@app.route("/shop/<slug>/feedback", methods=["POST"])
def submit_feedback(slug):
    """
    お客様のご意見をDBに保存し、AIでレビュー下書きを生成して返す（AJAX用）。
    """
    conn = get_db()
    shop = conn.execute(
        "SELECT s.id, s.name, s.business_type, u.email AS owner_email, u.notify_enabled "
        "FROM shops s LEFT JOIN users u ON u.id = s.user_id "
        "WHERE s.slug = ?",
        (slug,),
    ).fetchone()
    conn.close()
    if not shop:
        return jsonify({"success": False, "error": "店舗が見つかりません"}), 404

    data = request.get_json()
    rating  = data.get("rating")
    rating2 = data.get("rating2")
    rating3 = data.get("rating3")
    rating4 = data.get("rating4")
    rating5 = data.get("rating5")
    comment  = (data.get("comment")  or "").strip()
    answer6  = (data.get("answer6")  or "").strip()
    answer7  = (data.get("answer7")  or "").strip()
    
    submitted_at = datetime.now().strftime("%Y年%m月%d日 %H:%M")

    # 業種に応じてアンケート項目を取得
    shop_dict = dict(shop)
    b_type = shop_dict.get("business_type") or "default"
    opts = SURVEY_OPTIONS.get(b_type, SURVEY_OPTIONS["default"])

    # ===== AI（OpenRouter）によるレビュー下書きの生成 =====
    ai_draft = ""
    # Googleガイドラインに準拠し、すべての評価（星1〜5）に対して下書きを作成する用意をするが、
    # 批判的な内容が含まれる場合も誠実で自然なトーンでのフィードバックになるようAIに指示する。
    try:
        import requests
        
        # Check if any rating is 1 or 2
        has_low_rating = any(r and int(r) <= 2 for r in [rating, rating2, rating3, rating4, rating5])

        system_prompt = f"""あなたはGoogleマップの口コミを書くリアルな一般客です。あなたは「{shop['name']}（業種: {dict(shop).get('business_type') or '不明'}）」を訪れたお客様として、口コミを作成してください。

【文字数】以下の割合でランダムに選ぶ。毎回必ず異なる文字数になるようにばらけさせること：
- 普通（100〜150文字）：40%
- 長め（180〜250文字）：40%
- かなり長め（280〜350文字）：20%
短め（80文字以下）は絶対に生成しない。

【人物像】以下からランダムに1つ選び、その人物として書く：
- 初めて来た一人客
- 何度も通っている常連
- 友人・家族に誘われて来た
- ネットで見つけて予約した
- 職場の近くで気になっていた
- 旅行・出張中に立ち寄った
- 誕生日・記念日で利用した
- 疲れがひどくて思い切って来た
- 近所に住んでいてよく利用する
- 口コミを見て気になっていた

【トーン】以下からランダムに1つ選ぶ：
- 素っ気ない・淡白（短く事実だけ）
- 普通の感想（丁寧・特別感なし）
- 興奮・感動系（「！！」多め・テンション高め）
- 分析・詳細系（技術・手順を細かく説明）
- 比較系（「他の店と違って」「今まで行った中で」）
- ストーリー系（来店の経緯から結果まで流れで書く）
- 友人に話すような口語体（「〜だった」「〜だね」）
- 気遣い系（スタッフへの感謝を強調）
- リピート宣言系（「また必ず来ます」を軸に）
- 疑問→解決系（「不安だったけど〜」「迷ったけど〜」）

【文体・表現スタイル】以下からランダムに1つ選ぶ：
- 丁寧な敬語（〜です・〜ます調）
- やや砕けた口語（〜でした、〜だった混在）
- 絵文字を1〜2個使う（⭐・😊・✨など）
- 箇条書きを部分的に使う
- 感嘆符なし・落ち着いたトーン
- 「笑」「w」などを1か所使う（カジュアル）

【フォーカス】以下からランダムに1つ選ぶ（具体的なエピソードで表現すること）：
- 担当者が施術中にかけてくれた一言や気遣いについて
- 施術後に体がどう変わったか・何日続いたか
- 予約から来店までの流れ・待ち時間・スムーズさ
- 価格を払って「得したか損したか」の率直な感想
- 他の店でダメだったことがここではどうだったか
- 帰り道や翌日に気づいたこと・身体の変化

【書き出しパターン】以下から毎回異なるものを選ぶ（同じ書き出しを繰り返さない）：
「友人に勧められて」「仕事終わりにふらっと」「ずっと予約できなくて」「たまたまネットで見つけて」「近所なのにずっとスルーしてたけど」「肩がもう限界で」「記念日のご褒美に」「3回目の来店です」「半年ぶりに来たら」「前回よかったのでまた」「正直あまり期待してなかったけど」「地元民として」など

【アンケートの使い方】
アンケート結果は「何を重視したか」の参考情報。項目名をそのまま文章に出すのは禁止。
例：「接客の星が高い」→「担当の方が最初から話しやすくて」のように自分の体験として書く。
{'低評価（星1,2）の項目は、さらっと「〜はもう少しかな」程度に触れる。' if has_low_rating else '星3の項目は省略するか「まあ普通かな」レベルでさらっと。'}

【絶対禁止ワード・表現】
「居心地」「清潔感」「技術が高い」「技術が優れている」「丁寧な接客」「また利用したいと思います」「おすすめです」「癒されました」「リラックスできました」「スタッフの方々」「総合的に」「コスパが良い」「コスパ最高」「また伺います」

【禁止事項】
- アンケートの項目名（接客・技術・雰囲気など）をそのまま文章に使わない
- 「とても」「非常に」「本当に」を連続使用しない
- レビューサイトの定型文っぽい締め方をしない
- 綺麗にまとめすぎない。途中で話題が変わったり、雑さがあってよい

ルール絶対遵守: 【トーン：シンプル】などのメタ情報や前置きテキストは含めず、純粋な口コミテキストのみを出力してください。"""
            
        user_prompt = f"【アンケート結果】\n{opts['q1']}: 星{rating}\n"
        user_prompt += f"{opts['q2']}: 星{rating2}\n"
        user_prompt += f"{opts['q3']}: 星{rating3}\n"
        user_prompt += f"{opts['q4']}: 星{rating4}\n"
        user_prompt += f"{opts['q5']}: 星{rating5}\n"
        if answer6 and answer6 != "特になし":
            q6_label = opts.get("q6", {}).get("label", "来店・来院理由") if isinstance(opts.get("q6"), dict) else "来店・来院理由"
            user_prompt += f"{q6_label}: {answer6}\n"
        if answer7 and answer7 != "特になし":
            q7_label = opts.get("q7", {}).get("label", "特によかった点") if isinstance(opts.get("q7"), dict) else "特によかった点"
            user_prompt += f"{q7_label}: {answer7}\n"
        if comment:
            user_prompt += f"自由記入コメント: {comment}\n"

        headers = {
            "Authorization": f"Bearer {os.environ.get('OPENROUTER_API_KEY')}",
            "HTTP-Referer": "https://gugulabo.com",
            "X-Title": "Gugulabo Review Generator",
            "Content-Type": "application/json"
        }
        payload = {
            "model": "google/gemini-2.5-flash",
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt}
            ],
            "temperature": 1.0,
            "max_tokens": 500
        }
        resp = requests.post("https://openrouter.ai/api/v1/chat/completions", headers=headers, json=payload, timeout=15)
        if resp.status_code == 200:
            result_json = resp.json()
            if "choices" in result_json and len(result_json["choices"]) > 0:
                ai_draft = result_json["choices"][0]["message"]["content"].strip()
    except Exception as e:
        print("AI draft generation failed:", e)

    conn = get_db()
    if DB_TYPE == "postgresql":
        # postgres syntax implies parameters
        conn.execute(
            "INSERT INTO feedbacks (shop_id, rating, rating2, rating3, rating4, rating5, comment, ai_draft) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
            (shop["id"], rating, rating2, rating3, rating4, rating5, comment, ai_draft),
        )
    else:
        conn.execute(
            "INSERT INTO feedbacks (shop_id, rating, rating2, rating3, rating4, rating5, comment, ai_draft) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (shop["id"], rating, rating2, rating3, rating4, rating5, comment, ai_draft),
        )
    conn.commit()
    conn.close()

    # Fetch the review_url to give back to the client
    conn = get_db()
    shop_info = conn.execute("SELECT review_url FROM shops WHERE slug = ?", (slug,)).fetchone()
    conn.close()

    return jsonify({
        "success": True, 
        "ai_draft": ai_draft,
        "review_url": shop_info["review_url"] if shop_info else ""
    })


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
@payment_required
def manual():
    return render_template("manual.html")


@app.route("/settings", methods=["GET", "POST"])
@payment_required
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
        "SELECT notify_enabled, email, plan, stripe_customer_id, trial_ends_at FROM users WHERE id = ?",
        (current_user.id,),
    ).fetchone()
    conn.close()
    notify_enabled      = bool(user["notify_enabled"]) if user else True
    current_email       = user["email"] if user else ""
    current_plan        = user["plan"] if user else None
    stripe_customer_id  = user["stripe_customer_id"] if user else None
    trial_ends_at_val   = user["trial_ends_at"] if user else None
    is_trial            = bool(trial_ends_at_val) and not bool(current_plan)

    next_billing_date = None
    if stripe_customer_id and STRIPE_SECRET_KEY:
        try:
            subs = stripe.Subscription.list(
                customer=stripe_customer_id, limit=1, status="active"
            )
            if subs.data:
                from datetime import datetime as _dt
                next_billing_date = _dt.fromtimestamp(
                    subs.data[0].current_period_end
                ).strftime("%Y年%m月%d日")
        except Exception:
            pass

    return render_template(
        "settings.html",
        notify_enabled=notify_enabled,
        current_email=current_email,
        current_plan=current_plan,
        next_billing_date=next_billing_date,
        is_trial=is_trial,
    )


@app.route("/settings/email", methods=["POST"])
@payment_required
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


@app.route("/settings/password", methods=["POST"])
@payment_required
def settings_password():
    """パスワード変更処理"""
    current_password = request.form.get("current_password", "")
    new_password = request.form.get("new_password", "")
    new_password_confirm = request.form.get("new_password_confirm", "")

    if not current_password or not new_password or not new_password_confirm:
        flash("すべての項目を入力してください。", "error")
        return redirect(url_for("settings"))

    if new_password != new_password_confirm:
        flash("新しいパスワードが一致しません。", "error")
        return redirect(url_for("settings"))

    conn = get_db()
    user = conn.execute(
        "SELECT password_hash FROM users WHERE id = ?", (current_user.id,)
    ).fetchone()

    if not user or not bcrypt.checkpw(current_password.encode(), user["password_hash"].encode()):
        conn.close()
        flash("現在のパスワードが正しくありません。", "error")
        return redirect(url_for("settings"))

    new_hash = bcrypt.hashpw(new_password.encode(), bcrypt.gensalt()).decode()
    conn.execute(
        "UPDATE users SET password_hash = ? WHERE id = ?", (new_hash, current_user.id)
    )
    conn.commit()
    conn.close()
    flash("パスワードを変更しました。", "success")
    return redirect(url_for("settings"))


# ===== Stripe 決済 =====

@app.route("/subscribe")
@login_required
def subscribe():
    """決済案内ページ（登録直後 or 未払いユーザー向け）"""
    if current_user.is_paid:
        return redirect(url_for("index"))
    trial_ended = bool(current_user.trial_ends_at) and not current_user.is_paid
    return render_template("subscribe.html",
                           stripe_public_key=STRIPE_PUBLIC_KEY,
                           trial_ended=trial_ended)


@app.route("/create-checkout-session", methods=["POST"])
@login_required
def create_checkout_session():
    """Stripe Checkout セッションを作成してリダイレクト"""
    if current_user.is_paid:
        return redirect(url_for("index"))
    plan_param = request.form.get("plan", "monthly")
    if plan_param == "yearly":
        price_id  = STRIPE_PRICE_ID_YEARLY
        plan_name = "年間プラン"
    else:
        price_id  = STRIPE_PRICE_ID
        plan_name = "月額プラン"
    try:
        checkout_session = stripe.checkout.Session.create(
            payment_method_types=["card"],
            line_items=[{"price": price_id, "quantity": 1}],
            mode="subscription",
            success_url=url_for("success", _external=True) + "?session_id={CHECKOUT_SESSION_ID}",
            cancel_url=url_for("cancel", _external=True),
            customer_email=current_user.email,
            metadata={"user_id": str(current_user.id), "plan_name": plan_name},
        )
        return redirect(checkout_session.url, code=303)
    except Exception as e:
        flash(f"決済ページの作成に失敗しました: {e}", "error")
        return redirect(url_for("subscribe"))


@app.route("/success")
@login_required
def success():
    """決済成功後のページ"""
    session_id = request.args.get("session_id")
    if session_id:
        try:
            checkout_session = stripe.checkout.Session.retrieve(session_id)
            customer_id = checkout_session.get("customer")
            plan_name   = (checkout_session.get("metadata") or {}).get("plan_name", "月額プラン")
            conn = get_db()
            conn.execute(
                "UPDATE users SET plan = ?, stripe_customer_id = ? WHERE id = ?",
                (plan_name, customer_id, current_user.id),
            )
            conn.commit()
            conn.close()
        except Exception:
            pass
    return render_template("success.html")


@app.route("/cancel")
@login_required
def cancel():
    """決済キャンセル後のページ"""
    return render_template("cancel.html")


@app.route("/cancel-subscription", methods=["POST"])
@payment_required
def cancel_subscription():
    """サブスクリプションを解約する"""
    conn = get_db()
    user = conn.execute(
        "SELECT stripe_customer_id, trial_ends_at FROM users WHERE id = ?", (current_user.id,)
    ).fetchone()

    if not user:
        conn.close()
        flash("ユーザー情報が見つかりません。", "error")
        return redirect(url_for("settings"))

    # トライアルユーザー（Stripeサブスク未契約）の解約処理
    if not user["stripe_customer_id"]:
        if not user["trial_ends_at"]:
            conn.close()
            flash("解約対象のサブスクリプションが見つかりません。", "error")
            return redirect(url_for("settings"))
        conn.execute(
            "UPDATE users SET trial_ends_at = NULL WHERE id = ?", (current_user.id,)
        )
        conn.commit()
        conn.close()
        flash("トライアルを終了しました。", "success")
        return redirect(url_for("subscribe"))

    # 有料サブスクリプションの解約処理
    try:
        subs = stripe.Subscription.list(
            customer=user["stripe_customer_id"], limit=1, status="active"
        )
        if subs.data:
            stripe.Subscription.cancel(subs.data[0].id)
        conn.execute(
            "UPDATE users SET plan = NULL WHERE id = ?", (current_user.id,)
        )
        conn.commit()
        conn.close()
    except Exception as e:
        conn.close()
        flash(f"解約処理に失敗しました: {e}", "error")
        return redirect(url_for("settings"))

    flash("サブスクリプションを解約しました。", "success")
    return redirect(url_for("subscribe"))


@app.route("/webhook", methods=["POST"])
def webhook():
    """Stripe Webhook を受け取り、決済完了時にプランを更新する"""
    payload    = request.get_data()
    sig_header = request.headers.get("Stripe-Signature", "")

    try:
        event = stripe.Webhook.construct_event(payload, sig_header, STRIPE_WEBHOOK_SECRET)
    except ValueError:
        return "", 400
    except stripe.error.SignatureVerificationError:
        return "", 400

    if event["type"] == "checkout.session.completed":
        session     = event["data"]["object"]
        user_id     = (session.get("metadata") or {}).get("user_id")
        plan_name   = (session.get("metadata") or {}).get("plan_name", "月額プラン")
        customer_id = session.get("customer")
        if user_id:
            conn = get_db()
            conn.execute(
                "UPDATE users SET plan = ?, stripe_customer_id = ? WHERE id = ?",
                (plan_name, customer_id, int(user_id)),
            )
            conn.commit()
            conn.close()

    elif event["type"] == "customer.subscription.deleted":
        customer_id = event["data"]["object"].get("customer")
        if customer_id:
            conn = get_db()
            conn.execute(
                "UPDATE users SET plan = NULL WHERE stripe_customer_id = ?",
                (customer_id,),
            )
            conn.commit()
            conn.close()

    return "", 200


@app.route("/qr")
@payment_required
def qr_form():
    return render_template("qr.html", shop_name=SHOP_NAME)


@app.route("/bulk-create", methods=["GET", "POST"])
@payment_required
def bulk_create():
    """Bulk create shops from pasted CSV/TSV text and return a ZIP of QR codes"""
    if request.method == "GET":
        return render_template("bulk_create.html")

    csv_data = request.form.get("csv_data", "").strip()
    if not csv_data:
        flash("データが入力されていません。", "error")
        return redirect(url_for("bulk_create"))

    default_business_type = request.form.get("default_business_type", "massage")

    # Parse the pasted data
    lines = csv_data.splitlines()
    if not lines:
        flash("データが空です。", "error")
        return redirect(url_for("bulk_create"))

    # Try to determine delimiter (tab is common for copy-paste from Excel/Google Sheets)
    dialect = csv.Sniffer().sniff(lines[0] if len(lines) > 0 else "") if '\t' not in lines[0] else None
    reader = csv.reader(lines, dialect=dialect) if dialect else csv.reader(lines, delimiter='\t')
    
    conn = get_db()
    
    # Non-admin restriction check
    allow_creation = True
    if not current_user.is_admin:
        existing = conn.execute("SELECT COUNT(*) as cnt FROM shops WHERE user_id = ?", (current_user.id,)).fetchone()
        if existing and existing["cnt"] >= 1:
            conn.close()
            flash("一般ユーザーは1アカウントにつき1店舗までしか登録できません。", "error")
            return redirect(url_for("bulk_create"))

    created_shops = []
    success_count = 0
    error_count = 0
    row_num = 1
    
    for row in reader:
        # If non-admin and we already processed one new shop successfully, stop
        if not current_user.is_admin and success_count >= 1:
            break

        # Expected: Name, Review_URL, Unique_ID, Address, Slug, Business_Type, Place_ID
        if not row or len(row) < 2:
            error_count += 1
            row_num += 1
            continue
            
        # ヘッダー行をスキップ
        if row_num == 1 and ("店舗名" in row[0] or "Name" in row[0]):
            row_num += 1
            continue
            
        name = row[0].strip()
        review_url = ""
        unique_id = ""
        address = ""
        slug_input = ""
        business_type = default_business_type
        place_id = ""
        
        url_candidates = [x for x in row if "http" in x]
        if url_candidates:
            review_url = url_candidates[0].strip()
        else:
            review_url = row[1].strip() if len(row) > 1 else ""
            
        if not name or not review_url:
            error_count += 1
            row_num += 1
            continue

        # Detect Scraper Format: Name, Rating, Reviews, Address, Place ID, URL
        is_scraper_format = len(row) >= 6 and "http" in row[5] and len(row[4].strip()) > 10 and row[4].strip().startswith("ChI")
        
        if is_scraper_format:
            address = row[3].strip()
            place_id = row[4].strip()
            # Replace the generic map URL with a direct 'Write a Review' URL
            review_url = f"https://search.google.com/local/writereview?placeid={place_id}"
            business_type = predict_business_type_from_name(name)
        else:
            unique_id = row[2].strip() if len(row) > 2 else ""
            address = row[3].strip() if len(row) > 3 else ""
            slug_input = row[4].strip() if len(row) > 4 else ""
            business_type_input = row[5].strip() if len(row) > 5 else ""
            place_id = row[6].strip() if len(row) > 6 else ""
            
            if business_type_input:
                business_type = business_type_input
            else:
                business_type = predict_business_type_from_name(name)
        
        status = "trial"

        # Check existing place_id
        if place_id:
            dup = conn.execute("SELECT id, slug, review_url FROM shops WHERE place_id = ?", (place_id,)).fetchone()
            if dup:
                if dup["review_url"] != review_url:
                    conn.execute("UPDATE shops SET review_url = ? WHERE id = ?", (review_url, dup["id"]))
                created_shops.append({"id": dup["id"], "name": name, "slug": dup["slug"]})
                continue
                
        # Check existing unique_id
        if unique_id:
            dup_uid = conn.execute("SELECT id, slug FROM shops WHERE unique_id = ?", (unique_id,)).fetchone()
            if dup_uid:
                created_shops.append({"id": dup_uid["id"], "name": name, "slug": dup_uid["slug"]})
                continue
                
        # Slug normalization from input or name
        if slug_input:
            slug = re.sub(r"[^a-z0-9\-_]", "-", slug_input.lower())
            slug = re.sub(r"-{2,}", "-", slug).strip("-") or None
        else:
            kks = pykakasi.kakasi()
            result = kks.convert(name)
            romaji = "".join([item['hepburn'] for item in result])
            slug = re.sub(r"[^a-z0-9\-_]", "-", romaji.lower())
            slug = re.sub(r"-{2,}", "-", slug).strip("-") or None

        try:
            cursor = conn.execute(
                "INSERT INTO shops (name, review_url, unique_id, address, slug, user_id, business_type, place_id, status) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (name, review_url, unique_id or None, address, slug, current_user.id, business_type, place_id or None, status),
            )
        except DBIntegrityError:
            cursor = conn.execute(
                "INSERT INTO shops (name, review_url, unique_id, address, slug, user_id, business_type, place_id, status) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (name, review_url, unique_id or None, address, None, current_user.id, business_type, place_id or None, status),
            )
            slug = None
            
        shop_id = cursor.lastrowid
        if not slug:
            final_slug = f"shop-{shop_id}"
            conn.execute("UPDATE shops SET slug = ? WHERE id = ?", (final_slug, shop_id))
        else:
            final_slug = slug
            
        success_count += 1
        created_shops.append({"id": shop_id, "name": name, "slug": final_slug})
        row_num += 1

    conn.commit()
    conn.close()

    if len(created_shops) == 0:
        flash("有効なデータが見つかりませんでした。入力内容を確認してください。", "error")
        return redirect(url_for("bulk_create"))

    import xlsxwriter

    output_buf = io.BytesIO()
    wb = xlsxwriter.Workbook(output_buf, {'in_memory': True})
    ws = wb.add_worksheet("店舗・QRコード一覧")

    base_url = request.url_root.rstrip('/')
    header = next(csv.reader([lines[0]], dialect=dialect) if dialect else csv.reader([lines[0]], delimiter='\t'))
    header.extend(["Generated_URL", "QR_Code_Image"])
    
    ws.write_row(0, 0, header)
    
    # Reparse to write out rows
    reader_full = csv.reader(lines[1:], dialect=dialect) if dialect else csv.reader(lines[1:], delimiter='\t')
    
    shop_dict = {s["name"]: s for s in created_shops}
    
    url_col_idx = len(header) - 1
    qr_col_idx = len(header)
    
    for row_offset, row in enumerate(reader_full):
        excel_row = row_offset + 1 # 0-indexed, Header is row 0
        # lines[1:] already removed the header, so row_offset == 0 is the first data row.
        # Do not skip it.
        if not row or len(row) < 2:
            ws.write_row(excel_row, 0, row)
            continue
            
        name = row[0].strip()
        shop = shop_dict.get(name)
        if not shop:
            ws.write_row(excel_row, 0, row)
            continue

        url = f"{base_url}/shop/{shop['slug']}"
        row.append(url)
        ws.write_row(excel_row, 0, row)
        
        try:
            img = build_qr_image(
                url=url,
                fg_color="#000000",
                fg_color2="#000000",
                gradient_dir="radial",
                bg_color="#ffffff",
                corner_style="square",
                size=256,
            )
            img_io = io.BytesIO()
            img.save(img_io, format="PNG", optimize=True)
            
            # Position the image in the correct cell (row index, col index)
            # Adjust row height (in pixels/points) and col width (in characters)
            ws.set_row(excel_row, 80)
            ws.set_column(qr_col_idx, qr_col_idx, 15)
            
            # Insert image, specifying scale to fit cell ~100x100px
            ws.insert_image(excel_row, qr_col_idx, "qr.png", {'image_data': img_io, 'x_scale': 0.4, 'y_scale': 0.4, 'positioning': 1})
            
        except Exception as e:
            print(f"Failed to generate QR for {shop['name']}: {e}")

    wb.close()
    output_buf.seek(0)
    
    return send_file(
        output_buf,
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        as_attachment=True,
        download_name=f"shops_with_qr_{datetime.now().strftime('%Y%m%d%H%M%S')}.xlsx"
    )


@app.route("/qr/shops", methods=["GET"])
@payment_required
def get_shops():
    """ログイン中ユーザーの店舗一覧をJSON形式で返す"""
    conn = get_db()
    shops = conn.execute(
        "SELECT id, name, review_url, slug, place_id, line_user_id, status, business_type, created_at FROM shops WHERE user_id = ? ORDER BY created_at DESC",
        (current_user.id,),
    ).fetchall()
    conn.close()
    return jsonify([dict(s) for s in shops])


@app.route("/qr/shops", methods=["POST"])
@payment_required
def add_shop():
    """店舗を追加する（ログインユーザーに紐づける）"""
    data = request.get_json()
    name          = (data.get("name")          or "").strip()
    review_url    = (data.get("review_url")    or "").strip()
    slug_input    = (data.get("slug")          or "").strip()
    business_type = (data.get("business_type") or "").strip()
    place_id      = (data.get("place_id")      or "").strip()
    status        = (data.get("status")        or "trial").strip() or "trial"

    # place_id があれば口コミ投稿URLを自動生成（place_id優先・既存URLも上書き）
    if place_id:
        review_url = f"https://search.google.com/local/writereview?placeid={place_id}"

    if not name or not review_url:
        return jsonify({"error": "name と review_url は必須です（place ID を入力するか URL を直接入力してください）"}), 400

    conn = get_db()

    # 既存店舗の重複チェック（現ユーザーの place_id 基準のみ）
    if place_id:
        dup = conn.execute(
            "SELECT id FROM shops WHERE place_id = ? AND user_id = ?",
            (place_id, current_user.id),
        ).fetchone()
        if dup:
            conn.close()
            return jsonify({"success": True, "existing": True, "shop_id": dup["id"]}), 200

    # 1アカウント1店舗の制限（従来どおり）
    existing = conn.execute(
        "SELECT COUNT(*) as cnt FROM shops WHERE user_id = ?", (current_user.id,)
    ).fetchone()
    if existing and existing["cnt"] >= 1 and not current_user.is_admin:
        conn.close()
        return jsonify({"error": "一般店舗は1アカウントにつき1店舗までご登録いただけます。"}), 400

    # スラッグ正規化（任意入力時）
    if slug_input:
        slug = re.sub(r"[^a-z0-9\-_]", "-", slug_input.lower())
        slug = re.sub(r"-{2,}", "-", slug).strip("-") or None
    else:
        slug = None

    # INSERT（slug重複時はNULLで再挿入 → 後で shop-{id} を設定）
    # place_id UNIQUE 制約違反は別ユーザーが既に登録済みの場合に発生
    try:
        cursor = conn.execute(
            "INSERT INTO shops (name, review_url, slug, user_id, business_type, place_id, status) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (name, review_url, slug, current_user.id, business_type, place_id or None, status),
        )
    except DBIntegrityError as e:
        if place_id and "place_id" in str(e).lower():
            conn.close()
            return jsonify({"error": "この Place ID はすでに別のアカウントで登録されています。"}), 409
        # slug 重複の場合は slug=NULL で再試行
        try:
            cursor = conn.execute(
                "INSERT INTO shops (name, review_url, slug, user_id, business_type, place_id, status) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (name, review_url, None, current_user.id, business_type, place_id or None, status),
            )
        except DBIntegrityError:
            conn.close()
            return jsonify({"error": "この Place ID はすでに別のアカウントで登録されています。"}), 409
    shop_id = cursor.lastrowid
    if not slug:
        conn.execute("UPDATE shops SET slug = ? WHERE id = ?", (f"shop-{shop_id}", shop_id))
    conn.commit()
    conn.close()
    return jsonify({"success": True}), 201


@app.route("/qr/shops/<int:shop_id>", methods=["PATCH"])
@payment_required
def update_shop(shop_id):
    """店舗の place_id / line_user_id を更新する（所有者のみ）"""
    conn = get_db()
    shop = conn.execute(
        "SELECT id FROM shops WHERE id = ? AND user_id = ?", (shop_id, current_user.id)
    ).fetchone()
    if not shop:
        conn.close()
        return jsonify({"error": "店舗が見つかりません"}), 404

    data = request.get_json()
    place_id     = (data.get("place_id")     or "").strip() or None
    line_user_id = (data.get("line_user_id") or "").strip() or None

    conn.execute(
        "UPDATE shops SET place_id = ?, line_user_id = ? WHERE id = ?",
        (place_id, line_user_id, shop_id)
    )
    conn.commit()
    conn.close()
    return jsonify({"success": True})


@app.route("/qr/shops/<int:shop_id>", methods=["DELETE"])
@payment_required
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
@payment_required
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
@payment_required
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
@payment_required
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


@app.route("/qr/pop-template", methods=["POST"])
@payment_required
def qr_pop_template():
    """POPテンプレートにQRコードを嵌め込んでPNGを返す"""
    template_id = request.form.get("template_id", "1")
    url = (request.form.get("url") or "").strip()
    fg_color = request.form.get("fg_color", "#000000")
    bg_color = request.form.get("bg_color", "#ffffff")
    module_shape = request.form.get("module_shape", "square")

    if not url:
        return jsonify({"success": False, "error": "URLが必要です"}), 400

    # 各テンプレートのQR枠の中心座標とサイズ（実測値）
    qr_positions = {
        "1": {"cx": 628, "cy": 822, "size": 430},
        "2": {"cx": 604, "cy": 827, "size": 425},
        "3": {"cx": 427, "cy": 828, "size": 445},
        "4": {"cx": 499, "cy": 535, "size": 450},
        "5": {"cx": 467, "cy": 726, "size": 390},
    }
    pos = qr_positions.get(template_id, qr_positions["1"])

    template_path = os.path.join(app.static_folder, f"pop_template_{template_id}.png")
    if not os.path.exists(template_path):
        return jsonify({"success": False, "error": "テンプレートが見つかりません"}), 404

    try:
        # QR画像生成
        qr_img = build_qr_image(
            url=url,
            fg_color=fg_color,
            fg_color2=fg_color,
            gradient_dir="radial",
            bg_color=bg_color,
            corner_style=module_shape,
            size=pos["size"],
        )

        # テンプレート読み込み
        template = Image.open(template_path).convert("RGBA")

        # QRをリサイズして中心座標に貼り付け
        qr_resized = qr_img.resize((pos["size"], pos["size"]), Image.LANCZOS).convert("RGBA")
        paste_x = pos["cx"] - pos["size"] // 2
        paste_y = pos["cy"] - pos["size"] // 2
        template.paste(qr_resized, (paste_x, paste_y), qr_resized)

        output = io.BytesIO()
        template.convert("RGB").save(output, format="PNG")
        output.seek(0)
        return send_file(
            output,
            mimetype="image/png",
            as_attachment=True,
            download_name=f"pop_{template_id}.png",
        )
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


# ----- 管理画面: AI返答生成 -----

@app.route("/review")
@payment_required
def review_form():
    return render_template("review.html", shop_name=SHOP_NAME)


@app.route("/review/generate", methods=["POST"])
@payment_required
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
    # ユーザーの店舗IDを取得
    shops = conn.execute(
        "SELECT id FROM shops WHERE user_id = ?", (user_id,)
    ).fetchall()
    shop_ids = [s["id"] for s in shops]

    # 店舗に紐づく関連データを削除（外部キー制約に引っかからないよう逆順に）
    for shop_id in shop_ids:
        conn.execute("DELETE FROM coupon_deliveries WHERE shop_id = ?", (shop_id,))
        conn.execute("DELETE FROM coupons WHERE shop_id = ?", (shop_id,))
        conn.execute("DELETE FROM feedbacks WHERE shop_id = ?", (shop_id,))

    # 店舗・ユーザーを削除
    conn.execute("DELETE FROM shops WHERE user_id = ?", (user_id,))
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

@app.route("/meo")
def meo_guide():
    return render_template("meo.html")


@app.route('/privacy')
def privacy():
    return render_template('privacy.html')


@app.route('/terms')
def terms():
    return render_template('terms.html')

@app.route("/health")
def health():
    try:
        conn = get_db()
        conn.execute("SELECT 1")
        conn.close()
        return jsonify({"ok": True, "db": True}), 200
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 503

@app.route("/bulk-urls.csv")
@payment_required
def bulk_urls_csv():
    conn = get_db()
    rows = conn.execute(
        "SELECT id, name, slug FROM shops WHERE user_id = ? ORDER BY id",
        (current_user.id,)
    ).fetchall()
    conn.close()
    base_url = request.url_root.rstrip("/")
    buf = io.StringIO(newline="")
    w = csv.writer(buf)
    w.writerow(["id", "name", "slug", "url"])
    for r in rows:
        url = f"{base_url}/shop/{r['slug']}"
        w.writerow([r["id"], r["name"], r["slug"], url])
    data = buf.getvalue().encode("utf-8-sig")
    return send_file(
        io.BytesIO(data),
        mimetype="text/csv",
        as_attachment=True,
        download_name=f"shop_urls_{datetime.now().strftime('%Y%m%d%H%M%S')}.csv"
    )

# ===== LINE連携 =====

@app.route('/webhook/line', methods=['POST'])
def line_webhook():
    data = request.json
    for event in data.get('events', []):
        try:
            line_user_id = event['source']['userId']
            if event["type"] in ("follow", "message"):
                db = get_db()
                db.execute(
                    """INSERT OR REPLACE INTO line_pending
                       (line_user_id, created_at) VALUES (?, CURRENT_TIMESTAMP)""",
                    (line_user_id,)
                )
                db.commit()
                print(f"LINE pending saved: {line_user_id}", flush=True)
        except Exception as e:
            print(f"LINE webhook error: {e}", flush=True)
    return 'OK', 200


@app.route('/shop/<int:shop_id>/fetch-place-id', methods=['POST'])
@login_required
def fetch_place_id(shop_id):
    import requests
    conn = get_db()
    shop = conn.execute(
        "SELECT * FROM shops WHERE id = ? AND user_id = ?",
        (shop_id, current_user.id)
    ).fetchone()
    if not shop:
        return jsonify({"success": False, "message": "店舗が見つかりません"})

    query = f"{shop['name']} {shop['address'] or ''}"
    url = "https://places.googleapis.com/v1/places:searchText"
    headers = {
        "X-Goog-Api-Key": os.environ.get("PLACES_API_KEY"),
        "X-Goog-FieldMask": "places.id,places.displayName,places.formattedAddress"
    }
    payload = {"textQuery": query}

    response = requests.post(url, headers=headers, json=payload)
    data = response.json()

    places = data.get("places", [])
    if not places:
        return jsonify({"success": False, "message": "店舗が見つかりませんでした。店舗名・住所を確認してください。"})

    place = places[0]
    place_id = place["id"]
    place_name = place["displayName"]["text"]
    place_address = place.get("formattedAddress", "")

    conn.execute(
        "UPDATE shops SET place_id = ? WHERE id = ?",
        (place_id, shop_id)
    )
    conn.commit()

    return jsonify({
        "success": True,
        "message": "連携完了！",
        "place_name": place_name,
        "place_address": place_address
    })


@app.route('/shop/<int:shop_id>/line-connect')
@login_required
def line_connect(shop_id):
    """LINE連携用ページを表示する（所有者のみ）"""
    conn = get_db()
    shop = conn.execute(
        "SELECT * FROM shops WHERE id = ? AND user_id = ?", (shop_id, current_user.id)
    ).fetchone()
    conn.close()
    if not shop:
        return "店舗が見つかりません", 404
    return render_template('line_connect.html', shop=shop, line_add_url=LINE_ADD_FRIEND_URL)


@app.route('/shop/<int:shop_id>/line-connect/complete', methods=['POST'])
@login_required
def line_connect_complete(shop_id):
    conn = get_db()
    shop = conn.execute(
        "SELECT * FROM shops WHERE id = ? AND user_id = ?",
        (shop_id, current_user.id)
    ).fetchone()
    if not shop:
        return jsonify({"success": False, "message": "店舗が見つかりません"})

    pending = conn.execute(
        """SELECT line_user_id FROM line_pending
           WHERE created_at >= datetime('now', '-5 minutes')
           ORDER BY created_at DESC LIMIT 1"""
    ).fetchone()

    if not pending:
        return jsonify({
            "success": False,
            "message": "LINE連携が確認できませんでした。先にLINEでメッセージを送ってください。"
        })

    conn.execute(
        "UPDATE shops SET line_user_id = ? WHERE id = ?",
        (pending['line_user_id'], shop_id)
    )
    conn.execute(
        "DELETE FROM line_pending WHERE line_user_id = ?",
        (pending['line_user_id'],)
    )
    conn.commit()
    return jsonify({"success": True, "message": "LINE連携が完了しました！"})


if __name__ == "__main__":
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 5000)), debug=False)
