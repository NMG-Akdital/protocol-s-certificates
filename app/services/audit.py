from __future__ import annotations

import json
from typing import Any

from sqlalchemy.orm import Session

from ..models import AuditLog, User


def record(db: Session, user: User | None, action: str, entity: str = "", ref: str = "",
           details: dict[str, Any] | str | None = None, ip: str = "") -> None:
    if isinstance(details, dict):
        details = json.dumps(details, default=str, ensure_ascii=False)
    db.add(AuditLog(
        user_id=user.id if user else None,
        user_email=user.email if user else "system",
        action=action, entity=entity, entity_ref=str(ref or ""),
        details=details or "", ip=ip or "",
    ))
