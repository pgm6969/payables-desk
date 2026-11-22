"""SQLite state store. Every state change is committed before the next step,
so a killed process resumes from the last committed step."""
import json
import os
import sqlite3
import time
from contextlib import contextmanager

DB_PATH = os.environ.get("PAYDESK_DB", os.path.join(os.path.dirname(__file__), "..", "data", "paydesk.db"))

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    run_id      TEXT PRIMARY KEY,          -- hash of the input batch: same batch -> same run
    request     TEXT NOT NULL,             -- raw request JSON
    status      TEXT NOT NULL,             -- running | done | stopped
    cost_limit  REAL NOT NULL,             -- credits
    time_limit_s INTEGER NOT NULL,
    started_at  REAL NOT NULL,
    finished_at REAL
);
CREATE TABLE IF NOT EXISTS invoices (
    invoice_id  TEXT PRIMARY KEY,          -- run_id:line_no
    run_id      TEXT NOT NULL,
    line_no     INTEGER NOT NULL,
    raw         TEXT NOT NULL,             -- the invoice as received
    vendor_id   TEXT,
    invoice_no_norm TEXT,
    amount_paise INTEGER,
    invoice_date TEXT,
    state       TEXT NOT NULL,             -- see pipeline.STATES
    outcome     TEXT,                      -- paid | held | cannot_verify | rejected
    reasons     TEXT NOT NULL DEFAULT '[]',-- JSON list of {code, text, source_ids}
    updated_at  REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_inv_dup ON invoices(vendor_id, invoice_no_norm);
CREATE INDEX IF NOT EXISTS ix_inv_amt ON invoices(vendor_id, amount_paise, invoice_date);

CREATE TABLE IF NOT EXISTS sources (
    source_id   INTEGER PRIMARY KEY AUTOINCREMENT,
    invoice_id  TEXT,
    fact        TEXT NOT NULL,             -- e.g. "payee legal name = Blue Dart Express Ltd"
    url         TEXT NOT NULL,             -- API endpoint or web page
    field       TEXT NOT NULL,             -- exact field / passage that states the fact
    value       TEXT NOT NULL,
    record_date TEXT,                      -- date of the record itself, if known
    retrieved_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS approvals (
    invoice_id  TEXT PRIMARY KEY,
    decision    TEXT,                      -- NULL = waiting | approved | rejected
    decided_by  TEXT,
    decided_at  REAL,
    note        TEXT,
    vendor_hash TEXT                       -- vendor record (bank, GSTIN, domain, status) when approved
);
CREATE TABLE IF NOT EXISTS drafts (
    invoice_id  TEXT PRIMARY KEY,          -- drafted, NEVER sent: a person checks and sends it
    to_email    TEXT,
    phone       TEXT,
    subject     TEXT NOT NULL,
    body        TEXT NOT NULL,
    created_at  REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS vendor_checks (
    vendor_key TEXT PRIMARY KEY,
    report     TEXT NOT NULL,
    checked_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS payments (
    invoice_id  TEXT PRIMARY KEY,          -- one payment row per invoice, ever
    idempotency_key TEXT NOT NULL UNIQUE,  -- written BEFORE the provider is called
    provider    TEXT NOT NULL,
    status      TEXT NOT NULL,             -- pending | paid | failed
    provider_ref TEXT,
    amount_paise INTEGER NOT NULL,
    account_masked TEXT,
    created_at  REAL NOT NULL,
    updated_at  REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS ledger (
    entry_id    INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id      TEXT,
    invoice_id  TEXT,
    service     TEXT NOT NULL,             -- crustdata | model | payment
    endpoint    TEXT NOT NULL,
    credits     REAL NOT NULL DEFAULT 0,
    inr         REAL NOT NULL DEFAULT 0,
    cached      INTEGER NOT NULL DEFAULT 0,
    latency_ms  INTEGER,
    status      TEXT,
    at          REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS http_cache (
    cache_key   TEXT PRIMARY KEY,          -- endpoint + body hash
    response    TEXT NOT NULL,
    fetched_at  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
    event_id    INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id      TEXT,
    invoice_id  TEXT,
    kind        TEXT NOT NULL,
    detail      TEXT NOT NULL,
    at          REAL NOT NULL
);
"""


def connect() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(os.path.abspath(DB_PATH)), exist_ok=True)
    conn = sqlite3.connect(DB_PATH, isolation_level=None, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(SCHEMA)
    return conn


@contextmanager
def tx(conn: sqlite3.Connection):
    """One atomic step: either everything in the block is committed, or nothing."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise


def log_event(conn, run_id, invoice_id, kind, detail):
    conn.execute(
        "INSERT INTO events(run_id, invoice_id, kind, detail, at) VALUES (?,?,?,?,?)",
        (run_id, invoice_id, kind, json.dumps(detail, default=str), time.time()),
    )
