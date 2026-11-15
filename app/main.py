from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import HTTPException as FastAPIHTTPException
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.sessions import SessionMiddleware

from .config import APP_DIR, settings
from .db import init_db
from .routes import admin, auth, imports, public
from .security import NotAuthenticated, SecurityHeadersMiddleware
from .web import redirect, render

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

@asynccontextmanager
async def lifespan(_app):
    init_db()  # creates tables, adds new columns, installs integrity triggers
    yield


app = FastAPI(title="Protocol S Certificates", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
app.add_middleware(SecurityHeadersMiddleware)
app.add_middleware(
    SessionMiddleware,
    secret_key=settings.secret_key,
    session_cookie="ps_session",
    max_age=settings.session_max_age,
    same_site="lax",
    https_only=settings.session_cookie_secure,
)
app.mount("/static", StaticFiles(directory=str(APP_DIR / "static")), name="static")

app.include_router(public.router)
app.include_router(auth.router)
app.include_router(admin.router)
app.include_router(imports.router)


@app.exception_handler(NotAuthenticated)
async def _not_auth(request: Request, exc: NotAuthenticated):
    return redirect(f"/login?next={request.url.path}")


@app.exception_handler(StarletteHTTPException)
async def _http_error(request: Request, exc: StarletteHTTPException):
    titles = {403: "Not allowed", 404: "Page not found", 400: "Request not accepted", 429: "Too many requests"}
    return render(request, "error.html", {"code": exc.status_code,
                                          "title": titles.get(exc.status_code, "Something went wrong"),
                                          "detail": exc.detail if exc.status_code != 404 else ""},
                  status_code=exc.status_code)
