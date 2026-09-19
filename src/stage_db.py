#!/usr/bin/env python3
"""Small, local-only state store for Stage Outreach.

No dashboard and no network calls live here. A touchpoint is considered sent only
when the Gmail bridge has returned a real message id and the agent records it.
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
import unicodedata
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB = ROOT / "data" / "stage.sqlite3"

SCHEMA = """
PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS leads (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    brand TEXT NOT NULL,
    brand_key TEXT NOT NULL UNIQUE,
    website TEXT,
    category TEXT,
    country TEXT,
    contact_name TEXT,
    contact_email TEXT,
    language TEXT,
    source_url TEXT,
    source_type TEXT,
    fit_score INTEGER,
    notes TEXT,
    do_not_contact INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS touchpoints (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    lead_id INTEGER NOT NULL REFERENCES leads(id),
    step TEXT NOT NULL CHECK(step IN ('initial', 'followup1', 'followup2')),
    status TEXT NOT NULL CHECK(status IN ('draft', 'approved', 'sent', 'replied', 'bounced', 'cancelled', 'opted_out')),
    subject TEXT NOT NULL,
    body TEXT NOT NULL,
    approval_code TEXT UNIQUE,
    message_id TEXT,
    thread_id TEXT,
    approved_at TEXT,
    sent_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(lead_id, step)
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    lead_id INTEGER NOT NULL REFERENCES leads(id),
    touchpoint_id INTEGER REFERENCES touchpoints(id),
    event_type TEXT NOT NULL CHECK(event_type IN ('send', 'human_reply', 'auto_reply', 'bounce', 'opt_out', 'refusal')),
    external_id TEXT,
    event_at TEXT NOT NULL,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    UNIQUE(event_type, external_id)
);
CREATE INDEX IF NOT EXISTS idx_touchpoints_due ON touchpoints(status, step, sent_at);
CREATE INDEX IF NOT EXISTS idx_events_time ON events(event_type, event_at);
CREATE TABLE IF NOT EXISTS send_attempts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    touchpoint_id INTEGER NOT NULL UNIQUE REFERENCES touchpoints(id),
    rfc_message_id TEXT NOT NULL UNIQUE,
    state TEXT NOT NULL CHECK(state IN ('sending', 'sent', 'ambiguous')),
    message_id TEXT,
    thread_id TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_send_attempts_state ON send_attempts(state);
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def parse_dt(value: str) -> datetime:
    value = value.strip()
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def brand_key(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).casefold()
    normalized = re.sub(r"[^\w]+", " ", normalized, flags=re.UNICODE)
    return " ".join(normalized.split())


def connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA busy_timeout=5000")
    db.executescript(SCHEMA)
    columns = {row["name"] for row in db.execute("PRAGMA table_info(touchpoints)")}
    if "approval_code" not in columns:
        db.execute("ALTER TABLE touchpoints ADD COLUMN approval_code TEXT")
    db.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_touchpoints_approval_code ON touchpoints(approval_code) WHERE approval_code IS NOT NULL")
    # Backfill codes once for older local databases. The code is an identifier,
    # not a secret, and remains stable for the life of the touchpoint.
    for row in db.execute("SELECT id, created_at FROM touchpoints WHERE approval_code IS NULL").fetchall():
        date_part = str(row["created_at"] or now_iso())[:10].replace("-", "")
        db.execute("UPDATE touchpoints SET approval_code=? WHERE id=?", (f"OBS-{date_part}-{int(row['id']):04d}", row["id"]))
    db.commit()
    try:
        path.chmod(0o600)
    except OSError:
        pass
    return db


def output(payload: object) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def upsert_lead_record(db: sqlite3.Connection, args: argparse.Namespace) -> sqlite3.Row:
    timestamp = now_iso()
    key = brand_key(args.brand)
    existing = db.execute("SELECT * FROM leads WHERE brand_key=?", (key,)).fetchone()
    if existing and existing["do_not_contact"]:
        raise RuntimeError("lead blocked: brand is marked do_not_contact")
    db.execute(
        """INSERT INTO leads
        (brand, brand_key, website, category, country, contact_name, contact_email,
         language, source_url, source_type, fit_score, notes, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(brand_key) DO UPDATE SET
          brand=excluded.brand, website=excluded.website, category=excluded.category,
          country=excluded.country, contact_name=excluded.contact_name,
          contact_email=excluded.contact_email, language=excluded.language,
          source_url=excluded.source_url, source_type=excluded.source_type,
          fit_score=excluded.fit_score, notes=excluded.notes, updated_at=excluded.updated_at""",
        (args.brand.strip(), key, args.website, args.category, args.country,
         args.contact_name, args.contact_email, args.language, args.source_url,
         args.source_type, args.fit_score, args.notes, timestamp, timestamp),
    )
    db.commit()
    row = db.execute("SELECT * FROM leads WHERE brand_key = ?", (key,)).fetchone()
    return row


def lead_upsert(db: sqlite3.Connection, args: argparse.Namespace) -> None:
    row = upsert_lead_record(db, args)
    output({"status": "lead_saved", "lead_id": row["id"], "brand": row["brand"]})


def find_lead(db: sqlite3.Connection, args: argparse.Namespace) -> sqlite3.Row:
    if args.lead_id:
        row = db.execute("SELECT * FROM leads WHERE id = ?", (args.lead_id,)).fetchone()
    else:
        row = db.execute("SELECT * FROM leads WHERE brand_key = ?", (brand_key(args.brand),)).fetchone()
    if not row:
        raise SystemExit("Lead introuvable : enregistrer la marque avant le brouillon.")
    return row


def upsert_touchpoint_record(db: sqlite3.Connection, args: argparse.Namespace, lead: sqlite3.Row | None = None) -> sqlite3.Row:
    lead = lead or find_lead(db, args)
    timestamp = now_iso()
    status = args.status
    if status not in {"draft", "approved"}:
        raise RuntimeError("Un brouillon ne peut être que draft ou approved.")
    existing = db.execute("SELECT * FROM touchpoints WHERE lead_id=? AND step=?", (lead["id"], args.step)).fetchone()
    if existing:
        if existing["status"] not in {"draft", "approved"}:
            raise RuntimeError(f"touchpoint blocked: already {existing['status']}")
        if existing["status"] == "approved" and (args.subject != existing["subject"] or args.body != existing["body"]):
            raise RuntimeError("touchpoint blocked: approved content cannot be replaced")
        if existing["status"] == "approved":
            return existing
        db.execute(
            """UPDATE touchpoints SET subject=?, body=?, status=?, approved_at=?, updated_at=?
               WHERE id=?""",
            (args.subject, args.body, status, timestamp if status == "approved" else None, timestamp, existing["id"]),
        )
        db.commit()
        return db.execute("SELECT * FROM touchpoints WHERE id=?", (existing["id"],)).fetchone()
    db.execute(
        """INSERT INTO touchpoints
        (lead_id, step, status, subject, body, approval_code, approved_at, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, NULL, ?, ?, ?)""",
        (lead["id"], args.step, status, args.subject, args.body,
         timestamp if status == "approved" else None, timestamp, timestamp),
    )
    db.commit()
    row = db.execute(
        "SELECT * FROM touchpoints WHERE lead_id = ? AND step = ?",
        (lead["id"], args.step),
    ).fetchone()
    code = f"OBS-{timestamp[:10].replace('-', '')}-{int(row['id']):04d}"
    db.execute("UPDATE touchpoints SET approval_code=? WHERE id=?", (code, row["id"]))
    db.commit()
    return db.execute("SELECT * FROM touchpoints WHERE id=?", (row["id"],)).fetchone()


def touchpoint_upsert(db: sqlite3.Connection, args: argparse.Namespace) -> None:
    lead = find_lead(db, args)
    row = upsert_touchpoint_record(db, args, lead)
    output({"status": "touchpoint_saved", "touchpoint_id": row["id"], "approval_code": row["approval_code"], "brand": lead["brand"], "step": args.step, "state": row["status"]})


def resolve_touchpoint(db: sqlite3.Connection, approval: str) -> sqlite3.Row | None:
    value = str(approval or "").strip()
    if value.startswith("OBS-"):
        return db.execute("SELECT * FROM touchpoints WHERE approval_code=?", (value,)).fetchone()
    if value.isdigit():
        return db.execute("SELECT * FROM touchpoints WHERE id=?", (int(value),)).fetchone()
    return None


def approve_record(db: sqlite3.Connection, approval: str) -> sqlite3.Row:
    timestamp = now_iso()
    row = resolve_touchpoint(db, approval)
    if not row:
        raise RuntimeError("Touchpoint introuvable.")
    if row["status"] not in {"draft", "approved"}:
        raise RuntimeError(f"Touchpoint {row['id']} déjà {row['status']} : aucune réactivation automatique.")
    db.execute("UPDATE touchpoints SET status='approved', approved_at=?, updated_at=? WHERE id=?", (timestamp, timestamp, row["id"]))
    db.commit()
    return db.execute("SELECT * FROM touchpoints WHERE id=?", (row["id"],)).fetchone()


def approve(db: sqlite3.Connection, args: argparse.Namespace) -> None:
    value = args.approval_code or str(args.touchpoint_id or "")
    row = approve_record(db, value)
    output({"status": "approved", "touchpoint_id": row["id"], "approval_code": row["approval_code"], "brand": db.execute("SELECT brand FROM leads WHERE id=?", (row["lead_id"],)).fetchone()[0]})


def mark_sent_record(db: sqlite3.Connection, touchpoint_id: int, message_id: str, thread_id: str, sent_at: str = "") -> sqlite3.Row:
    timestamp = sent_at or now_iso()
    row = db.execute(
        """SELECT t.*, l.brand, l.do_not_contact, l.contact_email
           FROM touchpoints t JOIN leads l ON l.id=t.lead_id WHERE t.id=?""",
        (touchpoint_id,),
    ).fetchone()
    if not row:
        raise RuntimeError("Touchpoint introuvable.")
    if row["status"] == "sent":
        if row["message_id"] != message_id or row["thread_id"] != thread_id:
            raise RuntimeError("send blocked: existing receipt differs")
        return row
    if row["status"] != "approved":
        raise RuntimeError(f"Envoi bloqué : état {row['status']} au lieu de approved.")
    if row["do_not_contact"]:
        raise RuntimeError("Envoi bloqué : marque en do_not_contact.")
    db.execute(
        "UPDATE touchpoints SET status='sent', message_id=?, thread_id=?, sent_at=?, updated_at=? WHERE id=?",
        (message_id, thread_id, timestamp, now_iso(), touchpoint_id),
    )
    db.execute(
        "INSERT OR IGNORE INTO events (lead_id, touchpoint_id, event_type, external_id, event_at, metadata_json) VALUES (?, ?, 'send', ?, ?, ?)",
        (row["lead_id"], touchpoint_id, message_id, timestamp, json.dumps({"thread_id": thread_id})),
    )
    db.commit()
    return db.execute("SELECT * FROM touchpoints WHERE id=?", (touchpoint_id,)).fetchone()


def record_external_send_incident(db: sqlite3.Connection, args: argparse.Namespace) -> None:
    """Record an already-sent message that bypassed the local approval journal.

    This is an incident-recovery command, not part of the normal agent workflow.
    It always blocks future automated contact for the brand until a human clears it.
    """
    timestamp = args.sent_at or now_iso()
    key = brand_key(args.brand)
    existing_lead = db.execute("SELECT * FROM leads WHERE brand_key=?", (key,)).fetchone()
    incident_note = f"INCIDENT: external send recorded without human approval; {args.reason}"
    if existing_lead:
        lead_id = existing_lead["id"]
        notes = " | ".join(filter(None, [existing_lead["notes"], incident_note]))
        db.execute(
            "UPDATE leads SET do_not_contact=1, notes=?, updated_at=? WHERE id=?",
            (notes, timestamp, lead_id),
        )
    else:
        db.execute(
            """INSERT INTO leads
            (brand, brand_key, website, category, country, contact_name, contact_email,
             language, source_url, source_type, fit_score, notes, do_not_contact, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)""",
            (args.brand.strip(), key, args.website, args.category, args.country,
             args.contact_name, args.contact_email, args.language, args.source_url,
             "incident-test-input", args.fit_score, incident_note, timestamp, timestamp),
        )
        lead_id = db.execute("SELECT id FROM leads WHERE brand_key=?", (key,)).fetchone()["id"]
    existing_tp = db.execute("SELECT * FROM touchpoints WHERE lead_id=? AND step='initial'", (lead_id,)).fetchone()
    if existing_tp:
        if existing_tp["message_id"] != args.message_id or existing_tp["thread_id"] != args.thread_id:
            raise RuntimeError("incident blocked: existing initial touchpoint has a different receipt")
        touchpoint_id = existing_tp["id"]
        approval_code = existing_tp["approval_code"]
    else:
        db.execute(
            """INSERT INTO touchpoints
            (lead_id, step, status, subject, body, approval_code, message_id, thread_id, sent_at, created_at, updated_at)
            VALUES (?, 'initial', 'sent', ?, ?, NULL, ?, ?, ?, ?, ?)""",
            (lead_id, args.subject, args.body, args.message_id, args.thread_id, timestamp, timestamp, timestamp),
        )
        touchpoint_id = db.execute("SELECT last_insert_rowid()").fetchone()[0]
        approval_code = f"OBS-{timestamp[:10].replace('-', '')}-{int(touchpoint_id):04d}"
        db.execute("UPDATE touchpoints SET approval_code=? WHERE id=?", (approval_code, touchpoint_id))
    db.execute(
        "INSERT OR IGNORE INTO events (lead_id, touchpoint_id, event_type, external_id, event_at, metadata_json) VALUES (?, ?, 'send', ?, ?, ?)",
        (lead_id, touchpoint_id, args.message_id, timestamp, json.dumps({"thread_id": args.thread_id, "reason": args.reason, "human_approved": False}, ensure_ascii=False)),
    )
    db.commit()
    output({"status": "incident_recorded", "touchpoint_id": touchpoint_id, "approval_code": approval_code, "brand": args.brand, "do_not_contact": True, "message_id": args.message_id, "thread_id": args.thread_id})


def record_send(db: sqlite3.Connection, args: argparse.Namespace) -> None:
    row = mark_sent_record(db, args.touchpoint_id, args.message_id, args.thread_id, args.sent_at)
    output({"status": "send_recorded", "touchpoint_id": row["id"], "brand": db.execute("SELECT brand FROM leads WHERE id=?", (row["lead_id"],)).fetchone()[0], "message_id": row["message_id"], "thread_id": row["thread_id"]})


def touchpoint_for_reply(db: sqlite3.Connection, thread_id: str) -> sqlite3.Row | None:
    return db.execute(
        """SELECT t.*, l.brand, l.do_not_contact FROM touchpoints t JOIN leads l ON l.id=t.lead_id
           WHERE t.thread_id=? ORDER BY t.id DESC LIMIT 1""",
        (thread_id,),
    ).fetchone()


def record_reply(db: sqlite3.Connection, args: argparse.Namespace) -> None:
    row = touchpoint_for_reply(db, args.thread_id)
    if not row:
        raise SystemExit("Fil Gmail non relié à une marque connue ; aucune modification effectuée.")
    if args.reply_type not in {"human", "auto", "bounce", "refusal", "opt_out"}:
        raise SystemExit("reply_type invalide.")
    event_type = {"human": "human_reply", "auto": "auto_reply", "bounce": "bounce", "refusal": "refusal", "opt_out": "opt_out"}[args.reply_type]
    timestamp = args.reply_at or now_iso()
    metadata = {"subject": args.subject, "from": args.sender, "snippet": args.snippet}
    db.execute(
        "INSERT OR IGNORE INTO events (lead_id, touchpoint_id, event_type, external_id, event_at, metadata_json) VALUES (?, ?, ?, ?, ?, ?)",
        (row["lead_id"], row["id"], event_type, args.message_id, timestamp, json.dumps(metadata, ensure_ascii=False)),
    )
    if event_type == "human_reply":
        db.execute("UPDATE touchpoints SET status='replied', updated_at=? WHERE lead_id=? AND status IN ('sent','approved','draft')", (now_iso(), row["lead_id"]))
    elif event_type in {"bounce", "refusal", "opt_out"}:
        status = "bounced" if event_type == "bounce" else ("opted_out" if event_type == "opt_out" else "cancelled")
        db.execute("UPDATE touchpoints SET status=?, updated_at=? WHERE lead_id=? AND status IN ('sent','approved','draft')", (status, now_iso(), row["lead_id"]))
        if event_type == "opt_out":
            db.execute("UPDATE leads SET do_not_contact=1, updated_at=? WHERE id=?", (now_iso(), row["lead_id"]))
    db.commit()
    output({"status": "reply_recorded", "brand": row["brand"], "event_type": event_type, "thread_id": args.thread_id})


def due_followups(db: sqlite3.Connection, args: argparse.Namespace) -> None:
    now = parse_dt(args.now) if args.now else datetime.now(timezone.utc)
    result = []
    for row in db.execute(
        """SELECT l.*, t.id AS initial_id, t.thread_id AS initial_thread_id, t.sent_at AS initial_sent_at,
                  f1.id AS f1_id, f1.status AS f1_status, f1.sent_at AS f1_sent_at,
                  f2.id AS f2_id, f2.status AS f2_status, f2.sent_at AS f2_sent_at
           FROM leads l JOIN touchpoints t ON t.lead_id=l.id AND t.step='initial'
           LEFT JOIN touchpoints f1 ON f1.lead_id=l.id AND f1.step='followup1'
           LEFT JOIN touchpoints f2 ON f2.lead_id=l.id AND f2.step='followup2'
           WHERE t.status='sent' AND l.do_not_contact=0
           ORDER BY t.sent_at ASC"""
    ):
        sent_initial = parse_dt(row["initial_sent_at"])
        age = now - sent_initial
        candidate = None
        if age >= timedelta(days=4) and not row["f1_id"]:
            candidate = {"step": "followup1", "after_days": 4}
        elif row["f1_id"] and row["f1_status"] == "sent" and not row["f2_id"] and age >= timedelta(days=9):
            candidate = {"step": "followup2", "after_days": 9}
        if candidate:
            result.append({"lead_id": row["id"], "brand": row["brand"], "contact_email": row["contact_email"], "thread_id": row["initial_thread_id"], **candidate})
    output(result[: max(args.limit, 0)])


def show_lead(db: sqlite3.Connection, args: argparse.Namespace) -> None:
    if args.lead_id:
        lead = db.execute("SELECT * FROM leads WHERE id=?", (args.lead_id,)).fetchone()
    else:
        lead = db.execute("SELECT * FROM leads WHERE brand_key=?", (brand_key(args.brand),)).fetchone()
    if not lead:
        raise SystemExit("Lead introuvable.")
    touchpoints = db.execute("SELECT * FROM touchpoints WHERE lead_id=? ORDER BY id", (lead["id"],)).fetchall()
    events = db.execute("SELECT * FROM events WHERE lead_id=? ORDER BY id", (lead["id"],)).fetchall()
    output({"lead": dict(lead), "touchpoints": [dict(row) for row in touchpoints], "events": [dict(row) for row in events]})


def stats(db: sqlite3.Connection, args: argparse.Namespace) -> None:
    end = parse_dt(args.end) if args.end else datetime.now(timezone.utc)
    start = parse_dt(args.start) if args.start else end - timedelta(days=args.days)
    start_s, end_s = iso(start), iso(end)
    cohort = db.execute(
        """SELECT DISTINCT l.id, l.brand FROM leads l JOIN touchpoints t ON t.lead_id=l.id
           WHERE t.step='initial' AND t.status IN ('sent','replied') AND t.sent_at>=? AND t.sent_at<?""",
        (start_s, end_s),
    ).fetchall()
    ids = [row["id"] for row in cohort]
    replies = 0
    if ids:
        marks = ",".join("?" for _ in ids)
        replies = db.execute(
            f"SELECT COUNT(DISTINCT lead_id) AS n FROM events WHERE event_type='human_reply' AND event_at>=? AND event_at<? AND lead_id IN ({marks})",
            [start_s, end_s, *ids],
        ).fetchone()["n"]
    contacted = len(cohort)
    rate = round((replies / contacted) * 100, 2) if contacted else 0.0
    output({"period_start": start_s, "period_end": end_s, "brands_contacted": contacted, "human_replies": replies, "response_rate_percent": rate})


def main() -> int:
    parser = argparse.ArgumentParser(description="Stage Outreach local state store")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("init")

    p = sub.add_parser("lead-upsert")
    p.add_argument("--brand", required=True); p.add_argument("--website", default=""); p.add_argument("--category", default="")
    p.add_argument("--country", default=""); p.add_argument("--contact-name", default=""); p.add_argument("--contact-email", default="")
    p.add_argument("--language", default="en"); p.add_argument("--source-url", required=True); p.add_argument("--source-type", default="official website")
    p.add_argument("--fit-score", type=int, default=0); p.add_argument("--notes", default="")

    p = sub.add_parser("touchpoint-upsert")
    p.add_argument("--brand", default=""); p.add_argument("--lead-id", type=int, default=0)
    p.add_argument("--step", choices=["initial", "followup1", "followup2"], required=True)
    p.add_argument("--status", choices=["draft", "approved"], default="draft"); p.add_argument("--subject", required=True); p.add_argument("--body", required=True)

    p = sub.add_parser("approve"); p.add_argument("--touchpoint-id", type=int, default=0); p.add_argument("--approval-code", default="")
    p = sub.add_parser("record-send"); p.add_argument("--touchpoint-id", type=int, required=True); p.add_argument("--message-id", required=True); p.add_argument("--thread-id", required=True); p.add_argument("--sent-at", default="")
    p = sub.add_parser("record-external-send-incident"); p.add_argument("--brand", required=True); p.add_argument("--website", default=""); p.add_argument("--category", default=""); p.add_argument("--country", default=""); p.add_argument("--contact-name", default=""); p.add_argument("--contact-email", required=True); p.add_argument("--language", default="en"); p.add_argument("--source-url", default=""); p.add_argument("--fit-score", type=int, default=0); p.add_argument("--subject", required=True); p.add_argument("--body", required=True); p.add_argument("--message-id", required=True); p.add_argument("--thread-id", required=True); p.add_argument("--sent-at", default=""); p.add_argument("--reason", required=True)

    p = sub.add_parser("record-reply"); p.add_argument("--thread-id", required=True); p.add_argument("--message-id", required=True); p.add_argument("--reply-type", choices=["human", "auto", "bounce", "refusal", "opt_out"], default="human"); p.add_argument("--reply-at", default=""); p.add_argument("--sender", default=""); p.add_argument("--subject", default=""); p.add_argument("--snippet", default="")
    p = sub.add_parser("due-followups"); p.add_argument("--now", default=""); p.add_argument("--limit", type=int, default=5)
    p = sub.add_parser("show-lead"); p.add_argument("--brand", default=""); p.add_argument("--lead-id", type=int, default=0)
    p = sub.add_parser("stats"); p.add_argument("--start", default=""); p.add_argument("--end", default=""); p.add_argument("--days", type=int, default=7)

    args = parser.parse_args()
    db = connect(args.db)
    try:
        if args.command == "init": output({"status": "initialized", "db": str(args.db)})
        elif args.command == "lead-upsert": lead_upsert(db, args)
        elif args.command == "touchpoint-upsert": touchpoint_upsert(db, args)
        elif args.command == "approve": approve(db, args)
        elif args.command == "record-send": record_send(db, args)
        elif args.command == "record-external-send-incident": record_external_send_incident(db, args)
        elif args.command == "record-reply": record_reply(db, args)
        elif args.command == "due-followups": due_followups(db, args)
        elif args.command == "show-lead": show_lead(db, args)
        elif args.command == "stats": stats(db, args)
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (sqlite3.IntegrityError, RuntimeError) as exc:
        print(f"Erreur : {exc}", file=sys.stderr)
        raise SystemExit(2)
