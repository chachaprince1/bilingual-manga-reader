"""SQLite storage and non-destructive schema migrations."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

SCHEMA_VERSION = 7

SCHEMA = """
PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS volumes (
 id TEXT PRIMARY KEY,
 title TEXT NOT NULL,
 series TEXT NOT NULL,
 language TEXT NOT NULL CHECK(language IN ('jp','en')),
 source_path TEXT NOT NULL,
 page_count INTEGER NOT NULL,
 cover_path TEXT,
 mokuro_path TEXT,
 created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
 updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
 volume_label TEXT NOT NULL DEFAULT '',
 chapter_label TEXT NOT NULL DEFAULT '',
 source_kind TEXT NOT NULL DEFAULT 'referenced',
 thumbnail_path TEXT,
 available INTEGER NOT NULL DEFAULT 1,
 error TEXT NOT NULL DEFAULT '',
 origin_path TEXT NOT NULL DEFAULT '',
 import_signature TEXT NOT NULL DEFAULT '',
 content_type TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS pages (
 id TEXT PRIMARY KEY,
 volume_id TEXT NOT NULL REFERENCES volumes(id) ON DELETE CASCADE,
 ordinal INTEGER NOT NULL,
 path TEXT NOT NULL,
 width INTEGER,
 height INTEGER,
 fingerprint TEXT,
 fingerprint_left TEXT,
 fingerprint_right TEXT,
 chapter_label TEXT NOT NULL DEFAULT '',
 UNIQUE(volume_id, ordinal)
);
CREATE TABLE IF NOT EXISTS mappings (
 id TEXT PRIMARY KEY,
 series TEXT NOT NULL,
 jp_pages TEXT NOT NULL,
 en_pages TEXT NOT NULL,
 source TEXT NOT NULL,
 confidence REAL NOT NULL,
 manual INTEGER NOT NULL DEFAULT 0,
 created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS mapping_pages (
 mapping_id TEXT NOT NULL REFERENCES mappings(id) ON DELETE CASCADE,
 page_id TEXT NOT NULL REFERENCES pages(id) ON DELETE CASCADE,
 side TEXT NOT NULL CHECK(side IN ('jp','en')),
 position INTEGER NOT NULL,
 PRIMARY KEY(mapping_id, side, position)
);
CREATE TABLE IF NOT EXISTS progress (
 volume_id TEXT PRIMARY KEY REFERENCES volumes(id) ON DELETE CASCADE,
 page_id TEXT REFERENCES pages(id) ON DELETE SET NULL,
 updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS favorites (series TEXT PRIMARY KEY);
CREATE TABLE IF NOT EXISTS bookmarks (
 volume_id TEXT NOT NULL REFERENCES volumes(id) ON DELETE CASCADE,
 page_id TEXT NOT NULL REFERENCES pages(id) ON DELETE CASCADE,
 created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
 PRIMARY KEY(volume_id,page_id)
);
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS idx_volumes_series ON volumes(series, language);
CREATE INDEX IF NOT EXISTS idx_pages_volume ON pages(volume_id, ordinal);
CREATE INDEX IF NOT EXISTS idx_mapping_page ON mapping_pages(page_id, side);
CREATE INDEX IF NOT EXISTS idx_progress_recent ON progress(updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_bookmarks_recent ON bookmarks(created_at DESC);
"""


VOLUME_COLUMNS = {
    # SQLite only permits constant defaults in ALTER TABLE; existing rows are
    # backfilled immediately after this nullable migration column is added.
    "updated_at": "TEXT",
    "volume_label": "TEXT NOT NULL DEFAULT ''",
    "chapter_label": "TEXT NOT NULL DEFAULT ''",
    "source_kind": "TEXT NOT NULL DEFAULT 'referenced'",
    "thumbnail_path": "TEXT",
    "available": "INTEGER NOT NULL DEFAULT 1",
    "error": "TEXT NOT NULL DEFAULT ''",
    "origin_path": "TEXT NOT NULL DEFAULT ''",
    "import_signature": "TEXT NOT NULL DEFAULT ''",
    "content_type": "TEXT NOT NULL DEFAULT ''",
}

PAGE_COLUMNS = {
    "fingerprint": "TEXT",
    "fingerprint_left": "TEXT",
    "fingerprint_right": "TEXT",
    "chapter_label": "TEXT NOT NULL DEFAULT ''",
}


def _add_columns(db: sqlite3.Connection, table: str, definitions: dict[str, str]) -> None:
    columns = {row[1] for row in db.execute(f"PRAGMA table_info({table})")}
    for name, definition in definitions.items():
        if name not in columns:
            db.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")


def _backfill_mapping_pages(db: sqlite3.Connection) -> None:
    for mapping in db.execute("SELECT id,jp_pages,en_pages FROM mappings"):
        exists = db.execute(
            "SELECT 1 FROM mapping_pages WHERE mapping_id=? LIMIT 1", (mapping["id"],)
        ).fetchone()
        if exists:
            continue
        for side in ("jp", "en"):
            try:
                page_ids = json.loads(mapping[f"{side}_pages"])
            except (TypeError, json.JSONDecodeError):
                page_ids = []
            for position, page_id in enumerate(page_ids if isinstance(page_ids, list) else []):
                if db.execute("SELECT 1 FROM pages WHERE id=?", (page_id,)).fetchone():
                    db.execute(
                        "INSERT OR IGNORE INTO mapping_pages(mapping_id,page_id,side,position) VALUES(?,?,?,?)",
                        (mapping["id"], page_id, side, position),
                    )


def connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path, check_same_thread=False, timeout=30)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA foreign_keys=ON")
    db.execute("PRAGMA busy_timeout=30000")
    db.executescript(SCHEMA)
    _add_columns(db, "volumes", VOLUME_COLUMNS)
    _add_columns(db, "pages", PAGE_COLUMNS)
    db.execute("UPDATE volumes SET updated_at=COALESCE(updated_at,created_at,CURRENT_TIMESTAMP)")
    _backfill_mapping_pages(db)
    db.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
    db.commit()
    return db


def insert_mapping(
    db: sqlite3.Connection,
    mapping_id: str,
    series: str,
    jp_pages: list[str],
    en_pages: list[str],
    source: str,
    confidence: float,
    manual: bool = False,
) -> None:
    """Insert a grouped mapping and its exact page lookup rows."""
    db.execute(
        "INSERT INTO mappings(id,series,jp_pages,en_pages,source,confidence,manual) VALUES(?,?,?,?,?,?,?)",
        (
            mapping_id,
            series,
            json.dumps(jp_pages),
            json.dumps(en_pages),
            source,
            max(0.0, min(1.0, float(confidence))),
            int(manual),
        ),
    )
    for side, page_ids in (("jp", jp_pages), ("en", en_pages)):
        for position, page_id in enumerate(page_ids):
            db.execute(
                "INSERT INTO mapping_pages(mapping_id,page_id,side,position) VALUES(?,?,?,?)",
                (mapping_id, page_id, side, position),
            )


def delete_mappings_for_pages(db: sqlite3.Connection, page_ids: list[str]) -> None:
    if not page_ids:
        return
    ids: list[str] = []
    # Stay below SQLite's variable limit for very large series.
    for start in range(0, len(page_ids), 500):
        chunk = page_ids[start : start + 500]
        placeholders = ",".join("?" for _ in chunk)
        ids.extend(
            row[0]
            for row in db.execute(
                f"SELECT DISTINCT mapping_id FROM mapping_pages WHERE page_id IN ({placeholders})",
                chunk,
            )
        )
    ids = list(dict.fromkeys(ids))
    if ids:
        for start in range(0, len(ids), 500):
            chunk = ids[start : start + 500]
            db.execute(
                f"DELETE FROM mappings WHERE id IN ({','.join('?' for _ in chunk)})", chunk
            )
