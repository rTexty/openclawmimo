#!/usr/bin/env python3
"""Lenochka Web Dashboard — read-only Flask UI for CRM data."""

import sqlite3
from pathlib import Path
from flask import Flask, render_template, jsonify, request

app = Flask(__name__)

DB_PATH = (
    Path(__file__).resolve().parent.parent / "lenochka-memory" / "db" / "lenochka.db"
)


def get_db():
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    return conn


@app.route("/")
def index():
    return render_template("index.html")


if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0", port=5000)
