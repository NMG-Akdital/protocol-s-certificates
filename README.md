# Protocol S — Training Certificate Issuance & Online Verification

A web application for **Protocol S for Training and Consultancy** that issues A4 training certificates
with unique IDs and real QR codes. It also provides a public, mobile-friendly verification page that reads
the live database and shows **VALID**, **EXPIRED**, **REVOKED** or **NOT FOUND**.

## What it does

| Area | Features |
|---|---|
| Certificates | Create a draft → preview → issue; automatic IDs `PS-2026-000001`; A4 portrait vector PDF; long names and titles auto-fit; optional expiry ("Valid until"); revoke with a reason; reissue with corrections (old ID revoked and linked to the new one); search by ID, name or course code; CSV register export |
| Bulk | Excel/CSV upload → validation report (missing fields, bad dates, future dates, duplicates in the file and in the database) → sample PDF of the longest name → issue → **ZIP with one PDF per participant** |
| Batches | Register a group on one course run, mark who completed, issue all certificates at once and download a ZIP |
| Catalogue | Participants (private ID, phone, email), courses (code, title, hours, category, validity months), instructors (public-display flag, signature image) |
| Accreditations | Approvals the center holds (logo, wording, approval number, expiry), printed **only** on certificates for the courses they cover |
| Verification | `https://<domain>/verify/PS-2026-000001?k=…` from the QR code, or manual ID entry; no private data shown; full name only via the QR key (anti-harvesting); optional public PDF; rate-limited; every lookup logged |
| Security | Argon2id passwords, account lockout, roles (admin / issuer / viewer), CSRF on every form, signed session cookies, strict security headers, append-only audit log, records signed with HMAC, database triggers that block changes to issued certificates |

## Quick start (local trial)

```bash
python -m pip install -r requirements-dev.txt      # Linux/macOS; on Windows use WSL or Docker
python scripts/create_admin.py you@example.com "Your Name"
python scripts/seed_courses.py                     # optional: the 11 HSE courses from the brief (review hours!)
uvicorn app.main:app --reload
```

Open <http://localhost:8000/admin>. In development, QR codes point to `http://localhost:8000`, so they
work only on this computer. **Real certificates must be issued from the deployed system** (see
`docs/DEPLOYMENT.md`), because the QR code permanently contains the domain.

WeasyPrint needs Pango on the system (`apt install libpango-1.0-0 libpangoft2-1.0-0` on Debian/Ubuntu,
`brew install pango` on macOS).

## Tests

```bash
python -m pytest                                   # 25 end-to-end tests (SQLite)
TEST_DATABASE_URL=postgresql://user:pw@host/db python -m pytest   # same tests on PostgreSQL
python scripts/acceptance_test.py --spawn          # the 11-step acceptance test from the brief
```

## Documentation

* `docs/ARCHITECTURE.md`: components, lifecycle, verification and privacy design
* `docs/DEPLOYMENT.md`: going live on your domain, backups and updates
* `docs/DATABASE.md`: tables and columns
* `docs/TEST_PROCEDURE.md`: how to prove each QR code opens the right record (automated and by phone)

## Project layout

```
app/
  main.py            FastAPI app, middleware, error pages
  config.py          environment settings (no secrets in code)
  db.py, models.py   database, migrations, integrity triggers
  security.py        login, sessions, CSRF, roles, rate limits, headers
  routes/            public.py (verify), auth.py, admin.py, imports.py
  services/          certificates, numbering, pdf, qr, importer, audit, settings
  templates/         admin pages, public pages, pdf/certificate.html (the certificate design)
  static/            css, js, fonts (IBM Plex, OFL), brand/logo-mark.svg
scripts/             create_admin, seed_courses, make_sample, acceptance_test
tests/               end-to-end test suite
```

## Branding

The logo used is **Concept A** from the brand identity work, which is not yet approved. To change it, replace
`app/static/brand/logo-mark.svg` and set the brand text in **Admin → Settings**. The certificate design is
`app/templates/pdf/certificate.html`. An Arabic certificate version can be added later; the IBM Plex Sans
Arabic font is already bundled.
