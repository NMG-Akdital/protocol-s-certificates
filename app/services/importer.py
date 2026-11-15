"""Bulk import from CSV / Excel: parse, validate, detect duplicates, then issue."""
from __future__ import annotations

import csv
import io
import json
import logging
import re
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..config import settings
from ..models import Certificate, Course, ImportJob, Instructor, Participant, User
from . import audit, certificates as svc, settings_store
from .certificates import CertificateError, CertificateInput, clean_name, name_key, parse_hours
from .pdf import build_zip, get_or_create_pdf, pdf_filename

log = logging.getLogger(__name__)

TEMPLATE_HEADERS = ["Participant Name", "Course Title", "Course Code", "Duration", "Start Date",
                    "Completion Date", "Instructor Name", "Expiry Date (optional)",
                    "National ID (private, optional)", "Company (optional)"]

ALIASES = {
    "participant_name": {"participant_name", "participant", "name", "full_name", "trainee_name", "trainee", "student_name"},
    "course_title": {"course_title", "course", "course_name", "training_course", "training", "training_title"},
    "course_code": {"course_code", "code"},
    "duration": {"duration", "duration_hours", "hours", "training_hours", "total_hours"},
    "start_date": {"start_date", "training_start_date", "from", "date_from", "start"},
    "completion_date": {"completion_date", "end_date", "training_completion_date", "completed_on", "date_to",
                        "completion", "end"},
    "instructor_name": {"instructor_name", "instructor", "trainer", "trainer_name"},
    "national_id": {"national_id", "id_number", "iqama", "iqama_number", "passport", "national_id_private_optional"},
    "company": {"company", "employer", "company_optional", "organisation", "organization"},
    "expiry_date": {"expiry_date", "expiry", "valid_until", "expiration_date", "expired_date", "expire_date",
                    "expiry_date_optional"},
    "validity_months": {"validity_months", "validity", "validity_period_months"},
}
REQUIRED = ["participant_name", "course_code", "duration", "start_date", "completion_date"]


def normalise_header(h) -> str:
    h = re.sub(r"\(.*?\)", "", str(h or "")).strip().lower()
    return re.sub(r"[^a-z0-9]+", "_", h).strip("_")


def read_table(data: bytes, filename: str) -> tuple[list[str], list[dict]]:
    name = filename.lower()
    if name.endswith((".xlsx", ".xlsm")):
        return _read_xlsx(data)
    if name.endswith(".xls"):
        raise CertificateError("Old .xls files are not supported. Save the file as .xlsx or .csv and upload again.")
    if name.endswith(".csv") or name.endswith(".txt"):
        return _read_csv(data)
    raise CertificateError("Upload a .csv or .xlsx file")


def _read_csv(data: bytes) -> tuple[list[str], list[dict]]:
    for enc in ("utf-8-sig", "cp1256", "latin-1"):
        try:
            text = data.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    sample = text[:4096]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t")
    except csv.Error:
        dialect = csv.excel
    rows = list(csv.reader(io.StringIO(text), dialect))
    return _to_dicts(rows)


def _read_xlsx(data: bytes) -> tuple[list[str], list[dict]]:
    from openpyxl import load_workbook
    try:
        wb = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    except Exception:
        raise CertificateError("The Excel file could not be read. Check it opens in Excel and try again.")
    ws = wb.worksheets[0]
    rows = [list(r) for r in ws.iter_rows(values_only=True)]
    wb.close()
    return _to_dicts(rows)


def _to_dicts(rows: list[list]) -> tuple[list[str], list[dict]]:
    rows = [r for r in rows if any(str(c).strip() for c in r if c is not None)]
    if not rows:
        raise CertificateError("The file is empty")
    headers = [str(h).strip() if h is not None else "" for h in rows[0]]
    out = []
    for r in rows[1:]:
        out.append({headers[i]: r[i] for i in range(min(len(headers), len(r))) if headers[i]})
    return headers, out


def _map_columns(headers: list[str]) -> tuple[dict[str, str], list[str]]:
    mapping = {}
    for h in headers:
        key = normalise_header(h)
        for field, names in ALIASES.items():
            if key in names and field not in mapping:
                mapping[field] = h
    missing = [f for f in REQUIRED if f not in mapping]
    if "course_title" not in mapping:
        missing.append("course_title")
    return mapping, missing


_MONTHS = {m.lower(): i for i, m in enumerate(
    ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October",
     "November", "December"], 1)}


def parse_date(value) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    s = str(value or "").strip()
    if not s:
        raise ValueError("missing")
    m = re.fullmatch(r"(\d{4})-(\d{1,2})-(\d{1,2})(?:[ T].*)?", s)
    if m:
        return date(int(m[1]), int(m[2]), int(m[3]))
    m = re.fullmatch(r"(\d{1,2})[/.\-](\d{1,2})[/.\-](\d{4})", s)   # DD/MM/YYYY (day first)
    if m:
        return date(int(m[3]), int(m[2]), int(m[1]))
    m = re.fullmatch(r"(\d{1,2})\s+([A-Za-z]+)\s+(\d{4})", s)
    if m and m[2].lower()[:3] in {k[:3] for k in _MONTHS}:
        month = next(v for k, v in _MONTHS.items() if k.startswith(m[2].lower()[:3]))
        return date(int(m[3]), month, int(m[1]))
    raise ValueError("format")


def validate(db: Session, data: bytes, filename: str) -> dict:
    headers, raw_rows = read_table(data, filename)
    mapping, missing = _map_columns(headers)
    if missing:
        nice = {"participant_name": "Participant Name", "course_title": "Course Title", "course_code": "Course Code",
                "duration": "Duration", "start_date": "Start Date", "completion_date": "Completion Date"}
        raise CertificateError("Missing required column(s): " + ", ".join(nice.get(m, m) for m in missing))
    if len(raw_rows) > settings.max_import_rows:
        raise CertificateError(f"Too many rows ({len(raw_rows)}). The limit per file is {settings.max_import_rows}.")

    courses = {c.code.upper(): c for c in db.scalars(select(Course))}
    instructors = {i.full_name.casefold(): i for i in db.scalars(select(Instructor))}
    seen: dict[tuple, int] = {}
    today = date.today()
    rows = []
    for idx, r in enumerate(raw_rows, start=2):  # row 1 is the header
        get = lambda f: r.get(mapping[f]) if f in mapping else None
        errors, warnings = [], []
        name = clean_name(str(get("participant_name") or ""))
        title = clean_name(str(get("course_title") or ""))
        code = clean_name(str(get("course_code") or "")).upper()
        instructor = clean_name(str(get("instructor_name") or ""))
        national = clean_name(str(get("national_id") or ""))
        company = clean_name(str(get("company") or ""))
        if national.endswith(".0") and national[:-2].isdigit():  # Excel numeric IDs
            national = national[:-2]

        if not name:
            errors.append("Participant Name is missing")
        elif len(name) > 120:
            errors.append("Participant Name is longer than 120 characters")
        if not code:
            errors.append("Course Code is missing")
        elif not re.fullmatch(r"[A-Z0-9][A-Z0-9\-_.]{1,39}", code):
            errors.append("Course Code may only contain letters, digits, - _ .")
        if len(title) > 160:
            errors.append("Course Title is longer than 160 characters")
        hours = None
        try:
            hours = parse_hours(get("duration"))
        except CertificateError:
            errors.append("Duration must be a number of hours, e.g. 8 or '8 Hours'")
        start = end = None
        for field, label in (("start_date", "Start Date"), ("completion_date", "Completion Date")):
            try:
                val = parse_date(get(field))
                if field == "start_date":
                    start = val
                else:
                    end = val
            except ValueError as e:
                errors.append(f"{label} is missing" if str(e) == "missing"
                              else f"{label} is not a valid date (use YYYY-MM-DD or DD/MM/YYYY)")
        if start and end:
            if start > end:
                errors.append("Start Date is after Completion Date")
            if end > today:
                errors.append("Completion Date is in the future")

        course = courses.get(code)
        if code and not course:
            if not title:
                errors.append("New course code: Course Title is required")
            else:
                warnings.append(f"New course {code} will be added to the catalogue")
        elif course and title and title.casefold() != course.title.casefold():
            warnings.append(f"Title differs from catalogue ('{course.title}'); the file's title will be printed")
        if instructor and instructor.casefold() not in instructors:
            warnings.append(f"New instructor '{instructor}' will be added")
        if not instructor:
            warnings.append("No instructor name: the instructor signature line will be blank")

        expiry = None
        raw_exp, raw_months = get("expiry_date"), str(get("validity_months") or "").strip()
        if raw_exp not in (None, ""):
            try:
                expiry = parse_date(raw_exp)
                if end and expiry <= end:
                    errors.append("Expiry Date must be after the Completion Date")
            except ValueError:
                errors.append("Expiry Date is not a valid date")
        elif raw_months:
            if raw_months.endswith(".0"):
                raw_months = raw_months[:-2]
            if not raw_months.isdigit() or not 1 <= int(raw_months) <= 240:
                errors.append("Validity must be a whole number of months (1–240)")
            elif end:
                expiry = svc.add_months(end, int(raw_months)) - timedelta(days=1)
        elif course and course.validity_months and end:
            expiry = svc.add_months(end, int(course.validity_months)) - timedelta(days=1)
        if expiry and expiry < today and not errors:
            warnings.append(f"Already expired on {expiry.isoformat()}: verification will show EXPIRED")

        status = "error" if errors else "ok"
        dupe_ref = None
        if not errors:
            key = (name_key(name), code, end)
            if key in seen:
                status, dupe_ref = "duplicate", f"same as row {seen[key]} in this file"
            else:
                seen[key] = idx
                existing = svc.find_duplicates(db, name, code, end)
                if existing:
                    status = "duplicate"
                    dupe_ref = "already issued: " + ", ".join(c.certificate_no or f"draft #{c.id}" for c in existing)
        rows.append({
            "row": idx, "status": status, "errors": errors, "warnings": warnings, "duplicate_of": dupe_ref,
            "data": {"participant_name": name, "course_title": title or (course.title if course else ""),
                     "course_code": code, "duration": svc.hours_display(hours) if hours else "",
                     "start_date": start.isoformat() if start else "", "completion_date": end.isoformat() if end else "",
                     "instructor_name": instructor, "national_id": national, "company": company,
                     "expiry_date": expiry.isoformat() if expiry else ""},
        })
    summary = {"total": len(rows), "ok": sum(r["status"] == "ok" for r in rows),
               "error": sum(r["status"] == "error" for r in rows),
               "duplicate": sum(r["status"] == "duplicate" for r in rows)}
    return {"rows": rows, "summary": summary}


def _participant(db: Session, name: str, national: str, company: str) -> Participant:
    p = None
    if national:
        p = db.scalar(select(Participant).where(Participant.national_id == national))
    if p is None:
        q = select(Participant).where(func.lower(Participant.full_name) == name.lower())
        q = q.where(Participant.national_id.is_(None)) if not national else q.where(
            (Participant.national_id.is_(None)) | (Participant.national_id == national))
        p = db.scalar(q.limit(1))
    if p is None:
        p = Participant(full_name=name, national_id=national or None, company=company or None)
        db.add(p)
        db.flush()
    elif national and not p.national_id:
        p.national_id = national
    return p


def build_sample_certificate(row: dict) -> Certificate:
    d = row["data"]
    return Certificate(participant_name=d["participant_name"], course_title=d["course_title"],
                       course_code=d["course_code"], duration_hours=Decimal(d["duration"]),
                       start_date=date.fromisoformat(d["start_date"]),
                       completion_date=date.fromisoformat(d["completion_date"]),
                       instructor_name=d["instructor_name"], status="draft",
                       expiry_date=date.fromisoformat(d["expiry_date"]) if d.get("expiry_date") else None)


def run_job(job_id: int, user_id: int | None, session_factory) -> None:
    """Issue all valid rows (one DB transaction), then render PDFs and build the ZIP."""
    db: Session = session_factory()
    try:
        job = db.get(ImportJob, job_id)
        user = db.get(User, user_id) if user_id else None
        rows = [r for r in json.loads(job.rows_json) if r["status"] == "ok"]
        allow_pdf = settings_store.flag(settings_store.get_all(db), "default_allow_public_pdf")
        issued_ids = []
        for r in rows:
            d = r["data"]
            # Re-check duplicates at issue time (someone may have issued in the meantime).
            if svc.find_duplicates(db, d["participant_name"], d["course_code"], date.fromisoformat(d["completion_date"])):
                r["status"], r["duplicate_of"] = "duplicate", "issued meanwhile"
                continue
            course = db.scalar(select(Course).where(Course.code == d["course_code"]))
            if course is None:
                course = Course(code=d["course_code"], title=d["course_title"], duration_hours=Decimal(d["duration"]))
                db.add(course)
                db.flush()
                audit.record(db, user, "course.created_by_import", "course", course.code, {"job": job_id})
            instructor = None
            if d["instructor_name"]:
                instructor = db.scalar(select(Instructor).where(func.lower(Instructor.full_name) == d["instructor_name"].lower()))
                if instructor is None:
                    instructor = Instructor(full_name=d["instructor_name"])
                    db.add(instructor)
                    db.flush()
            p = _participant(db, d["participant_name"], d["national_id"], d["company"])
            cert = svc.create_draft(db, CertificateInput(
                participant_id=p.id, course_id=course.id,
                start_date=date.fromisoformat(d["start_date"]), completion_date=date.fromisoformat(d["completion_date"]),
                duration_hours=Decimal(d["duration"]), instructor_id=instructor.id if instructor else None,
                course_title=d["course_title"] or None, allow_public_pdf=allow_pdf,
                expiry_date=date.fromisoformat(d["expiry_date"]) if d.get("expiry_date") else None,
                no_expiry=not d.get("expiry_date")), user)
            cert.import_job_id = job_id
            svc.issue(db, cert, user)
            issued_ids.append(cert.id)
        job.status = "processing"
        job.total = len(issued_ids)
        audit.record(db, user, "import.issued", "import_job", job_id, {"issued": len(issued_ids)})
        db.commit()

        s = settings_store.get_all(db)
        items = []
        for n, cid in enumerate(issued_ids, start=1):
            cert = db.get(Certificate, cid)
            items.append((pdf_filename(cert), get_or_create_pdf(cert, s)))
            job.progress = n
            db.commit()
        fname = f"import-{job_id}_{datetime.now().strftime('%Y%m%d-%H%M%S')}.zip"
        (settings.exports_dir / fname).write_bytes(build_zip(items))
        job.zip_file = fname
        job.status = "completed"
        job.finished_at = datetime.now(timezone.utc)
        db.commit()
    except Exception as exc:  # pragma: no cover - logged and shown in UI
        log.exception("Import job %s failed", job_id)
        db.rollback()
        job = db.get(ImportJob, job_id)
        if job:
            job.status = "failed"
            job.error = f"{type(exc).__name__}: {exc}"[:2000]
            db.commit()
    finally:
        db.close()
