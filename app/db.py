from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator

from sqlalchemy import create_engine, event
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from .config import settings


class Base(DeclarativeBase):
    pass


_is_sqlite = settings.database_url.startswith("sqlite")
_url = settings.database_url
if _url.startswith("postgres://"):  # Heroku/Render style URLs
    _url = "postgresql+psycopg://" + _url[len("postgres://"):]
elif _url.startswith("postgresql://"):
    _url = "postgresql+psycopg://" + _url[len("postgresql://"):]

engine = create_engine(
    _url,
    future=True,
    pool_pre_ping=True,
    connect_args={"check_same_thread": False, "timeout": 30} if _is_sqlite else {},
)

if _is_sqlite:
    @event.listens_for(engine, "connect")
    def _sqlite_pragmas(dbapi_conn, _):  # pragma: no cover - trivial
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA foreign_keys=ON")
        cur.execute("PRAGMA busy_timeout=30000")
        cur.close()

SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, future=True)


def get_db() -> Iterator[Session]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


@contextmanager
def session_scope() -> Iterator[Session]:
    db = SessionLocal()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def init_db() -> None:
    from . import models  # noqa: F401  (register tables)
    Base.metadata.create_all(engine)
    _add_missing_columns()
    if _is_sqlite:
        _install_sqlite_guards()
    elif engine.dialect.name == "postgresql":
        _install_postgres_guards()


# Columns added after the first release. create_all() does not alter existing
# tables, so they are added here (works on SQLite and PostgreSQL).
_LATER_COLUMNS = [
    ("courses", "validity_months", "INTEGER"),
    ("certificates", "expiry_date", "DATE"),
    ("certificates", "accreditations_json", "TEXT DEFAULT '[]'"),
]


def _add_missing_columns() -> None:
    from sqlalchemy import inspect
    insp = inspect(engine)
    with engine.begin() as conn:
        for table, col, ddl in _LATER_COLUMNS:
            if col not in {c["name"] for c in insp.get_columns(table)}:
                conn.exec_driver_sql(f"ALTER TABLE {table} ADD COLUMN {col} {ddl}")


def _install_sqlite_guards() -> None:
    """Database-level protection: issued certificate data can never be changed,
    even by someone with direct SQL access through the app's connection.
    (PostgreSQL equivalents are in docs/DATABASE.md.)"""
    ddl = """
    CREATE TRIGGER trg_cert_immutable
    BEFORE UPDATE ON certificates
    WHEN OLD.status IN ('issued','revoked') AND (
        NEW.certificate_no IS NOT OLD.certificate_no OR
        NEW.participant_name IS NOT OLD.participant_name OR
        NEW.course_title IS NOT OLD.course_title OR
        NEW.course_code IS NOT OLD.course_code OR
        NEW.duration_hours IS NOT OLD.duration_hours OR
        NEW.start_date IS NOT OLD.start_date OR
        NEW.completion_date IS NOT OLD.completion_date OR
        NEW.issue_date IS NOT OLD.issue_date OR
        NEW.instructor_name IS NOT OLD.instructor_name OR
        NEW.expiry_date IS NOT OLD.expiry_date OR
        NEW.accreditations_json IS NOT OLD.accreditations_json OR
        NEW.signature IS NOT OLD.signature OR
        NEW.verify_token IS NOT OLD.verify_token
    )
    BEGIN SELECT RAISE(ABORT, 'issued certificate data is immutable'); END;
    """
    ddl2 = """
    CREATE TRIGGER IF NOT EXISTS trg_cert_no_unrevoke
    BEFORE UPDATE OF status ON certificates
    WHEN OLD.status = 'revoked' AND NEW.status != 'revoked'
    BEGIN SELECT RAISE(ABORT, 'revoked certificates cannot be reinstated'); END;
    """
    ddl3 = """
    CREATE TRIGGER IF NOT EXISTS trg_cert_no_delete
    BEFORE DELETE ON certificates
    WHEN OLD.status IN ('issued','revoked')
    BEGIN SELECT RAISE(ABORT, 'issued certificates cannot be deleted'); END;
    """
    ddl4 = """
    CREATE TRIGGER IF NOT EXISTS trg_audit_append_only_u
    BEFORE UPDATE ON audit_logs BEGIN SELECT RAISE(ABORT, 'audit log is append-only'); END;
    """
    ddl5 = """
    CREATE TRIGGER IF NOT EXISTS trg_audit_append_only_d
    BEFORE DELETE ON audit_logs BEGIN SELECT RAISE(ABORT, 'audit log is append-only'); END;
    """
    with engine.begin() as conn:
        conn.exec_driver_sql("DROP TRIGGER IF EXISTS trg_cert_immutable")  # recreate with current column list
        for stmt in (ddl, ddl2, ddl3, ddl4, ddl5):
            conn.exec_driver_sql(stmt)


_PROTECTED = ["certificate_no", "participant_name", "course_title", "course_code", "duration_hours", "start_date",
              "completion_date", "issue_date", "instructor_name", "expiry_date", "accreditations_json", "signature",
              "verify_token"]


def _install_postgres_guards() -> None:
    changed = " OR ".join(f"NEW.{c} IS DISTINCT FROM OLD.{c}" for c in _PROTECTED)
    statements = [
        f"""
        CREATE OR REPLACE FUNCTION ps_cert_guard() RETURNS trigger AS $$
        BEGIN
          IF TG_OP = 'DELETE' THEN
            IF OLD.status IN ('issued', 'revoked') THEN
              RAISE EXCEPTION 'issued certificates cannot be deleted';
            END IF;
            RETURN OLD;
          END IF;
          IF OLD.status IN ('issued', 'revoked') AND ({changed}) THEN
            RAISE EXCEPTION 'issued certificate data is immutable';
          END IF;
          IF OLD.status = 'revoked' AND NEW.status <> 'revoked' THEN
            RAISE EXCEPTION 'revoked certificates cannot be reinstated';
          END IF;
          RETURN NEW;
        END $$ LANGUAGE plpgsql;
        """,
        "DROP TRIGGER IF EXISTS trg_cert_guard ON certificates",
        "CREATE TRIGGER trg_cert_guard BEFORE UPDATE OR DELETE ON certificates FOR EACH ROW EXECUTE FUNCTION ps_cert_guard()",
        """
        CREATE OR REPLACE FUNCTION ps_audit_guard() RETURNS trigger AS $$
        BEGIN RAISE EXCEPTION 'audit log is append-only'; END $$ LANGUAGE plpgsql;
        """,
        "DROP TRIGGER IF EXISTS trg_audit_guard ON audit_logs",
        "CREATE TRIGGER trg_audit_guard BEFORE UPDATE OR DELETE ON audit_logs FOR EACH ROW EXECUTE FUNCTION ps_audit_guard()",
    ]
    with engine.begin() as conn:
        for stmt in statements:
            conn.exec_driver_sql(stmt)
