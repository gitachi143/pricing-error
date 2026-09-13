"""End to end: a Discord message in, a decision and a queued checkout out."""
import asyncio
import json
import time

import pytest

from app import db
from app.core import pipeline, playbooks
from app.core.product import ProductFacts, parse_html

PAGE = """<html><head><script type="application/ld+json">
{"@type":"Product","name":"Sony WH-1000XM5 Wireless Headphones - Black","brand":{"name":"Sony"},
 "sku":"86718543","offers":{"@type":"Offer","price":"39.99","priceCurrency":"USD",
 "availability":"https://schema.org/InStock"}}</script></head>
<body>reg. $399.99 <button>Add to cart</button></body></html>"""

PLAYBOOK = {
    "schema_version": "1.0", "merchant": "target.com", "captured_at": "2026-09-13T03:00:00Z",
    "confidence": 0.9, "site_facts": {},
    "selectors": {"add": {"css": "#add"}, "co": {"css": "#co"}, "cc": {"css": "#cc"},
                  "place": {"css": "#place"}, "num": {"css": ".num"}},
    "steps": {"add_to_cart": [{"action": "goto", "url": "{{deal.url}}"},
                              {"action": "click", "target": "add"}],
              "checkout_start": [{"action": "click", "target": "co"}],
              "payment": [{"action": "fill", "target": "cc", "value": "{{card.number}}"}],
              "place_order": [{"action": "click", "target": "place"},
                              {"action": "extract", "target": "num", "as": "order_number"}]},
    "success_signals": {"order_confirmed": {"css": ".num"}},
}


@pytest.fixture
def stub_fetch(monkeypatch):
    async def fake(url, client=None):
        f = parse_html(PAGE, url)
        f.url = url
        f.fetch_ms = 310
        f.http_status = 200
        return f
    monkeypatch.setattr(pipeline, "fetch_product", fake)


def run(msg, **kw):
    return asyncio.run(pipeline.process_message(msg, **kw))


MSG = "🚨 PRICE ERROR https://www.target.com/p/sony-wh-1000xm5/-/A-86718543 $39 was $399"


def test_full_path_produces_a_buy(stub_fetch):
    db.insert("cards", {"nickname": "Visa", "last4": "4242", "credit_limit": 5000,
                        "available": 5000, "priority": 10, "enabled": 1})
    playbooks.save(dict(PLAYBOOK))
    out = run(MSG, source="discord:#pricing-errors")
    assert len(out) == 1
    d = out[0]
    assert d["merchant"] == "target.com"
    assert d["trust_verdict"] == "TRUSTED"
    assert d["product_name"].startswith("Sony WH-1000XM5")
    assert d["listed_price"] == 39.99
    assert d["category"] == "electronics_audio"
    assert d["decision"] == "BUY"
    assert d["target_qty"] >= 1
    assert d["latency_ms"] is not None


def test_checkout_prep_is_queued_before_the_decision_lands(stub_fetch):
    db.insert("cards", {"nickname": "Visa", "last4": "4242", "credit_limit": 5000,
                        "available": 5000, "priority": 10, "enabled": 1})
    playbooks.save(dict(PLAYBOOK))
    out = run(MSG)
    jobs = db.q("SELECT * FROM job_queue WHERE deal_id=? ORDER BY id", (out[0]["id"],))
    kinds = [j["kind"] for j in jobs]
    assert kinds[0] == "prep"                       # prep is enqueued first, in parallel
    events = [e["message"] for e in db.q(
        "SELECT message FROM events WHERE deal_id=? ORDER BY id", (out[0]["id"],))]
    prep_at = next(i for i, m in enumerate(events) if "cart prep started" in m)
    fetch_at = next(i for i, m in enumerate(events) if "via jsonld" in m)
    assert prep_at < fetch_at                       # ...before the page even came back


def test_auto_buy_off_means_no_place_job(stub_fetch):
    db.insert("cards", {"nickname": "Visa", "last4": "4242", "credit_limit": 5000,
                        "available": 5000, "priority": 10, "enabled": 1})
    playbooks.save(dict(PLAYBOOK))
    db.set_setting("auto_buy", False)
    out = run(MSG)
    kinds = [j["kind"] for j in db.q("SELECT kind FROM job_queue WHERE deal_id=?", (out[0]["id"],))]
    assert "place" not in kinds


def test_auto_buy_on_queues_the_purchase(stub_fetch):
    db.insert("cards", {"nickname": "Visa", "last4": "4242", "credit_limit": 5000,
                        "available": 5000, "priority": 10, "enabled": 1})
    playbooks.save(dict(PLAYBOOK))
    db.set_setting("auto_buy", True)
    out = run(MSG)
    place = db.q1("SELECT * FROM job_queue WHERE deal_id=? AND kind='place'", (out[0]["id"],))
    assert place is not None
    payload = json.loads(place["payload"])
    assert payload["charges"][0]["qty"] >= 1
    db.set_setting("auto_buy", False)


def test_spoofed_link_is_blocked_without_fetching(monkeypatch):
    called = []

    async def boom(url, client=None):
        called.append(url)
        return ProductFacts(url=url)
    monkeypatch.setattr(pipeline, "fetch_product", boom)
    out = run("deal!! https://www.tаrget.com/p/sony-wh-1000xm5")
    assert out[0]["decision"] == "BLOCKED"
    assert called == []                             # never touched the page
    assert db.q1("SELECT * FROM job_queue") is None  # and never queued a checkout


def test_no_playbook_means_no_prep_but_still_researched(stub_fetch):
    out = run(MSG)
    assert out[0]["decision"] in ("BUY", "HOLD")
    assert db.q1("SELECT * FROM job_queue WHERE kind='prep'") is None
    msgs = [e["message"] for e in db.q("SELECT message FROM events WHERE deal_id=?", (out[0]["id"],))]
    assert any("no live playbook" in m for m in msgs)


def test_message_with_no_link_is_a_no_op():
    assert run("anyone know if this drops tomorrow?") == []


def test_every_step_is_written_to_the_console(stub_fetch):
    playbooks.save(dict(PLAYBOOK))
    out = run(MSG)
    stages = {e["stage"] for e in db.q("SELECT stage FROM events WHERE deal_id=?", (out[0]["id"],))}
    assert {"intake", "trust", "product", "research", "decision"} <= stages


# --- worker offline / stale queue -----------------------------------------
def test_stale_buy_jobs_expire_instead_of_being_bought_hours_later(stub_fetch):
    """The laptop was shut for 4 hours. That deal is dead — don't buy it on wake."""
    import time as _t
    from app.api import routes
    db.insert("cards", {"nickname": "Visa", "last4": "4242", "credit_limit": 5000,
                        "available": 5000, "priority": 10, "enabled": 1})
    playbooks.save(dict(PLAYBOOK))
    db.set_setting("auto_buy", True)
    out = run(MSG)
    db.set_setting("auto_buy", False)
    deal_id = out[0]["id"]
    job = db.q1("SELECT * FROM job_queue WHERE deal_id=? AND kind='place'", (deal_id,))
    assert job is not None

    # rewind every queued job 4 hours, as if the worker had been off
    db.execute("UPDATE job_queue SET created_at=? WHERE state='queued'", (_t.time() - 4 * 3600,))

    assert routes.worker_next(worker_id="test")["job"] is None
    assert db.q1("SELECT state FROM job_queue WHERE id=?", (job["id"],))["state"] == "expired"
    msgs = [e["message"] for e in db.q("SELECT message FROM events WHERE deal_id=?", (deal_id,))]
    assert any("worker was offline" in m for m in msgs)


def test_fresh_jobs_still_lease_newest_first(stub_fetch):
    """Two deals waiting: the worker should take the one that just landed."""
    import time as _t
    from app.api import routes
    playbooks.save(dict(PLAYBOOK))
    old_id = db.insert("job_queue", {"created_at": _t.time() - 300, "deal_id": None,
                                     "kind": "prep", "payload": "{}", "state": "queued"})
    new_id = db.insert("job_queue", {"created_at": _t.time(), "deal_id": None,
                                     "kind": "prep", "payload": "{}", "state": "queued"})
    leased = routes.worker_next(worker_id="test")["job"]
    assert leased["id"] == new_id, "should take the newest deal, not the backlog"
    assert db.q1("SELECT state FROM job_queue WHERE id=?", (old_id,))["state"] == "queued"
