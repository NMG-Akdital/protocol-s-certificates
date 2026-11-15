"""Acceptance test (project brief, section 12) — runs the real flow over HTTP.

Two ways to run it:

  1) Fresh local instance (proves certificate ID PS-<year>-000001):
       python scripts/acceptance_test.py --spawn

  2) Against your deployed system (use a test admin account):
       ACCEPT_EMAIL=admin@... ACCEPT_PASSWORD=... \
       python scripts/acceptance_test.py --base-url https://verify.example.com

Steps: create participant "Ahmed Mohamed" -> assign "Flagman Training" -> issue certificate ->
generate PDF -> decode the QR code from the rendered PDF image -> open that exact URL ->
check participant/course/VALID -> revoke from the admin dashboard -> open the same URL ->
check REVOKED. Evidence (PDF, QR text, page screenshots, report) goes to --out.
"""
from __future__ import annotations

import argparse
import io
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from datetime import date, datetime
from pathlib import Path

import httpx
import pymupdf
from PIL import Image
from pyzbar.pyzbar import decode

ROOT = Path(__file__).resolve().parent.parent
PARTICIPANT, COURSE_TITLE, COURSE_CODE = "Ahmed Mohamed", "Flagman Training", "PS-HSE-001"


class Fail(Exception):
    pass


def step(log: list, n: int, text: str, ok: bool = True, detail: str = ""):
    line = f"{'PASS' if ok else 'FAIL'}  Step {n:>2}: {text}" + (f"\n          {detail}" if detail else "")
    print(line)
    log.append(line)
    if not ok:
        raise Fail(text)


def csrf(html: str) -> str:
    m = re.search(r'name="csrf_token" value="([^"]+)"', html)
    if not m:
        raise Fail("no CSRF token on page")
    return m.group(1)


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def screenshot(url: str, out: Path) -> bool:
    chrome = shutil.which("google-chrome") or shutil.which("chromium") or shutil.which("chromium-browser")
    if not chrome:
        return False
    subprocess.run([chrome, "--headless=new", "--no-sandbox", "--disable-gpu", "--hide-scrollbars",
                    "--window-size=390,1100", "--force-device-scale-factor=2", f"--screenshot={out}", url],
                   capture_output=True, timeout=60)
    return out.exists()


def run(base: str, email: str, password: str, out: Path, expect_first_id: bool) -> list[str]:
    log: list[str] = []
    out.mkdir(parents=True, exist_ok=True)
    c = httpx.Client(base_url=base, follow_redirects=False, timeout=60)

    # sign in
    page = c.get("/login")
    r = c.post("/login", data={"email": email, "password": password, "csrf_token": csrf(page.text), "next": "/admin"})
    if r.status_code != 303:
        raise Fail(f"admin login failed (HTTP {r.status_code})")
    tok = csrf(c.get("/admin").text)

    # course (create if missing)
    courses = c.get("/admin/courses").text
    if f'<td class="mono">{COURSE_CODE}</td>' not in courses:
        c.post("/admin/courses/save", data={"csrf_token": tok, "code": COURSE_CODE, "title": COURSE_TITLE,
                                            "duration_hours": "8"})
    form = c.get("/admin/certificates/new").text
    m = re.search(r'<option value="(\d+)"[^>]*>' + re.escape(COURSE_CODE) + r" · ", form)
    if not m:
        raise Fail("course PS-HSE-001 not available")
    course_id = m.group(1)

    # 1-3: participant, course, issue
    today = date.today().isoformat()
    r = c.post("/admin/certificates", data={
        "csrf_token": tok, "new_participant_name": PARTICIPANT, "course_id": course_id, "course_title": COURSE_TITLE,
        "start_date": today, "completion_date": today, "action": "issue", "confirm_duplicate": "on"})
    step(log, 1, f'Create sample participant "{PARTICIPANT}"', r.status_code == 303, f"HTTP {r.status_code}")
    detail_url = r.headers["location"]
    detail = c.get(detail_url).text
    step(log, 2, f'Assign to "{COURSE_TITLE}" ({COURSE_CODE})', COURSE_TITLE in detail and COURSE_CODE in detail)
    cert_no = re.search(r"PS-\d{4}-\d{6}", detail).group(0)
    expected = f"PS-{date.today().year}-000001"
    ok3 = re.fullmatch(r"PS-\d{4}-\d{6}", cert_no) is not None and (cert_no == expected or not expect_first_id)
    step(log, 3, f"Generate certificate ID {cert_no}", ok3, f"expected {expected}" if expect_first_id else "")

    # 4: PDF
    pdf = c.get(detail_url + "/pdf?download=1").content
    (out / f"{cert_no}.pdf").write_bytes(pdf)
    doc = pymupdf.open(stream=pdf, filetype="pdf")
    pg = doc[0]
    a4 = round(pg.rect.width / 72 * 25.4), round(pg.rect.height / 72 * 25.4)
    text = pg.get_text()
    step(log, 4, "Generate the PDF certificate", pdf[:4] == b"%PDF" and doc.page_count == 1 and PARTICIPANT in text
         and cert_no in text, f"{len(pdf):,} bytes, {doc.page_count} page, {a4[0]}×{a4[1]} mm")

    # 5: QR from rendered PDF image
    png = pg.get_pixmap(dpi=200).tobytes("png")
    (out / f"{cert_no}.png").write_bytes(png)
    codes = decode(Image.open(io.BytesIO(png)))
    qr_url = codes[0].data.decode() if len(codes) == 1 else ""
    (out / "qr-content.txt").write_text(qr_url + "\n")
    step(log, 5, "QR code encodes the actual verification URL",
         qr_url.startswith(f"{base.rstrip('/')}/verify/{cert_no}?k="), qr_url)

    # 6-8: open the QR URL
    pub = httpx.Client(follow_redirects=True, timeout=60)
    v = pub.get(qr_url)
    step(log, 6, "Scan / open the QR URL", v.status_code == 200, f"HTTP {v.status_code}")
    step(log, 7, "Correct participant and course appear", PARTICIPANT in v.text and COURSE_TITLE in v.text
         and cert_no in v.text)
    step(log, 8, "Status is VALID", 'class="st">VALID<' in v.text)
    screenshot(qr_url, out / "verify-VALID.png")

    # 9: revoke via admin dashboard
    cid = detail_url.rsplit("/", 1)[1]
    r = c.post(f"/admin/certificates/{cid}/revoke", data={"csrf_token": tok, "reason": "Acceptance test — revocation check"})
    detail = c.get(detail_url).text
    step(log, 9, "Revoke the certificate from the admin dashboard", r.status_code == 303 and "b-revoked" in detail)

    # 10-11: same URL again
    v2 = pub.get(qr_url)
    step(log, 10, "Open the same QR URL again", v2.status_code == 200, f"HTTP {v2.status_code}")
    step(log, 11, "Status changes to REVOKED", 'class="st">REVOKED<' in v2.text and 'class="st">VALID<' not in v2.text)
    screenshot(qr_url, out / "verify-REVOKED.png")

    # extra: unknown ID
    v3 = pub.get(f"{base.rstrip('/')}/verify/PS-1999-999999")
    nf = v3.status_code == 404 and "NOT FOUND" in v3.text
    log.append(f"{'PASS' if nf else 'FAIL'}  Extra  : Unknown ID shows NOT FOUND")
    print(log[-1])
    screenshot(f"{base.rstrip('/')}/verify/PS-1999-999999", out / "verify-NOT_FOUND.png")
    return log


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-url")
    ap.add_argument("--spawn", action="store_true", help="start a fresh local server with an empty database")
    ap.add_argument("--out", default=str(ROOT / "acceptance-evidence"))
    a = ap.parse_args()
    out = Path(a.out)
    proc = None
    if a.spawn:
        tmp = tempfile.mkdtemp(prefix="ps-accept-")
        port = free_port()
        base = f"http://127.0.0.1:{port}"
        env = dict(os.environ, DATA_DIR=tmp, DATABASE_URL=f"sqlite:///{tmp}/accept.db", APP_ENV="development",
                   PUBLIC_BASE_URL=base, ADMIN_PASSWORD="Acceptance2026!x", QR_MODE="link")
        subprocess.run([sys.executable, str(ROOT / "scripts/create_admin.py"), "acceptance@local.test", "Acceptance Admin"],
                       env=env, check=True, capture_output=True)
        proc = subprocess.Popen([sys.executable, "-m", "uvicorn", "app.main:app", "--port", str(port)], cwd=ROOT, env=env,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(60):
            try:
                if httpx.get(base + "/healthz").status_code == 200:
                    break
            except httpx.HTTPError:
                time.sleep(0.25)
        email, password = "acceptance@local.test", "Acceptance2026!x"
    else:
        if not a.base_url:
            sys.exit("Use --spawn or --base-url")
        base = a.base_url.rstrip("/")
        email, password = os.environ.get("ACCEPT_EMAIL", ""), os.environ.get("ACCEPT_PASSWORD", "")
    try:
        log = run(base, email, password, out, expect_first_id=a.spawn)
        result = "PASSED"
    except Fail as e:
        log = [f"FAILED: {e}"]
        result = "FAILED"
    finally:
        if proc:
            proc.terminate()
    report = (f"# Acceptance test — {result}\n\nRun: {datetime.now().isoformat(timespec='seconds')}\nServer: {base}\n\n```\n"
              + "\n".join(log) + "\n```\n")
    (out / "REPORT.md").write_text(report)
    print(f"\nACCEPTANCE TEST {result}. Evidence in {out}")
    sys.exit(0 if result == "PASSED" else 1)


if __name__ == "__main__":
    main()
