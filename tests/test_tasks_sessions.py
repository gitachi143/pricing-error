"""Test bench, scheduled-task health, and merchant session tracking."""
import asyncio
import json
import time

import pytest

from app import db
from app.api import routes
from app.core import playbooks
from app.core.product import parse_html
from app.core import pipeline

PAGE = """<html><head><script type="application/ld+json">
{"@type":"Product","name":"Test Widget","brand":{"name":"Acme"},
 "offers":{"@type":"Offer","price":"19.99","priceCurrency":"USD",
 "availability":"https://schema.org/InStock"}}</script></head><body>reg. $199.99</body></html>"""


@pytest.fixture
def stub_fetch(monkeypatch):
    async def fake(url, client=None):
        f = parse_html(PAGE, url); f.url = url; f.http_status = 200; return f
    monkeypatch.setattr(pipeline, "fetch_product", fake)


def test_testbench_never_queues_a_checkout(stub_fetch):
    db.insert("cards", {"nickname": "Visa", "last4": "4242", "credit_limit": 5000,
                        "available": 5000, "priority": 10, "enabled": 1})
    db.set_setting("auto_buy", True)
    out = asyncio.run(routes.testbench(routes.TestIn(
        message="https://www.target.com/p/test-widget", dry_run=True)))
    db.set_setting("auto_buy", False)
    assert out["dry_run"] and len(out["results"]) == 1
    assert db.q1("SELECT id FROM job_queue") is None, "a dry run must not queue work"
    assert db.q1("SELECT is_test FROM deals ORDER BY id DESC LIMIT 1")["is_test"] == 1


def test_testbench_reports_shipping_and_cards(stub_fetch):
    db.insert("cards", {"nickname": "Visa", "last4": "4242", "credit_limit": 5000,
                        "available": 5000, "priority": 10, "enabled": 1})
    db.execute("UPDATE merchants SET shipping_pref='expedited', allow_paid_shipping=1, "
               "max_shipping_cost=10 WHERE domain='target.com'")
    playbooks.save({
        "schema_version": "1.1", "merchant": "target.com", "captured_at": "2026-09-13T03:00:00Z",
        "confidence": 0.9, "site_facts": {},
        "selectors": {"a": {"css": "#a"}, "b": {"css": "#b"}, "c": {"css": "#c"},
                      "std": {"text": "Standard"}, "fast": {"text": "2-day"}},
        "shipping_options": {
            "standard": {"label": "Standard", "cost": 0, "days": 5, "selector": "std"},
            "two_day": {"label": "2-day", "cost": 9.99, "days": 2, "selector": "fast"}},
        "steps": {"add_to_cart": [{"action": "click", "target": "a"}],
                  "checkout_start": [{"action": "click", "target": "b"}],
                  "payment": [{"action": "fill", "target": "c", "value": "{{card.number}}"}],
                  "place_order": [{"action": "click", "target": "c"}]},
        "success_signals": {"order_confirmed": {"css": "#a"}}})
    out = asyncio.run(routes.testbench(routes.TestIn(
        message="https://www.target.com/p/test-widget", dry_run=True)))
    r = out["results"][0]
    assert r["shipping"]["label"] == "2-day"
    assert r["playbook"]["shipping_options"] == ["standard", "two_day"]
    assert r["card_plan"]["charges"], "should show which card would pay"


def test_clearing_the_test_bench_leaves_real_deals_alone(stub_fetch):
    asyncio.run(routes.testbench(routes.TestIn(message="https://www.target.com/p/x", dry_run=True)))
    asyncio.run(pipeline.process_message("https://www.rei.com/product/1/y", source="discord"))
    assert routes.clear_testbench()["removed"] == 1
    remaining = db.q("SELECT merchant FROM deals")
    assert len(remaining) == 1 and remaining[0]["merchant"] == "rei.com"


# --- scheduled task health -------------------------------------------------
def test_task_health_flags_a_late_task():
    routes.record_task("shipping", "chatgpt", "ok", "3 orders updated", items=3)
    db.execute("UPDATE task_runs SET ts=? WHERE task='shipping'", (time.time() - 20 * 3600,))
    tasks = {t["task"]: t for t in routes.list_tasks()}
    assert tasks["shipping"]["health"] == "late"      # expected every 4h
    assert tasks["recon"]["health"] == "never"


def test_task_health_is_ok_when_recent():
    routes.record_task("recon", "chatgpt", "ok", "target.com mapped", items=1)
    assert {t["task"]: t for t in routes.list_tasks()}["recon"]["health"] == "ok"


def test_a_rejected_playbook_shows_as_failing():
    routes.record_task("recon", "chatgpt", "rejected", "rei.com — rejected", errors=["bad selector"])
    t = {x["task"]: x for x in routes.list_tasks()}["recon"]
    assert t["health"] == "failing" and t["last"]["errors"] == ["bad selector"]


def test_ingesting_a_playbook_records_a_task_run():
    from tests.test_playbooks import GOOD
    import copy
    routes.ingest_playbook(copy.deepcopy(GOOD))
    run = db.q1("SELECT * FROM task_runs WHERE task='recon' ORDER BY id DESC LIMIT 1")
    assert run and run["status"] == "ok" and "bestbuy.com" in run["summary"]


# --- sessions --------------------------------------------------------------
def test_worker_reports_sessions_without_sending_credentials():
    routes.report_sessions([
        routes.SessionIn(merchant="target.com", signed_in=True, has_credentials=True),
        routes.SessionIn(merchant="bestbuy.com", signed_in=False, has_credentials=True,
                         needs_manual_login=True, note="2FA challenge")])
    rows = {r["domain"]: r for r in routes.list_sessions()}
    assert rows["target.com"]["status"] == "signed_in"
    assert rows["bestbuy.com"]["status"] == "manual_login_needed"
    stored = db.q("SELECT * FROM merchant_sessions")
    assert not any("password" in json.dumps(dict(r)).lower() for r in stored)


def test_manual_login_needed_writes_a_console_line():
    routes.report_sessions([routes.SessionIn(merchant="nike.com", signed_in=False,
                                             has_credentials=False, needs_manual_login=True)])
    msgs = [e["message"] for e in db.q("SELECT message FROM events WHERE stage='session'")]
    assert any("--login nike.com" in m for m in msgs)


def test_session_status_for_a_merchant_never_checked():
    rows = {r["domain"]: r for r in routes.list_sessions()}
    assert rows["costco.com"]["status"] in ("unknown", "no_credentials")
