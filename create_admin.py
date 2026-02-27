"""
管理者アカウントを作成するスクリプト。

使い方:
    python create_admin.py

作成されるアカウント:
    email   : admin@gugulabo.com
    password: admin1234
    name    : 管理者
    is_admin: True

初回ログイン後にパスワードを変更することを推奨します。
また、既存の店舗データを管理者アカウントに紐づけます。
"""

import os
import bcrypt
from dotenv import load_dotenv

load_dotenv()

DATABASE_URL = os.environ.get("DATABASE_URL")
DB_TYPE = "postgresql" if DATABASE_URL else "sqlite"

if DB_TYPE == "postgresql":
    import psycopg2
    import psycopg2.extras
else:
    import sqlite3
    DATABASE = os.path.join(os.path.dirname(__file__), "review_system.db")

EMAIL = "admin@gugulabo.com"
PASSWORD = "admin1234"
NAME = "管理者"


class _PgCursor:
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


def get_db():
    if DB_TYPE == "postgresql":
        conn = psycopg2.connect(DATABASE_URL)
        return _PgConn(conn)
    conn = sqlite3.connect(DATABASE)
    conn.row_factory = sqlite3.Row
    return conn


def main():
    conn = get_db()

    # 既存管理者チェック
    existing = conn.execute("SELECT id FROM users WHERE email = ?", (EMAIL,)).fetchone()
    if existing:
        admin_id = existing["id"]
        print(f"管理者アカウントはすでに存在します（id={admin_id}）。")
    else:
        # パスワードハッシュ化
        password_hash = bcrypt.hashpw(PASSWORD.encode(), bcrypt.gensalt()).decode()

        cursor = conn.execute(
            "INSERT INTO users (email, password_hash, name, is_admin) VALUES (?, ?, ?, ?)",
            (EMAIL, password_hash, NAME, 1),
        )
        admin_id = cursor.lastrowid
        conn.commit()
        print(f"管理者アカウントを作成しました（id={admin_id}）。")
        print(f"  email   : {EMAIL}")
        print(f"  password: {PASSWORD}")
        print("  ※ 初回ログイン後にパスワードを変更してください。")

    # user_id が未設定の既存店舗を管理者に紐づける
    rows = conn.execute("SELECT id, name FROM shops WHERE user_id IS NULL").fetchall()
    if rows:
        conn.execute("UPDATE shops SET user_id = ? WHERE user_id IS NULL", (admin_id,))
        conn.commit()
        print(f"\n既存の店舗 {len(rows)} 件を管理者アカウントに紐づけました:")
        for r in rows:
            print(f"  - [{r['id']}] {r['name']}")
    else:
        print("\n紐づけが必要な既存店舗はありませんでした。")

    conn.close()
    print("\n完了しました。")


if __name__ == "__main__":
    main()
