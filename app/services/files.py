"""Uploaded images: stored in the database, cached on disk."""
from __future__ import annotations

import re
from pathlib import Path

from ..config import settings
from ..db import SessionLocal
from ..models import StoredFile

_NAME = re.compile(r"(sig|logo)_[0-9a-f]{16}\.png")


def store(name: str, data: bytes) -> None:
    (settings.uploads_dir / name).write_bytes(data)
    with SessionLocal() as db:
        db.merge(StoredFile(name=name, data=data))
        db.commit()


def local_path(name: str | None) -> Path | None:
    """Disk path of an uploaded image, restoring it from the database if the disk was wiped."""
    if not name or not _NAME.fullmatch(Path(name).name):
        return None
    path = settings.uploads_dir / Path(name).name
    if not path.exists():
        with SessionLocal() as db:
            row = db.get(StoredFile, path.name)
            if row is None:
                return None
            path.write_bytes(row.data)
    return path
