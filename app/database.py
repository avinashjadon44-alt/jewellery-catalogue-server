"""
SQLite storage for the server-side jewellery catalogue.

Rows are never hard-deleted. Deleting sets is_deleted=1 (a "tombstone") so
that every desktop client learns about the deletion on its next sync instead
of re-uploading the item.
"""
from contextlib import contextmanager
import os
from pathlib import Path
import sqlite3
from typing import Optional

BASE_DIR = Path(__file__).resolve().parent.parent

# Where the database and the uploaded images live. On a host with a persistent
# disk (e.g. Render), set JEWELLERY_DATA_DIR to the disk's mount path. When it
# is not set, the server keeps using ./data and ./uploads next to the code.
_DATA_ENV = os.environ.get("JEWELLERY_DATA_DIR", "").strip()
if _DATA_ENV:
    DB_PATH = Path(_DATA_ENV) / "catalogue.db"
    UPLOADS_DIR = Path(_DATA_ENV) / "uploads"
else:
    DB_PATH = BASE_DIR / "data" / "catalogue.db"
    UPLOADS_DIR = BASE_DIR / "uploads"


@contextmanager
def connect():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db():
    with connect() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS jewellery (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                code TEXT NOT NULL UNIQUE,
                category TEXT NOT NULL,
                price REAL NOT NULL DEFAULT 0,
                description TEXT NOT NULL DEFAULT '',
                image_filename TEXT NOT NULL,
                image_filename_right TEXT,
                scale REAL NOT NULL DEFAULT 0.30,
                x_offset INTEGER NOT NULL DEFAULT 0,
                y_offset INTEGER NOT NULL DEFAULT 28,
                is_deleted INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        # Databases created before tombstones existed: add the column in place.
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(jewellery)")}
        if "is_deleted" not in cols:
            conn.execute(
                "ALTER TABLE jewellery ADD COLUMN is_deleted INTEGER NOT NULL DEFAULT 0"
            )


def list_all(include_deleted: bool = False):
    sql = "SELECT * FROM jewellery"
    if not include_deleted:
        sql += " WHERE is_deleted = 0"
    sql += " ORDER BY category, name"
    with connect() as conn:
        return [dict(r) for r in conn.execute(sql).fetchall()]


def get_by_code(code: str) -> Optional[dict]:
    """Returns the row even if it is a tombstone."""
    with connect() as conn:
        row = conn.execute("SELECT * FROM jewellery WHERE code = ?", (code,)).fetchone()
    return dict(row) if row else None


def add(data: dict) -> None:
    ts = data.get("updated_at")  # client-supplied time keeps both sides equal after sync
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO jewellery
            (name, code, category, price, description, image_filename,
             image_filename_right, scale, x_offset, y_offset,
             is_deleted, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0,
                    COALESCE(?, CURRENT_TIMESTAMP), COALESCE(?, CURRENT_TIMESTAMP))
            """,
            (
                data["name"], data["code"], data["category"], data["price"],
                data["description"], data["image_filename"],
                data.get("image_filename_right"), data["scale"],
                data["x_offset"], data["y_offset"], ts, ts,
            ),
        )


def update(code: str, data: dict) -> None:
    """Update a row. Also un-deletes it (a newer edit beats an older delete)."""
    with connect() as conn:
        conn.execute(
            """
            UPDATE jewellery SET
                name=?, category=?, price=?, description=?,
                image_filename=?, image_filename_right=?,
                scale=?, x_offset=?, y_offset=?,
                is_deleted=0, updated_at=COALESCE(?, CURRENT_TIMESTAMP)
            WHERE code=?
            """,
            (
                data["name"], data["category"], data["price"], data["description"],
                data["image_filename"], data.get("image_filename_right"),
                data["scale"], data["x_offset"], data["y_offset"],
                data.get("updated_at"), code,
            ),
        )


def soft_delete(code: str, updated_at: Optional[str] = None) -> None:
    with connect() as conn:
        conn.execute(
            "UPDATE jewellery SET is_deleted=1, "
            "updated_at=COALESCE(?, CURRENT_TIMESTAMP) WHERE code=?",
            (updated_at, code),
        )