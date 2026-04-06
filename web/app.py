#!/usr/bin/env python3
"""Lenochka Web Dashboard — read-only Flask UI for CRM data."""

import sqlite3
import sys
from pathlib import Path
from flask import Flask, render_template, jsonify, request

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lenochka-memory"))
from config import DB_PATH  # noqa: E402

app = Flask(__name__)


def get_db():
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    return conn


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/stats")
def api_stats():
    conn = get_db()
    try:
        stats = {}
        for label, sql in [
            ("Контакты", "SELECT COUNT(*) as c FROM contacts"),
            ("Сообщения", "SELECT COUNT(*) as c FROM messages"),
            ("Memories", "SELECT COUNT(*) as c FROM memories"),
            (
                "Сделки",
                "SELECT COUNT(*) as c FROM deals WHERE stage NOT IN ('closed_won','closed_lost')",
            ),
            (
                "Задачи",
                "SELECT COUNT(*) as c FROM tasks WHERE status NOT IN ('done','cancelled')",
            ),
            ("Лиды", "SELECT COUNT(*) as c FROM leads"),
        ]:
            row = conn.execute(sql).fetchone()
            stats[label] = row["c"]
        return jsonify(stats)
    finally:
        conn.close()


@app.route("/api/messages")
def api_messages():
    limit = request.args.get("limit", 20, type=int)
    conn = get_db()
    try:
        rows = conn.execute(
            "SELECT m.text, m.from_user_id, m.sent_at, m.classification, "
            "c.name as contact_name "
            "FROM messages m "
            "LEFT JOIN chat_threads ct ON m.chat_thread_id = ct.id "
            "LEFT JOIN contacts c ON ct.contact_id = c.id "
            "ORDER BY m.sent_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return jsonify([dict(r) for r in rows])
    finally:
        conn.close()


@app.route("/api/deals")
def api_deals():
    conn = get_db()
    try:
        rows = conn.execute(
            "SELECT d.stage, d.amount, d.updated_at, c.name as contact_name "
            "FROM deals d "
            "LEFT JOIN contacts c ON d.contact_id = c.id "
            "WHERE d.stage NOT IN ('closed_won','closed_lost') "
            "ORDER BY d.updated_at DESC"
        ).fetchall()
        return jsonify([dict(r) for r in rows])
    finally:
        conn.close()


@app.route("/api/tasks")
def api_tasks():
    conn = get_db()
    try:
        rows = conn.execute(
            "SELECT t.description, t.due_at, t.status, t.priority, "
            "c.name as contact_name "
            "FROM tasks t "
            "LEFT JOIN contacts c ON t.contact_id = c.id "
            "WHERE t.status NOT IN ('done','cancelled') "
            "ORDER BY t.due_at ASC"
        ).fetchall()
        return jsonify([dict(r) for r in rows])
    finally:
        conn.close()


@app.route("/api/pipeline-health")
def api_pipeline_health():
    """Latency по стадиям за последние 24 часа."""
    conn = get_db()
    try:
        rows = conn.execute(
            "SELECT stage, status, "
            "COUNT(*) as total, "
            "AVG(duration_ms) as avg_ms, "
            "MAX(duration_ms) as max_ms, "
            "SUM(CASE WHEN status='error' THEN 1 ELSE 0 END) as errors "
            "FROM pipeline_runs "
            "WHERE created_at >= datetime('now', '-24 hours') "
            "GROUP BY stage, status "
            "ORDER BY stage"
        ).fetchall()
        return jsonify([dict(r) for r in rows])
    finally:
        conn.close()


@app.route("/api/failed-messages")
def api_failed_messages():
    limit = request.args.get("limit", 50, type=int)
    resolved = request.args.get("resolved", 0, type=int)
    conn = get_db()
    try:
        rows = conn.execute(
            "SELECT id, stage, error, retry_count, resolved, created_at "
            "FROM failed_messages WHERE resolved=? "
            "ORDER BY created_at DESC LIMIT ?",
            (resolved, limit),
        ).fetchall()
        return jsonify([dict(r) for r in rows])
    finally:
        conn.close()


@app.route("/api/failed-messages/<int:msg_id>/resolve", methods=["POST"])
def api_resolve_failed(msg_id: int):
    conn = get_db()
    try:
        conn.execute(
            "UPDATE failed_messages SET resolved=1 WHERE id=?", (msg_id,)
        )
        conn.commit()
        return jsonify({"status": "ok"})
    finally:
        conn.close()


if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0", port=5001)
