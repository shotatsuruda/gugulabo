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
import requests
import stripe
from dotenv import load_dotenv
from flask import (
    Flask, abort, flash, jsonify, redirect, render_template,
    request, session, url_for, send_file, make_response
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
app.config["SEND_FILE_MAX_AGE_DEFAULT"] = 0

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
STRIPE_PUBLIC_KEY    = os.environ.get("STRIPE_PUBLIC_KEY", "")
STRIPE_PRICE_ID         = os.environ.get("STRIPE_PRICE_ID", "")
STRIPE_PRICE_ID_YEARLY  = os.environ.get("STRIPE_PRICE_ID_YEARLY", "")
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
        """管理者 or 有料プラン契約中 or 管理者作成ユーザー or トライアル期間中ならアクセス可能"""
        if self.is_admin or bool(self.plan):
            return True
        if self.trial_ends_at is None:
            # trial_ends_at が NULL = 管理者作成ユーザー → 無料永続利用
            return True
        try:
            trial_end = (
                self.trial_ends_at if hasattr(self.trial_ends_at, "date")
                else datetime.fromisoformat(str(self.trial_ends_at))
            )
            return trial_end > datetime.now()
        except Exception:
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
        conn.execute("ALTER TABLE shops ADD COLUMN IF NOT EXISTS custom_questions TEXT")
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
        try:
            conn.execute("ALTER TABLE shops ADD COLUMN custom_questions TEXT")
        except Exception:
            pass

    # shopsテーブルへのGBPプロフィール関連カラム追加
    if DB_TYPE == "postgresql":
        conn.execute("ALTER TABLE shops ADD COLUMN IF NOT EXISTS main_menus TEXT")
        conn.execute("ALTER TABLE shops ADD COLUMN IF NOT EXISTS strengths TEXT")
        conn.execute("ALTER TABLE shops ADD COLUMN IF NOT EXISTS target_customers TEXT")
        conn.execute("ALTER TABLE shops ADD COLUMN IF NOT EXISTS nearest_station TEXT")
        conn.execute("ALTER TABLE shops ADD COLUMN IF NOT EXISTS reservation_method TEXT")
        conn.execute("ALTER TABLE shops ADD COLUMN IF NOT EXISTS price_range TEXT")
        conn.execute("ALTER TABLE shops ADD COLUMN IF NOT EXISTS profile_completed INTEGER DEFAULT 0")
    else:
        for _col, _def in [
            ("main_menus",         "TEXT"),
            ("strengths",          "TEXT"),
            ("target_customers",   "TEXT"),
            ("nearest_station",    "TEXT"),
            ("reservation_method", "TEXT"),
            ("price_range",        "TEXT"),
            ("profile_completed",  "INTEGER DEFAULT 0"),
        ]:
            try:
                conn.execute(f"ALTER TABLE shops ADD COLUMN {_col} {_def}")
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
        conn.execute("ALTER TABLE feedbacks ADD COLUMN IF NOT EXISTS salon_type TEXT")
        conn.execute("ALTER TABLE feedbacks ADD COLUMN IF NOT EXISTS menu TEXT")
        conn.execute("ALTER TABLE feedbacks ADD COLUMN IF NOT EXISTS satisfaction TEXT")
        conn.execute("ALTER TABLE feedbacks ADD COLUMN IF NOT EXISTS good_points TEXT")
        conn.execute("ALTER TABLE feedbacks ADD COLUMN IF NOT EXISTS revisit TEXT")
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
        try:
            conn.execute("ALTER TABLE feedbacks ADD COLUMN salon_type TEXT")
            conn.execute("ALTER TABLE feedbacks ADD COLUMN menu TEXT")
            conn.execute("ALTER TABLE feedbacks ADD COLUMN satisfaction TEXT")
            conn.execute("ALTER TABLE feedbacks ADD COLUMN good_points TEXT")
            conn.execute("ALTER TABLE feedbacks ADD COLUMN revisit TEXT")
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

    # アンケート送信テンプレートテーブル
    conn.execute(
        f"""
        CREATE TABLE IF NOT EXISTS review_templates (
            id         {pk},
            store_id   INTEGER NOT NULL,
            channel    TEXT NOT NULL,
            content    TEXT NOT NULL,
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(store_id, channel)
        )
        """
    )

    # GBP最新情報テーブル
    conn.execute(
        f"""
        CREATE TABLE IF NOT EXISTS gbp_posts (
            id         {pk},
            shop_id    INTEGER NOT NULL,
            content    TEXT NOT NULL,
            mode       TEXT NOT NULL DEFAULT 'auto',
            status     TEXT DEFAULT 'draft',
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            posted_at  DATETIME,
            FOREIGN KEY (shop_id) REFERENCES shops(id)
        )
        """
    )
    # gbp_postsのmodeカラム追加（旧スキーマ対応）
    if DB_TYPE == "postgresql":
        conn.execute("ALTER TABLE gbp_posts ADD COLUMN IF NOT EXISTS mode TEXT NOT NULL DEFAULT 'auto'")
    else:
        try:
            conn.execute("ALTER TABLE gbp_posts ADD COLUMN mode TEXT NOT NULL DEFAULT 'auto'")
        except Exception:
            pass

    # 投稿スタイル画像テーブル
    conn.execute(
        f"""
        CREATE TABLE IF NOT EXISTS post_style_images (
            id             {pk},
            shop_id        INTEGER NOT NULL,
            filename       TEXT NOT NULL,
            extracted_text TEXT,
            uploaded_at    DATETIME DEFAULT CURRENT_TIMESTAMP,
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

def get_review_url_from_place_id(place_id):
    """place_id から口コミ投稿URLを返す。"""
    return f"https://search.google.com/local/writereview?placeid={place_id}"


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

def generate_review_response(review_text: str, business_type: str = "", rating: int = 0) -> str:
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

    # 低評価（星1〜2）は140〜180文字、それ以外は100〜140文字
    if 1 <= rating <= 2:
        min_chars, max_chars = 140, 180
    else:
        min_chars, max_chars = 100, 140

    prompt = f"""あなたは{persona}です{context}。お客様からGoogleにいただいた以下のレビューに対して、丁寧でプロフェッショナルな返答文を日本語で作成してください。

【返答文の条件】
- 丁寧な敬語を使用する
- お客様への感謝の気持ちを伝える
- レビューの内容（良い点・気になった点）に具体的に言及する
- ポジティブな内容は喜びを表現する
- ネガティブな内容は真摯に受け止め、改善への姿勢を示す
- 文字数は必ず{min_chars}文字以上{max_chars}文字以下で作成すること（{min_chars}文字未満・{max_chars}文字超は厳禁）
- 署名は「{sign}」とする

【お客様のレビュー】
{review_text}

【返答文のみを出力してください。説明や前置きは不要です。】"""

    for _ in range(3):
        response = client.chat.completions.create(
            model="anthropic/claude-sonnet-4-6",
            max_tokens=1024,
            messages=[{"role": "user", "content": prompt}],
        )
        result = response.choices[0].message.content
        if min_chars <= len(result) <= max_chars:
            return result
        # 範囲外の場合はプロンプトを強化して再試行
        prompt = f"""以下の返答文は{len(result)}文字です。署名「{sign}」を含めて必ず{min_chars}文字以上{max_chars}文字以下で書き直してください。返答文のみ出力してください。\n\n{result}"""

    # 再試行後もmax_chars超の場合は句点で強制カット
    if len(result) > max_chars:
        cut = result[:max_chars]
        last_punct = max(cut.rfind('。'), cut.rfind('！'), cut.rfind('？'))
        result = cut[:last_punct + 1] if last_punct > min_chars - 20 else cut

    return result


_GBP_MODEL = "google/gemini-2.0-flash"
_OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
_STYLE_UPLOAD_DIR = os.path.join(os.path.dirname(__file__), "static", "uploads", "post_styles")


def _openrouter_headers() -> dict:
    return {
        "Authorization": f"Bearer {os.environ.get('OPENROUTER_API_KEY')}",
        "HTTP-Referer": "https://gugulabo.com",
        "X-Title": "Gugulabo GBP",
        "Content-Type": "application/json",
    }


def call_openrouter_text(prompt: str) -> str:
    """テキストのみのプロンプトでOpenRouterを呼び出す"""
    resp = requests.post(
        _OPENROUTER_URL,
        headers=_openrouter_headers(),
        json={
            "model": _GBP_MODEL,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": 1200,
        },
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()["choices"][0]["message"]["content"]


def call_openrouter_vision(image_path: str, prompt: str) -> str:
    """画像+テキストのプロンプトでOpenRouterを呼び出す"""
    import base64
    with open(image_path, "rb") as f:
        image_data = base64.b64encode(f.read()).decode("utf-8")
    ext = image_path.rsplit(".", 1)[-1].lower()
    mime_type = "image/png" if ext == "png" else "image/jpeg"
    resp = requests.post(
        _OPENROUTER_URL,
        headers=_openrouter_headers(),
        json={
            "model": _GBP_MODEL,
            "messages": [{
                "role": "user",
                "content": [
                    {"type": "image_url", "image_url": {"url": f"data:{mime_type};base64,{image_data}"}},
                    {"type": "text", "text": prompt},
                ],
            }],
            "max_tokens": 800,
        },
        timeout=60,
    )
    resp.raise_for_status()
    return resp.json()["choices"][0]["message"]["content"]


def _parse_patterns(raw: str) -> list:
    """OpenRouterのJSON応答からpatternsリストを取り出す（マークダウン除去付き）"""
    import json
    raw = raw.strip()
    if raw.startswith("```"):
        parts = raw.split("```")
        raw = parts[1] if len(parts) > 1 else raw
        if raw.startswith("json"):
            raw = raw[4:]
    raw = raw.strip()
    return json.loads(raw)["patterns"]


def predict_business_type_from_name(shop_name: str) -> str:
    """店舗名から業種を予測する（新サロン業態対応）"""
    client = OpenAI(
        api_key=OPENROUTER_API_KEY,
        base_url="https://openrouter.ai/api/v1",
    )
    prompt = f"""以下の店舗名から、最も当てはまる業種を以下の選択肢から1つだけ選んで出力してください。
選択肢: 美容院, メンズサロン, 女性専用サロン, ヘッドスパ専門, 縮毛矯正専門, 総合サロン
判断のヒント：
- 「メンズ」「men's」「男性」が含まれる → メンズサロン
- 「ヘッドスパ」「head spa」が含まれる → ヘッドスパ専門
- 「縮毛」「ストレート」が含まれる → 縮毛矯正専門
- 「ladies」「レディース」「女性専用」が含まれる → 女性専用サロン
- 上記に当てはまらない美容室・ヘアサロン → 美容院
- 複合的なサービスを提供するサロン → 総合サロン
出力は選択した業種名（1語）のみとし、説明などは一切不要です。

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
    resp = make_response(render_template("landing.html"))
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    resp.headers["Pragma"] = "no-cache"
    return resp


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
        "questions": [
            {
                "id": "menu",
                "text": "今回のメニューは何でしたか？",
                "type": "multi",
                "required": True,
                "options": ["カット", "カラー", "パーマ", "縮毛矯正",
                            "トリートメント", "ヘッドスパ", "ブリーチ",
                            "インナーカラー", "その他"]
            },
            {
                "id": "satisfaction",
                "text": "仕上がりはいかがでしたか？",
                "type": "single",
                "required": True,
                "options": ["とても満足", "満足", "普通", "少し不満", "不満"]
            },
            {
                "id": "good_points",
                "text": "特によかった点を教えてください",
                "type": "multi",
                "required": False,
                "options": ["カラーの発色", "スタイルの提案力", "カットの技術",
                            "グレージュ・透明感の仕上がり", "縮毛矯正の自然さ",
                            "ヘアダメージへの配慮", "スタッフの対応",
                            "店内の雰囲気", "丁寧なカウンセリング", "施術時間"]
            },
            {
                "id": "revisit",
                "text": "また来店したいと思いますか？",
                "type": "single",
                "required": True,
                "options": ["ぜひまた来たい", "たぶん来ると思う", "まだわからない"]
            },
            {
                "id": "comment",
                "text": "お気づきの点など（任意）",
                "type": "text",
                "required": False,
                "placeholder": "担当スタイリストへのメッセージやご要望があればご記入ください"
            }
        ]
    },
    "総合サロン": {
        "questions": [
            {"id": "menu", "text": "今回のメニューは何でしたか？", "type": "multi", "required": True,
             "options": ["カット", "カラー", "パーマ", "縮毛矯正", "トリートメント", "ヘッドスパ", "ブリーチ", "インナーカラー", "その他"]},
            {"id": "satisfaction", "text": "仕上がりはいかがでしたか？", "type": "single", "required": True,
             "options": ["とても満足", "満足", "普通", "少し不満", "不満"]},
            {"id": "good_points", "text": "特によかった点を教えてください", "type": "multi", "required": False,
             "options": ["カラーの発色", "スタイルの提案力", "カットの技術", "グレージュ・透明感の仕上がり",
                         "縮毛矯正の自然さ", "ヘアダメージへの配慮", "スタッフの対応", "店内の雰囲気",
                         "丁寧なカウンセリング", "施術時間"]},
            {"id": "revisit", "text": "また来店したいと思いますか？", "type": "single", "required": True,
             "options": ["ぜひまた来たい", "たぶん来ると思う", "まだわからない"]},
            {"id": "comment", "text": "お気づきの点など（任意）", "type": "text", "required": False,
             "placeholder": "担当スタイリストへのメッセージやご要望があればご記入ください"},
        ]
    },
    "女性専用サロン": {
        "questions": [
            {"id": "menu", "text": "今回のメニューは何でしたか？", "type": "multi", "required": True,
             "options": ["カット", "カラー", "パーマ", "縮毛矯正", "トリートメント", "ヘッドスパ", "ブリーチ", "インナーカラー", "その他"]},
            {"id": "satisfaction", "text": "仕上がりはいかがでしたか？", "type": "single", "required": True,
             "options": ["とても満足", "満足", "普通", "少し不満", "不満"]},
            {"id": "good_points", "text": "特によかった点を教えてください", "type": "multi", "required": False,
             "options": ["カラーの発色", "スタイルの提案力", "カットの技術", "グレージュ・透明感の仕上がり",
                         "縮毛矯正の自然さ", "ヘアダメージへの配慮", "スタッフの対応", "店内の落ち着いた雰囲気",
                         "丁寧なカウンセリング", "プライベート感"]},
            {"id": "revisit", "text": "また来店したいと思いますか？", "type": "single", "required": True,
             "options": ["ぜひまた来たい", "たぶん来ると思う", "まだわからない"]},
            {"id": "comment", "text": "お気づきの点など（任意）", "type": "text", "required": False,
             "placeholder": "担当スタイリストへのメッセージやご要望があればご記入ください"},
        ]
    },
    "メンズサロン": {
        "questions": [
            {"id": "menu", "text": "今回のメニューは何でしたか？", "type": "multi", "required": True,
             "options": ["カット", "フェード", "パーマ", "カラー", "ヘッドスパ", "スキャルプケア", "その他"]},
            {"id": "satisfaction", "text": "仕上がりはいかがでしたか？", "type": "single", "required": True,
             "options": ["とても満足", "満足", "普通", "少し不満", "不満"]},
            {"id": "good_points", "text": "特によかった点を教えてください", "type": "multi", "required": False,
             "options": ["カットの技術", "フェードの仕上がり", "スタイルの提案力", "スッキリした仕上がり",
                         "スタッフの対応", "店内の雰囲気", "施術時間の短さ", "価格のバランス"]},
            {"id": "revisit", "text": "また来店したいと思いますか？", "type": "single", "required": True,
             "options": ["ぜひまた来たい", "たぶん来ると思う", "まだわからない"]},
            {"id": "comment", "text": "お気づきの点など（任意）", "type": "text", "required": False,
             "placeholder": "担当スタイリストへのメッセージやご要望があればご記入ください"},
        ]
    },
    "ヘッドスパ専門": {
        "questions": [
            {"id": "menu", "text": "今回のメニューは何でしたか？", "type": "multi", "required": True,
             "options": ["ヘッドスパ", "トリートメント", "頭皮ケア", "リラクゼーションコース", "カット+ヘッドスパ", "その他"]},
            {"id": "satisfaction", "text": "いかがでしたか？", "type": "single", "required": True,
             "options": ["とても満足", "満足", "普通", "少し不満", "不満"]},
            {"id": "good_points", "text": "特によかった点を教えてください", "type": "multi", "required": False,
             "options": ["リラックス効果", "頭皮の気持ちよさ", "スタッフの技術", "施術後の髪のツヤ",
                         "店内の雰囲気・香り", "施術時間", "カウンセリングの丁寧さ", "清潔感"]},
            {"id": "revisit", "text": "また来店したいと思いますか？", "type": "single", "required": True,
             "options": ["ぜひまた来たい", "たぶん来ると思う", "まだわからない"]},
            {"id": "comment", "text": "お気づきの点など（任意）", "type": "text", "required": False,
             "placeholder": "施術についてご感想があればご記入ください"},
        ]
    },
    "縮毛矯正専門": {
        "questions": [
            {"id": "menu", "text": "今回のメニューは何でしたか？", "type": "multi", "required": True,
             "options": ["縮毛矯正", "パーマ", "デジタルパーマ", "ストレートパーマ", "カット+縮毛矯正", "その他"]},
            {"id": "satisfaction", "text": "仕上がりはいかがでしたか？", "type": "single", "required": True,
             "options": ["とても満足", "満足", "普通", "少し不満", "不満"]},
            {"id": "good_points", "text": "特によかった点を教えてください", "type": "multi", "required": False,
             "options": ["自然なストレート感", "ダメージの少なさ", "持ちの良さ", "カウンセリングの丁寧さ",
                         "仕上がりの手触り", "スタッフの技術", "施術時間", "薬剤の種類・説明"]},
            {"id": "revisit", "text": "また来店したいと思いますか？", "type": "single", "required": True,
             "options": ["ぜひまた来たい", "たぶん来ると思う", "まだわからない"]},
            {"id": "comment", "text": "お気づきの点など（任意）", "type": "text", "required": False,
             "placeholder": "仕上がりやお気づきの点があればご記入ください"},
        ]
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

    # カスタム設定があれば questions の menu / good_points / placeholder を上書き
    import json as _json
    custom_q = shop_dict.get("custom_questions")
    if custom_q:
        try:
            cq = _json.loads(custom_q)
            if "questions" in opts:
                opts = _json.loads(_json.dumps(opts))  # deep copy
                for q in opts["questions"]:
                    if q["id"] == "menu" and cq.get("menu_options"):
                        q["options"] = cq["menu_options"]
                    elif q["id"] == "good_points" and cq.get("good_points_options"):
                        q["options"] = cq["good_points_options"]
                    elif q["id"] == "comment" and cq.get("comment_placeholder"):
                        q["placeholder"] = cq["comment_placeholder"]
            if cq.get("survey_subtitle"):
                shop_dict["survey_subtitle"] = cq["survey_subtitle"]
        except Exception:
            pass

    # questions形式のサロンタイプから選択肢を抽出
    menu_options = []
    good_points_options = []
    comment_placeholder = ""
    if "questions" in opts:
        for q in opts["questions"]:
            if q["id"] == "menu":
                menu_options = q.get("options", [])
            elif q["id"] == "good_points":
                good_points_options = q.get("options", [])
            elif q["id"] == "comment":
                comment_placeholder = q.get("placeholder", "")

    return render_template(
        "survey.html",
        shop=shop_dict,
        coupon=dict(coupon) if coupon else None,
        survey_options=opts,
        menu_options=menu_options,
        good_points_options=good_points_options,
        comment_placeholder=comment_placeholder,
    )


@app.route("/shop/demo")
def shop_demo():
    if not request.args.get("demo"):
        return redirect(url_for("shop_demo", demo="1", business_type=request.args.get("business_type", "総合サロン")))
    b_type = request.args.get("business_type", "総合サロン")
    demo_names = {
        "総合サロン":    "サンプル総合サロン",
        "女性専用サロン": "サンプル女性専用サロン",
        "メンズサロン":  "サンプルメンズサロン",
        "ヘッドスパ専門": "サンプルヘッドスパ専門店",
        "縮毛矯正専門":  "サンプル縮毛矯正専門店",
        "美容院":       "サンプル美容院",
    }
    shop_dict = {
        "slug": "demo",
        "name": demo_names.get(b_type, f"サンプル{b_type}"),
        "review_url": "https://google.com",
        "business_type": b_type,
    }
    opts = SURVEY_OPTIONS.get(b_type, SURVEY_OPTIONS["default"])
    menu_options = []
    good_points_options = []
    comment_placeholder = ""
    if "questions" in opts:
        for q in opts["questions"]:
            if q["id"] == "menu":
                menu_options = q.get("options", [])
            elif q["id"] == "good_points":
                good_points_options = q.get("options", [])
            elif q["id"] == "comment":
                comment_placeholder = q.get("placeholder", "")
    return render_template(
        "survey.html",
        shop=shop_dict,
        coupon=None,
        survey_options=opts,
        menu_options=menu_options,
        good_points_options=good_points_options,
        comment_placeholder=comment_placeholder,
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
    comment = (data.get("comment") or "").strip()

    submitted_at = datetime.now().strftime("%Y年%m月%d日 %H:%M")

    # 業種に応じてアンケート項目を取得（DBにない場合は送信値を使用）
    shop_dict = dict(shop)
    b_type = shop_dict.get("business_type") or data.get("business_type") or "default"
    opts = SURVEY_OPTIONS.get(b_type, SURVEY_OPTIONS["default"])

    # サロンタイプ：選択式アンケートのデータ読み取り
    SALON_TYPES = {"美容院", "総合サロン", "女性専用サロン", "メンズサロン", "ヘッドスパ専門", "縮毛矯正専門"}
    if b_type in SALON_TYPES:
        menu         = (data.get("menu")         or "").strip()
        satisfaction = (data.get("satisfaction")  or "").strip()
        good_points  = (data.get("good_points")   or "").strip()
        revisit      = (data.get("revisit")        or "").strip()
        sat_map = {"とても満足": 5, "満足": 4, "普通": 3, "少し不満": 2, "不満": 1}
        rating  = sat_map.get(satisfaction, 3)
        rating2 = rating3 = rating4 = rating5 = 0
        answer6 = menu
        answer7 = good_points
    else:
        menu = satisfaction = good_points = revisit = ""
        rating  = data.get("rating")
        rating2 = data.get("rating2")
        rating3 = data.get("rating3")
        rating4 = data.get("rating4")
        rating5 = data.get("rating5")
        answer6 = (data.get("answer6") or "").strip()
        answer7 = (data.get("answer7") or "").strip()

    # ===== AI（OpenRouter）によるレビュー下書きの生成 =====
    ai_draft = ""
    # Googleガイドラインに準拠し、すべての評価（星1〜5）に対して下書きを作成する用意をするが、
    # 批判的な内容が含まれる場合も誠実で自然なトーンでのフィードバックになるようAIに指示する。
    try:
        import requests
        
        # Check if any rating is 1 or 2
        has_low_rating = any(r and int(r) <= 2 for r in [rating, rating2, rating3, rating4, rating5])

        system_prompt = f"""あなたはGoogleマップの口コミを書くリアルな一般客です。あなたは「{shop['name']}（業種: {dict(shop).get('business_type') or '不明'}）」を訪れたお客様として、口コミを作成してください。

【文字数】以下の文字数パターンからランダムに1つ選んで生成すること：
- 60〜120文字（80%の確率）
- 140〜180文字（20%の確率）
選んだ範囲内に必ず収めること。

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
            
        if b_type in SALON_TYPES:
            system_prompt = "あなたはGoogleマップの口コミを書く一般のお客様です。指示に従って自然な口コミを作成してください。"

            _SURVEY = (
                f"【アンケート回答】\n"
                f"- 今回のメニュー: {menu}\n"
                f"- 仕上がりの満足度: {satisfaction}\n"
                f"- よかった点: {good_points}\n"
                f"- また来店したいか: {revisit}\n"
                f"- コメント: {comment}\n\n"
            )
            _COMMON_RULES = (
                "【人物像】以下からランダムに1つ選び、その人物として書く：\n"
                "- 初めて予約した一見客\n"
                "- 何度も通っている常連\n"
                "- 友人に勧められて来た\n"
                "- SNSで見つけて気になっていた\n"
                "- 前の美容室をやめてここに乗り換えた\n"
                "- 記念日・誕生日のご褒美で来た\n"
                "- 引っ越してきて近所で探していた\n"
                "- 長年悩んでいた髪の悩みを相談しに来た\n\n"
                "【トーン】以下からランダムに1つ選ぶ：\n"
                "- 素っ気ない・淡白（短く事実だけ）\n"
                "- 普通の感想（特別感なし）\n"
                "- 比較系（「前の美容室と違って」「今まで行った中で」）\n"
                "- ストーリー系（来店経緯から結果まで流れで書く）\n"
                "- 友人に話すような口語体\n"
                "- 疑問→解決系（「不安だったけど」「迷ったけど」）\n\n"
                "【文体】以下からランダムに1つ選ぶ：\n"
                "- 丁寧な敬語（〜です・〜ます調）\n"
                "- やや砕けた口語（〜でした、〜だった混在）\n"
                "- 絵文字を1〜2個使う（✂️💇‍♀️✨など）\n"
                "- 感嘆符なし・落ち着いたトーン\n\n"
                "【書き出しパターン】以下から毎回異なるものを選ぶ：\n"
                "「友人に勧められて」「ずっと気になってたサロンで」「前の美容室に不満があって」「SNSで見つけて予約してみた」「引っ越してきてから探してて」「髪のダメージがひどくて」「記念日のご褒美に」「3回目の来店です」「半年ぶりに来たら」「初めての縮毛矯正で不安だったけど」「カラーで失敗続きだったので」など\n\n"
                "【フォーカス】以下からランダムに1つ選ぶ（具体的なエピソードで）：\n"
                "- 担当者がカウンセリングで言ってくれた一言\n"
                "- 施術後に触った髪の質感・手触りの変化\n"
                "- 仕上がりを鏡で見た瞬間の感想\n"
                "- 翌朝のスタイリングのしやすさ\n"
                "- 価格に対して得したか率直な感想\n"
                "- 前の美容室でダメだったことがここではどうだったか\n\n"
                "【絶対禁止ワード】\n"
                "「居心地」「清潔感」「技術が高い」「技術が優れている」「丁寧な接客」「また利用したいと思います」「おすすめです」「癒されました」「リラックスできました」「スタッフの方々」「総合的に」「コスパが良い」「コスパ最高」「また伺います」「大満足」「素晴らしい」「しっかり汲み取って」\n\n"
                "【禁止事項】\n"
                "- アンケートの項目名をそのまま文章に使わない\n"
                "- 「とても」「非常に」「本当に」を連続使用しない\n"
                "- 定型文っぽい締め方をしない\n"
                "- 綺麗にまとめすぎない。途中で話題が変わったり、雑さがあってよい\n"
                "- 「ご協力」「アンケート」などのワードを絶対に含めない\n\n"
                "口コミ文章のみを出力してください。"
            )

            # ── 業種別 few-shot 例 ──────────────────────────────
            _FEW_SHOTS_MENS = (
                "【実際の口コミ例（メンズサロン）】\n"
                "以下はメンズサロンに実際に投稿された口コミです。このリアルさ・自然さを参考にしてください。\n\n"
                "例1: ほぼ担当者マンツーマンで寄り添ってくれますよ。エレベーターでは2Fにとまらないので入り口入ってすぐ左の階段を使用してください。\n"
                "例2: 対応いただいた方(男性)の人柄が良かったです！あと、キャッシュレスで支払いができたのが1番良かったです！(現金支払のお店が多いので助かります！)ただ、場所が分かりずらいので-1😅\n"
                "例3: 初めて行かせていただきました！すごい上手に切ってくれてコミュ力もめちゃくちゃ高くて話しやすかった！みんな明るくて声が出ていたから話しかけやすかった。また切りに行かせていただきます♪\n"
                "例4: 美容院に行くのが苦手でしたが、ここのお店は行くのが楽しくセットの仕方等も詳しく教えてくれるのでめっちゃ良いです！今後も通います！\n"
                "例5: 雑談も楽しくさせて頂いたのであっという間の施術だったので家からも近いですしこれからも通おうと思います！\n\n"
            )
            _FEW_SHOTS_HEADSPA = (
                "【実際の口コミ例（ヘッドスパ）】\n"
                "以下はヘッドスパサロンに実際に投稿された口コミです。このリアルさ・自然さを参考にしてください。\n\n"
                "例1: 月に一度お世話になっていて癒しの美容室です♡技術はもちろん最高ですが、ヘッドスパでのリンパマッサージは、経過観察の耳の調子が良くなることを体験でき感動ですー！\n"
                "例2: 希望のヘアスタイルに沿うようにカウンセリングもしっかりしてくれます。ヘッドスパやトリートメントも毎回してもらうほどお気に入りです。\n"
                "例3: アシスタントの子のヘッドスパも上手で感動しました！またお願いします✨️\n\n"
            )
            _FEW_SHOTS_SHRINK = (
                "【実際の口コミ例（縮毛矯正・パーマ）】\n"
                "以下は縮毛矯正・パーマサロンに実際に投稿された口コミです。このリアルさ・自然さを参考にしてください。\n\n"
                "例1: 縮毛矯正、ドライカットをしていただいています。これがないと生きていけない！ばっちり綺麗なストレートになりました！ツヤツヤ、さらさらの髪質改善です！！\n"
                "例2: チャットgptで大阪エリアの美容室調べたら出てきたので来ました！初めての縮毛矯正でしたので、分からないところや質問など全て答えていただき安心して施術受けることができました！終わったあと自分の髪の毛の悩みが全部無くなって凄く嬉しかったです！\n"
                "例3: チルヘアさんには、もう2年くらいヘアケアしていただいています！隣の人間国宝さんの店長さんのくせ活カットのおかげで、自分のくせ毛は個性！と愛でられるようになりました＾＾\n"
                "例4: 憧れのサラサラストレートになれて嬉しいです🎶ホームケアの方法も細かく教えてくれるのでさらに艶々になれるように頑張ります🙂‍↕️\n"
                "例5: 個室の雰囲気もよく、マンツーマンで他の人の目もきにならず、落ち着いた環境の中施術していただけて良かったです。朝起きて鏡を見て、爆発してないサラサラな毛に感動しました！\n"
                "例6: 縮毛矯正とカットで利用させていただいています。髪質に合わせた施術や髪型を提案してくださるため、助かっています。\n\n"
            )
            _FEW_SHOTS_WOMEN = (
                "【実際の口コミ例（女性専用サロン）】\n"
                "以下は女性専用美容室に実際に投稿された口コミです。このリアルさ・自然さを参考にしてください。\n\n"
                "例1: ヘアカラーをするのが初めてでどんな色が似合うのか分からなかったけど、イメージに合う色にしてくれて、気に入っています！息子もカットするのを怖がりますが、帰る時にはまた、次もカットしてもらうと言っていました。\n"
                "例2: いつもざっくりとした希望しかお伝えしていないのですが、扱いやすいカラーやカットにしていただいています！次回予約特典のシャンプーも嬉しいです。美容師さんたちの落ち着いた雰囲気も、私は過ごしやすいです。\n"
                "例3: 初めて来てブリーチなしのWカラーをしてもらったのですが、理想の色になったのですごく満足してます！また、次カラーする時も行きます！\n"
                "例4: 葉山さんにカラーと前髪カットをしてもらいました！カラーの要望を伝えるといくつか提案してくれ、その中から選びました！髪の毛の負担が減るように染めてくれて色もいい感じ🫶前髪は、私の理想に近づくように私の意見を何度も聞きながら切ってくれました✂︎\n"
                "例5: いつもMiranさんにお願いしています✨️成人式だったので1ヶ月前からハイトーン準備して、前日に希望カラーにして貰いました🩶細かいところはいつもお任せですが、毎回ドンピシャの仕上がりですし、何より色落ちが綺麗で最高です✨️\n"
                "例6: 理想のカラーになるし、髪染めるたびにかわいい髪色を更新してくれて色落ちも最高にかわいい🩷店内の雰囲気もめっちゃくちゃいい！！こんなおんなじ美容室に通ってるのははじめてかも！？レベルで最高の仕上がりでした！\n\n"
            )
            _FEW_SHOTS_GENERAL = (
                "【実際の口コミ例】\n"
                "以下は実際にGoogleマップに投稿された口コミです。このリアルさ・自然さを参考にして生成してください。\n\n"
                "例1: かおり先生の技術の素晴らしさレイヤーカットして頂き１０歳若くなりました。デメリットはかおり先生の予約取りにくいところです。\n"
                "例2: ヘアカラーをするのが初めてでどんな色が似合うのか分からなかったけど、イメージに合う色にしてくれて、気に入っています！息子もカットするのを怖がりますが、帰る時にはまた、次もカットしてもらうと言っていました。\n"
                "例3: トリートメントとカラーをやってもらいました。カラーもドンピシャの色に染めてもらえました！自分の髪がこんなトゥルントゥルンになるんだと驚きと感動で、お店を出たあともしばらく触ってました笑\n"
                "例4: アーチ梅田店で部分カラーとエクステ取り外しとブローをしていただきました。早くて色もいい感じで、3000円以上のメニュー選択したので取り外しは無料で嬉しかったです。\n"
                "例5: いつもざっくりとした希望しかお伝えしていないのですが、扱いやすいカラーやカットにしていただいています！次回予約特典のシャンプーも嬉しいです。美容師さんたちの落ち着いた雰囲気も、私は過ごしやすいです。\n"
                "例6: 初めて来てブリーチなしのWカラーをしてもらったのですが、理想の色になったのですごく満足してます！また、次カラーする時も行きます！\n"
                "例7: 毎回丁寧なカウンセリングで素敵な髪型にしてくださいます☺️✂️過去一短く切ったショートカットもお任せしましたが、気に入っていますし周りからも好評でした🤍遅い時間まで対応してくださるので仕事終わりなど本当にありがたいです！\n"
                "例8: 葉山さんにカラーと前髪カットをしてもらいました！カラーの要望を伝えるといくつか提案してくれ、その中から選びました！髪の毛の負担が減るように染めてくれて色もいい感じ🫶前髪は、私の理想に近づくように私の意見を何度も聞きながら切ってくれました✂︎\n"
                "例9: 理想のカラーになるし、髪染めるたびにかわいい髪色を更新してくれて色落ちも最高にかわいい🩷店内の雰囲気もめっちゃくちゃいい！！こんなおんなじ美容室に通ってるのははじめてかも！？レベルで最高の仕上がりでした！\n"
                "例10: 初白髪ぼかしカラーしました。今までの白髪染めを除去することから始まり、これからどう変わっていくのかとても楽しみです！丁寧な説明と何でも言いやすい雰囲気に安心しました。\n\n"
            )

            if b_type == "メンズサロン":
                user_prompt = (
                    "あなたはメンズ美容室のお客様（男性）です。以下のアンケート回答をもとに、"
                    "Googleマップに投稿する自然な口コミ文章を日本語で書いてください。\n\n"
                    + _SURVEY +
                    "【口コミ作成のルール】\n"
                    "- 以下の文字数パターンからランダムに1つ選んで生成すること：\n"
                    "  ・60〜120文字（80%の確率）\n"
                    "  ・140〜180文字（20%の確率）\n"
                    "  選んだ範囲内に必ず収めること\n"
                    "- 男性目線の自然な口コミ文にする（フェード、刈り上げ、ビジネスヘアなどの言葉を適宜使う）\n"
                    "- 満足度が「普通」以下の場合は星4以下を示唆する表現にする\n"
                    "- 「また来たい」場合は再来店の意思を含める\n"
                    "- 箇条書きや記号は使わない\n"
                    "- 冒頭に「サンプル」などのテスト感のある言葉は使わない\n\n"
                    + _COMMON_RULES + _FEW_SHOTS_MENS
                )
            elif b_type == "ヘッドスパ専門":
                user_prompt = (
                    "あなたはヘッドスパ・トリートメント専門サロンのお客様です。以下のアンケート回答をもとに、"
                    "Googleマップに投稿する自然な口コミ文章を日本語で書いてください。\n\n"
                    + _SURVEY +
                    "【口コミ作成のルール】\n"
                    "- 以下の文字数パターンからランダムに1つ選んで生成すること：\n"
                    "  ・60〜120文字（80%の確率）\n"
                    "  ・140〜180文字（20%の確率）\n"
                    "  選んだ範囲内に必ず収めること\n"
                    "- ヘッドスパならではの体験（頭皮マッサージ、血行促進、ほぐされる感覚など）を具体的に表現する\n"
                    "- 満足度が「普通」以下の場合は星4以下を示唆する表現にする\n"
                    "- 「また来たい」場合は再来店の意思を含める\n"
                    "- 箇条書きや記号は使わない\n"
                    "- 冒頭に「サンプル」などのテスト感のある言葉は使わない\n\n"
                    + _COMMON_RULES + _FEW_SHOTS_HEADSPA
                )
            elif b_type == "縮毛矯正専門":
                user_prompt = (
                    "あなたは縮毛矯正・パーマ専門サロンのお客様です。以下のアンケート回答をもとに、"
                    "Googleマップに投稿する自然な口コミ文章を日本語で書いてください。\n\n"
                    + _SURVEY +
                    "【口コミ作成のルール】\n"
                    "- 以下の文字数パターンからランダムに1つ選んで生成すること：\n"
                    "  ・60〜120文字（80%の確率）\n"
                    "  ・140〜180文字（20%の確率）\n"
                    "  選んだ範囲内に必ず収めること\n"
                    "- 縮毛矯正・パーマ専門の技術的な言葉を使う（ダメージレス、自然なストレート、持ちが良い、薬剤選定、根元からしっかりなど）\n"
                    "- 満足度が「普通」以下の場合は星4以下を示唆する表現にする\n"
                    "- 「また来たい」場合は再来店の意思を含める\n"
                    "- 箇条書きや記号は使わない\n"
                    "- 冒頭に「サンプル」などのテスト感のある言葉は使わない\n\n"
                    + _COMMON_RULES + _FEW_SHOTS_SHRINK
                )
            elif b_type == "女性専用サロン":
                user_prompt = (
                    "あなたは女性専用美容室のお客様（女性）です。以下のアンケート回答をもとに、"
                    "Googleマップに投稿する自然な口コミ文章を日本語で書いてください。\n\n"
                    + _SURVEY +
                    "【口コミ作成のルール】\n"
                    "- 以下の文字数パターンからランダムに1つ選んで生成すること：\n"
                    "  ・60〜120文字（80%の確率）\n"
                    "  ・140〜180文字（20%の確率）\n"
                    "  選んだ範囲内に必ず収めること\n"
                    "- 女性専用ならではのプライベート感・落ち着いた雰囲気・丁寧さを強調する\n"
                    "- 美容室らしい言葉を使う（グレージュ、インナーカラー、縮毛矯正など）\n"
                    "- 満足度が「普通」以下の場合は星4以下を示唆する表現にする\n"
                    "- 「また来たい」場合は再来店の意思を含める\n"
                    "- 箇条書きや記号は使わない\n"
                    "- 冒頭に「サンプル」などのテスト感のある言葉は使わない\n\n"
                    + _COMMON_RULES + _FEW_SHOTS_WOMEN
                )
            else:
                # 美容院 / 総合サロン 共通
                user_prompt = (
                    "あなたは美容室のお客様です。以下のアンケート回答をもとに、"
                    "Googleマップに投稿する自然な口コミ文章を日本語で書いてください。\n\n"
                    + _SURVEY +
                    "【口コミ作成のルール】\n"
                    "- 以下の文字数パターンからランダムに1つ選んで生成すること：\n"
                    "  ・60〜120文字（80%の確率）\n"
                    "  ・140〜180文字（20%の確率）\n"
                    "  選んだ範囲内に必ず収めること\n"
                    "- 美容室らしい言葉を使う（グレージュ、インナーカラー、縮毛矯正など）\n"
                    "- 満足度が「普通」以下の場合は星4以下を示唆する表現にする\n"
                    "- 「また来たい」場合は再来店の意思を含める\n"
                    "- 箇条書きや記号は使わない\n"
                    "- 冒頭に「サンプル」などのテスト感のある言葉は使わない\n\n"
                    + _COMMON_RULES + _FEW_SHOTS_GENERAL
                )
        else:
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
                # 句点の後に自然な改行を挿入（連続する改行は除去）
                import re
                ai_draft = re.sub(r'。(?!\n)', '。\n', ai_draft)
    except Exception as e:
        print("AI draft generation failed:", e)

    conn = get_db()
    if DB_TYPE == "postgresql":
        conn.execute(
            "INSERT INTO feedbacks (shop_id, rating, rating2, rating3, rating4, rating5, comment, ai_draft, salon_type, menu, satisfaction, good_points, revisit) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (shop["id"], rating, rating2, rating3, rating4, rating5, comment, ai_draft, b_type, menu, satisfaction, good_points, revisit),
        )
    else:
        conn.execute(
            "INSERT INTO feedbacks (shop_id, rating, rating2, rating3, rating4, rating5, comment, ai_draft, salon_type, menu, satisfaction, good_points, revisit) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (shop["id"], rating, rating2, rating3, rating4, rating5, comment, ai_draft, b_type, menu, satisfaction, good_points, revisit),
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


@app.route("/shop/survey-settings", methods=["GET", "POST"])
@login_required
def survey_settings():
    """アンケートカスタマイズ設定ページ"""
    import json as _json
    conn = get_db()
    shops = conn.execute(
        "SELECT id, name, business_type, custom_questions FROM shops ORDER BY id"
    ).fetchall()
    conn.close()
    if not shops:
        flash("店舗が登録されていません。先に店舗を登録してください。", "warning")
        return redirect(url_for("qr_form"))

    # 対象ショップ（GETパラメータ or 最初の店舗）
    shop_id = request.args.get("shop_id", type=int) or shops[0]["id"]
    shop = next((s for s in shops if s["id"] == shop_id), shops[0])
    shop_dict = dict(shop)

    if request.method == "POST":
        shop_id_post = request.form.get("shop_id", type=int) or shop_dict["id"]
        menu_raw = request.form.get("menu_options", "")
        gp_raw   = request.form.get("good_points_options", "")
        placeholder = request.form.get("comment_placeholder", "").strip()
        subtitle    = request.form.get("survey_subtitle", "").strip()
        menu_options = [m.strip() for m in menu_raw.split(",") if m.strip()]
        gp_options   = [g.strip() for g in gp_raw.split(",") if g.strip()]
        cq = {
            "menu_options": menu_options,
            "good_points_options": gp_options,
            "comment_placeholder": placeholder,
            "survey_subtitle": subtitle,
        }
        conn2 = get_db()
        conn2.execute(
            "UPDATE shops SET custom_questions = ? WHERE id = ? AND user_id = ?",
            (_json.dumps(cq, ensure_ascii=False), shop_id_post, current_user.id),
        )
        conn2.commit()
        conn2.close()
        flash("アンケート設定を保存しました", "success")
        return redirect(url_for("survey_settings", shop_id=shop_id_post))

    # 現在のカスタム設定を読み込み
    b_type = shop_dict.get("business_type") or "default"
    base_opts = SURVEY_OPTIONS.get(b_type, SURVEY_OPTIONS["default"])
    menu_options = []
    gp_options   = []
    placeholder  = ""
    subtitle     = ""
    if "questions" in base_opts:
        for q in base_opts["questions"]:
            if q["id"] == "menu":
                menu_options = q.get("options", [])
            elif q["id"] == "good_points":
                gp_options = q.get("options", [])
            elif q["id"] == "comment":
                placeholder = q.get("placeholder", "")
    else:
        # 旧形式（q6/q7キー）からデフォルト選択肢を取得
        if "q6" in base_opts and isinstance(base_opts["q6"], dict):
            menu_options = base_opts["q6"].get("choices", [])
        if "q7" in base_opts and isinstance(base_opts["q7"], dict):
            gp_options = base_opts["q7"].get("choices", [])
    cq_json = shop_dict.get("custom_questions")
    if cq_json:
        try:
            cq = _json.loads(cq_json)
            if cq.get("menu_options"):
                menu_options = cq["menu_options"]
            if cq.get("good_points_options"):
                gp_options = cq["good_points_options"]
            if cq.get("comment_placeholder"):
                placeholder = cq["comment_placeholder"]
            if cq.get("survey_subtitle"):
                subtitle = cq["survey_subtitle"]
        except Exception:
            pass

    return render_template(
        "survey_settings.html",
        shops=[dict(s) for s in shops],
        shop=shop_dict,
        menu_options=menu_options,
        gp_options=gp_options,
        placeholder=placeholder,
        subtitle=subtitle,
    )


_TEMPLATE_DEFAULTS = {
    "template_1": "仕上がりはいかがでしたでしょうか？\nもしよろしければ、こちらからご感想をいただけますと嬉しいです。\nいただいたお声は、スタッフ一同とても励みになります。\n{url}",
    "template_2": "本日はありがとうございました。\nヘアスタイル、その後扱いやすさはいかがでしょうか？\nよろしければ、こちらから率直なご感想を教えていただけると嬉しいです。\n「ここが良かった」「こういう人におすすめ」など一言でも大歓迎です。\n{url}",
    "template_3": "本日は〇〇が担当させていただき、ありがとうございました。 今回のカラー／カットの仕上がりにご満足いただけていましたら、とても嬉しいです。 よろしければ、こちらから感想をいただけますでしょうか？ お客様のお声は、担当スタッフにとって大きな励みになります。 {url}",
    "template_4": "本日はご来店ありがとうございました。\nよろしければ、こちらからご感想をいただけると嬉しいです。\n1分ほどでご投稿いただけます。\n{url}",
    "template_5": "本日は初めてのご来店ありがとうございました。\n数ある美容室の中から当店を選んでいただき嬉しく思っております。\n本日の施術はいかがでしたか？\nもしよろしければこちらからご感想をお聞かせいただけると嬉しいです。\n{url}",
}


def _get_survey_stats_and_responses(user_id, period, satisfaction_filter):
    """アンケート統計＆回答一覧を取得する共通ヘルパー"""
    import datetime
    from collections import Counter
    now_dt = datetime.datetime.now()
    month_start = now_dt.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

    if period == "today":
        date_filter = now_dt.replace(hour=0, minute=0, second=0, microsecond=0)
    elif period == "week":
        date_filter = now_dt - datetime.timedelta(days=7)
    elif period == "all":
        date_filter = datetime.datetime(2000, 1, 1)
    else:
        date_filter = month_start

    conn = get_db()
    monthly_responses_raw = conn.execute(
        """
        SELECT satisfaction, revisit, menu
        FROM feedbacks f
        JOIN shops s ON s.id = f.shop_id
        WHERE s.user_id = ? AND salon_type IS NOT NULL
        AND date(f.submitted_at) >= date('now', 'start of month')
        """,
        (user_id,),
    ).fetchall()
    total_count = len(monthly_responses_raw)
    very_satisfied = sum(1 for r in monthly_responses_raw if r["satisfaction"] == "とても満足")
    revisit_yes    = sum(1 for r in monthly_responses_raw if r["revisit"] == "ぜひまた来たい")
    satisfaction_rate = round(very_satisfied / total_count * 100) if total_count > 0 else 0
    revisit_rate      = round(revisit_yes    / total_count * 100) if total_count > 0 else 0
    all_menus = []
    for r in monthly_responses_raw:
        if r["menu"]:
            all_menus.extend([m.strip() for m in r["menu"].split(",")])
    popular_menu = Counter(all_menus).most_common(1)[0][0] if all_menus else "データなし"

    base_query = """
        SELECT f.id, f.submitted_at, f.salon_type, f.menu, f.satisfaction,
               f.good_points, f.revisit, f.comment, f.ai_draft, s.name AS shop_name
        FROM feedbacks f
        JOIN shops s ON s.id = f.shop_id
        WHERE s.user_id = ? AND salon_type IS NOT NULL
        AND f.submitted_at >= ?
    """
    params = [user_id, date_filter.strftime("%Y-%m-%d %H:%M:%S")]
    if satisfaction_filter != "all":
        if satisfaction_filter == "普通以下":
            base_query += " AND satisfaction IN ('普通', '少し不満', '不満')"
        else:
            base_query += " AND satisfaction = ?"
            params.append(satisfaction_filter)
    base_query += " ORDER BY f.submitted_at DESC LIMIT 200"
    survey_responses = [dict(r) for r in conn.execute(base_query, params).fetchall()]
    for r in survey_responses:
        sa = r.get("submitted_at")
        if sa and isinstance(sa, str):
            r["submitted_at"] = sa[:16]
    conn.close()
    return dict(
        survey_responses=survey_responses,
        total_count=total_count,
        satisfaction_rate=satisfaction_rate,
        revisit_rate=revisit_rate,
        popular_menu=popular_menu,
        period=period,
        satisfaction_filter=satisfaction_filter,
    )


@app.route("/dashboard/answers")
@payment_required
def dashboard_answers():
    period = request.args.get("period", "month")
    satisfaction_filter = request.args.get("satisfaction_filter", "all")
    ctx = _get_survey_stats_and_responses(current_user.id, period, satisfaction_filter)
    return render_template("answers.html", **ctx)


@app.route("/dashboard/report")
@payment_required
def dashboard_report():
    ctx = _get_survey_stats_and_responses(current_user.id, "month", "all")
    return render_template("report.html", **ctx)


@app.route("/dashboard/template", methods=["GET", "POST"])
@login_required
def survey_template():
    """アンケート送信用テンプレートページ"""
    conn = get_db()
    shops = conn.execute(
        "SELECT id, name, slug FROM shops ORDER BY id"
    ).fetchall()

    if not shops:
        conn.close()
        flash("店舗が登録されていません。先に店舗を登録してください。", "warning")
        return redirect(url_for("qr_form"))

    # POST: AJAX保存
    if request.method == "POST":
        data = request.get_json(silent=True) or {}
        shop_id_post = data.get("shop_id", type(0)) if False else data.get("shop_id")
        channel = data.get("channel", "")
        content = data.get("content", "")

        # 対象店舗が自分のものか確認
        shop_ids = [s["id"] for s in shops]
        if not shop_id_post or int(shop_id_post) not in shop_ids or channel not in _TEMPLATE_DEFAULTS:
            conn.close()
            return {"ok": False, "error": "invalid"}, 400

        if DB_TYPE == "postgresql":
            conn.execute(
                """INSERT INTO review_templates (store_id, channel, content, updated_at)
                   VALUES (%s, %s, %s, NOW())
                   ON CONFLICT (store_id, channel) DO UPDATE SET content = EXCLUDED.content, updated_at = NOW()""",
                (int(shop_id_post), channel, content),
            )
        else:
            conn.execute(
                """INSERT INTO review_templates (store_id, channel, content, updated_at)
                   VALUES (?, ?, ?, CURRENT_TIMESTAMP)
                   ON CONFLICT (store_id, channel) DO UPDATE SET content = excluded.content, updated_at = CURRENT_TIMESTAMP""",
                (int(shop_id_post), channel, content),
            )
        conn.commit()
        conn.close()
        return {"ok": True}

    # GET
    shop_id = request.args.get("shop_id", type=int) or shops[0]["id"]
    shop = next((s for s in shops if s["id"] == shop_id), shops[0])
    shop_dict = dict(shop)
    survey_url = f"https://gugulabo.com/shop/{shop_dict['slug']}"

    # DBからテンプレート取得。なければデフォルトをINSERT
    rows = conn.execute(
        "SELECT channel, content FROM review_templates WHERE store_id = ?",
        (shop_dict["id"],),
    ).fetchall()
    templates = {r["channel"]: r["content"] for r in rows}

    for channel, default_content in _TEMPLATE_DEFAULTS.items():
        if channel not in templates:
            templates[channel] = default_content
            if DB_TYPE == "postgresql":
                conn.execute(
                    "INSERT INTO review_templates (store_id, channel, content) VALUES (%s, %s, %s) ON CONFLICT DO NOTHING",
                    (shop_dict["id"], channel, default_content),
                )
            else:
                conn.execute(
                    "INSERT OR IGNORE INTO review_templates (store_id, channel, content) VALUES (?, ?, ?)",
                    (shop_dict["id"], channel, default_content),
                )
    conn.commit()
    conn.close()

    return render_template(
        "survey_template.html",
        shops=[dict(s) for s in shops],
        shop=shop_dict,
        survey_url=survey_url,
        templates=templates,
    )


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
        return jsonify({"url": checkout_session.url})
    except Exception as e:
        return jsonify({"error": str(e)}), 400


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


@app.route("/legal")
def legal():
    return render_template("legal.html")


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


@app.route("/shops")
@payment_required
def shops_page():
    return render_template("shops.html")


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
            # place_id から #lrd 形式の口コミURLを生成（失敗時は writereview にフォールバック）
            review_url = get_review_url_from_place_id(place_id)
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
        "SELECT id, name, review_url, slug, place_id, line_user_id, status, business_type, created_at, main_menus, strengths, target_customers, nearest_station, reservation_method, price_range FROM shops WHERE user_id = ? ORDER BY created_at DESC",
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

    # place_id があれば #lrd 形式の口コミURLを自動生成（place_id優先・既存URLも上書き）
    if place_id:
        review_url = get_review_url_from_place_id(place_id)

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
    place_id           = (data.get("place_id")           or "").strip() or None
    line_user_id       = (data.get("line_user_id")       or "").strip() or None
    main_menus         = (data.get("main_menus")         or "").strip() or None
    strengths          = (data.get("strengths")          or "").strip() or None
    target_customers   = (data.get("target_customers")   or "").strip() or None
    nearest_station    = (data.get("nearest_station")    or "").strip() or None
    reservation_method = (data.get("reservation_method") or "").strip() or None
    price_range        = (data.get("price_range")        or "").strip() or None

    conn.execute(
        "UPDATE shops SET place_id = ?, line_user_id = ?, main_menus = ?, strengths = ?, target_customers = ?, nearest_station = ?, reservation_method = ?, price_range = ? WHERE id = ?",
        (place_id, line_user_id, main_menus, strengths, target_customers, nearest_station, reservation_method, price_range, shop_id)
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
    try:
        rating = int(request.form.get("rating") or 0)
    except ValueError:
        rating = 0
    if not review_text:
        return jsonify({"success": False, "error": "レビュー内容を入力してください。"})
    try:
        response_text = generate_review_response(review_text, business_type, rating)
        return jsonify({"success": True, "response": response_text})
    except Exception as e:
        return jsonify({"success": False, "error": f"AI返答生成に失敗しました: {str(e)}"})


# ----- GBP最新情報 -----

def _get_current_gbp_shop(conn, user_id):
    """セッションまたはデフォルトで現在のshopを取得"""
    shop_id = session.get("gbp_shop_id")
    shop = None
    if shop_id:
        shop = conn.execute(
            "SELECT * FROM shops WHERE id = ? AND user_id = ?",
            (shop_id, user_id),
        ).fetchone()
    if not shop:
        shop = conn.execute(
            "SELECT * FROM shops WHERE user_id = ? ORDER BY created_at DESC",
            (user_id,),
        ).fetchone()
    if shop:
        session["gbp_shop_id"] = shop["id"]
    return shop


@app.route("/gbp-posts/setup", methods=["GET", "POST"])
@payment_required
def gbp_posts_setup():
    """サロン詳細情報設定画面"""
    conn = get_db()
    shop_id = request.args.get("shop_id") or request.form.get("shop_id") or session.get("gbp_shop_id")
    if shop_id:
        shop = conn.execute(
            "SELECT * FROM shops WHERE id = ? AND user_id = ?",
            (shop_id, current_user.id),
        ).fetchone()
    else:
        shop = conn.execute(
            "SELECT * FROM shops WHERE user_id = ? ORDER BY created_at DESC",
            (current_user.id,),
        ).fetchone()

    if not shop:
        conn.close()
        return redirect(url_for("index"))

    session["gbp_shop_id"] = shop["id"]

    if request.method == "POST":
        vals = (
            (request.form.get("main_menus")         or "").strip(),
            (request.form.get("strengths")           or "").strip(),
            (request.form.get("target_customers")    or "").strip(),
            (request.form.get("nearest_station")     or "").strip(),
            (request.form.get("reservation_method")  or "").strip(),
            (request.form.get("price_range")         or "").strip(),
            shop["id"],
        )
        if DB_TYPE == "postgresql":
            conn.execute(
                "UPDATE shops SET main_menus=%s, strengths=%s, target_customers=%s,"
                " nearest_station=%s, reservation_method=%s, price_range=%s,"
                " profile_completed=1 WHERE id=%s", vals,
            )
        else:
            conn.execute(
                "UPDATE shops SET main_menus=?, strengths=?, target_customers=?,"
                " nearest_station=?, reservation_method=?, price_range=?,"
                " profile_completed=1 WHERE id=?", vals,
            )
        conn.commit()
        conn.close()
        return redirect(url_for("gbp_posts_page"))

    style_images = conn.execute(
        "SELECT id, filename, extracted_text FROM post_style_images WHERE shop_id = ? ORDER BY uploaded_at DESC",
        (shop["id"],),
    ).fetchall()
    conn.close()
    return render_template("gbp_setup.html", shop=shop, style_images=style_images)


@app.route("/gbp-posts")
@payment_required
def gbp_posts_page():
    """GBP最新情報メイン画面"""
    # ?shop_id= が渡された場合はセッションを更新
    if request.args.get("shop_id"):
        session["gbp_shop_id"] = int(request.args.get("shop_id"))
    conn = get_db()
    shop = _get_current_gbp_shop(conn, current_user.id)

    if not shop:
        conn.close()
        return redirect(url_for("index"))

    if not shop["profile_completed"] and not request.args.get("skip"):
        conn.close()
        return redirect(url_for("gbp_posts_setup"))

    posts = conn.execute(
        "SELECT * FROM gbp_posts WHERE shop_id = ? AND status = 'draft' ORDER BY created_at DESC",
        (shop["id"],),
    ).fetchall()
    style_total = conn.execute(
        "SELECT COUNT(*) as cnt FROM post_style_images WHERE shop_id = ?",
        (shop["id"],),
    ).fetchone()["cnt"]
    style_extracted = conn.execute(
        "SELECT COUNT(*) as cnt FROM post_style_images WHERE shop_id = ? AND extracted_text IS NOT NULL",
        (shop["id"],),
    ).fetchone()["cnt"]
    shops = conn.execute(
        "SELECT id, name FROM shops WHERE user_id = ? ORDER BY created_at DESC",
        (current_user.id,),
    ).fetchall()
    conn.close()
    return render_template("gbp_posts.html", shop=shop, posts=posts,
                           style_total=style_total, style_extracted=style_extracted, shops=shops)


@app.route("/gbp-posts/upload-style", methods=["POST"])
@payment_required
def gbp_posts_upload_style():
    """過去のGBP投稿画像をアップロードしてテキスト抽出"""
    conn = get_db()
    shop = _get_current_gbp_shop(conn, current_user.id)
    if not shop:
        conn.close()
        return jsonify({"success": False, "error": "店舗が見つかりません。"})

    os.makedirs(_STYLE_UPLOAD_DIR, exist_ok=True)

    files = request.files.getlist("images")
    if not files or all(f.filename == "" for f in files):
        conn.close()
        return jsonify({"success": False, "error": "ファイルが選択されていません。"})

    ALLOWED_EXT = {"jpg", "jpeg", "png"}
    MAX_SIZE = 10 * 1024 * 1024
    saved = 0

    for f in files[:5]:
        ext = f.filename.rsplit(".", 1)[-1].lower() if "." in f.filename else ""
        if ext not in ALLOWED_EXT:
            continue
        data = f.read()
        if len(data) > MAX_SIZE:
            continue

        ts = int(datetime.now().timestamp() * 1000)
        save_ext = "png" if ext == "png" else "jpg"
        filename = f"{shop['id']}_{ts}_{saved}.{save_ext}"
        save_path = os.path.join(_STYLE_UPLOAD_DIR, filename)
        with open(save_path, "wb") as fp:
            fp.write(data)

        try:
            extracted = call_openrouter_vision(
                save_path,
                "この画像はGoogleビジネスプロフィールの投稿スクリーンショットです。"
                "投稿本文のテキストをそのまま抽出してください。テキスト以外は出力しないでください。",
            )
        except Exception as e:
            app.logger.warning(f"Vision extraction failed: {e}")
            extracted = None

        if extracted:
            if DB_TYPE == "postgresql":
                conn.execute(
                    "INSERT INTO post_style_images (shop_id, filename, extracted_text) VALUES (%s, %s, %s)",
                    (shop["id"], filename, extracted),
                )
            else:
                conn.execute(
                    "INSERT INTO post_style_images (shop_id, filename, extracted_text) VALUES (?, ?, ?)",
                    (shop["id"], filename, extracted),
                )
            saved += 1
        else:
            # テキスト抽出失敗でもファイルは保存済みなので記録だけしておく
            if DB_TYPE == "postgresql":
                conn.execute(
                    "INSERT INTO post_style_images (shop_id, filename, extracted_text) VALUES (%s, %s, %s)",
                    (shop["id"], filename, None),
                )
            else:
                conn.execute(
                    "INSERT INTO post_style_images (shop_id, filename, extracted_text) VALUES (?, ?, ?)",
                    (shop["id"], filename, None),
                )

    conn.commit()
    conn.close()

    if saved == 0:
        return jsonify({
            "success": False,
            "error": "画像のテキスト抽出に失敗しました。GBP投稿のスクリーンショット（文字が含まれる画像）をアップロードしてください。",
            "count": 0,
        })
    return jsonify({"success": True, "count": saved})


@app.route("/gbp-posts/style-images/<int:image_id>", methods=["DELETE"])
@payment_required
def gbp_posts_delete_style_image(image_id):
    """スタイル画像を1枚削除"""
    conn = get_db()
    row = conn.execute(
        "SELECT psi.id, psi.filename FROM post_style_images psi"
        " JOIN shops s ON s.id = psi.shop_id"
        " WHERE psi.id = ? AND s.user_id = ?",
        (image_id, current_user.id),
    ).fetchone()
    if not row:
        conn.close()
        return jsonify({"success": False, "error": "画像が見つかりません。"})
    file_path = os.path.join(_STYLE_UPLOAD_DIR, row["filename"])
    if os.path.exists(file_path):
        os.remove(file_path)
    conn.execute("DELETE FROM post_style_images WHERE id = ?", (image_id,))
    conn.commit()
    conn.close()
    return jsonify({"success": True})


@app.route("/gbp-posts/generate", methods=["POST"])
@payment_required
def gbp_posts_generate():
    """統合生成ルート：テーマ×参照ソースの組み合わせで1パターン生成"""
    conn = get_db()
    shop = _get_current_gbp_shop(conn, current_user.id)
    if not shop:
        conn.close()
        return jsonify({"error": "店舗が見つかりません。"})

    data = request.get_json() or {}
    themes        = data.get("themes") or []
    hint          = (data.get("hint") or "").strip()
    use_shop_info = bool(data.get("use_shop_info"))
    use_style     = bool(data.get("use_style"))
    use_ai        = bool(data.get("use_ai"))

    if not use_shop_info and not use_style and not use_ai:
        conn.close()
        return jsonify({"error": "参照ソースを1つ以上選択してください。"})

    style_texts = []
    if use_style:
        rows = conn.execute(
            "SELECT extracted_text FROM post_style_images"
            " WHERE shop_id = ? AND extracted_text IS NOT NULL"
            " ORDER BY uploaded_at DESC",
            (shop["id"],),
        ).fetchall()
        style_texts = [r["extracted_text"] for r in rows]
        if not style_texts:
            conn.close()
            return jsonify({"error": "過去の投稿が未登録です。設定画面からスクショを追加してください。"})
    conn.close()

    themes_str = "、".join(themes) if themes else "（指定なし）"
    current_month = datetime.now().month

    parts = [
        "あなたは美容室のGoogleビジネスプロフィール「最新情報」の投稿文を書くプロです。",
        "以下の情報をもとに、集客につながる投稿文を1つ生成してください。",
        "",
        f"【投稿テーマ】\n{themes_str}",
    ]
    if hint:
        parts.append(f"\n【補足メモ】\n{hint}")
    if use_shop_info:
        parts.append(
            f"\n【サロン情報】\n"
            f"店名：{shop['name']}\n"
            f"メインメニュー：{shop['main_menus'] or '未設定'}\n"
            f"強み・こだわり：{shop['strengths'] or '未設定'}\n"
            f"ターゲット客層：{shop['target_customers'] or '未設定'}\n"
            f"アクセス：{shop['nearest_station'] or '未設定'}\n"
            f"予約方法：{shop['reservation_method'] or '未設定'}\n"
            f"価格帯：{shop['price_range'] or '未設定'}"
        )
    if use_style and style_texts:
        combined = "\n---\n".join(style_texts)
        parts.append(f"\n【過去の投稿例（文体・トーンを必ず踏襲すること）】\n{combined}")
    if use_ai and not use_shop_info and not use_style:
        parts.append("\nサロンの一般的な特徴を想定し、自由に魅力的な投稿文を生成してください。")
    parts.append(f"""
【生成条件】
- 文字数：150〜300文字
- 語尾は丁寧語（です・ます調）
- 絵文字を適度に使用（1〜3個程度）
- ハッシュタグは末尾に2〜3個
- MEOを意識したキーワードを自然に含める
- 現在の月（{current_month}月）の季節感を盛り込む
- 過去の投稿例がある場合はその文体・トーン・構成を必ず踏襲する

【出力形式】
投稿文のテキストのみを出力してください。
JSONや```などの余分な記号は一切含めないでください。""")

    prompt = "\n".join(parts)
    try:
        result = call_openrouter_text(prompt)
        return jsonify({"pattern": result.strip()})
    except Exception as e:
        app.logger.error(f"GBP generate error: {e}")
        return jsonify({"error": f"生成に失敗しました: {str(e)}"})


@app.route("/gbp-posts/save", methods=["POST"])
@payment_required
def gbp_posts_save():
    """GBP投稿文を下書き保存（新規INSERT / 既存UPDATE）"""
    data = request.get_json() or {}
    shop_id = data.get("shop_id")
    content = (data.get("content") or "").strip()
    mode    = (data.get("mode") or "auto").strip()
    post_id = data.get("post_id")  # 指定時はUPDATE

    if not shop_id or not content:
        return jsonify({"success": False, "error": "パラメータが不足しています。"})

    conn = get_db()
    shop = conn.execute(
        "SELECT id FROM shops WHERE id = ? AND user_id = ?",
        (shop_id, current_user.id),
    ).fetchone()
    if not shop:
        conn.close()
        return jsonify({"success": False, "error": "店舗が見つかりません。"})

    if post_id:
        conn.execute(
            "UPDATE gbp_posts SET content = ? WHERE id = ? AND shop_id = ?",
            (content, post_id, shop_id),
        )
        conn.commit()
        conn.close()
        return jsonify({"success": True, "post_id": post_id})

    if DB_TYPE == "postgresql":
        cur = conn.execute(
            "INSERT INTO gbp_posts (shop_id, content, mode, status) VALUES (%s, %s, %s, 'draft') RETURNING id",
            (shop_id, content, mode),
        )
        new_id = cur.fetchone()[0]
    else:
        cur = conn.execute(
            "INSERT INTO gbp_posts (shop_id, content, mode, status) VALUES (?, ?, ?, 'draft')",
            (shop_id, content, mode),
        )
        new_id = cur.lastrowid
    conn.commit()
    conn.close()
    return jsonify({"success": True, "post_id": new_id})


@app.route("/gbp-posts/<int:post_id>", methods=["DELETE"])
@payment_required
def gbp_posts_delete(post_id):
    """GBP下書きを削除"""
    conn = get_db()
    post = conn.execute(
        "SELECT gp.id FROM gbp_posts gp JOIN shops s ON s.id = gp.shop_id"
        " WHERE gp.id = ? AND s.user_id = ?",
        (post_id, current_user.id),
    ).fetchone()
    if not post:
        conn.close()
        return jsonify({"success": False, "error": "投稿が見つかりません。"})
    conn.execute("DELETE FROM gbp_posts WHERE id = ?", (post_id,))
    conn.commit()
    conn.close()
    return jsonify({"success": True})


@app.route("/gbp-posts/publish/<int:post_id>", methods=["POST"])
@payment_required
def gbp_posts_publish(post_id):
    """GBP投稿（GBP API承認後に実装）"""
    return jsonify({"message": "GBP API承認後に投稿機能が有効になります"})


# ----- 管理者: ユーザー管理 -----

@app.route("/admin")
@admin_required
def admin_users():
    """管理者向けユーザー管理画面"""
    conn = get_db()
    rows = conn.execute(
        "SELECT id, email, name, is_admin, created_at, plan, plan_expires_at FROM users ORDER BY id"
    ).fetchall()
    all_shops = conn.execute(
        "SELECT id, name, slug, user_id FROM shops ORDER BY id"
    ).fetchall()
    conn.close()

    # ユーザーごとに割り当て済み店舗をまとめる
    shop_map = {}
    for s in all_shops:
        uid = s["user_id"]
        if uid not in shop_map:
            shop_map[uid] = []
        shop_map[uid].append({"id": s["id"], "name": s["name"]})

    users = []
    for row in rows:
        d = dict(row)
        ca = d.get("created_at")
        if ca and hasattr(ca, "strftime"):
            d["created_at"] = ca.strftime("%Y-%m-%d %H:%M:%S")
        if not d.get("plan"):
            d["plan"] = "月額プラン"
        d["shops"] = shop_map.get(d["id"], [])
        users.append(d)

    shops_list = [dict(s) for s in all_shops]
    return render_template("admin.html", users=users, shops_list=shops_list)


@app.route("/admin/users/<int:user_id>/assign-shop", methods=["POST"])
@admin_required
def admin_assign_shop(user_id):
    """指定ユーザーに店舗を割り当てる"""
    shop_id = request.form.get("shop_id", type=int)
    if not shop_id:
        flash("店舗を選択してください。", "danger")
        return redirect(url_for("admin_users"))
    conn = get_db()
    conn.execute("UPDATE shops SET user_id = ? WHERE id = ?", (user_id, shop_id))
    conn.commit()
    shop = conn.execute("SELECT name FROM shops WHERE id = ?", (shop_id,)).fetchone()
    user = conn.execute("SELECT name FROM users WHERE id = ?", (user_id,)).fetchone()
    conn.close()
    flash(f"「{shop['name']}」を「{user['name']}」に割り当てました。", "success")
    return redirect(url_for("admin_users"))


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

                # 友だち追加時は案内メッセージを送信
                if event["type"] == "follow":
                    try:
                        from services.line_notify import send_line_message
                        send_line_message(
                            line_user_id,
                            "🎉 友だち追加ありがとうございます！\n\nGugulabo（গগラボ）です。\n\nLINE連携を完了するには、ブラウザに戻って「LINE連携を完了する」ボタンを押してください。\n\n連携完了後、毎週月曜日の朝9時にGoogleレビューの新着通知やMEOアドバイスをお届けします。"
                        )
                    except Exception as e:
                        print(f"follow message error: {e}", flush=True)
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
    conn = get_db()
    shop = conn.execute(
        "SELECT * FROM shops WHERE id = ? AND user_id = ?",
        (shop_id, current_user.id)
    ).fetchone()
    if not shop:
        return "店舗が見つかりません", 404
    line_add_url = os.environ.get("LINE_ADD_FRIEND_URL", "")
    return render_template("line_connect.html", shop=shop, line_add_url=line_add_url)


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

    # LINE連携完了メッセージを送信
    try:
        from services.line_notify import send_line_message
        send_line_message(
            pending['line_user_id'],
            "✅ LINE連携が完了しました！\n\nこれからGoogleレビューの新着通知や返答案、MEOアドバイスをお届けします。\n毎週月曜日の朝9時にレポートをお送りします。"
        )
    except Exception as e:
        print(f"LINE連携完了メッセージ送信エラー: {e}", flush=True)

    return jsonify({"success": True, "message": "LINE連携が完了しました！"})


@app.route('/threads/callback')
def threads_callback():
    """Threads OAuth コールバック（一時的・トークン取得用）"""
    code = request.args.get('code', '')
    return f"""
    <html><body>
    <h2>Threads OAuth Code</h2>
    <p>以下のcodeをコピーしてください：</p>
    <textarea rows="4" cols="80" onclick="this.select()">{code}</textarea>
    </body></html>
    """


if __name__ == "__main__":
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 5000)), debug=False)
