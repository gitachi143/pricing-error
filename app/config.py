"""Runtime configuration. Everything comes from env vars so Azure App Settings
(or Key Vault references) can drive it without code changes."""
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# Local dev convenience: load .env if present (Azure supplies real App Settings).
_envfile = ROOT / ".env"
if _envfile.exists():
    for _line in _envfile.read_text().splitlines():
        _line = _line.strip()
        if not _line or _line.startswith("#") or "=" not in _line:
            continue
        _k, _v = _line.split("=", 1)
        os.environ.setdefault(_k.strip(), _v.strip().strip('"').strip("'"))


def _bool(name: str, default: bool = False) -> bool:
    return os.environ.get(name, str(default)).strip().lower() in ("1", "true", "yes", "on")


# --- storage -------------------------------------------------------------
# Azure App Service mounts /home persistently; use it when present.
_default_db = "/home/data/dealbot.db" if Path("/home/data").exists() else str(ROOT / "data" / "dealbot.db")
DB_PATH = os.environ.get("DB_PATH", _default_db)

# --- auth ----------------------------------------------------------------
ADMIN_USER = os.environ.get("ADMIN_USER", "admin")
ADMIN_PASSWORD_HASH = os.environ.get("ADMIN_PASSWORD_HASH", "")
# Dev convenience only: if no hash is set, this plaintext password is used.
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "")
SESSION_SECRET = os.environ.get("SESSION_SECRET", "dev-insecure-secret-change-me")
SESSION_HOURS = int(os.environ.get("SESSION_HOURS", "168"))

# Bearer tokens for machine callers.
INGEST_TOKEN = os.environ.get("INGEST_TOKEN", "")   # ChatGPT scheduled tasks / Discord relay
WORKER_TOKEN = os.environ.get("WORKER_TOKEN", "")   # checkout worker

# --- pipeline knobs ------------------------------------------------------
FETCH_TIMEOUT = float(os.environ.get("FETCH_TIMEOUT", "8.0"))
FETCH_UA = os.environ.get(
    "FETCH_UA",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0 Safari/537.36",
)
ALLOW_NETWORK = _bool("ALLOW_NETWORK", True)        # off in tests
AUTO_BUY = _bool("AUTO_BUY", False)                 # master kill switch for placing orders
MAX_SPEND_PER_DEAL = float(os.environ.get("MAX_SPEND_PER_DEAL", "2000"))
MAX_SPEND_PER_DAY = float(os.environ.get("MAX_SPEND_PER_DAY", "5000"))

# Optional market-price providers (all optional; pipeline degrades gracefully).
SERPAPI_KEY = os.environ.get("SERPAPI_KEY", "")
EBAY_APP_ID = os.environ.get("EBAY_APP_ID", "")

VERSION = "1.0.0"
