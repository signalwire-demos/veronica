"""SQLite state store for Veronica — callers, call state, consent log.

callers    — persistent, keyed by ANI (phone). Grows with call volume.
call_state — ephemeral per-call. Heavy API responses. Deleted on hangup.
consent_log — append-only audit trail for SMS and email send consent.
"""

import json
import logging
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

DB_PATH = Path(__file__).parent / "veronica.db"

_CREATE_TABLES = """
CREATE TABLE IF NOT EXISTS callers (
    phone               TEXT PRIMARY KEY,
    owner_name          TEXT,
    line_type           TEXT,
    sms_eligible        INTEGER DEFAULT 0,
    candidate_email     TEXT,
    candidate_address   TEXT,
    address_normalized  TEXT,
    geocode_lat         REAL,
    geocode_lng         REAL,
    geocode_confidence  TEXT,
    dpv_match_code      TEXT,
    validated_email     TEXT,
    validated_address   TEXT,
    trestle_raw         TEXT,
    last_enriched_at    TEXT,
    last_call_at        TEXT,
    created_at          TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at          TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS call_state (
    call_id             TEXT PRIMARY KEY,
    phone               TEXT,
    state_json          TEXT NOT NULL DEFAULT '{}',
    created_at          REAL NOT NULL,
    updated_at          REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS consent_log (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    phone               TEXT NOT NULL,
    call_id             TEXT NOT NULL,
    consent_type        TEXT NOT NULL CHECK(consent_type IN ('sms', 'email_send')),
    consented           INTEGER NOT NULL,
    transcript_snippet  TEXT,
    created_at          TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_consent_phone ON consent_log(phone);
CREATE INDEX IF NOT EXISTS idx_consent_call ON consent_log(call_id);
"""


def _connect():
    """Open a new connection with WAL mode."""
    conn = sqlite3.connect(str(DB_PATH), timeout=5)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(_CREATE_TABLES)
    return conn


# ── Callers ──────────────────────────────────────────────────────────

def get_caller_by_phone(phone):
    """Lookup a caller by phone number. Returns dict or None."""
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT * FROM callers WHERE phone = ?", (phone,)
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def upsert_caller(phone, **fields):
    """Create or update a caller record. COALESCE preserves existing non-null values."""
    allowed = {
        "owner_name", "line_type", "sms_eligible", "candidate_email",
        "candidate_address", "address_normalized", "geocode_lat", "geocode_lng",
        "geocode_confidence", "dpv_match_code", "validated_email",
        "validated_address", "trestle_raw", "last_enriched_at", "last_call_at",
    }
    filtered = {k: v for k, v in fields.items() if k in allowed}

    conn = _connect()
    try:
        # Build dynamic upsert
        columns = ["phone"] + list(filtered.keys()) + ["created_at", "updated_at"]
        placeholders = ["?"] * len(columns)
        values = [phone] + list(filtered.values()) + [
            datetime.now(timezone.utc).isoformat(),
            datetime.now(timezone.utc).isoformat(),
        ]

        conflict_sets = [f"{k} = COALESCE(excluded.{k}, callers.{k})" for k in filtered]
        conflict_sets.append("updated_at = excluded.updated_at")

        sql = (
            f"INSERT INTO callers ({', '.join(columns)}) "
            f"VALUES ({', '.join(placeholders)}) "
            f"ON CONFLICT(phone) DO UPDATE SET {', '.join(conflict_sets)}"
        )
        conn.execute(sql, values)
        conn.commit()
        logger.info(f"Upserted caller phone={phone}")
        return get_caller_by_phone(phone)
    finally:
        conn.close()


def caller_is_stale(caller, ttl_days=180):
    """Check if a caller record needs re-enrichment."""
    enriched = caller.get("last_enriched_at")
    if not enriched:
        return True
    try:
        enriched_dt = datetime.fromisoformat(enriched)
        return datetime.now(timezone.utc) - enriched_dt > timedelta(days=ttl_days)
    except (ValueError, TypeError):
        return True


# ── Call State ───────────────────────────────────────────────────────

DEFAULT_CALL_STATE = {
    "owner_name": None,
    "line_type": None,
    "sms_eligible": False,
    "candidate_email": None,
    "candidate_address": None,
    "address_normalized": None,
    "record_source": "new",
    "identity_confirmed": False,
    "identity_mismatch": False,
    "working_email": None,
    "email_source": None,
    "email_attempts": 0,
    "spelling_attempts": 0,
    "zb_status": None,
    "zb_sub_status": None,
    "email_consent": None,
    "sms_consent": None,
    "follow_up_required": False,
    "follow_up_reason": None,
    "collected_address": None,
    "address_source": None,
    "address_attempts": 0,
    "address_validation_status": None,
}


def load_call_state(call_id):
    """Return the state dict for a call, or defaults if missing."""
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT state_json FROM call_state WHERE call_id = ?", (call_id,)
        ).fetchone()
        if row:
            state = json.loads(row[0])
            return {**DEFAULT_CALL_STATE, **state}
        return dict(DEFAULT_CALL_STATE)
    finally:
        conn.close()


def save_call_state(call_id, state, phone=None):
    """Upsert the JSON blob for a call."""
    now = time.time()
    blob = json.dumps(state, default=str)
    conn = _connect()
    try:
        conn.execute(
            """INSERT INTO call_state (call_id, phone, state_json, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(call_id) DO UPDATE SET
                   state_json = excluded.state_json,
                   updated_at = excluded.updated_at""",
            (call_id, phone, blob, now, now),
        )
        conn.commit()
    finally:
        conn.close()


def delete_call_state(call_id):
    """Remove a call's state after the call ends."""
    conn = _connect()
    try:
        conn.execute("DELETE FROM call_state WHERE call_id = ?", (call_id,))
        conn.commit()
        logger.info(f"Deleted call state for call_id={call_id}")
    finally:
        conn.close()


def cleanup_stale_states(max_age_hours=24):
    """Prune abandoned calls older than max_age_hours."""
    cutoff = time.time() - (max_age_hours * 3600)
    conn = _connect()
    try:
        cursor = conn.execute(
            "DELETE FROM call_state WHERE updated_at < ?", (cutoff,)
        )
        conn.commit()
        if cursor.rowcount:
            logger.info(f"Cleaned up {cursor.rowcount} stale call states")
    finally:
        conn.close()


# ── Consent Log ──────────────────────────────────────────────────────

def log_consent(phone, call_id, consent_type, consented, transcript_snippet=None):
    """Append a consent record to the audit trail."""
    conn = _connect()
    try:
        conn.execute(
            """INSERT INTO consent_log (phone, call_id, consent_type, consented, transcript_snippet)
               VALUES (?, ?, ?, ?, ?)""",
            (phone, call_id, consent_type, 1 if consented else 0, transcript_snippet),
        )
        conn.commit()
        logger.info(f"Logged consent: phone={phone} type={consent_type} consented={consented}")
    finally:
        conn.close()


def get_consent_history(phone):
    """Return all consent records for a phone number."""
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT * FROM consent_log WHERE phone = ? ORDER BY created_at DESC",
            (phone,),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()
