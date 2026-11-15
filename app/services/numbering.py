"""Certificate number allocation: PS-YYYY-NNNNNN.

The counter row is incremented with a single atomic UPDATE inside the caller's
transaction, so concurrent issuers can never receive the same number; the
UNIQUE constraint on certificates.certificate_no is a second safety net.
Numbers are never reused, even if a certificate is later revoked.
"""
from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..config import settings


def next_certificate_no(db: Session, year: int) -> str:
    scope = f"{settings.cert_prefix}-{year}"
    for _ in range(3):
        row = db.execute(
            text("UPDATE id_counters SET last_value = last_value + 1 WHERE scope = :s RETURNING last_value"),
            {"s": scope},
        ).first()
        if row is not None:
            return f"{scope}-{int(row[0]):06d}"
        try:
            with db.begin_nested():
                db.execute(text("INSERT INTO id_counters (scope, last_value) VALUES (:s, 0)"), {"s": scope})
        except IntegrityError:
            pass  # another transaction created it first; retry the UPDATE
    raise RuntimeError("Could not allocate a certificate number")


def peek_next(db: Session, year: int) -> str:
    scope = f"{settings.cert_prefix}-{year}"
    row = db.execute(text("SELECT last_value FROM id_counters WHERE scope = :s"), {"s": scope}).first()
    return f"{scope}-{(int(row[0]) if row else 0) + 1:06d}"
