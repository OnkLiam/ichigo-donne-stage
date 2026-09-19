from __future__ import annotations

import json
import os
import stat
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import stage_db  # noqa: E402
import stage_workflow as workflow  # noqa: E402


PARIS = ZoneInfo("Europe/Paris")


class OutreachWorkflowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.db = root / "stage.sqlite3"
        self.drafts = root / "drafts"
        self.state = root / "stage_offer_state.json"
        self.state.write_text(
            json.dumps(
                {
                    "last_index": 0,
                    "offered_date": "2026-09-14",
                    "offered_brand": "Example Hardware",
                    "offer_id": "OBS-OFFER-20260914",
                }
            ),
            encoding="utf-8",
        )
        self.now = datetime(2026, 9, 14, 10, 0, tzinfo=PARIS)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def prepare_kwargs(self) -> dict:
        return {
            "brand": "Example Hardware",
            "offer_id": "OBS-OFFER-20260914",
            "website": "https://brand.invalid",
            "category": "monitors",
            "country": "Taiwan / international",
            "contact_name": "Example Partnerships",
            "contact_email": "contact@brand.invalid",
            "language": "en",
            "source_url": "https://brand.invalid/contact",
            "source_type": "official website",
            "fit_score": 5,
            "product": "Example gaming monitor",
            "angle": "Une vidéo TikTok de découverte et test honnête d'un écran Example Hardware.",
            "risk": "Le site ne garantit pas qu'une demande de partenariat sera traitée.",
            "subject": "Observation x Example Hardware — proposition de vidéo TikTok",
            "body": (
                "Hello Example Partnerships,\n\n"
                "After looking at the official Example OLED product page, I thought of a video "
                "that could make sense for Example Hardware.\n\n"
                "I am ExampleCreator, the creator behind ObservationExample, a French TikTok channel focused on tech "
                "and gaming hardware. The account displays publicly 12.1K followers and 96.7K likes.\n\n"
                "The angle would be simple: show how the monitor performs in a real gaming setup, "
                "not just read out a spec sheet.\n\n"
                "My preferred format is a product provided in exchange for a dedicated TikTok. "
                "The content stays honest: I cannot promise a positive review, but I can give a "
                "clear take after using it. If you usually work through product loans, I can adapt "
                "to your process.\n\n"
                "Do you handle creator collaborations at Example Hardware? If not, could you point me to the "
                "right person?\n\n"
                "If you would rather not receive this type of request, just tell me and I will not "
                "contact you again.\n\n"
                "Best,\nExampleCreator\nObservationExample"
            ),
            "db_path": self.db,
            "drafts_dir": self.drafts,
            "state_path": self.state,
            "now": self.now,
        }

    def test_go_is_exact_and_not_triggered_by_unrelated_text(self) -> None:
        self.assertTrue(workflow.is_explicit_go(" oui go "))
        self.assertTrue(workflow.is_explicit_go("VAS-Y"))
        self.assertTrue(workflow.is_explicit_go("oui"))
        self.assertFalse(workflow.is_explicit_go("oui, prépare aussi Example Hardware 2 et Example Hardware"))
        self.assertFalse(workflow.is_explicit_go("go demain"))

    def test_prepare_is_idempotent_and_writes_private_draft(self) -> None:
        first = workflow.prepare_draft(**self.prepare_kwargs())
        second = workflow.prepare_draft(**self.prepare_kwargs())

        self.assertEqual(first["status"], "draft_prepared")
        self.assertEqual(first["approval_code"], second["approval_code"])
        self.assertEqual(first["touchpoint_id"], second["touchpoint_id"])
        draft_path = Path(first["draft_path"])
        self.assertTrue(draft_path.is_file())
        self.assertEqual(stat.S_IMODE(draft_path.stat().st_mode), 0o600)
        content = draft_path.read_text(encoding="utf-8")
        self.assertIn(first["approval_code"], content)
        self.assertIn("contact@brand.invalid", content)
        self.assertIn("Observation x Example Hardware", content)

        db = stage_db.connect(self.db)
        try:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM leads").fetchone()[0], 1)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM touchpoints").fetchone()[0], 1)
        finally:
            db.close()

    def test_prepare_rejects_stale_offer(self) -> None:
        kwargs = self.prepare_kwargs()
        kwargs["offer_id"] = "OBS-OFFER-20260913"
        with self.assertRaisesRegex(workflow.WorkflowError, "offer"):
            workflow.prepare_draft(**kwargs)

    def test_prepare_accepts_creator_template_without_opt_out_phrase(self) -> None:
        kwargs = self.prepare_kwargs()
        kwargs["body"] = "Bonjour l’équipe Example Hardware,\n\nJe suis ExampleCreator, créateur de contenu tech sur TikTok.\n\nJ’aimerais tester un produit en échange d’une vidéo dédiée.\n\nMerci,\nObservationExample"
        result = workflow.prepare_draft(**kwargs)
        self.assertEqual(result["status"], "draft_prepared")


if __name__ == "__main__":
    unittest.main()
