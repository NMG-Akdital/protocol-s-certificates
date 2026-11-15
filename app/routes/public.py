"""Public, read-only pages. Nothing here can create, change or revoke data."""
from __future__ import annotations

import hmac

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import PlainTextResponse, Response
from sqlalchemy.orm import Session

from ..config import settings
from ..db import get_db
from ..models import VerificationLog
from ..security import client_ip, ip_hash, limiter
from ..services import certificates as svc, settings_store
from ..services.pdf import get_or_create_pdf, pdf_filename
from ..web import redirect, render

router = APIRouter()


def _mask(name: str) -> str:
    parts = name.split()
    if len(parts) <= 1:
        return (name[:1] + "…") if name else ""
    return parts[0] + " " + " ".join(p[0] + "." for p in parts[1:] if p)


def _key_ok(cert, k: str | None) -> bool:
    return bool(k and cert.verify_token and hmac.compare_digest(k, cert.verify_token))


@router.get("/", include_in_schema=False)
def home(request: Request):
    return redirect("/verify")


@router.get("/healthz", include_in_schema=False)
def healthz():
    return PlainTextResponse("ok")


@router.get("/robots.txt", include_in_schema=False)
def robots():
    return PlainTextResponse("User-agent: *\nDisallow: /\n")


@router.get("/verify")
def verify_form(request: Request, db: Session = Depends(get_db), id: str = ""):
    if id.strip():
        return redirect(f"/verify/{id.strip().upper()}?m=1")
    s = settings_store.get_all(db)
    return render(request, "public/verify_form.html", {"s": s})


@router.get("/verify/{certificate_no}")
def verify(certificate_no: str, request: Request, k: str | None = None, m: str | None = None,
           db: Session = Depends(get_db)):
    ip = client_ip(request)
    if not limiter.allow(f"verify:{ip}", settings.verify_rate_limit_per_minute):
        raise HTTPException(status_code=429, detail="Too many verification requests. Please wait a minute and try again.")

    s = settings_store.get_all(db)
    cert = svc.get_by_no(db, certificate_no)
    shown_no = certificate_no.strip().upper()[:40]

    if cert is None or cert.status not in ("issued", "revoked"):
        result = "NOT_FOUND"
    elif not svc.signature_ok(cert):
        result = "INVALID"
    elif cert.status == "revoked":
        result = "REVOKED"
    elif svc.is_expired(cert):
        result = "EXPIRED"
    else:
        result = "VALID"

    with_key = cert is not None and _key_ok(cert, k)
    full_name = with_key or settings_store.flag(s, "public_full_name_without_key")
    db.add(VerificationLog(query=shown_no, result=result, via="qr" if k else "manual",
                           ip_hash=ip_hash(ip), user_agent=(request.headers.get("user-agent") or "")[:200]))
    db.commit()

    from datetime import datetime, timezone
    ctx = {"s": s, "result": result, "query": shown_no, "cert": None,
           "now_utc": datetime.now(timezone.utc).strftime("%d %B %Y, %H:%M UTC")}
    if result in ("VALID", "REVOKED", "EXPIRED"):
        replacement = None
        if cert.replaced_by_id:
            from ..models import Certificate
            r = db.get(Certificate, cert.replaced_by_id)
            replacement = r.certificate_no if r else None
        ctx.update({
            "cert": cert,
            "name": cert.participant_name if full_name else _mask(cert.participant_name),
            "name_masked": not full_name,
            "hours": svc.hours_display(cert.duration_hours),
            "completion": svc.format_date(cert.completion_date),
            "start": svc.format_date(cert.start_date),
            "issued": svc.format_date(cert.issue_date),
            "instructor": cert.instructor_name if cert.show_instructor_public else "",
            "expiry": svc.format_date(cert.expiry_date) if cert.expiry_date else "",
            "accreditations": svc.accreditations_of(cert),
            "replacement": replacement,
            "pdf_link": (f"/verify/{cert.certificate_no}/certificate.pdf?k={cert.verify_token}"
                         if result == "VALID" and with_key and cert.allow_public_pdf else None),
        })
    status = 404 if result == "NOT_FOUND" else 200
    return render(request, "public/verify_result.html", ctx, status_code=status)


@router.get("/verify/{certificate_no}/certificate.pdf")
def public_pdf(certificate_no: str, request: Request, k: str | None = None, db: Session = Depends(get_db)):
    ip = client_ip(request)
    if not limiter.allow(f"verify:{ip}", settings.verify_rate_limit_per_minute):
        raise HTTPException(status_code=429, detail="Too many requests.")
    cert = svc.get_by_no(db, certificate_no)
    if (cert is None or cert.status != "issued" or not cert.allow_public_pdf
            or not _key_ok(cert, k) or not svc.signature_ok(cert)):
        raise HTTPException(status_code=404)
    data = get_or_create_pdf(cert, settings_store.get_all(db))
    db.commit()
    return Response(data, media_type="application/pdf",
                    headers={"Content-Disposition": f'inline; filename="{pdf_filename(cert)}"'})
