# Protocol S — Certificate Issuance & Verification System: Architecture

## Stack

| Layer | Choice | Why |
|---|---|---|
| Web app | **Python 3.12+ / FastAPI**, server-rendered Jinja2 pages | One deployable service, no front-end build step, fast and well supported |
| Database | **SQLite** (single server) or **PostgreSQL** (managed hosting) via SQLAlchemy 2 | Same code on both; set `DATABASE_URL` |
| PDF | **WeasyPrint** (HTML/CSS → PDF) with embedded IBM Plex fonts | Vector text, print-sharp at any size, A4 portrait |
| QR | **segno**, vector SVG, error correction Q (~25%) | Encodes the real verification URL; survives creases and marks |
| Import | **openpyxl** (.xlsx) and the Python csv module | Excel and CSV without Office installed |
| Passwords | **Argon2id** (argon2-cffi) | Current best practice for password hashing |
| Deploy | **Docker** image, behind HTTPS (Render, Railway, Fly.io or any VPS + Caddy/Nginx) | Portable; persistent volume for DB and PDFs |

## Components

```
                    ┌─────────────────────────── FastAPI app ───────────────────────────┐
Public (phone) ───▶ │ /verify, /verify/{ID}?k=…   read-only, rate-limited, no private data │
                    │ /verify/{ID}/certificate.pdf (only if admin allowed it)            │
                    │                                                                     │
Staff (browser) ──▶ │ /login  → signed session cookie, CSRF on every form, lockout        │
                    │ /admin  → dashboard, certificates, participants, courses,           │
                    │           instructors, batches, bulk import, verifications,         │
                    │           audit log, users (admin), settings (admin)                │
                    └──────────────┬───────────────────────────┬──────────────────────────┘
                                   │                           │
                         services/ (business rules)     data/ (persistent volume)
                         certificates · numbering ·     protocol_s.db (SQLite) or PostgreSQL
                         pdf · qr · importer · audit    certificates/YYYY/PS-YYYY-NNNNNN.pdf
                                                        exports/ (bulk ZIPs) · uploads/ (signatures)
```

## Certificate lifecycle

`draft` → **issue** → `issued` → **revoke** (reason required) → `revoked`

* **Draft:** fully editable. Previews print "SAMPLE · NOT ISSUED" and have no QR code.
* **Issue:** allocates the next number (`PS-2026-000001`) from an atomic per-year counter, sets the
  issue date and a random 16-character verification key, and signs the record (HMAC-SHA256).
* **Issued data is immutable.** The app refuses edits, and on SQLite database triggers reject
  any UPDATE or DELETE of the printed fields. Corrections use **Reissue**: the old certificate is
  revoked with "Reissued as PS-…" and a new number is issued.
* **Revoked** certificates can't be reinstated, and numbers are never reused.

## Verification design

* The QR code encodes `https://<your-domain>/verify/PS-2026-000001?k=<key>`.
* The page reads the record from the database every time, so revoking takes effect immediately
  for printed and downloaded copies alike.
* Statuses: **VALID** (issued, signature intact, not past its "valid until" date), **EXPIRED** (genuine but
  past its expiry date), **REVOKED**, **NOT FOUND** (no record, or a draft).
  If a record fails its signature check, the page shows an "integrity check failed" warning and
  never shows VALID.
* **Privacy:** sequential numbers are easy to guess, so someone could type IDs one after another to
  collect trainee names. The full name is therefore shown only when the request carries the QR key.
  Typing an ID manually still gives the status, course and dates, with the name shortened to
  "Ahmed M.". Admins can change this in Settings. National ID, phone, email and company are
  never shown publicly.
* Rate limit per IP (default 30 lookups per minute). Every lookup is logged with a hashed IP.

## Expiry and accreditations

* Each course can have a validity period in months; "Valid until" = completion date + period − 1 day.
  It can be overridden per certificate or per import row, or set to "does not expire".
* Accreditations are approvals the center actually holds, entered by an admin with the official logo, the
  approval number and the courses covered. They're snapshotted into the certificate at issuance, signed
  with the record, printed above the footer, and listed on the verification page. A logo stops printing
  automatically after the approval's own expiry date.

## Roles

| Role | Can |
|---|---|
| admin | everything, including users, settings, revoke and reissue |
| issuer | manage participants, courses, instructors and batches; create, issue and bulk-import certificates |
| viewer | read-only dashboard, search and downloads |

## Data model (summary; full detail in DATABASE.md)

`users`, `courses`, `instructors`, `participants` (private fields), `batches`, `enrollments`,
`certificates` (snapshot of printed data + status + signature), `id_counters`, `import_jobs`,
`audit_logs` (append-only), `verification_logs`, `settings`.
