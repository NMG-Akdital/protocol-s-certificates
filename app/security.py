"""Authentication, sessions, CSRF, roles, rate limiting and security headers."""
from __future__ import annotations

import hashlib
import hmac
import secrets
import threading
import time
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
from fastapi import Depends, HTTPException, Request
from sqlalchemy.orm import Session
from starlette.middleware.base import BaseHTTPMiddleware

from .config import settings
from .db import get_db
from .models import User

_ph = PasswordHasher()
ROLES = ("admin", "issuer", "viewer")
ROLE_RANK = {"viewer": 1, "issuer": 2, "admin": 3}
MAX_FAILED = 5
LOCK_MINUTES = 15


# ---------- passwords ----------

def hash_password(pw: str) -> str:
    return _ph.hash(pw)


def verify_password(hash_: str, pw: str) -> bool:
    try:
        return _ph.verify(hash_, pw)
    except (VerificationError, InvalidHashError):
        return False


def password_problems(pw: str) -> list[str]:
    problems = []
    if len(pw) < 12:
        problems.append("at least 12 characters")
    if pw.lower() == pw or pw.upper() == pw:
        problems.append("both upper- and lower-case letters")
    if not any(ch.isdigit() for ch in pw):
        problems.append("at least one number")
    return problems


# ---------- login ----------

class LoginError(Exception):
    pass


def authenticate(db: Session, email: str, password: str) -> User:
    user = db.query(User).filter(User.email == email.strip().lower()).first()
    now = datetime.now(timezone.utc)
    if not user or not user.is_active:
        verify_password(_DUMMY_HASH, password)  # equalise timing
        raise LoginError("Invalid email or password")
    locked = user.locked_until
    if locked and locked.tzinfo is None:
        locked = locked.replace(tzinfo=timezone.utc)
    if locked and locked > now:
        raise LoginError("Account temporarily locked after repeated failed attempts. Try again later.")
    if not verify_password(user.password_hash, password):
        user.failed_logins = (user.failed_logins or 0) + 1
        if user.failed_logins >= MAX_FAILED:
            user.locked_until = now + timedelta(minutes=LOCK_MINUTES)
            user.failed_logins = 0
        db.commit()
        raise LoginError("Invalid email or password")
    if _ph.check_needs_rehash(user.password_hash):
        user.password_hash = hash_password(password)
    user.failed_logins = 0
    user.locked_until = None
    user.last_login_at = now
    db.commit()
    return user


_DUMMY_HASH = _ph.hash(secrets.token_urlsafe(16))


def start_session(request: Request, user: User) -> None:
    request.session.clear()
    request.session.update({"uid": user.id, "sv": user.session_version,
                            "csrf": secrets.token_urlsafe(32), "t": int(time.time())})


def end_session(request: Request) -> None:
    request.session.clear()


# ---------- current user / roles ----------

class NotAuthenticated(Exception):
    pass


def current_user_optional(request: Request, db: Session = Depends(get_db)) -> User | None:
    uid = request.session.get("uid")
    if not uid:
        return None
    if int(time.time()) - int(request.session.get("t", 0)) > settings.session_max_age:
        request.session.clear()
        return None
    user = db.get(User, uid)
    if not user or not user.is_active or user.session_version != request.session.get("sv"):
        request.session.clear()
        return None
    return user


def require_role(min_role: str):
    def dep(user: User | None = Depends(current_user_optional)) -> User:
        if user is None:
            raise NotAuthenticated()
        if ROLE_RANK.get(user.role, 0) < ROLE_RANK[min_role]:
            raise HTTPException(status_code=403, detail="You do not have permission for this action.")
        return user
    return dep


require_viewer = require_role("viewer")
require_issuer = require_role("issuer")
require_admin = require_role("admin")


# ---------- CSRF ----------

def csrf_token(request: Request) -> str:
    tok = request.session.get("csrf")
    if not tok:
        tok = secrets.token_urlsafe(32)
        request.session["csrf"] = tok
    return tok


async def verify_csrf(request: Request) -> None:
    if request.method in ("GET", "HEAD", "OPTIONS"):
        return
    form = await request.form()
    sent = form.get("csrf_token") or request.headers.get("x-csrf-token", "")
    expected = request.session.get("csrf", "")
    if not expected or not sent or not hmac.compare_digest(str(sent), expected):
        raise HTTPException(status_code=400, detail="Your session form expired. Go back, reload the page and try again.")


# ---------- rate limiting (per process) ----------

class RateLimiter:
    def __init__(self) -> None:
        self._hits: dict[str, deque] = defaultdict(deque)
        self._lock = threading.Lock()

    def allow(self, key: str, limit: int, window: float = 60.0) -> bool:
        now = time.monotonic()
        with self._lock:
            q = self._hits[key]
            while q and now - q[0] > window:
                q.popleft()
            if len(q) >= limit:
                return False
            q.append(now)
            if len(self._hits) > 50_000:  # memory guard
                self._hits.clear()
            return True


limiter = RateLimiter()


def client_ip(request: Request) -> str:
    return request.client.host if request.client else ""


def ip_hash(ip: str) -> str:
    return hmac.new(settings.secret_key.encode(), ip.encode(), hashlib.sha256).hexdigest()[:32]


# ---------- headers ----------

class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        response = await call_next(request)
        h = response.headers
        h.setdefault("X-Content-Type-Options", "nosniff")
        h.setdefault("X-Frame-Options", "DENY")
        h.setdefault("Referrer-Policy", "no-referrer")
        h.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
        h.setdefault("Content-Security-Policy",
                     "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; "
                     "script-src 'self'; font-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
        if settings.is_production:
            h.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
        if request.url.path.startswith(("/admin", "/login")):
            h.setdefault("Cache-Control", "no-store")
        return response
