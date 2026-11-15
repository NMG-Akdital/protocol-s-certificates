from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal

from sqlalchemy import (Boolean, Column, Date, DateTime, ForeignKey, Integer, LargeBinary, Numeric, String, Table,
                        Text, UniqueConstraint, Index)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    full_name: Mapped[str] = mapped_column(String(200))
    password_hash: Mapped[str] = mapped_column(String(255))
    role: Mapped[str] = mapped_column(String(20), default="issuer")  # admin | issuer | viewer
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    session_version: Mapped[int] = mapped_column(Integer, default=1)
    failed_logins: Mapped[int] = mapped_column(Integer, default=0)
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Course(Base):
    __tablename__ = "courses"
    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(40), unique=True, index=True)
    title: Mapped[str] = mapped_column(String(200))
    category: Mapped[str] = mapped_column(String(100), default="HSE & Occupational Safety")
    duration_hours: Mapped[Decimal] = mapped_column(Numeric(6, 1))
    validity_months: Mapped[int | None] = mapped_column(Integer, nullable=True)  # None = no expiry
    description: Mapped[str] = mapped_column(Text, default="")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


course_accreditations = Table(
    "course_accreditations", Base.metadata,
    Column("course_id", ForeignKey("courses.id", ondelete="CASCADE"), primary_key=True),
    Column("accreditation_id", ForeignKey("accreditations.id", ondelete="CASCADE"), primary_key=True),
)


class Accreditation(Base):
    """An approval / accreditation the center actually holds (entered by an admin).
    Its logo is printed only on certificates for the courses it covers."""
    __tablename__ = "accreditations"
    id: Mapped[int] = mapped_column(primary_key=True)
    body_name: Mapped[str] = mapped_column(String(120))               # e.g. IOSH
    statement: Mapped[str] = mapped_column(String(200))               # e.g. Approved Training Provider
    reference_no: Mapped[str] = mapped_column(String(80), default="")  # approval / provider number
    logo_file: Mapped[str] = mapped_column(String(255))
    applies_to_all: Mapped[bool] = mapped_column(Boolean, default=False)
    valid_until: Mapped[date | None] = mapped_column(Date, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    courses: Mapped[list[Course]] = relationship(secondary=course_accreditations)


class Instructor(Base):
    __tablename__ = "instructors"
    id: Mapped[int] = mapped_column(primary_key=True)
    full_name: Mapped[str] = mapped_column(String(200), index=True)
    title: Mapped[str] = mapped_column(String(200), default="Instructor / Trainer")
    show_publicly: Mapped[bool] = mapped_column(Boolean, default=True)
    signature_file: Mapped[str | None] = mapped_column(String(255), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Participant(Base):
    """Personal data. Only full_name is ever shown publicly (and only on a
    certificate's verification page). national_id / phone / email are private."""
    __tablename__ = "participants"
    id: Mapped[int] = mapped_column(primary_key=True)
    full_name: Mapped[str] = mapped_column(String(200), index=True)
    national_id: Mapped[str | None] = mapped_column(String(60), nullable=True, index=True)
    company: Mapped[str | None] = mapped_column(String(200), nullable=True)
    email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    phone: Mapped[str | None] = mapped_column(String(40), nullable=True)
    notes: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Batch(Base):
    __tablename__ = "batches"
    id: Mapped[int] = mapped_column(primary_key=True)
    reference: Mapped[str] = mapped_column(String(60), unique=True, index=True)
    course_id: Mapped[int] = mapped_column(ForeignKey("courses.id"))
    instructor_id: Mapped[int | None] = mapped_column(ForeignKey("instructors.id"), nullable=True)
    start_date: Mapped[date] = mapped_column(Date)
    end_date: Mapped[date] = mapped_column(Date)
    location: Mapped[str] = mapped_column(String(200), default="")
    notes: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    course: Mapped[Course] = relationship()
    instructor: Mapped[Instructor | None] = relationship()
    enrollments: Mapped[list["Enrollment"]] = relationship(back_populates="batch", cascade="all, delete-orphan")


class Enrollment(Base):
    __tablename__ = "enrollments"
    __table_args__ = (UniqueConstraint("batch_id", "participant_id", name="uq_enrollment"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    batch_id: Mapped[int] = mapped_column(ForeignKey("batches.id"))
    participant_id: Mapped[int] = mapped_column(ForeignKey("participants.id"))
    completed: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    batch: Mapped[Batch] = relationship(back_populates="enrollments")
    participant: Mapped[Participant] = relationship()


class IdCounter(Base):
    """One row per (prefix, year). Incremented atomically; values are never reused."""
    __tablename__ = "id_counters"
    scope: Mapped[str] = mapped_column(String(40), primary_key=True)  # e.g. "PS-2026"
    last_value: Mapped[int] = mapped_column(Integer, default=0)


class Certificate(Base):
    __tablename__ = "certificates"
    __table_args__ = (
        Index("ix_cert_dupe", "participant_name_key", "course_code", "completion_date"),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    certificate_no: Mapped[str | None] = mapped_column(String(40), unique=True, index=True, nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="draft", index=True)  # draft|issued|revoked

    participant_id: Mapped[int] = mapped_column(ForeignKey("participants.id"))
    course_id: Mapped[int] = mapped_column(ForeignKey("courses.id"))
    batch_id: Mapped[int | None] = mapped_column(ForeignKey("batches.id"), nullable=True)
    instructor_id: Mapped[int | None] = mapped_column(ForeignKey("instructors.id"), nullable=True)

    # Snapshot printed on the certificate (frozen at issuance).
    participant_name: Mapped[str] = mapped_column(String(200))
    participant_name_key: Mapped[str] = mapped_column(String(200), index=True)  # normalised, for search/dupes
    course_title: Mapped[str] = mapped_column(String(200))
    course_code: Mapped[str] = mapped_column(String(40))
    duration_hours: Mapped[Decimal] = mapped_column(Numeric(6, 1))
    start_date: Mapped[date] = mapped_column(Date)
    completion_date: Mapped[date] = mapped_column(Date)
    issue_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    expiry_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    accreditations_json: Mapped[str] = mapped_column(Text, default="[]")  # snapshot at issuance
    instructor_name: Mapped[str] = mapped_column(String(200), default="")
    show_instructor_public: Mapped[bool] = mapped_column(Boolean, default=True)
    allow_public_pdf: Mapped[bool] = mapped_column(Boolean, default=False)

    verify_token: Mapped[str | None] = mapped_column(String(64), nullable=True)
    signature: Mapped[str | None] = mapped_column(String(128), nullable=True)  # HMAC-SHA256 of snapshot
    pdf_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)

    created_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    issued_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    issued_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revocation_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    replaces_id: Mapped[int | None] = mapped_column(ForeignKey("certificates.id"), nullable=True)
    replaced_by_id: Mapped[int | None] = mapped_column(ForeignKey("certificates.id"), nullable=True)
    import_job_id: Mapped[int | None] = mapped_column(ForeignKey("import_jobs.id"), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    participant: Mapped[Participant] = relationship()
    course: Mapped[Course] = relationship()
    batch: Mapped[Batch | None] = relationship()
    instructor: Mapped[Instructor | None] = relationship()


class ImportJob(Base):
    __tablename__ = "import_jobs"
    id: Mapped[int] = mapped_column(primary_key=True)
    filename: Mapped[str] = mapped_column(String(255))
    status: Mapped[str] = mapped_column(String(20), default="validated")  # validated|processing|completed|failed|cancelled
    rows_json: Mapped[str] = mapped_column(Text)        # normalised rows + per-row validation
    summary_json: Mapped[str] = mapped_column(Text, default="{}")
    total: Mapped[int] = mapped_column(Integer, default=0)
    progress: Mapped[int] = mapped_column(Integer, default=0)
    zip_file: Mapped[str | None] = mapped_column(String(255), nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class AuditLog(Base):
    """Append-only (enforced by database triggers on SQLite)."""
    __tablename__ = "audit_logs"
    id: Mapped[int] = mapped_column(primary_key=True)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    user_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    user_email: Mapped[str] = mapped_column(String(255), default="system")
    action: Mapped[str] = mapped_column(String(60), index=True)
    entity: Mapped[str] = mapped_column(String(40), default="")
    entity_ref: Mapped[str] = mapped_column(String(80), default="")
    details: Mapped[str] = mapped_column(Text, default="")
    ip: Mapped[str] = mapped_column(String(64), default="")


class VerificationLog(Base):
    __tablename__ = "verification_logs"
    id: Mapped[int] = mapped_column(primary_key=True)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    query: Mapped[str] = mapped_column(String(80), index=True)
    result: Mapped[str] = mapped_column(String(20))  # VALID | REVOKED | NOT_FOUND | INVALID
    via: Mapped[str] = mapped_column(String(20))     # qr | manual
    ip_hash: Mapped[str] = mapped_column(String(64), default="")
    user_agent: Mapped[str] = mapped_column(String(200), default="")


class Setting(Base):
    __tablename__ = "settings"
    key: Mapped[str] = mapped_column(String(80), primary_key=True)
    value: Mapped[str] = mapped_column(Text, default="")


class StoredFile(Base):
    """Uploaded images (signatures, accreditation logos) kept in the database, so they
    survive on hosts whose disk is wiped on restart. The disk copy is only a cache."""
    __tablename__ = "stored_files"
    name: Mapped[str] = mapped_column(String(80), primary_key=True)
    data: Mapped[bytes] = mapped_column(LargeBinary)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
