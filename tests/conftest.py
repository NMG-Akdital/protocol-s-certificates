import os
import re
import sys
import tempfile
from pathlib import Path

import pytest

_tmp = tempfile.mkdtemp(prefix="ps-test-")
os.environ["DATA_DIR"] = _tmp
os.environ["DATABASE_URL"] = os.environ.get("TEST_DATABASE_URL") or f"sqlite:///{_tmp}/test.db"
os.environ["APP_ENV"] = "development"
os.environ["QR_MODE"] = "link"
os.environ["PUBLIC_BASE_URL"] = "http://testserver"
os.environ["VERIFY_RATE_LIMIT_PER_MINUTE"] = "10000"
os.environ["LOGIN_RATE_LIMIT_PER_MINUTE"] = "10000"
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient  # noqa: E402

from app.db import init_db, session_scope  # noqa: E402
from app.main import app  # noqa: E402
from app.models import User  # noqa: E402
from app.security import hash_password  # noqa: E402

PASSWORDS = {"admin": "AdminPass2026!", "issuer": "IssuerPass2026!", "viewer": "ViewerPass2026!"}


@pytest.fixture(scope="session", autouse=True)
def _db():
    init_db()
    with session_scope() as db:
        for role, pw in PASSWORDS.items():
            if not db.query(User).filter(User.email == f"{role}@test.local").first():
                db.add(User(email=f"{role}@test.local", full_name=role.title(), role=role, password_hash=hash_password(pw)))
    yield


def csrf_from(html: str) -> str:
    m = re.search(r'name="csrf_token" value="([^"]+)"', html)
    assert m, "csrf token not found in page"
    return m.group(1)


class Client(TestClient):
    csrf: str = ""

    def form(self, url, data=None, files=None, **kw):
        data = dict(data or {})
        data["csrf_token"] = self.csrf
        return self.post(url, data=data, files=files, follow_redirects=kw.pop("follow_redirects", False), **kw)


def login(role: str) -> Client:
    c = Client(app)
    page = c.get("/login")
    r = c.post("/login", data={"email": f"{role}@test.local", "password": PASSWORDS[role],
                               "csrf_token": csrf_from(page.text), "next": "/admin"}, follow_redirects=False)
    assert r.status_code == 303, r.text
    c.csrf = csrf_from(c.get("/admin").text)
    return c


@pytest.fixture()
def admin():
    return login("admin")


@pytest.fixture()
def issuer():
    return login("issuer")


@pytest.fixture()
def viewer():
    return login("viewer")


@pytest.fixture()
def public():
    return TestClient(app)
