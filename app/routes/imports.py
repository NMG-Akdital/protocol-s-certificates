"""Bulk certificate generation from CSV / Excel."""
from __future__ import annotations

import csv
import io
import json
import secrets
from datetime import datetime

from fastapi import APIRouter, BackgroundTasks, Depends, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import settings
from ..db import SessionLocal, get_db
from ..models import Certificate, ImportJob, User
from ..security import client_ip, require_issuer, require_viewer, verify_csrf
from ..services import audit, settings_store
from ..services.certificates import CertificateError
from ..services.importer import TEMPLATE_HEADERS, build_sample_certificate, run_job, validate
from ..services.pdf import build_zip, get_or_create_pdf, pdf_filename, render_pdf
from ..web import flash, redirect, render

router = APIRouter(prefix="/admin/import")
CSRF = [Depends(verify_csrf)]


@router.get("")
def import_page(request: Request, user: User = Depends(require_issuer), db: Session = Depends(get_db)):
    jobs = list(db.scalars(select(ImportJob).order_by(ImportJob.id.desc()).limit(20)))
    return render(request, "admin/import_upload.html", {"jobs": jobs, "headers": TEMPLATE_HEADERS}, user=user)


@router.get("/template.csv")
def template(user: User = Depends(require_viewer)):
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(TEMPLATE_HEADERS)
    w.writerow(["Ahmed Mohamed", "Flagman Training", "PS-HSE-001", "8 Hours", "2026-11-10", "2026-11-10",
                "Trainer Name", "2028-11-09", "", ""])
    return Response("﻿" + buf.getvalue(), media_type="text/csv",
                    headers={"Content-Disposition": 'attachment; filename="protocol-s-import-template.csv"'})


@router.post("", dependencies=CSRF)
async def upload(request: Request, file: UploadFile = File(...), user: User = Depends(require_issuer),
                 db: Session = Depends(get_db)):
    limit = settings.max_upload_mb * 1024 * 1024
    data = await file.read(limit + 1)
    try:
        if len(data) > limit:
            raise CertificateError(f"File is larger than {settings.max_upload_mb} MB")
        result = validate(db, data, file.filename or "")
    except CertificateError as e:
        flash(request, str(e), "err")
        return redirect("/admin/import")
    job = ImportJob(filename=(file.filename or "upload")[:255], status="validated",
                    rows_json=json.dumps(result["rows"], ensure_ascii=False),
                    summary_json=json.dumps(result["summary"]), created_by=user.id)
    db.add(job)
    db.flush()
    audit.record(db, user, "import.validated", "import_job", job.id, result["summary"], client_ip(request))
    db.commit()
    return redirect(f"/admin/import/{job.id}")


def _job(db: Session, job_id: int) -> ImportJob:
    job = db.get(ImportJob, job_id)
    if not job:
        raise HTTPException(status_code=404)
    return job


@router.get("/{job_id}")
def job_page(job_id: int, request: Request, user: User = Depends(require_viewer), db: Session = Depends(get_db)):
    job = _job(db, job_id)
    certs = list(db.scalars(select(Certificate).where(Certificate.import_job_id == job_id).order_by(Certificate.id)))
    return render(request, "admin/import_job.html", {
        "job": job, "rows": json.loads(job.rows_json), "summary": json.loads(job.summary_json or "{}"),
        "certs": certs}, user=user)


@router.get("/{job_id}/sample.pdf")
def job_sample(job_id: int, user: User = Depends(require_viewer), db: Session = Depends(get_db)):
    job = _job(db, job_id)
    rows = [r for r in json.loads(job.rows_json) if r["status"] == "ok"]
    if not rows:
        raise HTTPException(status_code=404, detail="No valid rows to preview.")
    longest = max(rows, key=lambda r: len(r["data"]["participant_name"]) + len(r["data"]["course_title"]))
    from ..models import Course
    from ..services import certificates as svc
    course = db.scalar(select(Course).where(Course.code == longest["data"]["course_code"]))
    accs = svc.accreditations_for(db, course.id) if course else []
    data = render_pdf(build_sample_certificate(longest), settings_store.get_all(db), sample=True, accreditations=accs)
    return Response(data, media_type="application/pdf", headers={"Content-Disposition": 'inline; filename="sample.pdf"'})


@router.post("/{job_id}/confirm", dependencies=CSRF)
def confirm(job_id: int, request: Request, background: BackgroundTasks, user: User = Depends(require_issuer),
            db: Session = Depends(get_db)):
    job = _job(db, job_id)
    if job.status != "validated":
        flash(request, "This import has already been processed.", "err")
        return redirect(f"/admin/import/{job_id}")
    if not json.loads(job.summary_json).get("ok"):
        flash(request, "There are no valid rows to issue.", "err")
        return redirect(f"/admin/import/{job_id}")
    job.status = "processing"
    audit.record(db, user, "import.confirmed", "import_job", job_id, None, client_ip(request))
    db.commit()
    background.add_task(run_job, job_id, user.id, SessionLocal)
    return redirect(f"/admin/import/{job_id}")


@router.post("/{job_id}/cancel", dependencies=CSRF)
def cancel(job_id: int, request: Request, user: User = Depends(require_issuer), db: Session = Depends(get_db)):
    job = _job(db, job_id)
    if job.status == "validated":
        job.status = "cancelled"
        audit.record(db, user, "import.cancelled", "import_job", job_id, None, client_ip(request))
        db.commit()
    return redirect("/admin/import")


@router.get("/{job_id}/zip")
def job_zip(job_id: int, user: User = Depends(require_viewer), db: Session = Depends(get_db)):
    job = _job(db, job_id)
    if job.zip_file and (settings.exports_dir / job.zip_file).exists():
        return FileResponse(settings.exports_dir / job.zip_file, media_type="application/zip",
                            filename=f"certificates-import-{job_id}.zip")
    # Rebuild from issued certificates (e.g. after a restart or a failed PDF step).
    certs = list(db.scalars(select(Certificate).where(Certificate.import_job_id == job_id,
                                                      Certificate.status != "draft")))
    if not certs:
        raise HTTPException(status_code=404, detail="No certificates were issued by this import.")
    s = settings_store.get_all(db)
    data = build_zip([(pdf_filename(c), get_or_create_pdf(c, s)) for c in certs])
    fname = f"import-{job_id}_{datetime.now().strftime('%Y%m%d-%H%M%S')}_{secrets.token_hex(3)}.zip"
    (settings.exports_dir / fname).write_bytes(data)
    job.zip_file = fname
    db.commit()
    return FileResponse(settings.exports_dir / fname, media_type="application/zip",
                        filename=f"certificates-import-{job_id}.zip")
