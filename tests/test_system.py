"""End-to-end tests through the HTTP interface."""
import io
import re
import threading
import zipfile
from datetime import date, timedelta

import pymupdf
import pytest
from openpyxl import Workbook
from PIL import Image
from pyzbar.pyzbar import decode
from sqlalchemy import text

from app.db import SessionLocal, engine
from app.models import Certificate, Course
from app.services import certificates as svc
from app.services.numbering import next_certificate_no


def _course(client, code="T-FLAG-01", title="Flagman Training", hours="8"):
    client.form("/admin/courses/save", {"code": code, "title": title, "duration_hours": hours})
    with SessionLocal() as db:
        return db.query(Course).filter(Course.code == code).one().id


def _issue(client, name, course_id, day=None, extra=None):
    day = day or (date.today() - timedelta(days=1)).isoformat()
    data = {"new_participant_name": name, "course_id": course_id, "start_date": day, "completion_date": day,
            "action": "issue", **(extra or {})}
    r = client.form("/admin/certificates", data)
    assert r.status_code == 303, r.text[:2000]
    cid = int(r.headers["location"].rsplit("/", 1)[1])
    with SessionLocal() as db:
        return db.get(Certificate, cid)


def _qr_from_pdf(pdf: bytes) -> str:
    page = pymupdf.open(stream=pdf, filetype="pdf")[0]
    pix = page.get_pixmap(dpi=150)
    img = Image.open(io.BytesIO(pix.tobytes("png")))
    codes = decode(img)
    assert len(codes) == 1, "expected exactly one QR code on the certificate"
    return codes[0].data.decode()


# ---------------- auth & security ----------------

def test_admin_requires_login(public):
    r = public.get("/admin", follow_redirects=False)
    assert r.status_code == 303 and "/login" in r.headers["location"]
    assert public.get("/admin/certificates/new", follow_redirects=False).status_code == 303


def test_wrong_password_and_lockout(public):
    page = public.get("/login")
    tok = re.search(r'name="csrf_token" value="([^"]+)"', page.text).group(1)
    for _ in range(5):
        r = public.post("/login", data={"email": "viewer@test.local", "password": "nope", "csrf_token": tok})
        assert r.status_code == 401
    # Correct password is now refused while locked.
    r = public.post("/login", data={"email": "viewer@test.local", "password": "ViewerPass2026!", "csrf_token": tok})
    assert r.status_code == 401 and "locked" in r.text
    from app.models import User
    with SessionLocal() as db:
        u = db.query(User).filter(User.email == "viewer@test.local").one()
        u.locked_until = None
        db.commit()


def test_csrf_required(admin):
    r = admin.post("/admin/courses/save", data={"code": "X-1", "title": "x", "duration_hours": "1"})
    assert r.status_code == 400


def test_roles(admin, viewer, issuer):
    cid = _course(admin, "T-ROLE-01", "Role Test")
    cert = _issue(issuer, "Role Person", cid)
    # viewer cannot create; issuer cannot revoke
    assert viewer.form("/admin/certificates", {"new_participant_name": "X", "course_id": cid}).status_code == 403
    assert issuer.form(f"/admin/certificates/{cert.id}/revoke", {"reason": "testing roles"}).status_code == 403
    assert viewer.get("/admin/users").status_code == 403
    assert issuer.get("/admin/settings").status_code == 403


def test_security_headers(public):
    r = public.get("/verify")
    assert r.headers["x-frame-options"] == "DENY"
    assert "frame-ancestors 'none'" in r.headers["content-security-policy"]
    assert r.headers["referrer-policy"] == "no-referrer"


# ---------------- pages render ----------------

def test_all_admin_pages_render(admin):
    cid = _course(admin, "T-PAGES-01", "Pages Test")
    cert = _issue(admin, "Pages Person", cid)
    for url in ["/admin", "/admin/certificates", "/admin/certificates/new", f"/admin/certificates/{cert.id}",
                "/admin/participants", "/admin/participants/new", f"/admin/participants/{cert.participant_id}",
                "/admin/courses", f"/admin/courses/{cid}", "/admin/instructors", "/admin/batches", "/admin/import",
                "/admin/verifications", "/admin/audit", "/admin/users", "/admin/settings", "/admin/account",
                f"/admin/certificates/{cert.id}/reissue", "/admin/certificates/export.csv",
                "/admin/import/template.csv", "/admin/settings/preview.pdf", "/admin/certificates?q=Pages"]:
        r = admin.get(url)
        assert r.status_code == 200, (url, r.status_code, r.text[:500])


# ---------------- lifecycle ----------------

def test_issue_verify_revoke(admin, public):
    cid = _course(admin, "T-LIFE-01", "Lifecycle Training")
    cert = _issue(admin, "Lifecycle Person", cid)
    assert re.fullmatch(r"PS-\d{4}-\d{6}", cert.certificate_no)
    assert svc.signature_ok(cert)

    pdf = admin.get(f"/admin/certificates/{cert.id}/pdf").content
    assert pdf[:4] == b"%PDF"
    url = _qr_from_pdf(pdf)
    assert url == f"http://testserver/verify/{cert.certificate_no}?k={cert.verify_token}"

    r = public.get(url.replace("http://testserver", ""))
    assert r.status_code == 200 and "VALID" in r.text and "Lifecycle Person" in r.text and "Lifecycle Training" in r.text

    assert admin.form(f"/admin/certificates/{cert.id}/revoke", {"reason": "no"}).status_code == 303  # too short
    with SessionLocal() as db:
        assert db.get(Certificate, cert.id).status == "issued"
    admin.form(f"/admin/certificates/{cert.id}/revoke", {"reason": "Issued in error"})
    r = public.get(url.replace("http://testserver", ""))
    assert "REVOKED" in r.text and "has been revoked by the training center" in r.text


def test_not_found_and_drafts_are_private(admin, public):
    assert public.get("/verify/PS-2099-999999").status_code == 404
    assert "NOT FOUND" in public.get("/verify/PS-2099-999999").text
    assert public.get("/verify/garbage<script>").status_code == 404
    cid = _course(admin, "T-DRAFT-01", "Draft Course")
    d = (date.today() - timedelta(days=1)).isoformat()
    r = admin.form("/admin/certificates", {"new_participant_name": "Draft Person", "course_id": cid,
                                           "start_date": d, "completion_date": d, "action": "draft"})
    did = int(r.headers["location"].rsplit("/", 1)[1])
    with SessionLocal() as db:
        assert db.get(Certificate, did).certificate_no is None
    pdf = admin.get(f"/admin/certificates/{did}/pdf").content
    page = pymupdf.open(stream=pdf, filetype="pdf")[0]
    assert "SAMPLE" in page.get_text() and not decode(Image.open(io.BytesIO(page.get_pixmap(dpi=120).tobytes("png"))))


def test_public_page_hides_private_data_and_masks_without_key(admin, public):
    cid = _course(admin, "T-PRIV-01", "Privacy Course")
    d = (date.today() - timedelta(days=1)).isoformat()
    r = admin.form("/admin/certificates", {"new_participant_name": "Sara Ahmed Private", "new_participant_national_id": "1098765432",
                                           "course_id": cid, "start_date": d, "completion_date": d, "action": "issue"})
    cid_ = int(r.headers["location"].rsplit("/", 1)[1])
    with SessionLocal() as db:
        c = db.get(Certificate, cid_)
    manual = public.get(f"/verify/{c.certificate_no}").text
    assert "VALID" in manual and "Sara A. P." in manual and "Sara Ahmed Private" not in manual
    assert "1098765432" not in manual
    full = public.get(f"/verify/{c.certificate_no}?k={c.verify_token}").text
    assert "Sara Ahmed Private" in full and "1098765432" not in full
    wrong = public.get(f"/verify/{c.certificate_no}?k=wrongkey").text
    assert "Sara Ahmed Private" not in wrong


def test_public_pdf_only_when_allowed(admin, public):
    cid = _course(admin, "T-PDF-01", "PDF Course")
    c = _issue(admin, "Pdf Person", cid)
    assert public.get(f"/verify/{c.certificate_no}/certificate.pdf?k={c.verify_token}").status_code == 404
    admin.form(f"/admin/certificates/{c.id}/public-pdf", {"enabled": "1"})
    assert "View certificate PDF" in public.get(f"/verify/{c.certificate_no}?k={c.verify_token}").text
    r = public.get(f"/verify/{c.certificate_no}/certificate.pdf?k={c.verify_token}")
    assert r.status_code == 200 and r.content[:4] == b"%PDF"
    assert public.get(f"/verify/{c.certificate_no}/certificate.pdf").status_code == 404  # key required


def test_issued_certificate_is_immutable(admin):
    cid = _course(admin, "T-IMM-01", "Immutable Course")
    c = _issue(admin, "Immutable Person", cid)
    # Through the app:
    r = admin.get(f"/admin/certificates/{c.id}/edit", follow_redirects=False)
    assert r.status_code == 303
    # Directly in the database (trigger):
    with engine.begin() as conn:
        with pytest.raises(Exception):
            conn.execute(text("UPDATE certificates SET participant_name='Hacker' WHERE id=:i"), {"i": c.id})
    with engine.begin() as conn:
        with pytest.raises(Exception):
            conn.execute(text("DELETE FROM certificates WHERE id=:i"), {"i": c.id})
    with engine.begin() as conn:
        with pytest.raises(Exception):
            conn.execute(text("DELETE FROM audit_logs"))


def test_tampered_record_never_shows_valid(admin, public):
    cid = _course(admin, "T-TAMP-01", "Tamper Course")
    c = _issue(admin, "Tamper Person", cid)
    from app.db import init_db
    with engine.begin() as conn:  # bypass triggers to simulate direct DB tampering
        if engine.dialect.name == "sqlite":
            conn.exec_driver_sql("DROP TRIGGER trg_cert_immutable")
        else:
            conn.exec_driver_sql("DROP TRIGGER trg_cert_guard ON certificates")
        conn.execute(text("UPDATE certificates SET duration_hours=40 WHERE id=:i"), {"i": c.id})
    init_db()  # reinstall the guards
    r = public.get(f"/verify/{c.certificate_no}?k={c.verify_token}")
    assert "NOT VERIFIED" in r.text and ">VALID<" not in r.text


def test_reissue(admin, public):
    cid = _course(admin, "T-RE-01", "Reissue Course")
    old = _issue(admin, "Mohamd Ali", cid)
    d = old.completion_date.isoformat()
    r = admin.form(f"/admin/certificates/{old.id}/reissue", {
        "participant_id": old.participant_id, "course_id": cid, "start_date": d, "completion_date": d,
        "corrected_name": "Mohamed Ali", "reason": "Name misspelled"})
    assert r.status_code == 303
    new_id = int(r.headers["location"].rsplit("/", 1)[1])
    with SessionLocal() as db:
        new, old2 = db.get(Certificate, new_id), db.get(Certificate, old.id)
    assert new.participant_name == "Mohamed Ali" and new.certificate_no != old.certificate_no
    assert old2.status == "revoked" and old2.replaced_by_id == new.id
    assert "Mohamd Ali" == old2.participant_name  # history preserved
    assert new.certificate_no in public.get(f"/verify/{old.certificate_no}?k={old.verify_token}").text


def test_duplicate_prevention(admin):
    cid = _course(admin, "T-DUP-01", "Dup Course")
    d = (date.today() - timedelta(days=2)).isoformat()
    _issue(admin, "Dup Person", cid, d)
    r = admin.form("/admin/certificates", {"new_participant_name": "dup  person", "course_id": cid, "start_date": d,
                                           "completion_date": d, "action": "issue"})
    assert r.status_code == 400 and "already exists" in r.text


def test_validation(admin):
    cid = _course(admin, "T-VAL-01", "Validation Course")
    future = (date.today() + timedelta(days=3)).isoformat()
    r = admin.form("/admin/certificates", {"new_participant_name": "Future", "course_id": cid, "start_date": future,
                                           "completion_date": future, "action": "issue"})
    assert r.status_code == 400 and "future" in r.text
    r = admin.form("/admin/certificates", {"new_participant_name": "Order", "course_id": cid, "start_date": "2026-01-05",
                                           "completion_date": "2026-01-01", "action": "issue"})
    assert r.status_code == 400 and "after the completion date" in r.text


def test_numbering_is_unique_under_concurrency():
    year = 2031
    results, errors = [], []

    def worker():
        for _ in range(10):
            db = SessionLocal()
            try:
                results.append(next_certificate_no(db, year))
                db.commit()
            except Exception as e:  # pragma: no cover
                errors.append(e)
                db.rollback()
            finally:
                db.close()

    threads = [threading.Thread(target=worker) for _ in range(8)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert not errors
    assert len(results) == 80 and len(set(results)) == 80
    assert sorted(results)[0] == "PS-2031-000001" and sorted(results)[-1] == "PS-2031-000080"


def test_long_name_fits_one_page(admin):
    cid = _course(admin, "T-LONG-01", "Working at Height, Fall Protection Equipment Inspection, Rescue Planning and "
                                      "Scaffolding Safety for Supervisors, Foremen and Competent Persons (Level 2)")
    c = _issue(admin, "Mohammed Abdulrahman Abdullah Saleh Al-Qahtani Al-Harbi Al-Dosari Al-Shammari Al-Mutairi "
                      "Al-Ghamdi Al-Zahrani", cid)
    doc = pymupdf.open(stream=admin.get(f"/admin/certificates/{c.id}/pdf").content, filetype="pdf")
    assert doc.page_count == 1
    page = doc[0]
    w, h = page.rect.width, page.rect.height
    assert round(w / 72 * 25.4) == 210 and round(h / 72 * 25.4) == 297  # A4 portrait
    for b in page.get_text("blocks"):
        assert b[0] >= 0 and b[2] <= w and b[3] <= h


# ---------------- bulk import ----------------

def _csv(rows):
    head = "Participant Name,Course Title,Course Code,Duration,Start Date,Completion Date,Instructor Name\n"
    return (head + "\n".join(rows)).encode()


def test_bulk_import_csv(admin):
    y = (date.today() - timedelta(days=5)).isoformat()
    data = _csv([
        f"Ahmed Bulk,Flagman Training,BULK-001,8 Hours,{y},{y},Trainer One",
        f"Mohamed Bulk,First Aid Training,BULK-002,6 Hours,{y},{y},Trainer One",
        f"Sara Bulk,Fire Fighting Training,BULK-003,8 Hours,{y},{y},Trainer Two",
        f"Sara Bulk,Fire Fighting Training,BULK-003,8 Hours,{y},{y},Trainer Two",   # duplicate in file
        f",Missing Name,BULK-004,8,{y},{y},",                                       # error
        f"Bad Date,Course,BULK-005,8,31/02/2026,{y},",                              # error
        f"Late Start,Course,BULK-006,8,{date.today().isoformat()},{y},",            # start after end
    ])
    r = admin.form("/admin/import", files={"file": ("batch.csv", data, "text/csv")})
    assert r.status_code == 303
    job_url = r.headers["location"]
    page = admin.get(job_url).text
    assert "Ready to issue" in page and "Participant Name is missing" in page and "same as row" in page
    assert admin.get(job_url + "/sample.pdf").content[:4] == b"%PDF"
    r = admin.form(job_url + "/confirm")
    assert r.status_code == 303
    page = admin.get(job_url).text
    assert "3 certificates issued" in page, page[:3000]
    z = zipfile.ZipFile(io.BytesIO(admin.get(job_url + "/zip").content))
    names = z.namelist()
    assert len(names) == 3 and all(n.endswith(".pdf") for n in names)
    for n in names:  # every PDF carries its own working QR
        url = _qr_from_pdf(z.read(n))
        assert re.search(r"/verify/PS-\d{4}-\d{6}\?k=", url)
    # Re-uploading the same file: all rows are now duplicates.
    r = admin.form("/admin/import", files={"file": ("batch.csv", data, "text/csv")})
    page = admin.get(r.headers["location"]).text
    assert "already issued" in page


def test_bulk_import_xlsx(admin):
    wb = Workbook()
    ws = wb.active
    ws.append(["Participant Name", "Course Title", "Course Code", "Duration", "Start Date", "Completion Date", "Instructor Name"])
    d = date.today() - timedelta(days=7)
    ws.append(["Excel Person", "Excel Course", "XL-001", 4, d, d, "Excel Trainer"])
    buf = io.BytesIO()
    wb.save(buf)
    r = admin.form("/admin/import", files={"file": ("p.xlsx", buf.getvalue(),
                                                    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")})
    job_url = r.headers["location"]
    admin.form(job_url + "/confirm")
    assert "1 certificate issued" in admin.get(job_url).text


def test_import_rejects_missing_columns(admin):
    r = admin.form("/admin/import", files={"file": ("x.csv", b"Name,Course\nA,B\n", "text/csv")}, follow_redirects=True)
    assert "Missing required column" in r.text


# ---------------- batches ----------------

def test_batch_issue(admin):
    cid = _course(admin, "T-BATCH-01", "Batch Course")
    d = (date.today() - timedelta(days=3)).isoformat()
    r = admin.form("/admin/batches", {"course_id": cid, "start_date": d, "end_date": d})
    bid = int(r.headers["location"].rsplit("/", 1)[1])
    admin.form(f"/admin/batches/{bid}/add", {"names": "Batch One\nBatch Two\nBatch Three"})
    r = admin.form(f"/admin/batches/{bid}/issue")
    assert r.status_code == 303 and "zip=" in r.headers["location"]
    with SessionLocal() as db:
        assert db.query(Certificate).filter(Certificate.batch_id == bid, Certificate.status == "issued").count() == 3
    z = zipfile.ZipFile(io.BytesIO(admin.get(f"/admin/batches/{bid}/zip").content))
    assert len(z.namelist()) == 3
    # second run issues nothing new
    admin.form(f"/admin/batches/{bid}/issue")
    with SessionLocal() as db:
        assert db.query(Certificate).filter(Certificate.batch_id == bid).count() == 3


def test_verification_and_audit_logged(admin, public):
    cid = _course(admin, "T-LOG-01", "Log Course")
    c = _issue(admin, "Log Person", cid)
    public.get(f"/verify/{c.certificate_no}?k={c.verify_token}")
    assert c.certificate_no in admin.get("/admin/verifications").text
    assert "certificate.issued" in admin.get("/admin/audit").text


# ---------------- expiry ----------------

def test_expiry_from_course_and_expired_status(admin, public):
    admin.form("/admin/courses/save", {"code": "T-EXP-01", "title": "First Aid", "duration_hours": "6", "validity_months": "24"})
    with SessionLocal() as db:
        cid = db.query(Course).filter(Course.code == "T-EXP-01").one().id
    c = _issue(admin, "Expiry Person", cid, "2026-11-10")
    assert c.expiry_date == date(2028, 11, 9)
    page = public.get(f"/verify/{c.certificate_no}?k={c.verify_token}").text
    assert ">VALID<" in page and "9 November 2028" in page
    text_ = pymupdf.open(stream=admin.get(f"/admin/certificates/{c.id}/pdf").content, filetype="pdf")[0].get_text()
    assert "VALID UNTIL" in text_.upper() and "9 November 2028" in text_

    # An explicit, already-passed expiry date -> EXPIRED (still genuine, not revoked)
    old = _issue(admin, "Old Expired Person", cid, "2020-01-10", {"expiry_date": "2022-01-09"})
    page = public.get(f"/verify/{old.certificate_no}?k={old.verify_token}")
    assert "EXPIRED" in page.text and "validity period ended" in page.text
    assert "expired" in admin.get("/admin/certificates?status=expired").text.lower()

    # No-expiry override
    ne = _issue(admin, "No Expiry Person", cid, "2026-11-01", {"no_expiry": "on"})
    assert ne.expiry_date is None


def test_import_expiry_column(admin):
    y = (date.today() - timedelta(days=5)).isoformat()
    head = "Participant Name,Course Title,Course Code,Duration,Start Date,Completion Date,Instructor Name,Expiry Date\n"
    data = (head + f"Imp Exp,Fire Warden,IMP-EXP-1,8,{y},{y},T,2029-01-31\n"
                   f"Imp Bad,Fire Warden,IMP-EXP-1,8,{y},{y},T,{y}\n").encode()
    r = admin.form("/admin/import", files={"file": ("e.csv", data, "text/csv")})
    job_url = r.headers["location"]
    page = admin.get(job_url).text
    assert "Expiry Date must be after the Completion Date" in page
    admin.form(job_url + "/confirm")
    with SessionLocal() as db:
        c = db.query(Certificate).filter(Certificate.participant_name == "Imp Exp").one()
        assert c.expiry_date == date(2029, 1, 31)


# ---------------- accreditations ----------------

def _logo_png():
    buf = io.BytesIO()
    Image.new("RGBA", (600, 200), (20, 40, 80, 255)).save(buf, format="PNG")
    return buf.getvalue()


def test_accreditation_only_on_covered_courses(admin, public):
    covered = _course(admin, "T-ACC-01", "Covered Course")
    other = _course(admin, "T-ACC-02", "Other Course")
    # must confirm the approval is held
    r = admin.post("/admin/accreditations", data={"csrf_token": admin.csrf, "body_name": "Body X",
                   "statement": "Approved Training Provider", "reference_no": "998877", "scope": "selected",
                   "course_ids": [str(covered)]}, files={"logo": ("l.png", _logo_png(), "image/png")}, follow_redirects=True)
    assert "Confirm that the center currently holds" in r.text
    r = admin.post("/admin/accreditations", data={"csrf_token": admin.csrf, "body_name": "Body X",
                   "statement": "Approved Training Provider", "reference_no": "998877", "scope": "selected",
                   "course_ids": [str(covered)], "confirm_held": "on"},
                   files={"logo": ("l.png", _logo_png(), "image/png")}, follow_redirects=True)
    assert "Body X saved" in r.text

    c1 = _issue(admin, "Acc Person", covered)
    c2 = _issue(admin, "No Acc Person", other)
    assert "998877" in c1.accreditations_json and c2.accreditations_json == "[]"
    t1 = " ".join(pymupdf.open(stream=admin.get(f"/admin/certificates/{c1.id}/pdf").content, filetype="pdf")[0].get_text().split())
    t2 = " ".join(pymupdf.open(stream=admin.get(f"/admin/certificates/{c2.id}/pdf").content, filetype="pdf")[0].get_text().split())
    assert "Approved Training Provider" in t1 and "998877" in t1
    assert "Approved Training Provider" not in t2
    assert "998877" in public.get(f"/verify/{c1.certificate_no}?k={c1.verify_token}").text
    # Free hosting wipes the disk on restart: logos must come back from the database.
    from app.config import settings as _s
    for f in _s.uploads_dir.glob("logo_*.png"):
        f.unlink()
    for f in _s.certificates_dir.rglob("*.pdf"):
        f.unlink()
    page = pymupdf.open(stream=admin.get(f"/admin/certificates/{c1.id}/pdf").content, filetype="pdf")[0]
    assert len(page.get_images()) >= 1, "accreditation logo missing after disk wipe"
    # snapshot is protected by the record signature
    assert svc.signature_ok(c1)
    assert admin.get("/admin/accreditations").status_code == 200


def test_setup_page_closed_once_users_exist(public):
    # Users exist in the test database, so the first-run page must be unavailable.
    assert public.get("/setup").status_code == 404
    assert public.post("/setup", data={"email": "x@y.z", "full_name": "x", "password": "Aa1aaaaaaaaaa",
                                       "confirm": "Aa1aaaaaaaaaa"}).status_code in (400, 404)


def test_text_qr_mode(admin):
    from app.services import settings_store
    from app.db import session_scope
    cid = _course(admin, "T-TXT-01", "Text QR Course")
    c = _issue(admin, "Text Qr Person", cid)
    with session_scope() as db:
        settings_store.set_many(db, {"qr_mode": "text"})
    try:
        with SessionLocal() as db:  # regenerate (archived PDF was made in link mode)
            from app.services.pdf import render_pdf
            cert = db.get(Certificate, c.id)
            pdf = render_pdf(cert, settings_store.get_all(db))
        txt = _qr_from_pdf(pdf)
        assert "Name: Text Qr Person" in txt and "Text QR Course" in txt
        assert f"Certificate ID: {c.certificate_no}" in txt
        assert f"Check code: {svc.check_code(c)}" in txt and "http" not in txt
        assert svc.check_code(c) in " ".join(pymupdf.open(stream=pdf, filetype="pdf")[0].get_text().split())
    finally:
        with session_scope() as db:
            settings_store.set_many(db, {"qr_mode": "link"})
