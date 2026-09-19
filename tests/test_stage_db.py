#!/usr/bin/env python3
import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DB = ROOT / "src" / "stage_db.py"


def run(db: Path, *args: str):
    result = subprocess.run([sys.executable, str(DB), "--db", str(db), *args], capture_output=True, text=True)
    if result.returncode:
        raise AssertionError(result.stderr or result.stdout)
    return json.loads(result.stdout)


class OutreachDbTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "test.sqlite3"
        run(self.db, "init")

    def tearDown(self):
        self.tmp.cleanup()

    def test_send_reply_and_cohort_metrics(self):
        lead = run(self.db, "lead-upsert", "--brand", "Example Hardware 2", "--source-url", "https://brand.invalid/support")
        draft = run(self.db, "touchpoint-upsert", "--lead-id", str(lead["lead_id"]), "--step", "initial", "--status", "approved", "--subject", "Collaboration", "--body", "Bonjour")
        sent_at = "2026-09-07T10:00:00Z"
        sent = run(self.db, "record-send", "--touchpoint-id", str(draft["touchpoint_id"]), "--message-id", "m1", "--thread-id", "t1", "--sent-at", sent_at)
        self.assertEqual(sent["status"], "send_recorded")
        run(self.db, "record-reply", "--thread-id", "t1", "--message-id", "r1", "--reply-type", "human", "--reply-at", "2026-09-08T10:00:00Z")
        metrics = run(self.db, "stats", "--start", "2026-09-07T00:00:00Z", "--end", "2026-09-14T00:00:00Z")
        self.assertEqual(metrics["brands_contacted"], 1)
        self.assertEqual(metrics["human_replies"], 1)
        self.assertEqual(metrics["response_rate_percent"], 100.0)

    def test_external_send_incident_is_blocked_from_followups(self):
        incident = run(
            self.db,
            "record-external-send-incident",
            "--brand", "Example Brand",
            "--contact-email", "contact@brand.invalid",
            "--source-url", "https://brand.invalid/contact",
            "--subject", "Incident",
            "--body", "External body",
            "--message-id", "external-1",
            "--thread-id", "external-1",
            "--sent-at", "2026-09-01T10:00:00Z",
            "--reason", "test recovery",
        )
        self.assertTrue(incident["do_not_contact"])
        shown = run(self.db, "show-lead", "--brand", "Example Brand")
        self.assertEqual(shown["lead"]["do_not_contact"], 1)
        self.assertEqual(shown["touchpoints"][0]["status"], "sent")
        metadata = json.loads(shown["events"][0]["metadata_json"])
        self.assertFalse(metadata["human_approved"])
        self.assertEqual(run(self.db, "due-followups", "--now", "2026-09-12T10:00:00Z"), [])

        lead = run(self.db, "lead-upsert", "--brand", "Example Hardware", "--source-url", "https://brand.invalid/contact")
        draft = run(self.db, "touchpoint-upsert", "--lead-id", str(lead["lead_id"]), "--step", "initial", "--status", "approved", "--subject", "Collaboration", "--body", "Hello")
        run(self.db, "record-send", "--touchpoint-id", str(draft["touchpoint_id"]), "--message-id", "m2", "--thread-id", "t2", "--sent-at", "2026-09-01T10:00:00Z")
        run(self.db, "record-reply", "--thread-id", "t2", "--message-id", "r2", "--reply-type", "opt_out", "--reply-at", "2026-09-02T10:00:00Z")
        due = run(self.db, "due-followups", "--now", "2026-09-12T10:00:00Z")
        self.assertEqual(due, [])


if __name__ == "__main__":
    unittest.main()
