"""First-run setup: the checklist, the install link, and the installer itself."""
import re
import stat
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import config, db
from app.api import routes
from app.main import app
from app.security import make_setup_token

WORKER = config.ROOT / "worker"


@pytest.fixture
def client():
    c = TestClient(app)
    c.post("/api/login", json={"password": config.ADMIN_PASSWORD or "test-pw"})
    return c


# --- the checklist ---------------------------------------------------------
def test_fresh_install_reports_what_is_missing(client):
    s = client.get("/api/setup/status").json()
    by = {x["key"]: x for x in s["steps"]}
    assert not s["complete"]
    assert by["cards"]["done"] is False and "none yet" in by["cards"]["detail"]
    assert by["worker"]["done"] is False
    assert by["playbooks"]["done"] is False
    assert by["merchants"]["done"] is True          # a starter merchant list ships with it
    assert all(x["href"] and x["cta"] for x in s["steps"]), "every step needs somewhere to go"


def test_adding_a_card_ticks_that_step_off(client):
    before = {x["key"]: x["done"] for x in client.get("/api/setup/status").json()["steps"]}
    assert before["cards"] is False
    client.post("/api/cards", json={"nickname": "Visa", "last4": "4242", "credit_limit": 5000})
    after = {x["key"]: x["done"] for x in client.get("/api/setup/status").json()["steps"]}
    assert after["cards"] is True


def test_worker_heartbeat_ticks_the_worker_step(client):
    client.post("/api/worker/heartbeat", json={"worker_id": "mac", "version": "1.2",
                                               "state": "running", "dry_run": True})
    by = {x["key"]: x for x in client.get("/api/setup/status").json()["steps"]}
    assert by["worker"]["done"] is True and "mac" in by["worker"]["detail"]


# --- the install link ------------------------------------------------------
def test_install_script_needs_a_valid_setup_token():
    c = TestClient(app)
    assert c.get("/api/worker/install.sh").status_code == 401
    assert c.get("/api/worker/install.sh?t=not-a-real-token").status_code == 401


def test_install_script_is_served_with_the_url_and_token_baked_in(client):
    link = client.post("/api/setup/link").json()
    assert link["command"].startswith("curl -fsSL")
    token = re.search(r"install\.sh\?t=([\w.\-]+)", link["command"]).group(1)
    r = TestClient(app).get(f"/api/worker/install.sh?t={token}")
    assert r.status_code == 200
    body = r.text
    assert body.startswith("#!/usr/bin/env bash")
    assert "export DEALDESK_URL=" in body and "export WORKER_TOKEN=" in body
    assert "com.dealdesk.worker" in body and "KeepAlive" in body and "RunAtLoad" in body
    assert r.headers["cache-control"] == "no-store"


def test_expired_setup_tokens_are_refused():
    stale = make_setup_token(minutes=-1)
    assert TestClient(app).get(f"/api/worker/install.sh?t={stale}").status_code == 401


def test_worker_bundle_contains_what_the_installer_needs(client):
    import io, tarfile
    r = client.get("/api/worker/bundle.tar.gz")
    assert r.status_code == 200
    names = set(tarfile.open(fileobj=io.BytesIO(r.content), mode="r:gz").getnames())
    assert {"run_worker.py", "executor.py", "vault.py", "vault_server.py"} <= names
    assert not any(n.endswith("vault.json") for n in names), "never ship a real vault"


def test_bundle_is_not_public():
    assert TestClient(app).get("/api/worker/bundle.tar.gz").status_code == 401


# --- the installer script itself ------------------------------------------
def test_installer_is_valid_bash_and_executable():
    script = WORKER / "install_mac.sh"
    assert script.stat().st_mode & stat.S_IXUSR, "installer must be executable"
    subprocess.run(["bash", "-n", str(script)], check=True)


def test_installer_sets_up_autostart_and_restart():
    body = (WORKER / "install_mac.sh").read_text()
    assert "<key>RunAtLoad</key><true/>" in body, "must start at login"
    assert "KeepAlive" in body and "SuccessfulExit" in body, "must restart if it dies"
    assert "chmod 600" in body, "config and vault must be locked down"
    assert "sudo" not in body, "nothing here should need root"


def test_installer_never_hardcodes_a_secret():
    body = (WORKER / "install_mac.sh").read_text()
    assert "wrk_" not in body and "ing_" not in body
    assert ': "${WORKER_TOKEN:?' in body, "it must require the token to be passed in"


# --- worker status ---------------------------------------------------------
def test_status_distinguishes_never_connected_from_offline(client):
    assert client.get("/api/worker/status").json()["state"] == "never_connected"
    client.post("/api/worker/heartbeat", json={"worker_id": "mac", "state": "running"})
    assert client.get("/api/worker/status").json()["state"] == "online"
    hb = db.get_setting("worker_last_seen")
    hb["ts"] -= 600
    db.set_setting("worker_last_seen", hb)
    assert client.get("/api/worker/status").json()["state"] == "offline"


def test_status_carries_the_detail_the_setup_page_shows(client):
    client.post("/api/worker/heartbeat", json={
        "worker_id": "mac", "state": "running", "version": "1.2", "dry_run": False,
        "uptime_seconds": 3600, "browser_ok": True, "os": "Darwin 26.2", "reconnects": 2})
    s = client.get("/api/worker/status").json()
    hb = s["heartbeat"]
    assert hb["dry_run"] is False and hb["browser_ok"] is True and hb["reconnects"] == 2
    assert s["online"] and s["seconds_since_seen"] is not None


def test_connecting_and_stopping_are_written_to_the_console(client):
    client.post("/api/worker/heartbeat", json={"worker_id": "mac", "state": "running",
                                               "version": "1.2", "dry_run": True})
    client.post("/api/worker/heartbeat", json={"worker_id": "mac", "state": "stopping"})
    msgs = [e["message"] for e in db.q("SELECT message FROM events WHERE stage='worker'")]
    assert any("connected" in m for m in msgs) and any("shut down" in m for m in msgs)


# --- removing a deal -------------------------------------------------------
def test_a_deal_can_be_removed(client):
    did = db.insert("deals", {"created_at": 1.0, "url": "https://x.com/p", "merchant": "x.com",
                              "status": "skipped", "decision": "SKIP"})
    db.insert("events", {"deal_id": did, "ts": 1.0, "stage": "intake", "message": "hi"})
    assert client.delete(f"/api/deals/{did}").status_code == 200
    assert db.q1("SELECT id FROM deals WHERE id=?", (did,)) is None
    assert db.q1("SELECT id FROM events WHERE deal_id=?", (did,)) is None


def test_a_deal_with_a_placed_order_cannot_be_removed(client):
    did = db.insert("deals", {"created_at": 1.0, "url": "https://x.com/p", "merchant": "x.com",
                              "status": "ordered", "decision": "BUY"})
    db.insert("orders", {"deal_id": did, "created_at": 1.0, "merchant": "x.com", "qty": 1,
                         "total": 99.0, "status": "placed"})
    r = client.delete(f"/api/deals/{did}")
    assert r.status_code == 400 and "placed order" in r.json()["detail"]
    assert db.q1("SELECT id FROM deals WHERE id=?", (did,)) is not None


# --- https behind Azure's TLS-terminating proxy ----------------------------
FWD = {"x-forwarded-proto": "https", "x-forwarded-host": "dealdesk.azurewebsites.net"}


def test_install_link_uses_https_behind_the_proxy(client):
    r = client.post("/api/setup/link", headers=FWD).json()
    assert r["base"] == "https://dealdesk.azurewebsites.net"
    assert r["command"].startswith('curl -fsSL "https://dealdesk.azurewebsites.net/')


def test_worker_config_never_points_at_plain_http(client):
    import re
    cmd = client.post("/api/setup/link", headers=FWD).json()["command"]
    token = re.search(r"install\.sh\?t=([\w.\-]+)", cmd).group(1)
    body = TestClient(app).get(f"/api/worker/install.sh?t={token}", headers=FWD).text
    url = re.search(r"export DEALDESK_URL='([^']+)'", body).group(1)
    bundle = re.search(r"export WORKER_BUNDLE_URL='([^']+)'", body).group(1)
    assert url.startswith("https://"), "the worker would send its bearer token in the clear"
    assert bundle.startswith("https://")


def test_chatgpt_prompts_are_given_the_https_endpoint(client):
    for p in client.get("/api/prompts", headers=FWD).json():
        assert "http://dealdesk" not in p["text"], f"{p['file']} points at plain http"
        if "/api/ingest" in p["text"]:
            assert "https://dealdesk.azurewebsites.net/api/ingest" in p["text"]


def test_local_http_still_works(client):
    """No forwarding headers (local dev) must not be rewritten to https."""
    assert client.post("/api/setup/link").json()["base"].startswith("http://")
