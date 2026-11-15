from __future__ import annotations

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from sqlalchemy.orm import Session

from ..config import settings
from ..db import get_db
from ..models import User
from ..security import (LoginError, authenticate, client_ip, current_user_optional, end_session, hash_password,
                        limiter, password_problems, require_viewer, start_session, verify_csrf, verify_password)
from ..services import audit
from ..web import flash, redirect, render

router = APIRouter()


# ---------- first-run setup (only while no user exists) ----------

def _setup_open(db: Session) -> bool:
    return db.query(User).count() == 0 and (bool(settings.setup_token) or not settings.is_production)


@router.get("/setup")
def setup_form(request: Request, db: Session = Depends(get_db)):
    if not _setup_open(db):
        raise HTTPException(status_code=404)
    return render(request, "auth_setup.html", {"need_token": bool(settings.setup_token)})


@router.post("/setup", dependencies=[Depends(verify_csrf)])
def setup(request: Request, token: str = Form(""), email: str = Form(...), full_name: str = Form(...),
          password: str = Form(...), confirm: str = Form(...), db: Session = Depends(get_db)):
    import hmac, re
    if not _setup_open(db):
        raise HTTPException(status_code=404)
    ip = client_ip(request)
    if not limiter.allow(f"setup:{ip}", 10):
        raise HTTPException(status_code=429, detail="Too many attempts. Wait a minute.")
    errors = []
    if settings.setup_token and not hmac.compare_digest(token.strip(), settings.setup_token):
        errors.append("The setup token is not correct. Copy SETUP_TOKEN from the hosting dashboard (Environment).")
    email = email.strip().lower()
    if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
        errors.append("Enter a valid email address.")
    if not full_name.strip():
        errors.append("Enter your name.")
    if password != confirm:
        errors.append("The passwords do not match.")
    problems = password_problems(password)
    if problems:
        errors.append("Password needs " + ", ".join(problems) + ".")
    if errors:
        return render(request, "auth_setup.html", {"need_token": bool(settings.setup_token), "errors": errors,
                                                   "email": email, "full_name": full_name}, status_code=400)
    u = User(email=email, full_name=full_name.strip()[:200], role="admin", password_hash=hash_password(password))
    db.add(u)
    db.flush()
    audit.record(db, u, "user.admin_created_setup", "user", email, None, ip)
    db.commit()
    start_session(request, u)
    flash(request, "Administrator account created. The setup page is now closed for good.")
    return redirect("/admin")


def _safe_next(nxt: str) -> str:
    return nxt if nxt.startswith("/admin") and "//" not in nxt else "/admin"


@router.get("/login")
def login_form(request: Request, next: str = "/admin", user: User | None = Depends(current_user_optional),
               db: Session = Depends(get_db)):
    if user:
        return redirect(_safe_next(next))
    if _setup_open(db):
        return redirect("/setup")
    return render(request, "auth_login.html", {"next": _safe_next(next)})


@router.post("/login", dependencies=[Depends(verify_csrf)])
def login(request: Request, email: str = Form(...), password: str = Form(...), next: str = Form("/admin"),
          db: Session = Depends(get_db)):
    ip = client_ip(request)
    if not limiter.allow(f"login:{ip}", settings.login_rate_limit_per_minute):
        raise HTTPException(status_code=429, detail="Too many login attempts. Wait a minute and try again.")
    try:
        user = authenticate(db, email, password)
    except LoginError as e:
        audit.record(db, None, "auth.login_failed", "user", email.strip().lower()[:80], None, ip)
        db.commit()
        return render(request, "auth_login.html", {"error": str(e), "email": email, "next": _safe_next(next)},
                      status_code=401)
    start_session(request, user)
    audit.record(db, user, "auth.login", "user", user.email, None, ip)
    db.commit()
    return redirect(_safe_next(next))


@router.post("/logout", dependencies=[Depends(verify_csrf)])
def logout(request: Request, user: User | None = Depends(current_user_optional), db: Session = Depends(get_db)):
    if user:
        audit.record(db, user, "auth.logout", "user", user.email, None, client_ip(request))
        db.commit()
    end_session(request)
    return redirect("/login")


@router.get("/admin/account")
def account(request: Request, user: User = Depends(require_viewer)):
    return render(request, "admin/account.html", {}, user=user)


@router.post("/admin/account/password", dependencies=[Depends(verify_csrf)])
def change_password(request: Request, current: str = Form(...), new: str = Form(...), confirm: str = Form(...),
                    user: User = Depends(require_viewer), db: Session = Depends(get_db)):
    u = db.get(User, user.id)
    errors = []
    if not verify_password(u.password_hash, current):
        errors.append("Current password is incorrect.")
    if new != confirm:
        errors.append("The new passwords do not match.")
    problems = password_problems(new)
    if problems:
        errors.append("New password needs " + ", ".join(problems) + ".")
    if errors:
        return render(request, "admin/account.html", {"errors": errors}, user=user, status_code=400)
    u.password_hash = hash_password(new)
    u.session_version += 1  # signs out other sessions
    audit.record(db, u, "user.password_changed", "user", u.email, None, client_ip(request))
    db.commit()
    start_session(request, u)
    flash(request, "Password changed. Other sessions have been signed out.")
    return redirect("/admin/account")
