# Deployment

## 0. Decide the verification address first

Every QR code permanently contains `PUBLIC_BASE_URL`, for example `https://verify.protocol-s.com`.
Choose a (sub)domain you'll keep for as long as the certificates are valid, and **don't issue real
certificates until the system runs on it with HTTPS**. Keep the domain registered and renewed.

## Option A: a VPS with Docker (recommended)

Any Linux server with 1 vCPU and 1–2 GB RAM is enough (Hetzner, DigitalOcean, AWS Lightsail, or a local
Saudi provider). Docker Compose runs the app and Caddy, which obtains and renews the HTTPS certificate
automatically.

```bash
# on the server
sudo apt update && sudo apt install -y docker.io docker-compose-plugin git
git clone <your-repo> protocol-s && cd protocol-s        # or upload the project folder
cp .env.example .env
nano .env        # set PUBLIC_BASE_URL, DOMAIN, SECRET_KEY, CERT_SIGNING_KEY
# generate keys: python3 -c "import secrets; print(secrets.token_urlsafe(48))"
```

1. In your DNS, create an **A record**: `verify` → the server's IP address.
2. Open ports 80 and 443 on the server firewall.
3. Start the stack and create the first administrator:

```bash
docker compose up -d --build
docker compose exec app python scripts/create_admin.py you@yourdomain.com "Your Name"
docker compose exec app python scripts/seed_courses.py     # optional
```

4. Open `https://verify.yourdomain.com/admin`, sign in, and then:
   * **Settings:** center name, authorized signatory, signature image. Use **Preview sample certificate** to check.
   * **Courses:** check each course's hours and validity (months).
   * **Accreditations:** add only approvals you hold, using the official logo and approval number, and select the courses they cover.
   * **Users:** create accounts for staff (issuer or viewer role).
5. Run the acceptance test against the live site with a test admin account:

```bash
docker compose exec -e ACCEPT_EMAIL=... -e ACCEPT_PASSWORD=... app \
  python scripts/acceptance_test.py --base-url https://verify.yourdomain.com --out /data/acceptance
```

Then scan the QR code in `data/acceptance/PS-….png` with a phone (see TEST_PROCEDURE.md).
The test certificate is revoked by the test itself, so it can stay in the register as proof.

> The Dockerfile could not be built in the environment where this system was developed, because Docker Hub
> was blocked there. The application itself was tested directly on SQLite and PostgreSQL. Run the
> acceptance test after your first deployment, as shown above.

## Option B0: free (Render free web service + Neon free PostgreSQL)

`render.yaml` is set up for this. All data lives in PostgreSQL (`DATABASE_URL`), including uploaded
signature images and logos, so nothing is lost when the free server's disk is wiped. PDFs are regenerated
from the signed records. Limitation: the free service sleeps after 15 minutes idle, so the first QR scan
afterwards takes about 30–50 seconds. To remove that, switch the service to a paid instance type.

1. Create a free Neon project (neon.tech) and copy its connection string.
2. In Render: New → Blueprint → this repository → paste the connection string into `DATABASE_URL`.
3. Open `https://<service>.onrender.com/setup`, enter `SETUP_TOKEN` (Render → service → Environment),
   and create the admin account.

## Option B: a managed platform (Render, Railway, Fly.io)

* Deploy from the Dockerfile.
* Add a **persistent disk/volume** mounted at `/data` (SQLite, archived PDFs, uploads). Without it, data is
  lost on every deploy. Alternatively, use a managed **PostgreSQL** and set `DATABASE_URL`. PDFs are
  regenerated automatically from the signed records if the disk is empty, but signature and logo uploads
  still need the volume.
* Set the environment variables from `.env.example`, and attach your custom domain with HTTPS.
* Run `python scripts/create_admin.py …` once from the platform's shell.

## Environment variables

| Variable | Required | Notes |
|---|---|---|
| `APP_ENV` | yes | `production` enables secure cookies, HSTS and the checks below |
| `PUBLIC_BASE_URL` | yes | `https://…`, used inside QR codes |
| `SECRET_KEY` | yes | ≥ 32 characters, signs login sessions |
| `CERT_SIGNING_KEY` | yes | ≥ 32 characters, signs certificate records. **Never change it after go-live** |
| `DATABASE_URL` | no | default SQLite in `/data`; PostgreSQL supported |
| `CERT_PREFIX` | no | default `PS` |
| `VERIFY_RATE_LIMIT_PER_MINUTE` | no | default 30 per IP |

The app refuses to start in production if a required value is missing or the URL is not HTTPS.

## Backups (essential)

The `data/` folder **is** the certificate register. Back it up daily and keep copies off the server:

```bash
# SQLite: consistent online copy + archived PDFs and uploads
sqlite3 data/protocol_s.db ".backup data/backup-$(date +%F).db"
tar czf protocol-s-$(date +%F).tgz data/backup-$(date +%F).db data/certificates data/uploads
```

Also keep a copy of `.env`. Without `CERT_SIGNING_KEY`, restored records can't pass their integrity check.

## Updating

```bash
git pull && docker compose up -d --build
```

New database columns are added automatically at startup, and existing certificates are untouched.

## Security notes

* Only staff should know `/admin`. Use strong passwords, and give accounts the lowest role they need.
* The rate limiter is per process. For heavy public traffic, add Cloudflare or rate limiting in Caddy.
* Issued certificates can't be edited or deleted, even through direct SQL (database triggers on SQLite
  and PostgreSQL). Corrections go through **Reissue**, which keeps a full audit trail.
