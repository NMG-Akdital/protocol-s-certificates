# Test procedure: proving the QR code opens the correct record

## A. Automated acceptance test (brief, section 12)

```bash
python scripts/acceptance_test.py --spawn                      # fresh local instance
python scripts/acceptance_test.py --base-url https://verify.yourdomain.com   # live system
#   (set ACCEPT_EMAIL / ACCEPT_PASSWORD for an admin test account)
```

| # | Step | How it is checked |
|---|---|---|
| 1 | Create participant "Ahmed Mohamed" | admin form over HTTP |
| 2 | Assign to Flagman Training (PS-HSE-001) | detail page shows course and code |
| 3 | Generate certificate ID | `PS-<year>-000001` on a fresh database |
| 4 | Generate PDF | 1 page, A4 210×297 mm, contains the name and ID |
| 5 | QR from the actual verification URL | the PDF is **rendered to an image and the QR is decoded** (zbar); it must equal `<base>/verify/<ID>?k=…` |
| 6 | Open the QR URL | the decoded URL is requested exactly as a phone would |
| 7 | Correct details | participant, course and ID appear on the page |
| 8 | Status VALID | the VALID banner is shown |
| 9 | Revoke from the admin dashboard | revoke form with a reason |
| 10 | Open the same URL again | same decoded URL |
| 11 | Status REVOKED | the REVOKED banner is shown, and VALID is not |

Evidence goes to `acceptance-evidence/`: the PDF, a PNG render, the decoded QR text, phone-size screenshots
of the VALID / REVOKED / NOT FOUND pages, and `REPORT.md`.

## B. Manual check with a phone (after deployment)

1. Issue a test certificate, then download and print the PDF (or show it on a screen).
2. Scan the QR code with the phone camera. The address shown must start with your domain and `/verify/`.
3. Check: green **VALID**, with the right name, course, dates and certificate ID.
4. On the admin page, revoke it with the reason "Test".
5. Scan the same printed QR code again. It must show red **REVOKED**.
6. Type the ID at `https://<domain>/verify`. You should see the status with the name shortened (privacy).
7. Type a wrong ID such as `PS-2026-999999`. It must show **NOT FOUND**.

## C. Full automated suite

`python -m pytest`: 25 tests covering login and lockout, CSRF, roles, all admin pages, issue → QR decode →
verify → revoke, drafts not public, private-data masking, public PDF permission, database-level
immutability, tamper detection, reissue, duplicate prevention, date validation, 80 concurrent ID
allocations without duplicates, long-name layout on one A4 page, CSV and Excel bulk import with ZIP and a
QR check on every PDF, batches, expiry (VALID → EXPIRED), import expiry column, accreditation logos only on
covered courses, and logging. They pass on SQLite and on PostgreSQL 16.
