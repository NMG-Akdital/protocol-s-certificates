"""Application configuration, read from environment variables.

No secret is hardcoded. In production (APP_ENV=production) the app refuses to
start unless SECRET_KEY, CERT_SIGNING_KEY and PUBLIC_BASE_URL are set.
"""
from __future__ import annotations

import os
import secrets
import sys
from dataclasses import dataclass
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
APP_DIR = BASE_DIR / "app"


def _load_dotenv(path: Path) -> None:
    """Minimal .env loader (no extra dependency). Real env vars win."""
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


_load_dotenv(BASE_DIR / ".env")


def _bool(name: str, default: bool) -> bool:
    v = os.environ.get(name)
    if v is None:
        return default
    return v.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    app_env: str
    database_url: str
    secret_key: str
    cert_signing_key: str
    public_base_url: str
    cert_prefix: str
    data_dir: Path
    session_cookie_secure: bool
    session_max_age: int
    verify_rate_limit_per_minute: int
    login_rate_limit_per_minute: int
    max_upload_mb: int
    max_import_rows: int
    trust_proxy_headers: bool
    setup_token: str

    @property
    def is_production(self) -> bool:
        return self.app_env == "production"

    @property
    def certificates_dir(self) -> Path:
        return self.data_dir / "certificates"

    @property
    def uploads_dir(self) -> Path:
        return self.data_dir / "uploads"

    @property
    def exports_dir(self) -> Path:
        return self.data_dir / "exports"


def load_settings() -> Settings:
    app_env = os.environ.get("APP_ENV", "development").strip().lower()
    data_dir = Path(os.environ.get("DATA_DIR", str(BASE_DIR / "data"))).resolve()
    data_dir.mkdir(parents=True, exist_ok=True)

    secret_key = os.environ.get("SECRET_KEY", "")
    signing_key = os.environ.get("CERT_SIGNING_KEY", "")
    # On Render, RENDER_EXTERNAL_URL (https://<service>.onrender.com) is used until a custom domain is set.
    base_url = (os.environ.get("PUBLIC_BASE_URL") or os.environ.get("RENDER_EXTERNAL_URL", "")).rstrip("/")

    if app_env == "production":
        missing = [n for n, v in (("SECRET_KEY", secret_key), ("CERT_SIGNING_KEY", signing_key),
                                  ("PUBLIC_BASE_URL", base_url)) if not v]
        if missing:
            sys.exit(f"FATAL: missing required environment variables: {', '.join(missing)}")
        if not base_url.startswith("https://"):
            sys.exit("FATAL: PUBLIC_BASE_URL must start with https:// in production")
        if len(secret_key) < 32 or len(signing_key) < 32:
            sys.exit("FATAL: SECRET_KEY and CERT_SIGNING_KEY must be at least 32 characters")
    else:
        # Development: persist generated keys so certificates stay verifiable across restarts.
        keyfile = data_dir / ".dev-keys"
        if not (secret_key and signing_key):
            if keyfile.exists():
                saved = dict(l.split("=", 1) for l in keyfile.read_text().splitlines() if "=" in l)
            else:
                saved = {"SECRET_KEY": secrets.token_urlsafe(48), "CERT_SIGNING_KEY": secrets.token_urlsafe(48)}
                keyfile.write_text("\n".join(f"{k}={v}" for k, v in saved.items()))
            secret_key = secret_key or saved["SECRET_KEY"]
            signing_key = signing_key or saved["CERT_SIGNING_KEY"]
        base_url = base_url or "http://localhost:8000"

    return Settings(
        app_env=app_env,
        database_url=os.environ.get("DATABASE_URL", f"sqlite:///{data_dir / 'protocol_s.db'}"),
        secret_key=secret_key,
        cert_signing_key=signing_key,
        public_base_url=base_url,
        cert_prefix=os.environ.get("CERT_PREFIX", "PS").strip().upper() or "PS",
        data_dir=data_dir,
        session_cookie_secure=_bool("SESSION_COOKIE_SECURE", app_env == "production"),
        session_max_age=int(os.environ.get("SESSION_MAX_AGE_SECONDS", str(8 * 3600))),
        verify_rate_limit_per_minute=int(os.environ.get("VERIFY_RATE_LIMIT_PER_MINUTE", "30")),
        login_rate_limit_per_minute=int(os.environ.get("LOGIN_RATE_LIMIT_PER_MINUTE", "10")),
        max_upload_mb=int(os.environ.get("MAX_UPLOAD_MB", "5")),
        max_import_rows=int(os.environ.get("MAX_IMPORT_ROWS", "2000")),
        trust_proxy_headers=_bool("TRUST_PROXY_HEADERS", app_env == "production"),
        setup_token=os.environ.get("SETUP_TOKEN", ""),
    )


settings = load_settings()
for d in (settings.certificates_dir, settings.uploads_dir, settings.exports_dir):
    d.mkdir(parents=True, exist_ok=True)
