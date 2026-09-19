#!/usr/bin/env python3
"""Deterministic Stage outreach workflow.

Research and copy are performed by the agent, but the state-changing steps are
centralized here so a Discord confirmation cannot silently become an email send.
This module never talks to Gmail or Discord.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import unicodedata
from datetime import datetime
from email.utils import parseaddr
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

import stage_db

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_STATE = ROOT / "data" / "stage_offer_state.json"
DEFAULT_DRAFTS = ROOT / "drafts"
TIMEZONE = ZoneInfo("Europe/Paris")
GO_COMMANDS = frozenset({"go", "oui", "oui go", "oui, go", "vas-y", "vas y"})
URL_RE = re.compile(r"https?://[^\s<>()]+", re.IGNORECASE)
EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
OPT_OUT_RE = re.compile(
    r"(?:ne\s+(?:plus|souhaitez\s+plus)\s+(?:me\s+)?contacter|ne\s+plus\s+recevoir|"
    r"no\s+longer\s+contact|please\s+let\s+me\s+know.*(?:not|stop)|"
    r"tell\s+me.*(?:not|stop|no\s+longer)|unsubscribe|opt[- ]out)",
    re.IGNORECASE,
)


class WorkflowError(RuntimeError):
    """An expected, user-actionable workflow rejection."""


def normalize_command(text: str) -> str:
    value = unicodedata.normalize("NFKC", str(text or "")).casefold().strip()
    value = re.sub(r"\s+", " ", value)
    return value.rstrip(".!?;:")


def is_explicit_go(text: str) -> bool:
    """Return true only for a short confirmation, never for a mixed request."""
    return normalize_command(text) in GO_COMMANDS


def _parse_now(value: str | None) -> datetime:
    if not value:
        return datetime.now(TIMEZONE)
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=TIMEZONE)
    return parsed.astimezone(TIMEZONE)


def load_current_offer(state_path: Path = DEFAULT_STATE, now: datetime | None = None) -> dict:
    now = now or datetime.now(TIMEZONE)
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError) as exc:
        raise WorkflowError("offre du jour introuvable : le cron du matin doit d'abord s'exécuter") from exc
    today = now.astimezone(TIMEZONE).date().isoformat()
    brand = str(state.get("offered_company") or state.get("offered_brand") or "").strip()
    if state.get("offered_date") != today or not brand:
        raise WorkflowError("offre expirée ou absente : aucune préparation ne peut démarrer")
    offer_id = str(state.get("offer_id") or f"OBS-OFFER-{today.replace('-', '')}")
    if not re.fullmatch(r"OBS-OFFER-\d{8}(?:-\d{2})?", offer_id):
        raise WorkflowError("identifiant d'offre invalide")
    if state.get("offer_id") != offer_id:
        state["offer_id"] = offer_id
        state_path.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        try:
            state_path.chmod(0o600)
        except OSError:
            pass
    return {"offer_id": offer_id, "offered_date": today, "brand": brand}


def _validate_url(value: str, field: str) -> str:
    value = str(value or "").strip()
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise WorkflowError(f"{field} doit être une URL HTTP(S) complète")
    return value


def _validate_email(value: str) -> str:
    value = str(value or "").strip()
    address = parseaddr(value)[1]
    if address != value or not EMAIL_RE.fullmatch(value):
        raise WorkflowError("contact_email doit être une adresse professionnelle publique valide")
    if value.casefold().endswith(("@example.com", "@example.org", "@example.net")):
        raise WorkflowError("contact_email de test refusé pour une préparation réelle")
    return value


def _safe_draft_path(code: str, brand: str, drafts_dir: Path) -> Path:
    slug = re.sub(r"[^a-z0-9]+", "-", unicodedata.normalize("NFKD", brand).encode("ascii", "ignore").decode().lower()).strip("-")
    slug = slug or "brand"
    root = drafts_dir.resolve()
    root.mkdir(parents=True, exist_ok=True)
    path = (root / f"{code}-{slug}.md").resolve()
    if path.parent != root:
        raise WorkflowError("chemin de brouillon hors du dossier autorisé")
    return path


def _validate_payload(data: dict) -> None:
    if not str(data.get("brand") or "").strip():
        raise WorkflowError("brand est obligatoire")
    if not str(data.get("offer_id") or "").strip():
        raise WorkflowError("offer_id est obligatoire")
    _validate_url(data.get("website"), "website")
    _validate_url(data.get("source_url"), "source_url")
    if "official" not in str(data.get("source_type") or "").casefold():
        raise WorkflowError("source_type doit indiquer une source officielle")
    _validate_email(data.get("contact_email"))
    subject = str(data.get("subject") or "").strip()
    body = str(data.get("body") or "").strip()
    if not subject or "\n" in subject or "\r" in subject:
        raise WorkflowError("subject doit être une ligne non vide")
    if not body:
        raise WorkflowError("body est obligatoire")
    if len(URL_RE.findall(body)) > 1:
        raise WorkflowError("le premier email ne peut contenir qu'un seul lien")
    try:
        fit_score = int(data.get("fit_score", 0))
    except (TypeError, ValueError) as exc:
        raise WorkflowError("fit_score doit être compris entre 0 et 5") from exc
    if not 0 <= fit_score <= 5:
        raise WorkflowError("fit_score doit être compris entre 0 et 5")


def _draft_markdown(data: dict, row, draft_path: Path) -> str:
    risk = str(data.get("risk") or "Aucun risque particulier signalé.").strip()
    return (
        "# Stage Outreach — brouillon\n\n"
        f"- **Lot :** `{row['approval_code']}`\n"
        f"- **Entreprise :** {data['brand']}\n"
        f"- **Secteur / sujet :** {data.get('category') or 'technologie'}\n"
        f"- **Localisation :** {data.get('country') or 'à vérifier'}\n"
        f"- **Contact public :** {data['contact_name'] or 'non précisé'} <{data['contact_email']}>\n"
        f"- **Site officiel :** {data['website']}\n"
        f"- **Source du contact :** {data['source_url']} ({data['source_type']})\n"
        f"- **Score de pertinence :** {data.get('fit_score', 0)}/5\n"
        f"- **Angle :** {data.get('angle') or 'Stage d’observation : découvrir l’équipe et ses métiers.'}\n"
        f"- **Risque / limite :** {risk}\n"
        "\n## Objet\n\n"
        f"{data['subject']}\n\n"
        "## Corps exact soumis à validation\n\n"
        "```text\n"
        f"{data['body'].rstrip()}\n"
        "```\n\n"
        f"Fichier local : `{draft_path}`\n"
    )


def prepare_draft(
    *,
    brand: str,
    offer_id: str,
    website: str,
    category: str,
    country: str,
    contact_name: str,
    contact_email: str,
    language: str,
    source_url: str,
    source_type: str,
    fit_score: int,
    product: str,
    angle: str,
    risk: str,
    subject: str,
    body: str,
    db_path: Path = stage_db.DEFAULT_DB,
    drafts_dir: Path = DEFAULT_DRAFTS,
    state_path: Path = DEFAULT_STATE,
    now: datetime | None = None,
) -> dict:
    data = locals().copy()
    data.pop("db_path", None)
    data.pop("drafts_dir", None)
    data.pop("state_path", None)
    data.pop("now", None)
    _validate_payload(data)
    offer = load_current_offer(state_path, now)
    if offer["offer_id"] != offer_id or offer["brand"].casefold() != str(brand).strip().casefold():
        raise WorkflowError("offer ne correspond pas à la marque du jour")

    lead_args = SimpleNamespace(
        brand=brand, website=website, category=category, country=country,
        contact_name=contact_name, contact_email=contact_email, language=language,
        source_url=source_url, source_type=source_type, fit_score=int(fit_score),
        notes=json.dumps({"offer_id": offer_id, "angle": angle, "risk": risk}, ensure_ascii=False),
    )
    touchpoint_args = SimpleNamespace(step="initial", status="draft", subject=subject.strip(), body=body.strip())
    db = stage_db.connect(Path(db_path))
    try:
        lead = stage_db.upsert_lead_record(db, lead_args)
        touchpoint = stage_db.upsert_touchpoint_record(db, touchpoint_args, lead)
    finally:
        db.close()

    draft_path = _safe_draft_path(str(touchpoint["approval_code"]), brand, Path(drafts_dir))
    if touchpoint["status"] == "approved":
        # Never overwrite a human-approved content set, even if a later agent run
        # repeats the research with different wording.
        if not draft_path.exists():
            draft_path.write_text(_draft_markdown(data, touchpoint, draft_path), encoding="utf-8")
            draft_path.chmod(0o600)
        return {
            "status": "already_approved",
            "approval_code": touchpoint["approval_code"],
            "touchpoint_id": touchpoint["id"],
            "draft_path": str(draft_path),
            "brand": brand,
            "contact_email": contact_email,
        }
    draft_path.write_text(_draft_markdown(data, touchpoint, draft_path), encoding="utf-8")
    draft_path.chmod(0o600)
    return {
        "status": "draft_prepared",
        "approval_code": touchpoint["approval_code"],
        "touchpoint_id": touchpoint["id"],
        "draft_path": str(draft_path),
        "brand": brand,
        "contact_email": contact_email,
        "source_url": source_url,
    }


def create_offer(*, company: str, website: str, source_url: str, reason: str,
                 fit_score: int, state_path: Path = DEFAULT_STATE,
                 now: datetime | None = None) -> dict:
    """Create one explicit, source-backed candidate token locally."""
    now = now or datetime.now(TIMEZONE)
    company = str(company or "").strip()
    if not company:
        raise WorkflowError("company est obligatoire")
    _validate_url(website, "website")
    _validate_url(source_url, "source_url")
    try:
        fit_score = int(fit_score)
    except (TypeError, ValueError) as exc:
        raise WorkflowError("fit_score doit être compris entre 0 et 5") from exc
    if not 0 <= fit_score <= 5:
        raise WorkflowError("fit_score doit être compris entre 0 et 5")
    today = now.astimezone(TIMEZONE).date().isoformat()
    previous = {}
    try:
        previous = json.loads(state_path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        pass
    sequence = int(previous.get("sequence", 0)) + 1 if previous.get("offered_date") == today else 1
    offer_id = f"OBS-OFFER-{today.replace('-', '')}-{sequence:02d}"
    state = {
        "sequence": sequence, "offered_date": today,
        "offered_company": company, "offered_brand": company,
        "offer_id": offer_id, "website": website, "source_url": source_url,
        "reason": str(reason or "").strip(), "fit_score": fit_score,
    }
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    try:
        state_path.chmod(0o600)
    except OSError:
        pass
    return {"status": "offer_created", "offer_id": offer_id, "company": company,
            "source_url": source_url, "fit_score": fit_score}


def approve_draft(approval: str, db_path: Path = stage_db.DEFAULT_DB) -> dict:
    db = stage_db.connect(Path(db_path))
    try:
        row = stage_db.approve_record(db, approval)
        lead = db.execute("SELECT brand FROM leads WHERE id=?", (row["lead_id"],)).fetchone()
        return {"status": "approved", "approval_code": row["approval_code"], "touchpoint_id": row["id"], "brand": lead["brand"]}
    finally:
        db.close()


def show_draft(approval: str, db_path: Path = stage_db.DEFAULT_DB) -> dict:
    db = stage_db.connect(Path(db_path))
    try:
        row = stage_db.resolve_touchpoint(db, approval)
        if not row:
            raise WorkflowError("touchpoint introuvable")
        lead = db.execute("SELECT * FROM leads WHERE id=?", (row["lead_id"],)).fetchone()
        return {"lead": dict(lead), "touchpoint": dict(row)}
    finally:
        db.close()


def _prepare_from_args(args: argparse.Namespace) -> dict:
    body = args.body
    if args.body_file:
        body = Path(args.body_file).read_text(encoding="utf-8")
    if body is None:
        raise WorkflowError("--body ou --body-file est obligatoire")
    return prepare_draft(
        brand=args.brand, offer_id=args.offer_id, website=args.website,
        category=args.category, country=args.country, contact_name=args.contact_name,
        contact_email=args.contact_email, language=args.language, source_url=args.source_url,
        source_type=args.source_type, fit_score=args.fit_score, product=args.product,
        angle=args.angle, risk=args.risk, subject=args.subject, body=body,
        db_path=args.db, drafts_dir=args.drafts_dir, state_path=args.state_path,
        now=_parse_now(args.now),
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Stage deterministic outreach workflow")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("offer-current")
    p.add_argument("--state-path", type=Path, default=DEFAULT_STATE)
    p.add_argument("--now", default="")

    p = sub.add_parser("offer-create")
    p.add_argument("--company", required=True)
    p.add_argument("--website", required=True)
    p.add_argument("--source-url", required=True)
    p.add_argument("--reason", default="")
    p.add_argument("--fit-score", type=int, default=0)
    p.add_argument("--state-path", type=Path, default=DEFAULT_STATE)
    p.add_argument("--now", default="")

    p = sub.add_parser("is-go")
    p.add_argument("text", nargs="?")
    p.add_argument("--text", dest="text_option", default="")

    p = sub.add_parser("prepare")
    p.add_argument("--brand", "--company", dest="brand", required=True); p.add_argument("--offer-id", required=True)
    p.add_argument("--website", required=True); p.add_argument("--category", default="")
    p.add_argument("--country", default=""); p.add_argument("--contact-name", default="")
    p.add_argument("--contact-email", required=True); p.add_argument("--language", default="en")
    p.add_argument("--source-url", required=True); p.add_argument("--source-type", default="official website")
    p.add_argument("--fit-score", type=int, default=0); p.add_argument("--product", default="")
    p.add_argument("--angle", default=""); p.add_argument("--risk", default="")
    p.add_argument("--subject", required=True); body = p.add_mutually_exclusive_group(required=True)
    body.add_argument("--body"); body.add_argument("--body-file", type=Path)
    p.add_argument("--db", type=Path, default=stage_db.DEFAULT_DB); p.add_argument("--drafts-dir", type=Path, default=DEFAULT_DRAFTS)
    p.add_argument("--state-path", type=Path, default=DEFAULT_STATE); p.add_argument("--now", default="")

    p = sub.add_parser("approve")
    p.add_argument("approval"); p.add_argument("--db", type=Path, default=stage_db.DEFAULT_DB)

    p = sub.add_parser("show")
    p.add_argument("approval"); p.add_argument("--db", type=Path, default=stage_db.DEFAULT_DB)

    args = parser.parse_args()
    try:
        if args.command == "offer-current":
            result = load_current_offer(args.state_path, _parse_now(args.now))
        elif args.command == "offer-create":
            result = create_offer(company=args.company, website=args.website, source_url=args.source_url,
                                  reason=args.reason, fit_score=args.fit_score,
                                  state_path=args.state_path, now=_parse_now(args.now))
        elif args.command == "is-go":
            text = args.text_option or args.text or ""
            result = {"is_explicit_go": is_explicit_go(text), "normalized": normalize_command(text)}
        elif args.command == "prepare":
            result = _prepare_from_args(args)
        elif args.command == "approve":
            result = approve_draft(args.approval, args.db)
        else:
            result = show_draft(args.approval, args.db)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (WorkflowError, RuntimeError, OSError) as exc:
        print(str(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
