import os, sys, tempfile
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["DB_PATH"] = os.path.join(tempfile.mkdtemp(), "test.db")
os.environ["ALLOW_NETWORK"] = "0"
os.environ["SESSION_SECRET"] = "test-secret"
os.environ["ADMIN_PASSWORD"] = "test-pw"
# Tests must not depend on the developer's .env — blank the hash so the plaintext
# test password is the one that works.
os.environ["ADMIN_PASSWORD_HASH"] = ""
os.environ["INGEST_TOKEN"] = "test-ingest-token"
os.environ["WORKER_TOKEN"] = "test-worker-token"

import pytest
from app import db


@pytest.fixture(autouse=True)
def fresh_db():
    # filters/settings/merchants are reset too — init_db() re-seeds them, so a
    # test that edits a filter can't leak into the next one.
    for t in ("deals", "events", "cards", "card_rules", "orders", "playbooks",
              "job_queue", "comps", "shipping_updates", "filters", "settings",
              "merchants"):
        try:
            db.execute(f"DELETE FROM {t}")
        except Exception:
            pass
    db.init_db()
    yield
