"""Certificate PDF rendering (HTML/CSS -> PDF with WeasyPrint; vector text and QR)."""
from __future__ import annotations

import hashlib
import io
import zipfile
from pathlib import Path
from urllib.parse import urlparse

from jinja2 import Environment, FileSystemLoader, select_autoescape
from weasyprint import HTML

from ..config import APP_DIR, settings
from ..models import Certificate
from .certificates import (accreditations_of, format_date, format_range, hours_display, public_lookup_url,
                           verification_url)
from .qr import qr_svg_data_uri

_env = Environment(loader=FileSystemLoader(str(APP_DIR / "templates")),
                   autoescape=select_autoescape(["html"]))
STATIC_DIR = APP_DIR / "static"


def _fit(text: str, steps: list[tuple[int, float]], smallest: float) -> float:
    n = len(text or "")
    for max_len, pt in steps:
        if n <= max_len:
            return pt
    return smallest


def name_size(name: str) -> float:
    return _fit(name, [(20, 32), (26, 28), (34, 24), (44, 21), (60, 18.5)], 16)


def course_size(title: str) -> float:
    return _fit(title, [(30, 22), (44, 18.5), (64, 16), (100, 14)], 12.5)


def _upload_uri(filename: str | None) -> str | None:
    from .files import local_path
    p = local_path(filename)
    return p.as_uri() if p else None


def render_html(cert: Certificate, s: dict[str, str], sample: bool = False,
                accreditations: list[dict] | None = None) -> str:
    issued = cert.status in ("issued", "revoked") and cert.certificate_no
    accs = accreditations_of(cert) if issued else (accreditations or [])
    accs = [dict(a, logo_uri=_upload_uri(a.get("logo"))) for a in accs][:4]
    qr = qr_svg_data_uri(verification_url(cert)) if issued else None
    lookup = urlparse(public_lookup_url())
    instructor_sig = _upload_uri(cert.instructor.signature_file) if cert.instructor else None
    name_pt, course_pt = name_size(cert.participant_name), course_size(cert.course_title)
    compact = name_pt <= 18.5 or course_pt <= 14
    if compact and accs:  # longest names + titles + accreditation row: shrink a little more
        name_pt, course_pt = name_pt - 1.5, course_pt - 1
    return _env.get_template("pdf/certificate.html").render(
        c=cert, s=s, sample=sample or not issued,
        qr=qr,
        verify_url=verification_url(cert) if issued else "",
        name_pt=name_pt,
        compact=compact,
        course_pt=course_pt,
        hours=hours_display(cert.duration_hours),
        completion=format_date(cert.completion_date),
        start=format_date(cert.start_date),
        expiry=format_date(cert.expiry_date) if cert.expiry_date else "",
        accreditations=accs,
        dates=format_range(cert.start_date, cert.completion_date),
        issued=format_date(cert.issue_date) if cert.issue_date else "",
        lookup_display=(lookup.netloc + lookup.path),
        instructor_sig=instructor_sig,
        signatory_sig=_upload_uri(s.get("signatory_signature_file")),
    )


def render_pdf(cert: Certificate, s: dict[str, str], sample: bool = False,
               accreditations: list[dict] | None = None) -> bytes:
    html = render_html(cert, s, sample=sample, accreditations=accreditations)
    return HTML(string=html, base_url=str(STATIC_DIR) + "/").write_pdf(
        pdf_variant=None, uncompressed_pdf=False,
        metadata={"title": f"Certificate {cert.certificate_no or 'preview'}"},
    )


def archive_path(cert: Certificate) -> Path:
    year = cert.certificate_no.split("-")[1]
    folder = settings.certificates_dir / year
    folder.mkdir(parents=True, exist_ok=True)
    return folder / f"{cert.certificate_no}.pdf"


def get_or_create_pdf(cert: Certificate, s: dict[str, str], accreditations: list[dict] | None = None) -> bytes:
    """Return the archived PDF for an issued certificate, generating it on first use.
    If the archive is missing or altered, it is regenerated from the signed record."""
    if cert.status not in ("issued", "revoked") or not cert.certificate_no:
        return render_pdf(cert, s, sample=True, accreditations=accreditations)
    path = archive_path(cert)
    if path.exists():
        data = path.read_bytes()
        if cert.pdf_sha256 and hashlib.sha256(data).hexdigest() == cert.pdf_sha256:
            return data
    data = render_pdf(cert, s)
    path.write_bytes(data)
    cert.pdf_sha256 = hashlib.sha256(data).hexdigest()  # caller commits
    return data


def pdf_filename(cert: Certificate) -> str:
    safe = "".join(ch if ch.isalnum() else "_" for ch in cert.participant_name)[:60].strip("_")
    return f"{cert.certificate_no or 'DRAFT'}_{safe}.pdf"


def build_zip(items: list[tuple[str, bytes]]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for name, data in items:
            zf.writestr(name, data)
    return buf.getvalue()
