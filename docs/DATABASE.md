# Database structure

Created automatically at startup (`init_db`). Works on SQLite and PostgreSQL. Columns added in later
versions are added to existing databases automatically.

## certificates (the register)

| Column | Notes |
|---|---|
| id | internal key |
| certificate_no | `PS-YYYY-NNNNNN`, **UNIQUE**, assigned at issuance, never reused |
| status | `draft` / `issued` / `revoked` (EXPIRED is derived from `expiry_date`) |
| participant_id, course_id, batch_id, instructor_id | links |
| participant_name, course_title, course_code, duration_hours, start_date, completion_date, issue_date, expiry_date, instructor_name | **snapshot printed on the certificate**, frozen at issuance |
| accreditations_json | approvals printed on this certificate (snapshot at issuance) |
| participant_name_key | normalised name for search and duplicate detection |
| show_instructor_public, allow_public_pdf | public-page options |
| verify_token | random key in the QR link (shows the full name, enables the PDF) |
| signature | HMAC-SHA256 of the snapshot (key: `CERT_SIGNING_KEY`); a mismatch is never shown as VALID |
| pdf_sha256 | hash of the archived PDF file |
| created_by, issued_by, issued_at, revoked_by, revoked_at, revocation_reason | accountability |
| replaces_id, replaced_by_id | reissue chain |
| import_job_id | bulk import that created it |

**Database triggers** (SQLite: `trg_cert_immutable`, `trg_cert_no_unrevoke`, `trg_cert_no_delete`;
PostgreSQL: `trg_cert_guard`) reject any change to the snapshot, number, key or signature of an issued or
revoked certificate. They also block un-revoking and deleting issued certificates.

## Other tables

| Table | Purpose |
|---|---|
| id_counters | `scope` (`PS-2026`) → `last_value`; incremented atomically per issuance |
| participants | full_name; **private:** national_id, company, email, phone, notes |
| courses | code (unique), title, category, duration_hours, validity_months, is_active |
| instructors | full_name, title, show_publicly, signature_file |
| accreditations, course_accreditations | approvals held: body, wording, number, logo, validity, covered courses |
| batches, enrollments | training runs and their participants (completed flag) |
| import_jobs | uploaded file, per-row validation, progress, ZIP |
| users | email, Argon2id password hash, role, lockout, session_version |
| audit_logs | append-only (triggers block UPDATE and DELETE): who did what, when, from which IP |
| verification_logs | each public lookup: ID, result, QR or manual, hashed IP |
| settings | center name, signatory, public-page options |
