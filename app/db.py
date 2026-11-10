"""Tiny SQLite store. Full TINs are never saved, only the last 4 digits."""

from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime

from . import config

SCHEMA = [
    """
CREATE TABLE IF NOT EXISTS applications (
  id TEXT PRIMARY KEY,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  business_name TEXT NOT NULL,
  applicant TEXT NOT NULL,          -- JSON, TIN masked
  tin_submitted INTEGER NOT NULL,
  middesk_id TEXT,
  middesk_status TEXT,
  recommendation TEXT,
  priority TEXT,
  reasons TEXT,                     -- JSON list
  tasks TEXT,                       -- JSON list
  final_decision TEXT,              -- approved / declined (by a human)
  reviewer_note TEXT,
  decided_at TEXT,                  -- when the human decision was recorded
  kyc TEXT,                         -- JSON list, SIMULATED identity checks
  core_account_ref TEXT,            -- SIMULATED core account reference
  account_last4 TEXT,               -- last 4 only, never a full account number
  funding_status TEXT,              -- null / awaiting_deposit / funded
  funded_amount_cents INTEGER,
  account_opened_at TEXT,
  accounts TEXT,                    -- JSON list, one SIMULATED account per selected product
  channel TEXT,                     -- online / in_branch
  created_by TEXT,                  -- staff id of the banker for in-branch applications
  primary_officer TEXT,             -- staff id
  assignees TEXT                    -- JSON list of staff ids
)
""",
    # Append-only audit trail. Triggers below reject any UPDATE or DELETE.
    """
CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  app_id TEXT NOT NULL,
  at TEXT NOT NULL,
  kind TEXT NOT NULL,
  detail TEXT                       -- JSON, never a full TIN
)
""",
    "CREATE INDEX IF NOT EXISTS idx_events_app_id ON events (app_id)",
    "CREATE TRIGGER IF NOT EXISTS events_no_update BEFORE UPDATE ON events "
    "BEGIN SELECT RAISE(ABORT, 'audit log is append-only'); END",
    "CREATE TRIGGER IF NOT EXISTS events_no_delete BEFORE DELETE ON events "
    "BEGIN SELECT RAISE(ABORT, 'audit log is append-only'); END",
    # Uploaded file metadata. The file itself lives in DOCUMENTS_DIR under the id.
    """
CREATE TABLE IF NOT EXISTS documents (
  id TEXT PRIMARY KEY,
  app_id TEXT NOT NULL,
  created_at TEXT NOT NULL,
  doc_type TEXT NOT NULL,
  filename TEXT NOT NULL,           -- sanitized display name only
  content_type TEXT NOT NULL,
  size INTEGER NOT NULL,
  sha256 TEXT NOT NULL
)
""",
    "CREATE INDEX IF NOT EXISTS idx_documents_app_id ON documents (app_id)",
    # Account maintenance requests. Details never hold a full TIN/EIN or SSN (see app/maintenance.py).
    """
CREATE TABLE IF NOT EXISTS maintenance_requests (
  id TEXT PRIMARY KEY,
  app_id TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  kind TEXT NOT NULL,
  status TEXT NOT NULL,             -- pending / approved / rejected / completed
  details TEXT,                     -- JSON, sanitized
  requested_by TEXT,                -- staff id
  decided_by TEXT,                  -- staff id
  decided_at TEXT,
  decision_note TEXT,
  account_ref TEXT
)
""",
    "CREATE INDEX IF NOT EXISTS idx_maintenance_app_id ON maintenance_requests (app_id)",
]

# Columns added after the first release. Older databases get them on startup.
ADDED_COLUMNS = {
    "decided_at": "TEXT",
    "kyc": "TEXT",
    "core_account_ref": "TEXT",
    "account_last4": "TEXT",
    "funding_status": "TEXT",
    "funded_amount_cents": "INTEGER",
    "account_opened_at": "TEXT",
    "accounts": "TEXT",
    "channel": "TEXT",
    "created_by": "TEXT",
    "primary_officer": "TEXT",
    "assignees": "TEXT",
}

_migrated: set[str] = set()
_migrate_lock = threading.Lock()

JSON_FIELDS = {"applicant", "reasons", "tasks", "kyc", "accounts", "assignees"}
MAX_ASSIGNEES = 5


def now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _add_missing_columns(path: str) -> None:
    with _migrate_lock:
        if path in _migrated:
            return
        c = sqlite3.connect(path)
        try:
            with c:
                for stmt in SCHEMA:
                    c.execute(stmt)
                have = {r[1] for r in c.execute("PRAGMA table_info(applications)")}
                for name, kind in ADDED_COLUMNS.items():
                    if name not in have:
                        try:
                            c.execute(f"ALTER TABLE applications ADD COLUMN {name} {kind}")
                        except sqlite3.OperationalError as e:
                            if "duplicate column name" not in str(e):  # another process added it first
                                raise
        finally:
            c.close()
        _migrated.add(path)


@contextmanager
def conn() -> Iterator[sqlite3.Connection]:
    """Open a connection, commit on success (roll back on error), and always close it."""
    path = config.DATABASE_PATH
    if path not in _migrated:
        _add_missing_columns(path)
    c = sqlite3.connect(path)
    c.row_factory = sqlite3.Row
    try:
        with c:
            for stmt in SCHEMA:  # cheap; recreates tables if the file was removed while running
                c.execute(stmt)
            yield c
    finally:
        c.close()


def _row(r):
    if r is None:
        return None
    d = dict(r)
    for f in JSON_FIELDS:
        d[f] = json.loads(d[f]) if d.get(f) else None
    d["tin_submitted"] = bool(d["tin_submitted"])
    d["channel"] = d.get("channel") or "online"
    d["assignees"] = d["assignees"] or []
    if d["accounts"] is None:
        d["accounts"] = []
        if d.get("core_account_ref"):  # saved before one-account-per-product: show it as a single account
            d["accounts"] = [
                {
                    "product_id": None,
                    "product_name": "Account",
                    "account_ref": d["core_account_ref"],
                    "account_last4": d["account_last4"],
                    "funding_status": d["funding_status"],
                    "expected_deposit_cents": None,
                    "funded_amount_cents": d["funded_amount_cents"],
                    "opened_at": d["account_opened_at"],
                }
            ]
    return d


def insert(
    app: dict,
    event: dict | None = None,
    kyc_event: dict | None = None,
    extra_events: list[tuple[str, dict]] | None = None,
) -> None:
    """Insert an application. If `event` is given, its "submitted" audit event is written in the same transaction.
    `extra_events` is a list of (kind, detail) written in the same transaction.
    `app["kyc"]` (simulated identity checks) is stored with it, and `kyc_event` is its "kyc_completed" event."""
    with conn() as c:
        c.execute(
            "INSERT INTO applications (id, created_at, updated_at, business_name, applicant, tin_submitted, kyc, "
            "channel, created_by) VALUES (?,?,?,?,?,?,?,?,?)",
            (
                app["id"],
                now(),
                now(),
                app["business_name"],
                json.dumps(app["applicant"]),
                int(app["tin_submitted"]),
                json.dumps(app["kyc"]) if app.get("kyc") is not None else None,
                app.get("channel") or "online",
                app.get("created_by"),
            ),
        )
        if event is not None:
            add_event(app["id"], "submitted", event, c=c)
        if kyc_event is not None:
            add_event(app["id"], "kyc_completed", kyc_event, c=c)
        for kind, detail in extra_events or []:
            add_event(app["id"], kind, detail, c=c)


ALLOWED_UPDATE = {
    "middesk_id",
    "middesk_status",
    "recommendation",
    "priority",
    "reasons",
    "tasks",
}
# final_decision / reviewer_note / decided_at are written only by set_final_decision().
# The account and funding columns are written only by add_account() and set_funded().


def _update(c: sqlite3.Connection, app_id: str, fields: dict) -> None:
    fields = {k: v for k, v in fields.items() if k in ALLOWED_UPDATE}
    if not fields:
        return
    fields["updated_at"] = now()
    cols = ", ".join(f"{k}=?" for k in fields)
    vals = [json.dumps(v) if k in JSON_FIELDS else v for k, v in fields.items()]
    c.execute(f"UPDATE applications SET {cols} WHERE id=?", (*vals, app_id))


def update(app_id: str, **fields) -> None:
    with conn() as c:
        _update(c, app_id, fields)


EVENT_KEYS = ("middesk_status", "recommendation", "priority")


def update_with_results_event(app_id: str, source: str, **fields) -> dict | None:
    """Store results and, if status/recommendation/priority changed, append the
    results_updated event, all in one write transaction (writers are serialized)."""
    with conn() as c:
        c.execute("BEGIN IMMEDIATE")
        before = _row(c.execute("SELECT * FROM applications WHERE id=?", (app_id,)).fetchone())
        _update(c, app_id, fields)
        after = _row(c.execute("SELECT * FROM applications WHERE id=?", (app_id,)).fetchone())
        if after is not None and (before is None or any(before[k] != after[k] for k in EVENT_KEYS)):
            add_event(app_id, "results_updated", {**{k: after[k] for k in EVENT_KEYS}, "source": source}, c=c)
        return after


def set_final_decision(app_id: str, final_decision: str, reviewer_note: str | None) -> bool:
    """Record the human decision once. Returns False if one was already recorded."""
    ts = now()
    with conn() as c:
        cur = c.execute(
            "UPDATE applications SET final_decision=?, reviewer_note=?, decided_at=?, updated_at=? "
            "WHERE id=? AND final_decision IS NULL",
            (final_decision, reviewer_note, ts, ts, app_id),
        )
        if cur.rowcount != 1:
            return False
        add_event(app_id, "decision_recorded", {"decision": final_decision, "note": reviewer_note}, c=c)
        return True


def _save_accounts(c: sqlite3.Connection, app_id: str, accounts: list[dict]) -> None:
    """Write the accounts list. The older single-account columns mirror the FIRST account."""
    first = accounts[0]
    c.execute(
        "UPDATE applications SET accounts=?, core_account_ref=?, account_last4=?, funding_status=?, "
        "funded_amount_cents=?, account_opened_at=?, updated_at=? WHERE id=?",
        (
            json.dumps(accounts),
            first["account_ref"],
            first["account_last4"],
            first["funding_status"],
            first["funded_amount_cents"],
            first["opened_at"],
            now(),
            app_id,
        ),
    )


def add_account(app_id: str, account: dict) -> bool:
    """Store one (simulated) account of an approved application, with its "account_opened" event.
    Returns False if the application is not approved or already has an account for that product."""
    with conn() as c:
        c.execute("BEGIN IMMEDIATE")
        row = _row(c.execute("SELECT * FROM applications WHERE id=?", (app_id,)).fetchone())
        if row is None or row["final_decision"] != "approved":
            return False
        accounts = row["accounts"]
        if any(a["product_id"] == account["product_id"] for a in accounts):
            return False
        accounts.append(
            {
                "product_id": account["product_id"],
                "product_name": account["product_name"],
                "account_ref": account["account_ref"],
                "account_last4": account["account_last4"],
                "funding_status": "awaiting_deposit",
                "expected_deposit_cents": account.get("expected_deposit_cents"),
                "funded_amount_cents": None,
                "opened_at": now(),
            }
        )
        _save_accounts(c, app_id, accounts)
        add_event(
            app_id,
            "account_opened",
            {
                "product_id": account["product_id"],
                "account_ref": account["account_ref"],
                "account_last4": account["account_last4"],
                "simulated": True,
            },
            c=c,
        )
        return True


def set_funded(app_id: str, product_id: str | None, amount_cents: int) -> bool:
    """Record the (simulated) first deposit of one account once, with its "deposit_received" event.
    Returns False if that account is not waiting for a deposit."""
    with conn() as c:
        c.execute("BEGIN IMMEDIATE")
        row = _row(c.execute("SELECT * FROM applications WHERE id=?", (app_id,)).fetchone())
        if row is None:
            return False
        accounts = row["accounts"]
        acct = next((a for a in accounts if a["product_id"] == product_id), None)
        if acct is None or acct["funding_status"] != "awaiting_deposit":
            return False
        acct["funding_status"] = "funded"
        acct["funded_amount_cents"] = amount_cents
        _save_accounts(c, app_id, accounts)
        add_event(
            app_id,
            "deposit_received",
            {"product_id": product_id, "amount_cents": amount_cents, "simulated": True},
            c=c,
        )
        return True


def add_assignee(app_id: str, staff: dict) -> str:
    """Assign one staff member and record "assignee_added". Returns ok / missing / duplicate / full."""
    with conn() as c:
        c.execute("BEGIN IMMEDIATE")
        row = _row(c.execute("SELECT * FROM applications WHERE id=?", (app_id,)).fetchone())
        if row is None:
            return "missing"
        ids = row["assignees"]
        if staff["id"] in ids:
            return "duplicate"
        if len(ids) >= MAX_ASSIGNEES:
            return "full"
        ids.append(staff["id"])
        c.execute("UPDATE applications SET assignees=?, updated_at=? WHERE id=?", (json.dumps(ids), now(), app_id))
        add_event(app_id, "assignee_added", {"staff_id": staff["id"], "name": staff["name"]}, c=c)
        return "ok"


def remove_assignee(app_id: str, staff_id: str, name: str) -> str:
    """Unassign one staff member and record "assignee_removed". Returns ok / missing / not_assigned."""
    with conn() as c:
        c.execute("BEGIN IMMEDIATE")
        row = _row(c.execute("SELECT * FROM applications WHERE id=?", (app_id,)).fetchone())
        if row is None:
            return "missing"
        if staff_id not in row["assignees"]:
            return "not_assigned"
        ids = [i for i in row["assignees"] if i != staff_id]
        c.execute("UPDATE applications SET assignees=?, updated_at=? WHERE id=?", (json.dumps(ids), now(), app_id))
        add_event(app_id, "assignee_removed", {"staff_id": staff_id, "name": name}, c=c)
        return "ok"


def set_primary_officer(app_id: str, staff: dict | None) -> bool:
    """Set or clear the primary officer and record "primary_officer_set". False if the application is missing."""
    with conn() as c:
        c.execute("BEGIN IMMEDIATE")
        if c.execute("SELECT 1 FROM applications WHERE id=?", (app_id,)).fetchone() is None:
            return False
        c.execute(
            "UPDATE applications SET primary_officer=?, updated_at=? WHERE id=?",
            (staff["id"] if staff else None, now(), app_id),
        )
        add_event(
            app_id,
            "primary_officer_set",
            {"staff_id": staff["id"] if staff else None, "name": staff["name"] if staff else None},
            c=c,
        )
        return True


def get(app_id: str):
    with conn() as c:
        return _row(c.execute("SELECT * FROM applications WHERE id=?", (app_id,)).fetchone())


def get_by_middesk_id(mid: str):
    with conn() as c:
        return _row(c.execute("SELECT * FROM applications WHERE middesk_id=?", (mid,)).fetchone())


def claim_middesk_id(app_id: str, mid: str):
    """Link a Middesk business to an application that has no Middesk ID yet.
    Covers a webhook that arrives before create_business() has returned."""
    with conn() as c:
        c.execute(
            "UPDATE applications SET middesk_id=?, updated_at=? WHERE id=? AND middesk_id IS NULL",
            (mid, now(), app_id),
        )
        return _row(c.execute("SELECT * FROM applications WHERE id=? AND middesk_id=?", (app_id, mid)).fetchone())


def list_all() -> list:
    with conn() as c:
        return [_row(r) for r in c.execute("SELECT * FROM applications ORDER BY created_at DESC").fetchall()]


def add_event(app_id: str, kind: str, detail: dict | None = None, c: sqlite3.Connection | None = None) -> None:
    """Append one audit event. Pass `c` to write inside an existing transaction."""
    args = (app_id, now(), kind, json.dumps(detail) if detail is not None else None)
    sql = "INSERT INTO events (app_id, at, kind, detail) VALUES (?,?,?,?)"
    if c is not None:
        c.execute(sql, args)
        return
    with conn() as own:
        own.execute(sql, args)


def list_events(app_id: str | None = None) -> list:
    """Events oldest first, for one application or for all of them."""
    sql, args = "SELECT * FROM events", ()
    if app_id is not None:
        sql, args = sql + " WHERE app_id=?", (app_id,)
    with conn() as c:
        rows = c.execute(sql + " ORDER BY id", args).fetchall()
    return [{**dict(r), "detail": json.loads(r["detail"]) if r["detail"] else None} for r in rows]


def add_document(doc: dict, max_docs: int) -> bool:
    """Save document metadata and its "document_uploaded" event in one transaction.
    Returns False (nothing saved) if the application already has `max_docs` documents."""
    with conn() as c:
        c.execute("BEGIN IMMEDIATE")
        n = c.execute("SELECT COUNT(*) FROM documents WHERE app_id=?", (doc["app_id"],)).fetchone()[0]
        if n >= max_docs:
            return False
        c.execute(
            "INSERT INTO documents (id, app_id, created_at, doc_type, filename, content_type, size, sha256) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (
                doc["id"],
                doc["app_id"],
                doc["created_at"],
                doc["doc_type"],
                doc["filename"],
                doc["content_type"],
                doc["size"],
                doc["sha256"],
            ),
        )
        add_event(
            doc["app_id"],
            "document_uploaded",
            {"doc_type": doc["doc_type"], "filename": doc["filename"], "size": doc["size"]},
            c=c,
        )
        return True


def count_documents(app_id: str) -> int:
    with conn() as c:
        return c.execute("SELECT COUNT(*) FROM documents WHERE app_id=?", (app_id,)).fetchone()[0]


def list_documents(app_id: str) -> list:
    with conn() as c:
        rows = c.execute("SELECT * FROM documents WHERE app_id=? ORDER BY created_at, rowid", (app_id,)).fetchall()
        return [dict(r) for r in rows]


def get_document(app_id: str, doc_id: str) -> dict | None:
    with conn() as c:
        r = c.execute("SELECT * FROM documents WHERE app_id=? AND id=?", (app_id, doc_id)).fetchone()
        return dict(r) if r else None


def _maint_row(r) -> dict | None:
    if r is None:
        return None
    d = dict(r)
    d["details"] = json.loads(d["details"]) if d.get("details") else {}
    return d


def add_maintenance(req: dict, events: list[tuple[str, dict]]) -> None:
    """Save a maintenance request and its audit events in one transaction."""
    ts = now()
    with conn() as c:
        c.execute(
            "INSERT INTO maintenance_requests (id, app_id, created_at, updated_at, kind, status, details, "
            "requested_by, decided_by, decided_at, decision_note, account_ref) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                req["id"],
                req["app_id"],
                ts,
                ts,
                req["kind"],
                req["status"],
                json.dumps(req["details"]),
                req.get("requested_by"),
                None,
                None,
                None,
                req.get("account_ref"),
            ),
        )
        for kind, detail in events:
            add_event(req["app_id"], kind, detail, c=c)


def get_maintenance(request_id: str) -> dict | None:
    with conn() as c:
        return _maint_row(c.execute("SELECT * FROM maintenance_requests WHERE id=?", (request_id,)).fetchone())


def list_maintenance(app_id: str) -> list[dict]:
    """Newest first."""
    with conn() as c:
        rows = c.execute(
            "SELECT * FROM maintenance_requests WHERE app_id=? ORDER BY created_at DESC, rowid DESC", (app_id,)
        ).fetchall()
        return [_maint_row(r) for r in rows]


def decide_maintenance(request_id: str, approve: bool, decided_by: dict | None, note: str | None, apply_fn, summary):
    """A human decision on a pending request, once. Approve runs `apply_fn(app_row, at)` (the SIMULATED core
    update, returns the new stored values) and completes the request; reject changes nothing else. Everything,
    including the audit events, is written in one transaction (an error from `apply_fn` rolls it all back).
    Returns (result, request) with result ok / missing / decided."""
    with conn() as c:
        c.execute("BEGIN IMMEDIATE")
        req = _maint_row(c.execute("SELECT * FROM maintenance_requests WHERE id=?", (request_id,)).fetchone())
        if req is None:
            return "missing", None
        if req["status"] != "pending":
            return "decided", req
        ts = now()
        who = {
            "decided_by": decided_by["id"] if decided_by else None,
            "name": decided_by["name"] if decided_by else None,
        }
        base = {"request_id": request_id, "kind": req["kind"], **who}
        if approve:
            row = _row(c.execute("SELECT * FROM applications WHERE id=?", (req["app_id"],)).fetchone())
            new = apply_fn(row, ts)
            if "accounts" in new:
                _save_accounts(c, req["app_id"], new["accounts"])
            sets = {k: json.dumps(new[k]) for k in ("applicant",) if k in new}
            sets.update({k: new[k] for k in ("business_name", "tin_submitted") if k in new})
            if sets:
                cols = ", ".join(f"{k}=?" for k in sets)
                c.execute(
                    f"UPDATE applications SET {cols}, updated_at=? WHERE id=?", (*sets.values(), ts, req["app_id"])
                )
        status = "completed" if approve else "rejected"
        c.execute(
            "UPDATE maintenance_requests SET status=?, decided_by=?, decided_at=?, decision_note=?, updated_at=? "
            "WHERE id=?",
            (status, who["decided_by"], ts, note, ts, request_id),
        )
        add_event(req["app_id"], "maintenance_approved" if approve else "maintenance_rejected", base, c=c)
        if approve:
            add_event(req["app_id"], "maintenance_completed", {**base, "summary": summary, "simulated": True}, c=c)
        return "ok", _maint_row(c.execute("SELECT * FROM maintenance_requests WHERE id=?", (request_id,)).fetchone())
