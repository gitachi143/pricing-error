"""Single-admin auth: scrypt password hash + HMAC-signed session cookie.
Machine callers (ChatGPT tasks, checkout worker) use bearer tokens instead."""
import base64
import hashlib
import hmac
import json
import secrets
import time

from fastapi import HTTPException, Request

from . import config

COOKIE = "dealbot_session"


# --- password ------------------------------------------------------------
def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.scrypt(password.encode(), salt=salt, n=2 ** 14, r=8, p=1, dklen=32)
    return "scrypt$16384$8$1$" + base64.b64encode(salt).decode() + "$" + base64.b64encode(dk).decode()


def verify_password(password: str) -> bool:
    stored = config.ADMIN_PASSWORD_HASH
    if stored:
        try:
            scheme, n, r, p, salt_b64, dk_b64 = stored.split("$")
            if scheme != "scrypt":
                return False
            dk = hashlib.scrypt(password.encode(), salt=base64.b64decode(salt_b64),
                                n=int(n), r=int(r), p=int(p), dklen=len(base64.b64decode(dk_b64)))
            return hmac.compare_digest(dk, base64.b64decode(dk_b64))
        except Exception:
            return False
    if config.ADMIN_PASSWORD:
        return hmac.compare_digest(password, config.ADMIN_PASSWORD)
    return False


# --- session cookie ------------------------------------------------------
def _sign(payload: bytes) -> str:
    sig = hmac.new(config.SESSION_SECRET.encode(), payload, hashlib.sha256).digest()
    return base64.urlsafe_b64encode(payload).decode().rstrip("=") + "." + \
        base64.urlsafe_b64encode(sig).decode().rstrip("=")


def _unpad(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def make_session() -> str:
    return _sign(json.dumps({"u": config.ADMIN_USER,
                             "exp": time.time() + config.SESSION_HOURS * 3600}).encode())


def read_session(token: str | None) -> dict | None:
    if not token or "." not in token:
        return None
    body, sig = token.rsplit(".", 1)
    try:
        payload = _unpad(body)
        expect = hmac.new(config.SESSION_SECRET.encode(), payload, hashlib.sha256).digest()
        if not hmac.compare_digest(_unpad(sig), expect):
            return None
        data = json.loads(payload)
        if data.get("exp", 0) < time.time():
            return None
        return data
    except Exception:
        return None


# --- FastAPI dependencies ------------------------------------------------
def require_user(request: Request) -> dict:
    sess = read_session(request.cookies.get(COOKIE))
    if not sess:
        raise HTTPException(status_code=401, detail="login required")
    return sess


def _bearer(request: Request) -> str:
    auth = request.headers.get("authorization", "")
    return auth[7:].strip() if auth.lower().startswith("bearer ") else ""


def require_ingest(request: Request) -> bool:
    """ChatGPT scheduled tasks / Discord relay. Falls back to an admin session
    so you can paste payloads from the dashboard while testing."""
    tok = _bearer(request) or request.headers.get("x-ingest-token", "")
    if config.INGEST_TOKEN and hmac.compare_digest(tok, config.INGEST_TOKEN):
        return True
    if read_session(request.cookies.get(COOKIE)):
        return True
    raise HTTPException(status_code=401, detail="bad ingest token")


def require_worker(request: Request) -> bool:
    tok = _bearer(request) or request.headers.get("x-worker-token", "")
    if config.WORKER_TOKEN and hmac.compare_digest(tok, config.WORKER_TOKEN):
        return True
    if read_session(request.cookies.get(COOKIE)):
        return True
    raise HTTPException(status_code=401, detail="bad worker token")
