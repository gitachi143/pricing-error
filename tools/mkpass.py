"""python3 tools/mkpass.py [password]  → prints a scrypt hash for ADMIN_PASSWORD_HASH."""
import secrets, string, sys
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent.parent))
from app.security import hash_password
alpha = string.ascii_letters + string.digits
pw = sys.argv[1] if len(sys.argv) > 1 else "-".join(
    "".join(secrets.choice(alpha) for _ in range(6)) for _ in range(3))
print("password:", pw)
print("ADMIN_PASSWORD_HASH=" + hash_password(pw))
