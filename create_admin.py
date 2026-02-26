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
import sqlite3
import bcrypt

DATABASE = os.path.join(os.path.dirname(__file__), "review_system.db")

EMAIL = "admin@gugulabo.com"
PASSWORD = "admin1234"
NAME = "管理者"


def main():
    conn = sqlite3.connect(DATABASE)
    conn.row_factory = sqlite3.Row

    # 既存管理者チェック
    existing = conn.execute("SELECT id FROM users WHERE email = ?", (EMAIL,)).fetchone()
    if existing:
        admin_id = existing["id"]
        print(f"管理者アカウントはすでに存在します（id={admin_id}）。")
    else:
        # パスワードハッシュ化
        password_hash = bcrypt.hashpw(PASSWORD.encode(), bcrypt.gensalt()).decode()

        cursor = conn.execute(
            "INSERT INTO users (email, password_hash, name, is_admin) VALUES (?, ?, ?, 1)",
            (EMAIL, password_hash, NAME),
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
