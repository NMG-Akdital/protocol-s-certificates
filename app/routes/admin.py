"""Admin dashboard: certificates, participants, courses, instructors, batches, logs, users, settings."""
from __future__ import annotations

import csv
import io
import re
import secrets
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, Response
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..config import settings
from ..db import get_db
from ..models import (Accreditation, AuditLog, Batch, Certificate, Course, Enrollment, Instructor, Participant, User,
                      VerificationLog)
from ..security import (ROLES, client_ip, hash_password, password_problems, require_admin, require_issuer,
                        require_viewer, verify_csrf)
from ..services import audit, certificates as svc, settings_store
from ..services.certificates import CertificateError, CertificateInput, clean_name, parse_hours
from ..services.importer import read_table, normalise_header
from ..services.pdf import build_zip, get_or_create_pdf, pdf_filename, render_pdf
from ..web import flash, redirect, render

router = APIRouter(prefix="/admin")
CSRF = [Depends(verify_csrf)]


# ---------------- helpers ----------------

def _date(value, field: str) -> date:
    try:
        return date.fromisoformat(str(value or "").strip())
    except ValueError:
        raise CertificateError(f"{field}: enter a valid date (YYYY-MM-DD)")


def _int(value) -> int | None:
    if value is None:
        return None
    try:
        return int(value) if str(value).strip() else None
    except (TypeError, ValueError):
        return None


def _get(db: Session, model, id_: int):
    obj = db.get(model, id_)
    if not obj:
        raise HTTPException(status_code=404)
    return obj


def save_image_upload(upload: UploadFile, prefix: str = "sig") -> str:
    """Validate an uploaded signature image and store it as a clean PNG."""
    from PIL import Image
    data = upload.file.read(1024 * 1024 + 1)
    if len(data) > 1024 * 1024:
        raise CertificateError("Signature image must be under 1 MB")
    try:
        img = Image.open(io.BytesIO(data))
        img.verify()
        img = Image.open(io.BytesIO(data))
        if img.format not in ("PNG", "JPEG"):
            raise CertificateError("Signature must be a PNG or JPG image")
        img = img.convert("RGBA")
        img.thumbnail((1600, 800))
    except CertificateError:
        raise
    except Exception:
        raise CertificateError("The file is not a valid image")
    name = f"{prefix}_{secrets.token_hex(8)}.png"
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    from ..services import files
    files.store(name, buf.getvalue())
    return name


def _lists(db: Session) -> dict:
    return {
        "courses": list(db.scalars(select(Course).where(Course.is_active).order_by(Course.title))),
        "instructors": list(db.scalars(select(Instructor).where(Instructor.is_active).order_by(Instructor.full_name))),
        "batches": list(db.scalars(select(Batch).order_by(Batch.id.desc()).limit(200))),
    }


def _export_zip(name_prefix: str, certs: list[Certificate], db: Session) -> str:
    s = settings_store.get_all(db)
    items = [(pdf_filename(c), get_or_create_pdf(c, s)) for c in certs]
    db.commit()
    fname = f"{name_prefix}_{datetime.now().strftime('%Y%m%d-%H%M%S')}_{secrets.token_hex(4)}.zip"
    (settings.exports_dir / fname).write_bytes(build_zip(items))
    return fname


# ---------------- dashboard ----------------

@router.get("")
def dashboard(request: Request, user: User = Depends(require_viewer), db: Session = Depends(get_db)):
    counts = dict(db.execute(select(Certificate.status, func.count()).group_by(Certificate.status)).all())
    since = datetime.now(timezone.utc) - timedelta(days=30)
    ver30 = db.scalar(select(func.count()).select_from(VerificationLog).where(VerificationLog.at >= since))
    recent = list(db.scalars(select(Certificate).where(Certificate.status != "draft")
                             .order_by(Certificate.issued_at.desc()).limit(8)))
    recent_ver = list(db.scalars(select(VerificationLog).order_by(VerificationLog.id.desc()).limit(8)))
    today = date.today()
    expiring = db.scalar(select(func.count()).select_from(Certificate).where(
        Certificate.status == "issued", Certificate.expiry_date >= today, Certificate.expiry_date <= today + timedelta(days=60)))
    expired = db.scalar(select(func.count()).select_from(Certificate).where(
        Certificate.status == "issued", Certificate.expiry_date < today))
    return render(request, "admin/dashboard.html", {
        "counts": counts, "ver30": ver30, "expiring": expiring, "expired": expired, "recent": recent, "recent_ver": recent_ver,
        "participants": db.scalar(select(func.count()).select_from(Participant)),
        "courses_n": db.scalar(select(func.count()).select_from(Course)),
    }, user=user)


# ---------------- certificates ----------------

@router.get("/certificates")
def cert_list(request: Request, q: str = "", status: str = "", course_id: str = "",
              user: User = Depends(require_viewer), db: Session = Depends(get_db)):
    certs = svc.search(db, q, status if status in ("draft", "issued", "revoked", "expired", "expiring") else "",
                       _int(course_id))
    return render(request, "admin/certificates.html", {"certs": certs, "q": q, "status": status,
                                                         "course_id": _int(course_id), **_lists(db)}, user=user)


@router.get("/certificates/export.csv")
def cert_export(user: User = Depends(require_viewer), db: Session = Depends(get_db)):
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["Certificate ID", "Status", "Participant", "Course Code", "Course Title", "Hours", "Start Date",
                "Completion Date", "Issue Date", "Valid Until", "Instructor", "Revoked At", "Revocation Reason"])
    for c in db.scalars(select(Certificate).where(Certificate.status != "draft").order_by(Certificate.id)):
        status = "EXPIRED" if c.status == "issued" and svc.is_expired(c) else c.status.upper()
        w.writerow([c.certificate_no, status, c.participant_name, c.course_code, c.course_title,
                    svc.hours_display(c.duration_hours), c.start_date, c.completion_date, c.issue_date,
                    c.expiry_date or "", c.instructor_name, c.revoked_at or "", c.revocation_reason or ""])
    return Response("﻿" + buf.getvalue(), media_type="text/csv",
                    headers={"Content-Disposition": 'attachment; filename="certificate-register.csv"'})


@router.get("/certificates/new")
def cert_new(request: Request, participant_id: str = "", batch_id: str = "",
             user: User = Depends(require_issuer), db: Session = Depends(get_db)):
    participant = db.get(Participant, _int(participant_id)) if _int(participant_id) else None
    form = {"participant_id": participant.id if participant else "",
            "participant_label": participant.full_name if participant else "",
            "batch_id": _int(batch_id) or "",
            "allow_public_pdf": settings_store.flag(settings_store.get_all(db), "default_allow_public_pdf")}
    return render(request, "admin/certificate_form.html", {"form": form, "mode": "new", **_lists(db)}, user=user)


def _input_from_form(db: Session, form, user: User) -> CertificateInput:
    pid = _int(form.get("participant_id"))
    new_name = clean_name(form.get("new_participant_name", ""))
    if not pid:
        if not new_name:
            raise CertificateError("Choose an existing participant or enter a new participant's full name")
        if len(new_name) > 120:
            raise CertificateError("Participant name is too long (120 characters max)")
        p = Participant(full_name=new_name, national_id=clean_name(form.get("new_participant_national_id", "")) or None,
                        company=clean_name(form.get("new_participant_company", "")) or None)
        db.add(p)
        db.flush()
        audit.record(db, user, "participant.created", "participant", p.id, {"name": p.full_name})
        pid = p.id
    course = db.get(Course, _int(form.get("course_id")) or 0)
    if not course:
        raise CertificateError("Choose a course")
    hours_raw = (form.get("duration_hours") or "").strip()
    title_override = clean_name(form.get("course_title", ""))
    if len(title_override) > 160:
        raise CertificateError("Course title is too long (160 characters max)")
    return CertificateInput(
        participant_id=pid, course_id=course.id,
        start_date=_date(form.get("start_date"), "Start date"),
        completion_date=_date(form.get("completion_date"), "Completion date"),
        duration_hours=parse_hours(hours_raw) if hours_raw else Decimal(course.duration_hours),
        instructor_id=_int(form.get("instructor_id")),
        course_title=title_override or None,
        batch_id=_int(form.get("batch_id")),
        allow_public_pdf=form.get("allow_public_pdf") == "on",
        expiry_date=_date(form.get("expiry_date"), "Valid until") if str(form.get("expiry_date") or "").strip() else None,
        no_expiry=form.get("no_expiry") == "on",
    )


@router.post("/certificates", dependencies=CSRF)
async def cert_create(request: Request, user: User = Depends(require_issuer), db: Session = Depends(get_db)):
    form = await request.form()
    ip = client_ip(request)
    try:
        data = _input_from_form(db, form, user)
        p = db.get(Participant, data.participant_id)
        course = db.get(Course, data.course_id)
        dupes = svc.find_duplicates(db, p.full_name, course.code, data.completion_date)
        if dupes and form.get("confirm_duplicate") != "on":
            raise CertificateError(
                f"A certificate for {p.full_name} / {course.code} / {data.completion_date} already exists "
                f"({', '.join(d.certificate_no or 'draft #' + str(d.id) for d in dupes)}). "
                "Tick 'Create anyway' only if this is intentional.")
        cert = svc.create_draft(db, data, user, ip)
        if form.get("action") == "issue":
            svc.issue(db, cert, user, ip)
        db.commit()
    except CertificateError as e:
        db.rollback()
        return render(request, "admin/certificate_form.html",
                      {"form": dict(form), "mode": "new", "error": str(e), **_lists(db)}, user=user, status_code=400)
    flash(request, f"Certificate {cert.certificate_no} issued." if cert.certificate_no else "Draft saved.")
    if cert.certificate_no and svc.is_expired(cert):
        flash(request, "Note: this certificate's validity has already ended, so verification shows EXPIRED.", "err")
    return redirect(f"/admin/certificates/{cert.id}")


@router.get("/certificates/{cid}")
def cert_detail(cid: int, request: Request, user: User = Depends(require_viewer), db: Session = Depends(get_db)):
    cert = _get(db, Certificate, cid)
    related = {}
    for attr in ("replaces_id", "replaced_by_id"):
        if getattr(cert, attr):
            related[attr] = db.get(Certificate, getattr(cert, attr))
    history = list(db.scalars(select(AuditLog).where(AuditLog.entity == "certificate",
                                                     AuditLog.entity_ref.in_([str(cert.id), cert.certificate_no or "-"]))
                              .order_by(AuditLog.id)))
    verifications = list(db.scalars(select(VerificationLog).where(VerificationLog.query == (cert.certificate_no or "-"))
                                    .order_by(VerificationLog.id.desc()).limit(20)))
    return render(request, "admin/certificate_detail.html", {
        "c": cert, "related": related, "history": history, "verifications": verifications,
        "verify_url": svc.verification_url(cert) if cert.certificate_no else None,
        "sig_ok": svc.signature_ok(cert) if cert.certificate_no else None,
        "expired": svc.is_expired(cert),
        "check": svc.check_code(cert),
        "accs": svc.accreditations_of(cert) if cert.certificate_no else svc.accreditations_for(db, cert.course_id),
    }, user=user)


@router.get("/certificates/{cid}/edit")
def cert_edit(cid: int, request: Request, user: User = Depends(require_issuer), db: Session = Depends(get_db)):
    cert = _get(db, Certificate, cid)
    if cert.status != "draft":
        flash(request, "Issued certificates are locked. Use Reissue to correct details.", "err")
        return redirect(f"/admin/certificates/{cid}")
    form = {"participant_id": cert.participant_id, "participant_label": cert.participant_name,
            "course_id": cert.course_id, "course_title": cert.course_title if cert.course_title != cert.course.title else "",
            "duration_hours": svc.hours_display(cert.duration_hours), "start_date": cert.start_date.isoformat(),
            "completion_date": cert.completion_date.isoformat(), "instructor_id": cert.instructor_id or "",
            "batch_id": cert.batch_id or "", "allow_public_pdf": cert.allow_public_pdf,
            "expiry_date": cert.expiry_date.isoformat() if cert.expiry_date else ""}
    return render(request, "admin/certificate_form.html", {"form": form, "mode": "edit", "c": cert, **_lists(db)}, user=user)


@router.post("/certificates/{cid}/edit", dependencies=CSRF)
async def cert_update(cid: int, request: Request, user: User = Depends(require_issuer), db: Session = Depends(get_db)):
    cert = _get(db, Certificate, cid)
    form = await request.form()
    try:
        svc.update_draft(db, cert, _input_from_form(db, form, user), user, client_ip(request))
        if form.get("action") == "issue":
            svc.issue(db, cert, user, client_ip(request))
        db.commit()
    except CertificateError as e:
        db.rollback()
        return render(request, "admin/certificate_form.html",
                      {"form": dict(form), "mode": "edit", "c": cert, "error": str(e), **_lists(db)},
                      user=user, status_code=400)
    flash(request, f"Certificate {cert.certificate_no} issued." if cert.certificate_no else "Draft updated.")
    return redirect(f"/admin/certificates/{cid}")


@router.post("/certificates/{cid}/issue", dependencies=CSRF)
def cert_issue(cid: int, request: Request, user: User = Depends(require_issuer), db: Session = Depends(get_db)):
    cert = _get(db, Certificate, cid)
    try:
        svc.issue(db, cert, user, client_ip(request))
        db.commit()
        flash(request, f"Certificate {cert.certificate_no} issued.")
    except CertificateError as e:
        db.rollback()
        flash(request, str(e), "err")
    return redirect(f"/admin/certificates/{cid}")


@router.post("/certificates/{cid}/delete", dependencies=CSRF)
def cert_delete(cid: int, request: Request, user: User = Depends(require_issuer), db: Session = Depends(get_db)):
    cert = _get(db, Certificate, cid)
    if cert.status != "draft":
        flash(request, "Only drafts can be deleted. Issued certificates can be revoked.", "err")
        return redirect(f"/admin/certificates/{cid}")
    audit.record(db, user, "certificate.draft_deleted", "certificate", cid,
                 {"participant": cert.participant_name}, client_ip(request))
    db.delete(cert)
    db.commit()
    flash(request, "Draft deleted.")
    return redirect("/admin/certificates")


@router.post("/certificates/{cid}/revoke", dependencies=CSRF)
def cert_revoke(cid: int, request: Request, reason: str = Form(""), user: User = Depends(require_admin),
                db: Session = Depends(get_db)):
    cert = _get(db, Certificate, cid)
    try:
        svc.revoke(db, cert, reason, user, client_ip(request))
        db.commit()
        flash(request, f"Certificate {cert.certificate_no} revoked. Its verification page now shows REVOKED.")
    except CertificateError as e:
        db.rollback()
        flash(request, str(e), "err")
    return redirect(f"/admin/certificates/{cid}")


@router.post("/certificates/{cid}/public-pdf", dependencies=CSRF)
def cert_public_pdf(cid: int, request: Request, enabled: str = Form("0"), user: User = Depends(require_admin),
                    db: Session = Depends(get_db)):
    cert = _get(db, Certificate, cid)
    cert.allow_public_pdf = enabled == "1"
    audit.record(db, user, "certificate.public_pdf_" + ("enabled" if cert.allow_public_pdf else "disabled"),
                 "certificate", cert.certificate_no or cid, None, client_ip(request))
    db.commit()
    flash(request, "Public PDF download " + ("enabled." if cert.allow_public_pdf else "disabled."))
    return redirect(f"/admin/certificates/{cid}")


@router.get("/certificates/{cid}/reissue")
def cert_reissue_form(cid: int, request: Request, user: User = Depends(require_admin), db: Session = Depends(get_db)):
    cert = _get(db, Certificate, cid)
    if cert.status != "issued":
        flash(request, "Only a currently valid certificate can be reissued.", "err")
        return redirect(f"/admin/certificates/{cid}")
    form = {"participant_id": cert.participant_id, "participant_label": cert.participant_name,
            "course_id": cert.course_id, "course_title": cert.course_title if cert.course_title != cert.course.title else "",
            "duration_hours": svc.hours_display(cert.duration_hours), "start_date": cert.start_date.isoformat(),
            "completion_date": cert.completion_date.isoformat(), "instructor_id": cert.instructor_id or "",
            "batch_id": cert.batch_id or "", "allow_public_pdf": cert.allow_public_pdf,
            "expiry_date": cert.expiry_date.isoformat() if cert.expiry_date else "", "no_expiry": not cert.expiry_date}
    return render(request, "admin/certificate_form.html", {"form": form, "mode": "reissue", "c": cert, **_lists(db)},
                  user=user)


@router.post("/certificates/{cid}/reissue", dependencies=CSRF)
async def cert_reissue(cid: int, request: Request, user: User = Depends(require_admin), db: Session = Depends(get_db)):
    cert = _get(db, Certificate, cid)
    form = await request.form()
    try:
        data = _input_from_form(db, form, user)
        # Allow correcting the participant's printed name during reissue.
        corrected = clean_name(form.get("corrected_name", ""))
        if corrected and corrected != cert.participant_name:
            p = db.get(Participant, data.participant_id)
            audit.record(db, user, "participant.name_corrected", "participant", p.id,
                         {"from": p.full_name, "to": corrected})
            p.full_name = corrected
        new = svc.reissue(db, cert, data, form.get("reason", ""), user, client_ip(request))
        db.commit()
    except CertificateError as e:
        db.rollback()
        return render(request, "admin/certificate_form.html",
                      {"form": dict(form), "mode": "reissue", "c": cert, "error": str(e), **_lists(db)},
                      user=user, status_code=400)
    flash(request, f"Reissued as {new.certificate_no}. {cert.certificate_no} is now revoked.")
    return redirect(f"/admin/certificates/{new.id}")


@router.get("/certificates/{cid}/pdf")
def cert_pdf(cid: int, download: int = 0, user: User = Depends(require_viewer), db: Session = Depends(get_db)):
    cert = _get(db, Certificate, cid)
    data = get_or_create_pdf(cert, settings_store.get_all(db), svc.accreditations_for(db, cert.course_id))
    db.commit()
    disp = "attachment" if download else "inline"
    return Response(data, media_type="application/pdf",
                    headers={"Content-Disposition": f'{disp}; filename="{pdf_filename(cert)}"'})


# ---------------- participants ----------------

@router.get("/participants")
def participants(request: Request, q: str = "", user: User = Depends(require_viewer), db: Session = Depends(get_db)):
    stmt = select(Participant).order_by(Participant.id.desc())
    q = clean_name(q)
    if q:
        like = f"%{q}%"
        stmt = stmt.where(Participant.full_name.ilike(like) | Participant.national_id.ilike(like)
                          | Participant.company.ilike(like))
    rows = list(db.scalars(stmt.limit(300)))
    counts = dict(db.execute(select(Certificate.participant_id, func.count()).where(Certificate.status != "draft")
                             .group_by(Certificate.participant_id)).all())
    return render(request, "admin/participants.html", {"rows": rows, "q": q, "counts": counts}, user=user)


@router.get("/participants/search.json")
def participant_search(q: str = "", user: User = Depends(require_issuer), db: Session = Depends(get_db)):
    q = clean_name(q)
    if len(q) < 2:
        return []
    rows = db.scalars(select(Participant).where(Participant.full_name.ilike(f"%{q}%") | Participant.national_id.ilike(f"%{q}%"))
                      .order_by(Participant.full_name).limit(15))
    return [{"id": p.id, "label": p.full_name + (f" · {p.company}" if p.company else "")} for p in rows]


@router.get("/participants/new")
def participant_new(request: Request, user: User = Depends(require_issuer)):
    return render(request, "admin/participant_form.html", {"p": None}, user=user)


@router.get("/participants/{pid}")
def participant_detail(pid: int, request: Request, user: User = Depends(require_viewer), db: Session = Depends(get_db)):
    p = _get(db, Participant, pid)
    certs = list(db.scalars(select(Certificate).where(Certificate.participant_id == pid).order_by(Certificate.id.desc())))
    return render(request, "admin/participant_form.html", {"p": p, "certs": certs}, user=user)


@router.post("/participants/save", dependencies=CSRF)
def participant_save(request: Request, pid: str = Form(""), full_name: str = Form(""), national_id: str = Form(""),
                     company: str = Form(""), email: str = Form(""), phone: str = Form(""), notes: str = Form(""),
                     user: User = Depends(require_issuer), db: Session = Depends(get_db)):
    name = clean_name(full_name)
    p = db.get(Participant, _int(pid)) if _int(pid) else None
    if not name or len(name) > 120:
        return render(request, "admin/participant_form.html", {"p": p, "error": "Full name is required (max 120 characters)."},
                      user=user, status_code=400)
    if p is None:
        p = Participant(full_name=name)
        db.add(p)
        action = "participant.created"
    else:
        action = "participant.updated"
    p.full_name = name
    p.national_id = clean_name(national_id) or None
    p.company = clean_name(company) or None
    p.email = clean_name(email) or None
    p.phone = clean_name(phone) or None
    p.notes = notes.strip()[:2000]
    db.flush()
    audit.record(db, user, action, "participant", p.id, {"name": p.full_name}, client_ip(request))
    db.commit()
    flash(request, "Participant saved. (Certificates already issued keep the name printed on them.)")
    return redirect(f"/admin/participants/{p.id}")


@router.post("/participants/import", dependencies=CSRF)
async def participant_import(request: Request, file: UploadFile = File(...), user: User = Depends(require_issuer),
                             db: Session = Depends(get_db)):
    data = await file.read(settings.max_upload_mb * 1024 * 1024 + 1)
    try:
        if len(data) > settings.max_upload_mb * 1024 * 1024:
            raise CertificateError(f"File is larger than {settings.max_upload_mb} MB")
        headers, rows = read_table(data, file.filename or "")
    except CertificateError as e:
        flash(request, str(e), "err")
        return redirect("/admin/participants")
    mapping = {normalise_header(h): h for h in headers}
    name_col = next((mapping[k] for k in ("participant_name", "full_name", "name") if k in mapping), None)
    if not name_col:
        flash(request, "No 'Participant Name' / 'Full Name' column found.", "err")
        return redirect("/admin/participants")
    col = lambda *keys: next((mapping[k] for k in keys if k in mapping), None)
    nid, comp, mail, ph = col("national_id", "id_number", "iqama"), col("company"), col("email"), col("phone", "mobile")
    added = skipped = 0
    for r in rows[: settings.max_import_rows]:
        name = clean_name(str(r.get(name_col) or ""))
        if not name or len(name) > 120:
            skipped += 1
            continue
        national = clean_name(str(r.get(nid) or "")) if nid else ""
        exists = db.scalar(select(Participant).where(func.lower(Participant.full_name) == name.lower(),
                                                     (Participant.national_id == national) if national else True))
        if exists:
            skipped += 1
            continue
        db.add(Participant(full_name=name, national_id=national or None,
                           company=clean_name(str(r.get(comp) or "")) or None if comp else None,
                           email=clean_name(str(r.get(mail) or "")) or None if mail else None,
                           phone=clean_name(str(r.get(ph) or "")) or None if ph else None))
        added += 1
    audit.record(db, user, "participant.imported", "participant", file.filename or "",
                 {"added": added, "skipped": skipped}, client_ip(request))
    db.commit()
    flash(request, f"Imported {added} participants; skipped {skipped} (blank or already registered).")
    return redirect("/admin/participants")


# ---------------- courses ----------------

@router.get("/courses")
def courses(request: Request, user: User = Depends(require_viewer), db: Session = Depends(get_db)):
    rows = list(db.scalars(select(Course).order_by(Course.category, Course.code)))
    cats = sorted({c.category for c in rows if c.category})
    return render(request, "admin/courses.html", {"rows": rows, "cats": cats, "edit": None}, user=user)


@router.get("/courses/{cid}")
def course_edit(cid: int, request: Request, user: User = Depends(require_issuer), db: Session = Depends(get_db)):
    rows = list(db.scalars(select(Course).order_by(Course.category, Course.code)))
    cats = sorted({c.category for c in rows if c.category})
    return render(request, "admin/courses.html", {"rows": rows, "cats": cats, "edit": _get(db, Course, cid)}, user=user)


@router.post("/courses/save", dependencies=CSRF)
def course_save(request: Request, cid: str = Form(""), code: str = Form(""), title: str = Form(""),
                category: str = Form(""), duration_hours: str = Form(""), validity_months: str = Form(""),
                description: str = Form(""),
                is_active: str = Form(""), user: User = Depends(require_issuer), db: Session = Depends(get_db)):
    code = clean_name(code).upper()
    title = clean_name(title)
    c = db.get(Course, _int(cid)) if _int(cid) else None
    try:
        if not re.fullmatch(r"[A-Z0-9][A-Z0-9\-_.]{1,39}", code):
            raise CertificateError("Course code: 2–40 letters, digits, - _ . (e.g. PS-HSE-001)")
        if not title or len(title) > 160:
            raise CertificateError("Course title is required (max 160 characters)")
        hours = parse_hours(duration_hours)
        if c is None:
            c = Course(code=code, title=title, duration_hours=hours)
            db.add(c)
        c.code, c.title, c.duration_hours = code, title, hours
        vm = validity_months.strip()
        if vm and (not vm.isdigit() or not 1 <= int(vm) <= 240):
            raise CertificateError("Validity must be a whole number of months between 1 and 240, or blank for no expiry")
        c.validity_months = int(vm) if vm else None
        c.category = clean_name(category) or "HSE & Occupational Safety"
        c.description = description.strip()[:2000]
        c.is_active = is_active == "on" or not cid
        db.flush()
        audit.record(db, user, "course.saved", "course", c.code, {"title": c.title, "hours": str(hours)}, client_ip(request))
        db.commit()
    except IntegrityError:
        db.rollback()
        flash(request, f"Course code {code} already exists.", "err")
        return redirect("/admin/courses")
    except CertificateError as e:
        db.rollback()
        flash(request, str(e), "err")
        return redirect(f"/admin/courses/{cid}" if cid else "/admin/courses")
    flash(request, f"Course {code} saved.")
    return redirect("/admin/courses")


# ---------------- instructors ----------------

@router.get("/instructors")
def instructors(request: Request, user: User = Depends(require_viewer), db: Session = Depends(get_db)):
    rows = list(db.scalars(select(Instructor).order_by(Instructor.full_name)))
    return render(request, "admin/instructors.html", {"rows": rows, "edit": None}, user=user)


@router.get("/instructors/{iid}")
def instructor_edit(iid: int, request: Request, user: User = Depends(require_issuer), db: Session = Depends(get_db)):
    rows = list(db.scalars(select(Instructor).order_by(Instructor.full_name)))
    return render(request, "admin/instructors.html", {"rows": rows, "edit": _get(db, Instructor, iid)}, user=user)


@router.post("/instructors/save", dependencies=CSRF)
async def instructor_save(request: Request, user: User = Depends(require_issuer), db: Session = Depends(get_db)):
    form = await request.form()
    iid = _int(form.get("iid"))
    name = clean_name(form.get("full_name", ""))
    if not name or len(name) > 120:
        flash(request, "Instructor name is required (max 120 characters).", "err")
        return redirect("/admin/instructors")
    i = db.get(Instructor, iid) if iid else None
    if i is None:
        i = Instructor(full_name=name)
        db.add(i)
    i.full_name = name
    i.title = clean_name(form.get("title", "")) or "Instructor / Trainer"
    i.show_publicly = form.get("show_publicly") == "on"
    i.is_active = form.get("is_active") == "on" or not iid
    sig = form.get("signature")
    try:
        if sig is not None and getattr(sig, "filename", ""):
            i.signature_file = save_image_upload(sig)
        if form.get("remove_signature") == "on":
            i.signature_file = None
    except CertificateError as e:
        db.rollback()
        flash(request, str(e), "err")
        return redirect("/admin/instructors")
    db.flush()
    audit.record(db, user, "instructor.saved", "instructor", i.id, {"name": i.full_name}, client_ip(request))
    db.commit()
    flash(request, "Instructor saved. (Certificates already issued keep the instructor name printed on them.)")
    return redirect("/admin/instructors")


# ---------------- batches ----------------

@router.get("/batches")
def batches(request: Request, user: User = Depends(require_viewer), db: Session = Depends(get_db)):
    rows = list(db.scalars(select(Batch).order_by(Batch.id.desc()).limit(300)))
    return render(request, "admin/batches.html", {"rows": rows, **_lists(db)}, user=user)


@router.post("/batches", dependencies=CSRF)
def batch_create(request: Request, course_id: str = Form(""), instructor_id: str = Form(""), start_date: str = Form(""),
                 end_date: str = Form(""), location: str = Form(""), notes: str = Form(""),
                 user: User = Depends(require_issuer), db: Session = Depends(get_db)):
    try:
        course = db.get(Course, _int(course_id) or 0)
        if not course:
            raise CertificateError("Choose a course")
        s, e = _date(start_date, "Start date"), _date(end_date, "End date")
        if s > e:
            raise CertificateError("Start date is after end date")
        n = db.scalar(select(func.count()).select_from(Batch).where(Batch.reference.like(f"B-{s.year}-%"))) + 1
        b = Batch(reference=f"B-{s.year}-{n:04d}", course_id=course.id, instructor_id=_int(instructor_id),
                  start_date=s, end_date=e, location=clean_name(location), notes=notes.strip()[:2000])
        db.add(b)
        db.flush()
        audit.record(db, user, "batch.created", "batch", b.reference, {"course": course.code}, client_ip(request))
        db.commit()
    except CertificateError as e:
        db.rollback()
        flash(request, str(e), "err")
        return redirect("/admin/batches")
    flash(request, f"Batch {b.reference} created. Add participants below.")
    return redirect(f"/admin/batches/{b.id}")


@router.get("/batches/{bid}")
def batch_detail(bid: int, request: Request, user: User = Depends(require_viewer), db: Session = Depends(get_db)):
    b = _get(db, Batch, bid)
    certs = {c.participant_id: c for c in db.scalars(select(Certificate).where(Certificate.batch_id == bid,
                                                                             Certificate.status != "revoked"))}
    return render(request, "admin/batch_detail.html", {"b": b, "certs": certs}, user=user)


@router.post("/batches/{bid}/add", dependencies=CSRF)
def batch_add(bid: int, request: Request, names: str = Form(""), participant_ids: str = Form(""),
              user: User = Depends(require_issuer), db: Session = Depends(get_db)):
    b = _get(db, Batch, bid)
    existing = {e.participant_id for e in b.enrollments}
    added = 0
    ids = [i for i in (_int(x) for x in re.split(r"[,\s]+", participant_ids or "")) if i]
    for line in (names or "").splitlines():
        name = clean_name(line)
        if not name:
            continue
        if len(name) > 120:
            flash(request, f"Skipped a name longer than 120 characters: {name[:40]}…", "err")
            continue
        p = db.scalar(select(Participant).where(func.lower(Participant.full_name) == name.lower()))
        if p is None:
            p = Participant(full_name=name)
            db.add(p)
            db.flush()
        ids.append(p.id)
    for pid in ids:
        if pid in existing or not db.get(Participant, pid):
            continue
        db.add(Enrollment(batch_id=b.id, participant_id=pid, completed=True))
        existing.add(pid)
        added += 1
    audit.record(db, user, "batch.participants_added", "batch", b.reference, {"added": added}, client_ip(request))
    db.commit()
    flash(request, f"{added} participant(s) added to {b.reference}.")
    return redirect(f"/admin/batches/{bid}")


@router.post("/batches/{bid}/enrollment/{eid}", dependencies=CSRF)
def batch_enrollment(bid: int, eid: int, request: Request, action: str = Form(""),
                     user: User = Depends(require_issuer), db: Session = Depends(get_db)):
    e = _get(db, Enrollment, eid)
    if e.batch_id != bid:
        raise HTTPException(status_code=404)
    if action == "toggle":
        e.completed = not e.completed
    elif action == "remove":
        has_cert = db.scalar(select(func.count()).select_from(Certificate).where(
            Certificate.batch_id == bid, Certificate.participant_id == e.participant_id, Certificate.status != "revoked"))
        if has_cert:
            flash(request, "This participant already has a certificate from this batch.", "err")
            return redirect(f"/admin/batches/{bid}")
        db.delete(e)
    db.commit()
    return redirect(f"/admin/batches/{bid}")


@router.post("/batches/{bid}/issue", dependencies=CSRF)
def batch_issue(bid: int, request: Request, user: User = Depends(require_issuer), db: Session = Depends(get_db)):
    b = _get(db, Batch, bid)
    ip = client_ip(request)
    issued, skipped = [], []
    for e in b.enrollments:
        if not e.completed:
            continue
        p = e.participant
        if svc.find_duplicates(db, p.full_name, b.course.code, b.end_date):
            skipped.append(p.full_name)
            continue
        try:
            cert = svc.create_draft(db, CertificateInput(
                participant_id=p.id, course_id=b.course_id, start_date=b.start_date, completion_date=b.end_date,
                duration_hours=Decimal(b.course.duration_hours), instructor_id=b.instructor_id, batch_id=b.id,
                allow_public_pdf=settings_store.flag(settings_store.get_all(db), "default_allow_public_pdf")), user, ip)
            svc.issue(db, cert, user, ip)
            issued.append(cert)
        except CertificateError as ex:
            db.rollback()
            flash(request, f"{p.full_name}: {ex}", "err")
            return redirect(f"/admin/batches/{bid}")
    db.commit()
    if issued:
        fname = _export_zip(b.reference, issued, db)
        flash(request, f"Issued {len(issued)} certificate(s)."
              + (f" Skipped {len(skipped)} already certified." if skipped else ""), "ok")
        request.session["last_zip"] = fname
        return redirect(f"/admin/batches/{bid}?zip={fname}")
    flash(request, "No new certificates to issue" + (f" ({len(skipped)} already certified)." if skipped else "."), "err")
    return redirect(f"/admin/batches/{bid}")


@router.get("/batches/{bid}/zip")
def batch_zip(bid: int, user: User = Depends(require_viewer), db: Session = Depends(get_db)):
    b = _get(db, Batch, bid)
    certs = list(db.scalars(select(Certificate).where(Certificate.batch_id == bid, Certificate.status == "issued")))
    if not certs:
        raise HTTPException(status_code=404, detail="No issued certificates in this batch yet.")
    return FileResponse(settings.exports_dir / _export_zip(b.reference, certs, db), media_type="application/zip",
                        filename=f"{b.reference}-certificates.zip")


@router.get("/exports/{name}")
def export_file(name: str, user: User = Depends(require_viewer)):
    if not re.fullmatch(r"[A-Za-z0-9_\-]+\.zip", name):
        raise HTTPException(status_code=404)
    path = settings.exports_dir / name
    if not path.exists():
        raise HTTPException(status_code=404)
    return FileResponse(path, media_type="application/zip", filename=name)


# ---------------- logs ----------------

@router.get("/verifications")
def verifications(request: Request, q: str = "", user: User = Depends(require_viewer), db: Session = Depends(get_db)):
    stmt = select(VerificationLog).order_by(VerificationLog.id.desc())
    if q.strip():
        stmt = stmt.where(VerificationLog.query.ilike(f"%{q.strip()}%"))
    rows = list(db.scalars(stmt.limit(500)))
    stats = dict(db.execute(select(VerificationLog.result, func.count()).group_by(VerificationLog.result)).all())
    return render(request, "admin/verifications.html", {"rows": rows, "q": q, "stats": stats}, user=user)


@router.get("/audit")
def audit_log(request: Request, q: str = "", user: User = Depends(require_admin), db: Session = Depends(get_db)):
    stmt = select(AuditLog).order_by(AuditLog.id.desc())
    if q.strip():
        like = f"%{q.strip()}%"
        stmt = stmt.where(AuditLog.action.ilike(like) | AuditLog.entity_ref.ilike(like) | AuditLog.user_email.ilike(like))
    return render(request, "admin/audit.html", {"rows": list(db.scalars(stmt.limit(500))), "q": q}, user=user)


# ---------------- users ----------------

@router.get("/users")
def users(request: Request, user: User = Depends(require_admin), db: Session = Depends(get_db)):
    return render(request, "admin/users.html", {"rows": list(db.scalars(select(User).order_by(User.id))),
                                                  "roles": ROLES}, user=user)


@router.post("/users", dependencies=CSRF)
def user_create(request: Request, email: str = Form(""), full_name: str = Form(""), role: str = Form("issuer"),
                password: str = Form(""), user: User = Depends(require_admin), db: Session = Depends(get_db)):
    email = email.strip().lower()
    problems = password_problems(password)
    if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email) or role not in ROLES or not clean_name(full_name) or problems:
        flash(request, "Check the details. " + ("Password needs " + ", ".join(problems) + "." if problems else ""), "err")
        return redirect("/admin/users")
    if db.scalar(select(User).where(User.email == email)):
        flash(request, "A user with this email already exists.", "err")
        return redirect("/admin/users")
    u = User(email=email, full_name=clean_name(full_name), role=role, password_hash=hash_password(password))
    db.add(u)
    db.flush()
    audit.record(db, user, "user.created", "user", email, {"role": role}, client_ip(request))
    db.commit()
    flash(request, f"User {email} created. Ask them to change the temporary password after signing in.")
    return redirect("/admin/users")


@router.post("/users/{uid}", dependencies=CSRF)
def user_update(uid: int, request: Request, action: str = Form(""), role: str = Form(""), password: str = Form(""),
                user: User = Depends(require_admin), db: Session = Depends(get_db)):
    u = _get(db, User, uid)
    if u.id == user.id and action in ("toggle", "role"):
        flash(request, "You cannot deactivate or change the role of your own account.", "err")
        return redirect("/admin/users")
    if action == "toggle":
        u.is_active = not u.is_active
        u.session_version += 1
    elif action == "role" and role in ROLES:
        u.role = role
        u.session_version += 1
    elif action == "password":
        problems = password_problems(password)
        if problems:
            flash(request, "Password needs " + ", ".join(problems) + ".", "err")
            return redirect("/admin/users")
        u.password_hash = hash_password(password)
        u.session_version += 1
        u.failed_logins, u.locked_until = 0, None
    else:
        raise HTTPException(status_code=400)
    audit.record(db, user, f"user.{action}", "user", u.email, {"role": u.role, "active": u.is_active}, client_ip(request))
    db.commit()
    flash(request, f"User {u.email} updated.")
    return redirect("/admin/users")


# ---------------- settings ----------------

@router.get("/settings")
def settings_page(request: Request, user: User = Depends(require_admin), db: Session = Depends(get_db)):
    return render(request, "admin/settings.html", {"s": settings_store.get_all(db)}, user=user)


@router.post("/settings", dependencies=CSRF)
async def settings_save(request: Request, user: User = Depends(require_admin), db: Session = Depends(get_db)):
    form = await request.form()
    updates = {}
    for key in ("center_name", "brand_name", "brand_descriptor", "signatory_name", "signatory_title", "contact_line"):
        updates[key] = clean_name(form.get(key, ""))[:200]
    if not updates["center_name"]:
        updates["center_name"] = settings_store.DEFAULTS["center_name"]
    for key in ("default_allow_public_pdf", "public_full_name_without_key"):
        updates[key] = "1" if form.get(key) == "on" else "0"
    if form.get("certificate_design") in ("classic", "executive", "heritage"):
        updates["certificate_design"] = form.get("certificate_design")
    if form.get("qr_mode") in ("text", "text_link", "link"):
        updates["qr_mode"] = form.get("qr_mode")
    sig = form.get("signature")
    try:
        if sig is not None and getattr(sig, "filename", ""):
            updates["signatory_signature_file"] = save_image_upload(sig)
        if form.get("remove_signature") == "on":
            updates["signatory_signature_file"] = ""
    except CertificateError as e:
        flash(request, str(e), "err")
        return redirect("/admin/settings")
    settings_store.set_many(db, updates)
    audit.record(db, user, "settings.updated", "settings", "", {k: v for k, v in updates.items()}, client_ip(request))
    db.commit()
    flash(request, "Settings saved. New and regenerated PDFs use these details; issued certificate data is unchanged.")
    return redirect("/admin/settings")


@router.get("/settings/preview.pdf")
def settings_preview(user: User = Depends(require_viewer), db: Session = Depends(get_db)):
    """A sample certificate (watermarked, no QR) to check branding before issuing."""
    s = settings_store.get_all(db)
    fake = Certificate(participant_name="Ahmed Mohamed", course_title="Flagman / Banksman Training",
                       course_code="PS-HSE-001", duration_hours=Decimal("8"), start_date=date.today(),
                       completion_date=date.today(), instructor_name="[Instructor Name]", status="draft")
    accs = [{"body": a.body_name, "statement": a.statement, "ref": a.reference_no, "logo": a.logo_file}
            for a in db.scalars(select(Accreditation).where(Accreditation.is_active))][:4]
    data = render_pdf(fake, s, sample=True, accreditations=accs)
    return Response(data, media_type="application/pdf", headers={"Content-Disposition": 'inline; filename="sample.pdf"'})


# ---------------- accreditations ----------------

@router.get("/accreditations")
def accreditations(request: Request, edit: str = "", user: User = Depends(require_admin), db: Session = Depends(get_db)):
    rows = list(db.scalars(select(Accreditation).order_by(Accreditation.id)))
    courses = list(db.scalars(select(Course).order_by(Course.code)))
    return render(request, "admin/accreditations.html", {
        "rows": rows, "courses": courses, "edit": db.get(Accreditation, _int(edit)) if _int(edit) else None}, user=user)


@router.post("/accreditations", dependencies=CSRF)
async def accreditation_save(request: Request, user: User = Depends(require_admin), db: Session = Depends(get_db)):
    form = await request.form()
    aid = _int(form.get("aid"))
    a = db.get(Accreditation, aid) if aid else None
    try:
        body = clean_name(form.get("body_name", ""))
        statement = clean_name(form.get("statement", ""))
        if not body or not statement:
            raise CertificateError("Enter the awarding body and the approval wording exactly as your approval letter states it")
        if form.get("confirm_held") != "on":
            raise CertificateError("Confirm that the center currently holds this approval")
        logo = form.get("logo")
        logo_file = save_image_upload(logo, "logo") if logo is not None and getattr(logo, "filename", "") else None
        if a is None and not logo_file:
            raise CertificateError("Upload the official logo file supplied with your approval")
        if a is None:
            a = Accreditation(body_name=body, statement=statement, logo_file=logo_file)
            db.add(a)
        a.body_name, a.statement = body[:120], statement[:200]
        a.reference_no = clean_name(form.get("reference_no", ""))[:80]
        if logo_file:
            a.logo_file = logo_file
        a.applies_to_all = form.get("scope") == "all"
        ids = {int(x) for x in form.getlist("course_ids") if str(x).isdigit()}
        a.courses = [] if a.applies_to_all else list(db.scalars(select(Course).where(Course.id.in_(ids)))) if ids else []
        if not a.applies_to_all and not a.courses:
            raise CertificateError("Choose the courses this approval covers (or 'All courses')")
        vu = str(form.get("valid_until") or "").strip()
        a.valid_until = _date(vu, "Approval valid until") if vu else None
        a.is_active = form.get("is_active") == "on" or not aid
        db.flush()
        audit.record(db, user, "accreditation.saved", "accreditation", a.id,
                     {"body": a.body_name, "ref": a.reference_no, "all": a.applies_to_all,
                      "courses": [c.code for c in a.courses]}, client_ip(request))
        db.commit()
    except CertificateError as e:
        db.rollback()
        flash(request, str(e), "err")
        return redirect("/admin/accreditations" + (f"?edit={aid}" if aid else ""))
    flash(request, f"{a.body_name} saved. It appears on certificates issued from now on for the selected courses; "
                   "certificates already issued are unchanged.")
    return redirect("/admin/accreditations")


@router.get("/uploads/{name}")
def upload_file(name: str, user: User = Depends(require_viewer)):
    from ..services import files
    path = files.local_path(name)
    if path is None:
        raise HTTPException(status_code=404)
    return FileResponse(path, media_type="image/png")
