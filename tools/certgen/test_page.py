"""Drive the generator page in real Chrome: fill, issue, download every PDF, decode the QR codes."""
import io
import sys
from pathlib import Path

import pymupdf
from PIL import Image
from playwright.sync_api import sync_playwright
from pyzbar.pyzbar import decode

PAGE = Path(__file__).parent / "dist/index.html"
OUT = Path("/tmp/certgen-out")
OUT.mkdir(exist_ok=True)
NAME = sys.argv[1] if len(sys.argv) > 1 else "Ahmed Mohamed Ali"
COURSE_IDX = sys.argv[2] if len(sys.argv) > 2 else "0"

with sync_playwright() as p:
    b = p.chromium.launch(executable_path="/usr/bin/google-chrome", args=["--no-sandbox"])
    ctx = b.new_context(accept_downloads=True, viewport={"width": 1400, "height": 1000})
    page = ctx.new_page()
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
    page.goto(PAGE.as_uri())
    page.wait_for_timeout(800)
    # settings
    page.click("[data-tab=set]")
    page.fill("#s_sig", "Eng. Mohamed Hassan")
    page.fill("#s_sigt", "General Manager")
    page.fill("#s_iosh_n", "1234567")
    page.fill("#s_osha_n", "OA-2026-0099")
    page.check("#s_acc_def")
    page.click("#sf button[type=submit]")
    page.wait_for_timeout(300)
    page.click("[data-tab=new]")
    page.fill("#name", NAME)
    page.fill("#nid", "2456789012")
    page.fill("#nat", "Egyptian")
    page.select_option("#course", COURSE_IDX)
    page.fill("#sd", "2026-11-10")
    page.fill("#cd", "2026-11-11")
    page.fill("#ins", "Eng. Khaled Mansour")
    page.wait_for_timeout(600)
    page.click("#issueBtn")
    page.wait_for_function("document.getElementById('prevTitle').textContent.startsWith('PS-')", timeout=15000)
    print("issued:", page.inner_text("#prevTitle"))
    for d in ["classic", "executive", "heritage", "modern", "card"]:
        page.click(f"[data-design={d}]")
        page.wait_for_timeout(500)
        page.locator("#stage").screenshot(path=str(OUT / f"preview-{d}.png"))
        with page.expect_download(timeout=60000) as dl:
            page.click("#dlOne")
        path = OUT / dl.value.suggested_filename
        dl.value.save_as(path)
        doc = pymupdf.open(path)
        pg = doc[0]
        res = {}
        for i, pgx in enumerate(doc):
            im = Image.open(io.BytesIO(pgx.get_pixmap(dpi=300 if d == "card" else 200).tobytes("png")))
            q = decode(im)
            res[i] = q[0].data.decode() if q else None
        mm = (round(pg.rect.width / 72 * 25.4, 1), round(pg.rect.height / 72 * 25.4, 1))
        print(d, path.name, doc.page_count, "pages", mm, "| QR on pages:", {k: bool(v) for k, v in res.items()})
        if d == "classic":
            print("---- QR text ----\n" + (res[0] or "NONE") + "\n-----------------")
    # all-in-one
    with page.expect_download(timeout=90000) as d1:
        page.click("#dlAll")
    d1.value.save_as(OUT / d1.value.suggested_filename)
    page.wait_for_timeout(4000)
    print("dlAll first file:", d1.value.suggested_filename, pymupdf.open(OUT / d1.value.suggested_filename).page_count, "pages")
    page.click("[data-tab=reg]")
    page.wait_for_timeout(500)
    print("register rows:", page.locator("#regBody tr").count(), page.inner_text("#regBody tr >> nth=0")[:120].replace("\t", " | "))
    print("JS errors:", errors)
    b.close()
