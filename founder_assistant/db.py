"""SQLite storage. Raw inputs are never deleted; derived rows point back to their source.

Traceability chain:
  daily_reports -> purchases/sales (+items) -> ai_extractions -> messages -> media (file on disk)
"""
from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any, Iterable

SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS messages (
    id              INTEGER PRIMARY KEY,
    zalo_msg_id     TEXT UNIQUE,
    user_id         TEXT,
    kind            TEXT NOT NULL,          -- text | image | audio | file | other
    text            TEXT,                   -- original text as sent
    transcript      TEXT,                   -- STT output for audio
    received_at     TEXT NOT NULL,          -- ISO datetime, local tz
    event_json      TEXT,                   -- raw webhook payload
    status          TEXT NOT NULL DEFAULT 'received',
    error           TEXT,
    media_url       TEXT,                   -- kept so a failed download can be retried
    media_mime      TEXT,
    attempts        INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS media (
    id          INTEGER PRIMARY KEY,
    message_id  INTEGER NOT NULL REFERENCES messages(id),
    kind        TEXT NOT NULL,              -- image | audio | file
    source_url  TEXT,
    file_path   TEXT,
    sha256      TEXT,
    mime        TEXT,
    size        INTEGER,
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_media_sha ON media(sha256);

CREATE TABLE IF NOT EXISTS ai_extractions (
    id              INTEGER PRIMARY KEY,
    message_id      INTEGER NOT NULL REFERENCES messages(id),
    model           TEXT,
    prompt_version  TEXT,
    input_text      TEXT,
    ocr_text        TEXT,
    doc_type        TEXT,
    raw_json        TEXT NOT NULL,
    created_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS products (
    id              INTEGER PRIMARY KEY,
    name            TEXT NOT NULL,
    key             TEXT NOT NULL UNIQUE,
    category        TEXT,
    default_unit    TEXT,
    created_at      TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS product_aliases (
    alias_key   TEXT PRIMARY KEY,
    alias       TEXT NOT NULL,
    product_id  INTEGER NOT NULL REFERENCES products(id)
);

CREATE TABLE IF NOT EXISTS purchases (
    id              INTEGER PRIMARY KEY,
    extraction_id   INTEGER NOT NULL REFERENCES ai_extractions(id),
    message_id      INTEGER NOT NULL REFERENCES messages(id),
    doc_type        TEXT NOT NULL,
    purchase_date   TEXT NOT NULL,
    date_source     TEXT NOT NULL,          -- document | message
    supplier        TEXT,
    stated_total    REAL,                   -- total printed/said in source (FACT) or NULL
    computed_total  REAL,                   -- sum of known item amounts (computed)
    status          TEXT NOT NULL,          -- confirmed | needs_confirmation | cancelled | duplicate
    status_reason   TEXT,
    fingerprint     TEXT,
    duplicate_of    INTEGER,
    created_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS purchase_items (
    id                  INTEGER PRIMARY KEY,
    purchase_id         INTEGER NOT NULL REFERENCES purchases(id),
    product_id          INTEGER REFERENCES products(id),
    raw_name            TEXT NOT NULL,
    category            TEXT NOT NULL,      -- ingredient | packaging | gas | transport | platform_fee | salary | utilities | other
    quantity            REAL,
    unit_raw            TEXT,
    unit                TEXT,               -- canonical, NULL if unknown
    base_unit           TEXT,
    quantity_base       REAL,
    unit_price          REAL,               -- per `unit`
    unit_price_source   TEXT,               -- source | computed | NULL
    unit_price_base     REAL,               -- per base_unit
    amount              REAL,
    amount_source       TEXT,               -- source | computed | NULL
    evidence            TEXT,               -- exact snippet from source text/OCR
    status              TEXT NOT NULL,      -- ok | needs_confirmation
    status_reason       TEXT
);

CREATE TABLE IF NOT EXISTS sales (
    id                  INTEGER PRIMARY KEY,
    extraction_id       INTEGER NOT NULL REFERENCES ai_extractions(id),
    message_id          INTEGER NOT NULL REFERENCES messages(id),
    doc_type            TEXT NOT NULL,
    sale_date           TEXT NOT NULL,
    date_source         TEXT NOT NULL,
    gross_revenue       REAL,               -- revenue stated in source, NULL if absent
    computed_items_total REAL,
    revenue             REAL,               -- value counted in reports (stated, else computed)
    revenue_source      TEXT,               -- source | computed
    bill_count          INTEGER,
    item_count          INTEGER,
    cash                REAL,
    bank_transfer       REAL,
    e_wallet            REAL,
    platform_fee        REAL,
    status              TEXT NOT NULL,
    status_reason       TEXT,
    fingerprint         TEXT,
    duplicate_of        INTEGER,
    created_at          TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sale_items (
    id              INTEGER PRIMARY KEY,
    sale_id         INTEGER NOT NULL REFERENCES sales(id),
    raw_name        TEXT NOT NULL,
    item_key        TEXT NOT NULL,
    quantity        REAL,
    unit_price      REAL,
    unit_price_source TEXT,
    amount          REAL,
    amount_source   TEXT,
    evidence        TEXT
);

CREATE TABLE IF NOT EXISTS price_history (
    id                  INTEGER PRIMARY KEY,
    product_id          INTEGER NOT NULL REFERENCES products(id),
    price_date          TEXT NOT NULL,
    base_unit           TEXT NOT NULL,
    unit_price_base     REAL NOT NULL,
    quantity_base       REAL,
    supplier            TEXT,
    source              TEXT NOT NULL,      -- purchase | price_list
    purchase_item_id    INTEGER UNIQUE REFERENCES purchase_items(id),
    extraction_id       INTEGER NOT NULL REFERENCES ai_extractions(id),
    evidence            TEXT,
    created_at          TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_price_product ON price_history(product_id, base_unit, price_date);

CREATE TABLE IF NOT EXISTS alerts (
    id          INTEGER PRIMARY KEY,
    alert_date  TEXT NOT NULL,
    severity    TEXT NOT NULL,              -- critical | warning | info
    code        TEXT NOT NULL,
    message     TEXT NOT NULL,
    message_id  INTEGER REFERENCES messages(id),
    ref_table   TEXT,
    ref_id      INTEGER,
    resolved    INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS daily_reports (
    id              INTEGER PRIMARY KEY,
    report_date     TEXT NOT NULL,
    generated_at    TEXT NOT NULL,
    text            TEXT NOT NULL,
    xlsx_path       TEXT,
    data_json       TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS kv (
    key         TEXT PRIMARY KEY,
    value       TEXT,
    updated_at  TEXT
);
"""


class DB:
    def __init__(self, path: Path | str):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self._migrate()

    def _migrate(self) -> None:
        """Additive migrations for databases created by earlier versions (never drops data)."""
        cols = {r["name"] for r in self.conn.execute("PRAGMA table_info(messages)")}
        for name, ddl in (("media_url", "TEXT"), ("media_mime", "TEXT"), ("attempts", "INTEGER NOT NULL DEFAULT 1")):
            if name not in cols:
                self.conn.execute(f"ALTER TABLE messages ADD COLUMN {name} {ddl}")
        self.conn.commit()

    def execute(self, sql: str, params: Iterable[Any] = ()) -> sqlite3.Cursor:
        return self.conn.execute(sql, tuple(params))

    def insert(self, table: str, row: dict[str, Any]) -> int:
        cols = ", ".join(row)
        qs = ", ".join("?" for _ in row)
        cur = self.conn.execute(f"INSERT INTO {table} ({cols}) VALUES ({qs})", tuple(row.values()))
        return int(cur.lastrowid)

    def update(self, table: str, row_id: int, values: dict[str, Any]) -> None:
        sets = ", ".join(f"{k} = ?" for k in values)
        self.conn.execute(f"UPDATE {table} SET {sets} WHERE id = ?", (*values.values(), row_id))

    def one(self, sql: str, params: Iterable[Any] = ()) -> sqlite3.Row | None:
        return self.conn.execute(sql, tuple(params)).fetchone()

    def all(self, sql: str, params: Iterable[Any] = ()) -> list[sqlite3.Row]:
        return self.conn.execute(sql, tuple(params)).fetchall()

    def commit(self) -> None:
        self.conn.commit()

    def kv_get(self, key: str) -> str | None:
        r = self.one("SELECT value FROM kv WHERE key = ?", (key,))
        return r["value"] if r else None

    def kv_set(self, key: str, value: str, now: str) -> None:
        self.execute(
            "INSERT INTO kv(key, value, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
            (key, value, now),
        )
        self.commit()
