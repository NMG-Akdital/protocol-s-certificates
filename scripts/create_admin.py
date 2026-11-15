"""Create (or reset) an administrator account.

Usage:
  python scripts/create_admin.py admin@example.com "Full Name"
The password is read interactively (or from ADMIN_PASSWORD for automated deploys).
"""
import getpass, os, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.db import init_db, session_scope
from app.models import User
from app.security import hash_password, password_problems
from app.services import audit

if len(sys.argv) < 3:
    sys.exit(__doc__)
email, name = sys.argv[1].strip().lower(), sys.argv[2].strip()
pw = os.environ.get("ADMIN_PASSWORD") or getpass.getpass("Password (12+ chars, upper/lower case, a number): ")
problems = password_problems(pw)
if problems:
    sys.exit("Password needs " + ", ".join(problems))
init_db()
with session_scope() as db:
    u = db.query(User).filter(User.email == email).first()
    if u:
        u.password_hash, u.role, u.is_active = hash_password(pw), "admin", True
        u.session_version += 1
        u.failed_logins, u.locked_until = 0, None
        audit.record(db, None, "user.admin_reset_cli", "user", email)
        print(f"Updated {email} (admin).")
    else:
        db.add(User(email=email, full_name=name, role="admin", password_hash=hash_password(pw)))
        audit.record(db, None, "user.admin_created_cli", "user", email)
        print(f"Created admin {email}.")
