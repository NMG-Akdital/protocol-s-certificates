"""Center-level settings editable from the admin dashboard (stored in the DB)."""
from __future__ import annotations

import os

from sqlalchemy.orm import Session

from ..models import Setting

DEFAULTS: dict[str, str] = {
    "center_name": "Protocol S for Training and Consultancy",
    "brand_name": "PROTOCOL S",
    "brand_descriptor": "FOR TRAINING AND CONSULTANCY",
    "signatory_name": "",
    "signatory_title": "Authorized Representative",
    "signatory_signature_file": "",
    "contact_line": "",            # e.g. website or email shown in certificate footer (optional)
    "default_allow_public_pdf": "0",
    "public_full_name_without_key": "0",  # show full name when the ID is typed manually (no QR key)
    # What the QR code contains:
    #   text      = the certificate details as text (works offline, no website needed)
    #   text_link = the details as text + the online verification link
    #   link      = only the online verification link (live status, revocation)
    "qr_mode": os.environ.get("QR_MODE", "text"),
}


def get_all(db: Session) -> dict[str, str]:
    values = dict(DEFAULTS)
    for row in db.query(Setting).all():
        values[row.key] = row.value
    return values


def get(db: Session, key: str) -> str:
    row = db.get(Setting, key)
    return row.value if row else DEFAULTS.get(key, "")


def set_many(db: Session, updates: dict[str, str]) -> None:
    for key, value in updates.items():
        if key not in DEFAULTS:
            continue
        row = db.get(Setting, key)
        if row:
            row.value = value
        else:
            db.add(Setting(key=key, value=value))


def flag(values: dict[str, str], key: str) -> bool:
    return values.get(key, "0") in {"1", "true", "on", "yes"}
