"""Certificate lifecycle: draft -> issued -> (revoked). Issued data is immutable."""
from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
import unicodedata
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from ..config import settings
from ..models import Accreditation, Certificate, Course, Instructor, Participant, User, utcnow
from . import audit
from .numbering import next_certificate_no


class CertificateError(ValueError):
    pass


# ---------- helpers ----------

_space = re.compile(r"\s+")


def clean_name(value: str) -> str:
    value = unicodedata.normalize("NFC", value or "")
    value = "".join(ch for ch in value if unicodedata.category(ch)[0] != "C")  # drop control chars
    return _space.sub(" ", value).strip()


def name_key(value: str) -> str:
    return clean_name(value).casefold()


def parse_hours(value) -> Decimal:
    if isinstance(value, (int, float, Decimal)):
        d = Decimal(str(value))
    else:
        m = re.search(r"\d+(?:[.,]\d+)?", str(value or ""))
        if not m:
            raise CertificateError("Duration must be a number of hours")
        d = Decimal(m.group(0).replace(",", "."))
    if d <= 0 or d > 2000:
        raise CertificateError("Duration must be between 0 and 2000 hours")
    return d.quantize(Decimal("0.1"))


def hours_display(d: Decimal | float | int) -> str:
    d = Decimal(str(d))
    return str(int(d)) if d == d.to_integral_value() else f"{d.normalize()}"


def verification_url(cert: Certificate) -> str:
    return f"{settings.public_base_url}/verify/{cert.certificate_no}?k={cert.verify_token}"


def public_lookup_url() -> str:
    return f"{settings.public_base_url}/verify"


def _canonical(cert: Certificate) -> bytes:
    payload = {
        "no": cert.certificate_no,
        "name": cert.participant_name,
        "course": cert.course_title,
        "code": cert.course_code,
        "hours": hours_display(cert.duration_hours),
        "start": cert.start_date.isoformat(),
        "end": cert.completion_date.isoformat(),
        "issued": cert.issue_date.isoformat() if cert.issue_date else None,
        "instructor": cert.instructor_name,
        "exp": cert.expiry_date.isoformat() if cert.expiry_date else None,
        "acc": cert.accreditations_json or "[]",
        "k": cert.verify_token,
    }
    return json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def compute_signature(cert: Certificate) -> str:
    return hmac.new(settings.cert_signing_key.encode(), _canonical(cert), hashlib.sha256).hexdigest()


def signature_ok(cert: Certificate) -> bool:
    return bool(cert.signature) and hmac.compare_digest(cert.signature, compute_signature(cert))


# ---------- input ----------

@dataclass
class CertificateInput:
    participant_id: int
    course_id: int
    start_date: date
    completion_date: date
    duration_hours: Decimal
    instructor_id: int | None = None
    instructor_name: str = ""
    course_title: str | None = None   # override of the course's title for this certificate
    batch_id: int | None = None
    allow_public_pdf: bool = False
    expiry_date: date | None = None       # explicit "valid until"; None = use the course's validity period
    no_expiry: bool = False               # force "does not expire" for this certificate


def _validate(db: Session, data: CertificateInput) -> tuple[Participant, Course, Instructor | None]:
    participant = db.get(Participant, data.participant_id)
    course = db.get(Course, data.course_id)
    if not participant:
        raise CertificateError("Participant not found")
    if not course:
        raise CertificateError("Course not found")
    if data.start_date > data.completion_date:
        raise CertificateError("Training start date cannot be after the completion date")
    if data.completion_date > date.today():
        raise CertificateError("Completion date cannot be in the future")
    instructor = db.get(Instructor, data.instructor_id) if data.instructor_id else None
    if data.instructor_id and not instructor:
        raise CertificateError("Instructor not found")
    if not clean_name(participant.full_name):
        raise CertificateError("Participant name is empty")
    if data.expiry_date and data.expiry_date <= data.completion_date:
        raise CertificateError("Expiry date must be after the completion date")
    return participant, course, instructor


def add_months(d: date, months: int) -> date:
    import calendar
    y, m = divmod(d.month - 1 + months, 12)
    y, m = d.year + y, m + 1
    return date(y, m, min(d.day, calendar.monthrange(y, m)[1]))


def compute_expiry(course: Course, completion: date, explicit: date | None = None, no_expiry: bool = False) -> date | None:
    if no_expiry:
        return None
    if explicit:
        return explicit
    if course.validity_months:
        return add_months(completion, int(course.validity_months)) - __import__("datetime").timedelta(days=1)
    return None


def is_expired(cert: Certificate, today: date | None = None) -> bool:
    return bool(cert.expiry_date) and cert.expiry_date < (today or date.today())


def accreditations_for(db: Session, course_id: int, on_date: date | None = None) -> list[dict]:
    """Approvals that cover this course and are in force on the given date."""
    on_date = on_date or date.today()
    out = []
    for a in db.scalars(select(Accreditation).where(Accreditation.is_active).order_by(Accreditation.id)):
        if a.valid_until and a.valid_until < on_date:
            continue
        if a.applies_to_all or any(c.id == course_id for c in a.courses):
            out.append({"body": a.body_name, "statement": a.statement, "ref": a.reference_no, "logo": a.logo_file})
    return out


def accreditations_of(cert: Certificate) -> list[dict]:
    try:
        return json.loads(cert.accreditations_json or "[]")
    except ValueError:
        return []


def find_duplicates(db: Session, participant_name: str, course_code: str, completion_date: date,
                    exclude_id: int | None = None) -> list[Certificate]:
    q = select(Certificate).where(
        Certificate.participant_name_key == name_key(participant_name),
        Certificate.course_code == course_code,
        Certificate.completion_date == completion_date,
        Certificate.status.in_(["issued", "draft"]),
    )
    if exclude_id:
        q = q.where(Certificate.id != exclude_id)
    return list(db.scalars(q))


def _apply(cert: Certificate, data: CertificateInput, participant: Participant, course: Course,
           instructor: Instructor | None) -> None:
    cert.participant_id = participant.id
    cert.participant_name = clean_name(participant.full_name)
    cert.participant_name_key = name_key(participant.full_name)
    cert.course_id = course.id
    cert.course_title = clean_name(data.course_title or course.title)
    cert.course_code = course.code
    cert.duration_hours = data.duration_hours
    cert.start_date = data.start_date
    cert.completion_date = data.completion_date
    cert.batch_id = data.batch_id
    cert.expiry_date = compute_expiry(course, data.completion_date, data.expiry_date, data.no_expiry)
    cert.instructor_id = instructor.id if instructor else None
    cert.instructor_name = clean_name(instructor.full_name if instructor else data.instructor_name)
    cert.show_instructor_public = instructor.show_publicly if instructor else True
    cert.allow_public_pdf = data.allow_public_pdf


# ---------- lifecycle ----------

def create_draft(db: Session, data: CertificateInput, user: User | None, ip: str = "") -> Certificate:
    participant, course, instructor = _validate(db, data)
    cert = Certificate(status="draft", created_by=user.id if user else None)
    _apply(cert, data, participant, course, instructor)
    db.add(cert)
    db.flush()
    audit.record(db, user, "certificate.draft_created", "certificate", cert.id,
                 {"participant": cert.participant_name, "course": cert.course_code}, ip)
    return cert


def update_draft(db: Session, cert: Certificate, data: CertificateInput, user: User | None, ip: str = "") -> Certificate:
    if cert.status != "draft":
        raise CertificateError("Issued certificates cannot be edited. Revoke and reissue instead.")
    participant, course, instructor = _validate(db, data)
    _apply(cert, data, participant, course, instructor)
    audit.record(db, user, "certificate.draft_updated", "certificate", cert.id, None, ip)
    return cert


def issue(db: Session, cert: Certificate, user: User | None, ip: str = "",
          issue_date: date | None = None) -> Certificate:
    if cert.status != "draft":
        raise CertificateError("Only draft certificates can be issued")
    issue_date = issue_date or date.today()
    if issue_date < cert.completion_date:
        raise CertificateError("Issue date cannot be before the completion date")
    cert.certificate_no = next_certificate_no(db, issue_date.year)
    cert.issue_date = issue_date
    cert.accreditations_json = json.dumps(accreditations_for(db, cert.course_id, issue_date), ensure_ascii=False)
    cert.verify_token = secrets.token_urlsafe(12)
    cert.signature = compute_signature(cert)
    cert.status = "issued"
    cert.issued_by = user.id if user else None
    cert.issued_at = utcnow()
    db.flush()
    audit.record(db, user, "certificate.issued", "certificate", cert.certificate_no,
                 {"participant": cert.participant_name, "course": cert.course_code}, ip)
    return cert


def revoke(db: Session, cert: Certificate, reason: str, user: User | None, ip: str = "") -> Certificate:
    reason = clean_name(reason)
    if cert.status != "issued":
        raise CertificateError("Only issued certificates can be revoked")
    if len(reason) < 5:
        raise CertificateError("A revocation reason (at least 5 characters) is required")
    cert.status = "revoked"
    cert.revoked_at = utcnow()
    cert.revoked_by = user.id if user else None
    cert.revocation_reason = reason[:1000]
    audit.record(db, user, "certificate.revoked", "certificate", cert.certificate_no, {"reason": reason}, ip)
    return cert


def reissue(db: Session, old: Certificate, data: CertificateInput, reason: str, user: User | None,
            ip: str = "") -> Certificate:
    """Revoke the old certificate and issue a replacement with a new number."""
    if old.status != "issued":
        raise CertificateError("Only a currently valid certificate can be reissued")
    reason = clean_name(reason)
    if len(reason) < 5:
        raise CertificateError("A reason for reissuing is required")
    participant, course, instructor = _validate(db, data)
    new = Certificate(status="draft", created_by=user.id if user else None, replaces_id=old.id)
    _apply(new, data, participant, course, instructor)
    db.add(new)
    db.flush()
    issue(db, new, user, ip)
    revoke(db, old, f"Reissued as {new.certificate_no}: {reason}", user, ip)
    old.replaced_by_id = new.id
    audit.record(db, user, "certificate.reissued", "certificate", old.certificate_no,
                 {"new": new.certificate_no, "reason": reason}, ip)
    return new


def search(db: Session, q: str = "", status: str = "", course_id: int | None = None, limit: int = 200):
    stmt = select(Certificate).order_by(Certificate.id.desc())
    q = clean_name(q)
    if q:
        like = f"%{q.casefold()}%"
        stmt = stmt.where(or_(Certificate.certificate_no.ilike(f"%{q}%"),
                              Certificate.participant_name_key.like(like),
                              Certificate.course_code.ilike(f"%{q}%")))
    if status == "expired":
        stmt = stmt.where(Certificate.status == "issued", Certificate.expiry_date < date.today())
    elif status == "expiring":
        from datetime import timedelta
        stmt = stmt.where(Certificate.status == "issued", Certificate.expiry_date >= date.today(),
                          Certificate.expiry_date <= date.today() + timedelta(days=60))
    elif status:
        stmt = stmt.where(Certificate.status == status)
    if course_id:
        stmt = stmt.where(Certificate.course_id == course_id)
    return list(db.scalars(stmt.limit(limit)))


def get_by_no(db: Session, number: str) -> Certificate | None:
    number = (number or "").strip().upper()
    if not re.fullmatch(r"[A-Z0-9]{1,10}-\d{4}-\d{6}", number):
        return None
    return db.scalar(select(Certificate).where(Certificate.certificate_no == number))


def format_date(d: date | datetime | None) -> str:
    if not d:
        return ""
    return f"{d.day} {d.strftime('%B %Y')}"


def format_range(a: date, b: date) -> str:
    if a == b:
        return format_date(a)
    if a.year == b.year and a.month == b.month:
        return f"{a.day}–{b.day} {b.strftime('%B %Y')}"
    if a.year == b.year:
        return f"{a.day} {a.strftime('%B')} – {format_date(b)}"
    return f"{format_date(a)} – {format_date(b)}"


def check_code(cert: Certificate) -> str:
    """Short code printed on the certificate and inside its QR text. Only the center (holding
    CERT_SIGNING_KEY) can produce it, so a forged QR with made-up details won't match."""
    if not cert.certificate_no:
        return ""
    raw = hmac.new(settings.cert_signing_key.encode(), b"check:" + _canonical(cert), hashlib.sha256).hexdigest()
    return f"{raw[:4]}-{raw[4:8]}".upper()


def qr_text(cert: Certificate, center: str, include_link: bool = False) -> str:
    lines = [
        center.upper(),
        "CERTIFICATE OF COMPLETION - VERIFICATION",
        f"Certificate ID: {cert.certificate_no}",
        f"Name: {cert.participant_name}",
        f"Course: {cert.course_title} ({cert.course_code})",
        f"Duration: {hours_display(cert.duration_hours)} hours",
        f"Training dates: {format_range(cert.start_date, cert.completion_date)}",
        f"Issue date: {format_date(cert.issue_date)}",
        f"Valid until: {format_date(cert.expiry_date) if cert.expiry_date else 'No expiry'}",
    ]
    if cert.instructor_name and cert.show_instructor_public:
        lines.append(f"Instructor: {cert.instructor_name}")
    lines.append(f"Check code: {check_code(cert)}")
    if include_link:
        lines.append(f"Verify online: {verification_url(cert)}")
    return "\n".join(lines)
