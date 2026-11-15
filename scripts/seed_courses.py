"""Optional: add the course catalogue from the project brief.

Durations are starting values only. Review them in Admin > Courses before
issuing, because each certificate prints the duration.
Existing course codes are left untouched.
"""
import sys
from decimal import Decimal
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app.db import init_db, session_scope
from app.models import Course

COURSES = [
    ("PS-HSE-001", "Flagman / Banksman Training", 8),
    ("PS-HSE-002", "First Aid Training", 6),
    ("PS-HSE-003", "Fire Fighting Training", 8),
    ("PS-HSE-004", "Fire Warden Training", 8),
    ("PS-HSE-005", "Work at Height Safety", 8),
    ("PS-HSE-006", "Scaffolding Safety", 8),
    ("PS-HSE-007", "Confined Space Safety", 8),
    ("PS-HSE-008", "Permit to Work (PTW)", 8),
    ("PS-HSE-009", "HSE Induction", 4),
    ("PS-HSE-010", "Manual Handling", 4),
    ("PS-HSE-011", "Lifting Operations Safety", 8),
]
init_db()
with session_scope() as db:
    added = 0
    for code, title, hours in COURSES:
        if not db.query(Course).filter(Course.code == code).first():
            db.add(Course(code=code, title=title, duration_hours=Decimal(hours)))
            added += 1
print(f"Added {added} course(s). Review durations in Admin > Courses.")
