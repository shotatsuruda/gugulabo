"""
blueprints/reminder.py  ―  来店リマインダー Blueprint
"""
import sqlite3
import os
from datetime import date, datetime, timedelta

from flask import Blueprint, jsonify, request
from flask_login import login_required, current_user

reminder_bp = Blueprint("reminder", __name__, url_prefix="/reminder")

REMINDER_DB = os.path.join(os.path.dirname(os.path.dirname(__file__)), "reminder.db")


def _get_db():
    conn = sqlite3.connect(REMINDER_DB)
    conn.row_factory = sqlite3.Row
    return conn


def _store_id():
    return current_user.id


# ------------------------------------------------------------------ #
#  来店記録                                                            #
#  POST /reminder/visits                                               #
#  body: { customer_name, menu_name, visited_at(省略時=today) }        #
# ------------------------------------------------------------------ #

@reminder_bp.route("/visits", methods=["POST"])
@login_required
def record_visit():
    data = request.get_json(silent=True) or {}
    customer_name = (data.get("customer_name") or "").strip()
    menu_name = (data.get("menu_name") or "").strip()
    visited_at = (data.get("visited_at") or "").strip() or date.today().isoformat()

    if not customer_name or not menu_name:
        return jsonify({"error": "customer_name と menu_name は必須です"}), 400

    store_id = _store_id()
    db = _get_db()
    try:
        row = db.execute(
            "SELECT id FROM customers WHERE store_id = ? AND name = ?",
            (store_id, customer_name),
        ).fetchone()
        if row:
            customer_id = row["id"]
        else:
            cur = db.execute(
                "INSERT INTO customers (store_id, name) VALUES (?, ?)",
                (store_id, customer_name),
            )
            customer_id = cur.lastrowid

        db.execute(
            "INSERT INTO visits (customer_id, visited_at, menu_name) VALUES (?, ?, ?)",
            (customer_id, visited_at, menu_name),
        )
        db.commit()
    finally:
        db.close()

    return jsonify({"ok": True, "customer_id": customer_id}), 201


# ------------------------------------------------------------------ #
#  アラート一覧                                                        #
#  GET /reminder/alerts                                                #
# ------------------------------------------------------------------ #

@reminder_bp.route("/alerts", methods=["GET"])
@login_required
def get_alerts():
    store_id = _store_id()
    today = date.today().isoformat()

    db = _get_db()
    try:
        rows = db.execute(
            """
            SELECT
                c.name            AS customer_name,
                v.menu_name,
                MAX(v.visited_at) AS last_visited,
                mt.cycle_days,
                mt.message_body
            FROM visits v
            JOIN customers c        ON c.id       = v.customer_id
            JOIN message_templates mt
                                    ON mt.store_id = c.store_id
                                   AND mt.menu_name = v.menu_name
            WHERE c.store_id = ?
            GROUP BY c.id, v.menu_name, mt.id
            HAVING DATE(last_visited, '+' || mt.cycle_days || ' days') <= ?
            ORDER BY last_visited ASC
            """,
            (store_id, today),
        ).fetchall()
    finally:
        db.close()

    result = []
    for r in rows:
        last_dt = datetime.strptime(r["last_visited"], "%Y-%m-%d").date()
        due_date = last_dt + timedelta(days=r["cycle_days"])
        days_overdue = (date.today() - due_date).days
        result.append({
            "customer_name": r["customer_name"],
            "menu_name":     r["menu_name"],
            "last_visited":  r["last_visited"],
            "due_date":      due_date.isoformat(),
            "days_overdue":  days_overdue,
            "message":       r["message_body"].replace("{name}", r["customer_name"]),
        })

    return jsonify(result)


# ------------------------------------------------------------------ #
#  テンプレート管理                                                    #
#  GET /POST /reminder/templates                                       #
#  PUT /DELETE /reminder/templates/<id>                                #
# ------------------------------------------------------------------ #

@reminder_bp.route("/templates", methods=["GET"])
@login_required
def list_templates():
    store_id = _store_id()
    db = _get_db()
    try:
        rows = db.execute(
            "SELECT * FROM message_templates WHERE store_id = ? ORDER BY id",
            (store_id,),
        ).fetchall()
    finally:
        db.close()
    return jsonify([dict(r) for r in rows])


@reminder_bp.route("/templates", methods=["POST"])
@login_required
def create_template():
    data = request.get_json(silent=True) or {}
    menu_name    = (data.get("menu_name")    or "").strip()
    cycle_days   = data.get("cycle_days")
    message_body = (data.get("message_body") or "").strip()

    if not menu_name or cycle_days is None or not message_body:
        return jsonify({"error": "menu_name, cycle_days, message_body は必須です"}), 400

    store_id = _store_id()
    db = _get_db()
    try:
        cur = db.execute(
            "INSERT INTO message_templates (store_id, menu_name, cycle_days, message_body)"
            " VALUES (?, ?, ?, ?)",
            (store_id, menu_name, int(cycle_days), message_body),
        )
        db.commit()
        new_id = cur.lastrowid
    finally:
        db.close()

    return jsonify({"ok": True, "id": new_id}), 201


@reminder_bp.route("/templates/<int:template_id>", methods=["PUT"])
@login_required
def update_template(template_id):
    store_id = _store_id()
    data = request.get_json(silent=True) or {}
    menu_name    = (data.get("menu_name")    or "").strip()
    cycle_days   = data.get("cycle_days")
    message_body = (data.get("message_body") or "").strip()

    if not menu_name or cycle_days is None or not message_body:
        return jsonify({"error": "menu_name, cycle_days, message_body は必須です"}), 400

    db = _get_db()
    try:
        if not db.execute(
            "SELECT id FROM message_templates WHERE id = ? AND store_id = ?",
            (template_id, store_id),
        ).fetchone():
            return jsonify({"error": "見つかりません"}), 404

        db.execute(
            "UPDATE message_templates"
            " SET menu_name = ?, cycle_days = ?, message_body = ?"
            " WHERE id = ?",
            (menu_name, int(cycle_days), message_body, template_id),
        )
        db.commit()
    finally:
        db.close()

    return jsonify({"ok": True})


@reminder_bp.route("/templates/<int:template_id>", methods=["DELETE"])
@login_required
def delete_template(template_id):
    store_id = _store_id()
    db = _get_db()
    try:
        if not db.execute(
            "SELECT id FROM message_templates WHERE id = ? AND store_id = ?",
            (template_id, store_id),
        ).fetchone():
            return jsonify({"error": "見つかりません"}), 404

        db.execute("DELETE FROM message_templates WHERE id = ?", (template_id,))
        db.commit()
    finally:
        db.close()

    return jsonify({"ok": True})


# ------------------------------------------------------------------ #
#  顧客一覧（来店履歴つき）                                            #
#  GET /reminder/customers                                             #
# ------------------------------------------------------------------ #

@reminder_bp.route("/customers", methods=["GET"])
@login_required
def list_customers():
    store_id = _store_id()
    db = _get_db()
    try:
        customers = db.execute(
            "SELECT id, name, memo, created_at FROM customers"
            " WHERE store_id = ? ORDER BY name",
            (store_id,),
        ).fetchall()

        result = []
        for c in customers:
            visits = db.execute(
                "SELECT visited_at, menu_name FROM visits"
                " WHERE customer_id = ? ORDER BY visited_at DESC",
                (c["id"],),
            ).fetchall()
            result.append({
                "id":         c["id"],
                "name":       c["name"],
                "memo":       c["memo"],
                "created_at": c["created_at"],
                "visits":     [dict(v) for v in visits],
            })
    finally:
        db.close()

    return jsonify(result)
