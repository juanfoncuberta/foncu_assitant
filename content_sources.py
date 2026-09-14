"""
Persistent registry of content sources for the LinkedIn/X content agent.
Stored in assistant.db (created automatically).

On first use, if the table is empty, seeds a curated list of default sources.
"""

import os
import sqlite3

DB_PATH = os.environ.get("DB_PATH", "assistant.db")

_DEFAULT_SOURCES = [
    ("One Useful Thing", "https://www.oneusefulthing.org/feed", "rss"),
    ("Latent Space", "https://www.latent.space/feed", "rss"),
    ("Xataka", "https://www.xataka.com/feedburner.xml", "rss"),
]


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS content_sources (
            id       INTEGER PRIMARY KEY AUTOINCREMENT,
            name     TEXT    NOT NULL UNIQUE,
            url      TEXT    NOT NULL,
            type     TEXT    NOT NULL,
            active   INTEGER NOT NULL DEFAULT 1,
            added_at TEXT    NOT NULL DEFAULT (datetime('now'))
        )
        """
    )
    conn.commit()
    _seed_defaults(conn)
    return conn


def _seed_defaults(conn: sqlite3.Connection) -> None:
    count = conn.execute("SELECT COUNT(*) FROM content_sources").fetchone()[0]
    if count == 0:
        conn.executemany(
            "INSERT INTO content_sources (name, url, type) VALUES (?, ?, ?)",
            _DEFAULT_SOURCES,
        )
        conn.commit()


def add_source(name: str, url: str, source_type: str) -> dict:
    with _conn() as conn:
        conn.execute(
            "INSERT INTO content_sources (name, url, type) VALUES (?, ?, ?)",
            (name, url, source_type),
        )
    return {"status": "ok", "name": name, "url": url, "type": source_type}


def list_active_sources() -> list[dict]:
    with _conn() as conn:
        rows = conn.execute(
            "SELECT id, name, url, type, added_at FROM content_sources WHERE active = 1 ORDER BY id"
        ).fetchall()
    return [
        {"id": r[0], "name": r[1], "url": r[2], "type": r[3], "added_at": r[4]}
        for r in rows
    ]


def deactivate_source(name: str) -> dict:
    with _conn() as conn:
        conn.execute(
            "UPDATE content_sources SET active = 0 WHERE name = ?",
            (name,),
        )
    return {"status": "ok", "deactivated": name}
