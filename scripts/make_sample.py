"""Render a sample certificate PDF (realistic data) without touching the main database.

Usage: python scripts/make_sample.py [output.pdf] [--issued]
"""
import os, sys, tempfile
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

tmp = tempfile.mkdtemp()
os.environ.setdefault("DATA_DIR", tmp)
os.environ["DATABASE_URL"] = f"sqlite:///{tmp}/sample.db"

from datetime import date
from decimal import Decimal
from app.db import init_db, session_scope
from app.models import Accreditation, Course, Instructor, Participant
from app.services import certificates as svc, settings_store
from app.services.pdf import render_pdf

out = Path(sys.argv[1] if len(sys.argv) > 1 and not sys.argv[1].startswith("--") else "sample/sample-certificate.pdf")
name = os.environ.get("SAMPLE_NAME", "Ahmed Mohamed")
course_title = os.environ.get("SAMPLE_COURSE", "Flagman / Banksman Training")
init_db()
with session_scope() as db:
    settings_store.set_many(db, {"signatory_name": os.environ.get("SAMPLE_SIGNATORY", "[Authorized Signatory Name]")})
    months = int(os.environ["SAMPLE_VALIDITY_MONTHS"]) if os.environ.get("SAMPLE_VALIDITY_MONTHS") else None
    p = Participant(full_name=name); c = Course(code="PS-HSE-001", title=course_title, duration_hours=Decimal("8"), validity_months=months)
    # SAMPLE_LOGOS="file.png|Body|Statement|Ref;..." — logo files are copied from paths you provide.
    for spec in filter(None, os.environ.get("SAMPLE_LOGOS", "").split(";")):
        path, body, statement, ref = (spec.split("|") + ["", "", ""])[:4]
        import secrets
        from app.services import files
        dest = ""
        if path:
            dest = f"logo_{secrets.token_hex(8)}.png"; files.store(dest, Path(path).read_bytes())
        db.add(Accreditation(body_name=body, statement=statement, reference_no=ref, logo_file=dest, applies_to_all=True))
    i = Instructor(full_name=os.environ.get("SAMPLE_INSTRUCTOR", "[Instructor Name]"))
    db.add_all([p, c, i]); db.flush()
    cert = svc.create_draft(db, svc.CertificateInput(participant_id=p.id, course_id=c.id, start_date=date(2026, 11, 10),
                            completion_date=date(2026, 11, 10), duration_hours=Decimal("8"), instructor_id=i.id), None)
    if "--issued" in sys.argv:
        svc.issue(db, cert, None, issue_date=date(2026, 11, 15))
    pdf = render_pdf(cert, settings_store.get_all(db), sample="--issued" not in sys.argv)
out.parent.mkdir(parents=True, exist_ok=True)
out.write_bytes(pdf)
print(out, len(pdf), "bytes", cert.certificate_no, svc.verification_url(cert) if cert.certificate_no else "")
