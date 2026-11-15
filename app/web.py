"""Template rendering helpers shared by all routes."""
from __future__ import annotations

from datetime import date, datetime
from typing import Any

from fastapi import Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from .config import APP_DIR, settings
from .security import ROLE_RANK, csrf_token
from .services.certificates import format_date, format_range, hours_display

templates = Jinja2Templates(directory=str(APP_DIR / "templates"))


def _dt(value: Any) -> str:
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d %H:%M UTC")
    if isinstance(value, date):
        return value.isoformat()
    return value or ""


templates.env.filters["dt"] = _dt
templates.env.filters["longdate"] = format_date
templates.env.filters["hours"] = hours_display
templates.env.globals["daterange"] = format_range
templates.env.globals["settings"] = settings


def flash(request: Request, message: str, kind: str = "ok") -> None:
    msgs = request.session.get("flash", [])
    msgs.append({"m": message, "k": kind})
    request.session["flash"] = msgs[-5:]


def render(request: Request, name: str, ctx: dict[str, Any] | None = None, status_code: int = 200,
           user=None) -> HTMLResponse:
    ctx = dict(ctx or {})
    ctx.setdefault("user", user)
    ctx["request"] = request
    ctx.setdefault("today", date.today())
    ctx["csrf"] = csrf_token(request) if user is not None or name.startswith(("admin/", "auth")) else ""
    ctx["flashes"] = request.session.pop("flash", []) if hasattr(request, "session") else []
    ctx["can"] = lambda role: bool(user) and ROLE_RANK.get(user.role, 0) >= ROLE_RANK[role]
    return templates.TemplateResponse(request, name, ctx, status_code=status_code)


def redirect(url: str) -> RedirectResponse:
    return RedirectResponse(url, status_code=303)
