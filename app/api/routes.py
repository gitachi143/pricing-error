"""All HTTP endpoints. Grouped by what the dashboard page needs."""
from __future__ import annotations

import asyncio
import json
import time

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from .. import config, db
from ..core import cards as cards_mod
from ..core import events, pipeline, playbooks
from ..core import shipping as shipping_mod
from ..core.decision import active_filters
from ..core.trust import check_url
from ..security import require_ingest, require_user, require_worker

router = APIRouter()
ingest = APIRouter(dependencies=[Depends(require_ingest)])
ui = APIRouter(dependencies=[Depends(require_user)])
work = APIRouter(dependencies=[Depends(require_worker)])


def public_base(request: Request) -> str:
    """The URL a client outside Azure should use.

    App Service terminates TLS and forwards plain http to the container, so
    request.base_url says "http://…". Anything we hand out — the worker's
    DEALDESK_URL, the endpoints baked into the ChatGPT prompts — has to be the
    https one, or the worker ends up sending its bearer token in the clear.
    """
    base = str(request.base_url).rstrip("/")
    proto = request.headers.get("x-forwarded-proto", "").split(",")[0].strip()
    if proto:
        base = base.replace("http://", f"{proto}://", 1)
    host = request.headers.get("x-forwarded-host", "").split(",")[0].strip()
    if host:
        scheme = base.split("://", 1)[0]
        base = f"{scheme}://{host}"
    return base


def record_task(task: str, source: str, status: str, summary: str,
                items: int = 0, errors: list | None = None, **detail) -> int:
    """Every call from a ChatGPT scheduled task lands here so the Tasks page can
    show what ran, when, and whether it worked."""
    return db.insert("task_runs", {
        "ts": time.time(), "task": task, "source": source, "status": status,
        "summary": summary, "items": items,
        "errors": json.dumps(errors) if errors else None,
        "detail": json.dumps(detail, default=str) if detail else None})


def _j(row: dict, *keys) -> dict:
    for k in keys:
        if row.get(k):
            try:
                row[k] = json.loads(row[k])
            except Exception:
                pass
    return row


# ======================= ingest (Discord relay + ChatGPT tasks) ===========
class MessageIn(BaseModel):
    message: str = ""
    url: str | None = None
    source: str = "discord"
    posted_at: float | None = None


@ingest.post("/ingest/message")
async def ingest_message(body: MessageIn):
    t0 = time.perf_counter()
    results = await pipeline.process_message(body.message, body.source, url_override=body.url)
    record_task("discord", body.source, "ok" if results else "partial",
                f"{len(results)} link(s): " + ", ".join(
                    f"{r.get('merchant')} → {r.get('decision')}" for r in results) or "no links found",
                items=len(results))
    return {"handled": len(results), "elapsed_ms": int((time.perf_counter() - t0) * 1000),
            "deals": [{"id": r.get("id"), "merchant": r.get("merchant"),
                       "product": r.get("product_name"), "decision": r.get("decision"),
                       "qty": r.get("target_qty"), "score": r.get("score")} for r in results]}


class TestIn(BaseModel):
    message: str = ""
    dry_run: bool = True


@ui.post("/testbench")
async def testbench(body: TestIn):
    """Run a message through every stage without touching a cart or a card."""
    t0 = time.perf_counter()
    results = await pipeline.process_message(body.message, source="testbench",
                                             dry_run=body.dry_run)
    out = []
    for r in results:
        deal = get_deal(r["id"])
        merchant = deal.get("merchant") or ""
        pb = playbooks.get_active(merchant)
        ship_opts = (pb or {}).get("playbook", {}).get("shipping_options", {}) if pb else {}
        ship = shipping_mod.choose(merchant, ship_opts,
                                   order_value=float(deal.get("listed_price") or 0)
                                   * max(1, int(deal.get("target_qty") or 1)))
        plan = None
        if deal.get("listed_price"):
            f = active_filters()
            plan = cards_mod.plan_purchase(
                merchant, float(deal["listed_price"]),
                max(1, int(deal.get("target_qty") or 1)),
                tax_rate=float(f.get("tax_rate", 0)),
                daily_cap=float(db.get_setting("daily_spend_cap", 0) or 0)).to_dict()
        sess = db.q1("SELECT * FROM merchant_sessions WHERE merchant=?", (merchant,))
        out.append({"deal": deal, "shipping": ship.to_dict(), "card_plan": plan,
                    "playbook": {"version": pb["version"], "confidence": pb["confidence"],
                                 "shipping_options": list(ship_opts.keys())} if pb else None,
                    "session": sess})
    return {"elapsed_ms": int((time.perf_counter() - t0) * 1000),
            "dry_run": body.dry_run, "results": out}


@ui.delete("/testbench")
def clear_testbench():
    ids = [r["id"] for r in db.q("SELECT id FROM deals WHERE is_test=1")]
    for did in ids:
        db.execute("DELETE FROM events WHERE deal_id=?", (did,))
        db.execute("DELETE FROM job_queue WHERE deal_id=?", (did,))
    db.execute("DELETE FROM deals WHERE is_test=1")
    return {"removed": len(ids)}


class ShippingIn(BaseModel):
    order_number: str | None = None
    order_id: int | None = None
    merchant: str | None = None
    status: str                       # placed|label_created|in_transit|out_for_delivery|delivered|exception|cancelled
    carrier: str | None = None
    tracking_number: str | None = None
    eta: str | None = None
    note: str | None = None
    delivered_at: str | None = None


class ShippingBatch(BaseModel):
    source: str = "chatgpt-shipping-task"
    checked_at: str | None = None
    updates: list[ShippingIn] = Field(default_factory=list)


@ingest.post("/ingest/shipping")
def ingest_shipping(body: ShippingBatch):
    applied, unmatched = [], []
    for u in body.updates:
        row = None
        if u.order_id:
            row = db.q1("SELECT * FROM orders WHERE id=?", (u.order_id,))
        if not row and u.order_number:
            row = db.q1("SELECT * FROM orders WHERE order_number=? ORDER BY id DESC LIMIT 1",
                        (u.order_number,))
        if not row and u.tracking_number:
            row = db.q1("SELECT * FROM orders WHERE tracking_number=? ORDER BY id DESC LIMIT 1",
                        (u.tracking_number,))
        if not row:
            unmatched.append(u.order_number or u.tracking_number or "?")
            continue
        patch = {"ship_status": u.status, "last_tracking_update": time.time()}
        if u.carrier: patch["carrier"] = u.carrier
        if u.tracking_number: patch["tracking_number"] = u.tracking_number
        if u.eta: patch["eta"] = u.eta
        if u.status == "delivered":
            patch["delivered_at"] = time.time()
        if u.status == "cancelled":
            patch["status"] = "cancelled"
            cards_mod.release_charge(row["card_id"], row["total"] or 0)
            events.log(row["deal_id"], "shipping",
                       f"order {row['order_number']} CANCELLED by {row['merchant']} — "
                       f"${row['total']:,.2f} credit released", level="error")
        db.update("orders", row["id"], patch)
        db.insert("shipping_updates", {
            "order_id": row["id"], "ts": time.time(), "status": u.status, "carrier": u.carrier,
            "tracking_number": u.tracking_number, "eta": u.eta, "note": u.note,
            "source": body.source, "raw": json.dumps(u.model_dump())})
        if row["ship_status"] != u.status:
            events.log(row["deal_id"], "shipping",
                       f"{row['product_name'] or row['merchant']} → {u.status.replace('_',' ')}"
                       + (f" (ETA {u.eta})" if u.eta else ""),
                       level="ok" if u.status == "delivered" else "info")
        applied.append(row["id"])
    record_task("shipping", body.source, "ok" if not unmatched else "partial",
                f"{len(applied)} order(s) updated"
                + (f", {len(unmatched)} unmatched" if unmatched else ""),
                items=len(applied), errors=unmatched or None, checked_at=body.checked_at)
    return {"applied": len(applied), "order_ids": applied, "unmatched": unmatched}


@ingest.post("/ingest/playbook")
def ingest_playbook(body: dict):
    res = playbooks.save(body, generated_by=body.get("generated_by", "chatgpt-recon-task"))
    events.log(None, "playbook",
               f"{res.get('merchant')} playbook {'accepted → ' + res.get('status','') if res.get('accepted') else 'REJECTED'}",
               level="ok" if res.get("accepted") else "error",
               errors=res.get("errors"), version=res.get("version"))
    record_task("recon", body.get("generated_by", "chatgpt-recon-task"),
                "ok" if res.get("accepted") else "rejected",
                f"{res.get('merchant')} {res.get('version', '')}"
                + ("" if res.get("accepted") else " — rejected"),
                items=1 if res.get("accepted") else 0, errors=res.get("errors"),
                merchant=res.get("merchant"), confidence=body.get("confidence"))
    if not res.get("accepted"):
        raise HTTPException(status_code=422, detail=res)
    return res


class CompIn(BaseModel):
    key: str
    market_price: float
    source: str = "chatgpt-comps"
    sample_size: int = 1


@ingest.post("/ingest/comps")
def ingest_comps(items: list[CompIn]):
    for it in items:
        db.insert("comps", {"key": it.key.lower().strip(), "market_price": it.market_price,
                            "source": it.source, "sample_size": it.sample_size,
                            "updated_at": time.time()})
    record_task("comps", items[0].source if items else "chatgpt-comps", "ok",
                f"{len(items)} comp(s) refreshed", items=len(items))
    return {"stored": len(items)}


# ======================= deals & console =================================
@ui.get("/deals")
def list_deals(status: str = "", decision: str = "", q: str = "", limit: int = 100):
    sql = "SELECT * FROM deals WHERE 1=1"
    args: list = []
    if status:
        sql += " AND status=?"; args.append(status)
    if decision:
        sql += " AND decision=?"; args.append(decision)
    if q:
        sql += " AND (product_name LIKE ? OR merchant LIKE ? OR url LIKE ?)"
        args += [f"%{q}%"] * 3
    sql += " ORDER BY created_at DESC LIMIT ?"; args.append(min(limit, 500))
    return [_j(r, "trust_reasons", "decision_reasons") for r in db.q(sql, args)]


@ui.get("/deals/{deal_id}")
def get_deal(deal_id: int):
    row = db.q1("SELECT * FROM deals WHERE id=?", (deal_id,))
    if not row:
        raise HTTPException(404, "no such deal")
    _j(row, "trust_reasons", "decision_reasons", "raw_product")
    row["events"] = events.recent(200, deal_id=deal_id)
    row["orders"] = db.q("SELECT * FROM orders WHERE deal_id=? ORDER BY id", (deal_id,))
    row["jobs"] = [_j(x, "payload", "result")
                   for x in db.q("SELECT * FROM job_queue WHERE deal_id=? ORDER BY id", (deal_id,))]
    if row.get("merchant"):
        pb = playbooks.get_active(row["merchant"])
        row["playbook"] = {"version": pb["version"], "confidence": pb["confidence"]} if pb else None
    return row


@ui.post("/deals/{deal_id}/buy")
def force_buy(deal_id: int, qty: int | None = None):
    deal = db.q1("SELECT * FROM deals WHERE id=?", (deal_id,))
    if not deal:
        raise HTTPException(404, "no such deal")
    f = active_filters()
    want = qty or deal["target_qty"] or 1
    plan = cards_mod.plan_purchase(deal["merchant"], deal["listed_price"] or 0, want,
                                   tax_rate=float(f.get("tax_rate", 0)),
                                   daily_cap=float(db.get_setting("daily_spend_cap", 0) or 0))
    if not plan.charges:
        raise HTTPException(400, plan.shortfall_reason or "no card available")
    job = pipeline._enqueue(deal_id, "place", {
        "url": deal["final_url"] or deal["url"], "merchant": deal["merchant"],
        "qty": plan.qty_planned, "size": deal["size"],
        "max_price": (deal["listed_price"] or 0) * 1.05,
        "charges": [c.to_dict() for c in plan.charges]})
    db.update("deals", deal_id, {"status": "ordering", "decision": "BUY",
                                 "target_qty": plan.qty_planned})
    events.log(deal_id, "checkout", f"manual BUY — {plan.qty_planned} unit(s) queued", level="ok",
               plan=plan.to_dict())
    return {"job_id": job, "plan": plan.to_dict()}


@ui.post("/deals/{deal_id}/skip")
def skip_deal(deal_id: int, reason: str = "manual skip"):
    db.update("deals", deal_id, {"status": "skipped", "decision": "SKIP"})
    db.execute("UPDATE job_queue SET state='done' WHERE deal_id=? AND state='queued'", (deal_id,))
    events.log(deal_id, "decision", f"SKIP — {reason}", level="info")
    return {"ok": True}


@ui.delete("/deals/{deal_id}")
def delete_deal(deal_id: int):
    """Remove a deal and everything attached to it. Orders are kept — deleting a
    deal must never erase the record of money that was actually spent."""
    if db.q1("SELECT id FROM orders WHERE deal_id=? AND status='placed'", (deal_id,)):
        raise HTTPException(400, "this deal has a placed order — cancel the order instead")
    db.execute("DELETE FROM events WHERE deal_id=?", (deal_id,))
    db.execute("DELETE FROM job_queue WHERE deal_id=?", (deal_id,))
    db.execute("DELETE FROM orders WHERE deal_id=? AND status!='placed'", (deal_id,))
    db.execute("DELETE FROM deals WHERE id=?", (deal_id,))
    return {"ok": True}


@ui.post("/deals/{deal_id}/recheck")
async def recheck(deal_id: int):
    deal = db.q1("SELECT * FROM deals WHERE id=?", (deal_id,))
    if not deal:
        raise HTTPException(404, "no such deal")
    return await pipeline.process_link(deal["final_url"] or deal["url"],
                                       deal["raw_message"] or "", "recheck")


class TrustDomainIn(BaseModel):
    domain: str
    name: str = ""
    return_window_days: int = 30
    free_returns: bool = True
    cancel_risk: str = "medium"


@ui.post("/merchants/trust")
def trust_domain(body: TrustDomainIn):
    db.execute(
        "INSERT INTO merchants(domain,name,trusted,return_window_days,free_returns,"
        "restocking_fee_pct,cancel_risk,checkout_difficulty,updated_at) "
        "VALUES(?,?,1,?,?,0,?, 'medium', ?) ON CONFLICT(domain) DO UPDATE SET "
        "trusted=1,name=excluded.name,return_window_days=excluded.return_window_days,"
        "free_returns=excluded.free_returns,cancel_risk=excluded.cancel_risk,updated_at=excluded.updated_at",
        (body.domain.lower(), body.name or body.domain, body.return_window_days,
         1 if body.free_returns else 0, body.cancel_risk, time.time()))
    events.log(None, "merchant", f"{body.domain} added to the trusted list", level="ok")
    return {"ok": True}


@ui.get("/events")
def list_events(limit: int = 200, level: str = "", deal_id: int | None = None):
    return events.recent(limit=min(limit, 1000), level=level or None, deal_id=deal_id)


@ui.get("/stats")
def stats():
    day = time.time() - 86400
    week = time.time() - 7 * 86400
    def one(sql, args=()):
        r = db.q1(sql, args)
        return list(r.values())[0] if r else 0
    by_decision = {r["decision"]: r["n"] for r in db.q(
        "SELECT decision, COUNT(*) n FROM deals WHERE created_at>? GROUP BY decision", (week,))}
    spend = one("SELECT COALESCE(SUM(total),0) FROM orders WHERE created_at>? AND status IN ('placed','pending')", (day,))
    return {
        "deals_24h": one("SELECT COUNT(*) FROM deals WHERE created_at>?", (day,)),
        "deals_7d": one("SELECT COUNT(*) FROM deals WHERE created_at>?", (week,)),
        "by_decision_7d": by_decision,
        "blocked_7d": one("SELECT COUNT(*) FROM deals WHERE created_at>? AND decision='BLOCKED'", (week,)),
        "orders_open": one("SELECT COUNT(*) FROM orders WHERE status='placed' AND (ship_status IS NULL OR ship_status NOT IN ('delivered','cancelled'))"),
        "orders_failed_24h": one("SELECT COUNT(*) FROM orders WHERE status='failed' AND created_at>?", (day,)),
        "spend_24h": round(float(spend), 2),
        "daily_cap": float(db.get_setting("daily_spend_cap", config.MAX_SPEND_PER_DAY) or 0),
        "credit_available": round(float(one("SELECT COALESCE(SUM(available),0) FROM cards WHERE enabled=1")), 2),
        "credit_total": round(float(one("SELECT COALESCE(SUM(credit_limit),0) FROM cards WHERE enabled=1")), 2),
        "auto_buy": bool(db.get_setting("auto_buy", config.AUTO_BUY)),
        "median_latency_ms": one("SELECT COALESCE(AVG(latency_ms),0) FROM deals WHERE created_at>?", (day,)),
        "playbooks": playbooks.coverage(),
        "inventory_value": round(float(one(
            "SELECT COALESCE(SUM(o.total),0) FROM orders o WHERE o.status='placed'")), 2),
    }


class UrlIn(BaseModel):
    url: str


@ui.post("/trust-check")
def trust_check(body: UrlIn):
    """Paste-a-link tester for the Trust page."""
    from ..core.links import extract_urls
    urls = extract_urls(body.url) or [body.url]
    return [check_url(u, pipeline.known_domains(), pipeline._shorteners()).to_dict() for u in urls]


# ======================= cards ===========================================
class CardIn(BaseModel):
    nickname: str
    network: str = "visa"
    last4: str = ""
    credit_limit: float = 0
    available: float | None = None
    priority: int = 100
    statement_day: int | None = None
    max_per_txn: float = 0
    daily_cap: float = 0
    enabled: bool = True
    notes: str = ""


@ui.get("/cards")
def list_cards():
    rows = db.q("SELECT * FROM cards ORDER BY priority, id")
    for r in rows:
        r["spent_today"] = round(cards_mod.spent_today(r["id"]), 2)
        r["rules"] = db.q("SELECT * FROM card_rules WHERE card_id=? ORDER BY rank", (r["id"],))
    return rows


@ui.post("/cards")
def add_card(body: CardIn):
    d = body.model_dump()
    d["available"] = body.available if body.available is not None else body.credit_limit
    d["enabled"] = 1 if body.enabled else 0
    d["updated_at"] = time.time()
    cid = db.insert("cards", d)
    events.log(None, "cards", f"card added: {body.nickname} ·{body.last4} "
                              f"(${d['available']:,.0f} of ${body.credit_limit:,.0f} available)")
    return {"id": cid}


@ui.put("/cards/{card_id}")
def edit_card(card_id: int, body: dict):
    allowed = {"nickname", "network", "last4", "credit_limit", "available", "priority",
               "statement_day", "max_per_txn", "daily_cap", "enabled", "notes", "cooldown_until"}
    patch = {k: (1 if v is True else 0 if v is False else v)
             for k, v in body.items() if k in allowed}
    patch["updated_at"] = time.time()
    db.update("cards", card_id, patch)
    if "available" in patch:
        c = db.q1("SELECT nickname,last4 FROM cards WHERE id=?", (card_id,))
        events.log(None, "cards", f"{c['nickname']} ·{c['last4']} available credit set to "
                                  f"${float(patch['available']):,.2f}")
    return {"ok": True}


@ui.delete("/cards/{card_id}")
def delete_card(card_id: int):
    db.execute("DELETE FROM cards WHERE id=?", (card_id,))
    return {"ok": True}


class RuleIn(BaseModel):
    merchant: str
    card_id: int
    rank: int = 0
    shipping_method: str = ""
    enabled: bool = True


@ui.get("/card-rules")
def list_rules():
    return db.q("SELECT r.*, c.nickname, c.last4, c.network, c.available "
                "FROM card_rules r JOIN cards c ON c.id=r.card_id ORDER BY r.merchant, r.rank")


@ui.post("/card-rules")
def add_rule(body: RuleIn):
    rid = db.insert("card_rules", {"merchant": body.merchant.lower().strip(), "card_id": body.card_id,
                                   "rank": body.rank, "shipping_method": body.shipping_method,
                                   "enabled": 1 if body.enabled else 0})
    return {"id": rid}


@ui.put("/card-rules/{rule_id}")
def edit_rule(rule_id: int, body: dict):
    allowed = {"merchant", "card_id", "rank", "shipping_method", "enabled"}
    db.update("card_rules", rule_id, {k: (1 if v is True else 0 if v is False else v)
                                      for k, v in body.items() if k in allowed})
    return {"ok": True}


@ui.delete("/card-rules/{rule_id}")
def delete_rule(rule_id: int):
    db.execute("DELETE FROM card_rules WHERE id=?", (rule_id,))
    return {"ok": True}


@ui.post("/cards/plan-preview")
def plan_preview(merchant: str, unit_price: float, qty: int):
    f = active_filters()
    return cards_mod.plan_purchase(merchant, unit_price, qty,
                                   tax_rate=float(f.get("tax_rate", 0)),
                                   daily_cap=float(db.get_setting("daily_spend_cap", 0) or 0)).to_dict()


# ======================= filters / merchants / settings ==================
@ui.get("/filters")
def get_filters():
    row = db.q1("SELECT * FROM filters WHERE active=1 ORDER BY id DESC LIMIT 1")
    return {"id": row["id"] if row else None, "name": row["name"] if row else "default",
            "values": active_filters(), "defaults": db.DEFAULT_FILTERS,
            "all": db.q("SELECT id,name,active,updated_at FROM filters ORDER BY id")}


@ui.put("/filters")
def put_filters(body: dict):
    row = db.q1("SELECT * FROM filters WHERE active=1 ORDER BY id DESC LIMIT 1")
    values = dict(db.DEFAULT_FILTERS)
    values.update(body or {})
    if row:
        db.update("filters", row["id"], {"json": json.dumps(values), "updated_at": time.time()})
    else:
        db.insert("filters", {"name": "default", "active": 1, "json": json.dumps(values),
                              "updated_at": time.time()})
    events.log(None, "filters", "buying filters updated", changed=list((body or {}).keys()))
    return {"ok": True, "values": values}


@ui.get("/merchants")
def list_merchants():
    rows = db.q("SELECT * FROM merchants ORDER BY domain")
    sessions = {r["merchant"]: r for r in db.q("SELECT * FROM merchant_sessions")}
    for r in rows:
        pb = playbooks.get_active(r["domain"])
        opts = (pb or {}).get("playbook", {}).get("shipping_options", {}) if pb else {}
        r["shipping_options"] = shipping_mod._norm(opts)
        r["shipping_example"] = shipping_mod.choose(
            r["domain"], opts, order_value=250).to_dict()
        r["session"] = sessions.get(r["domain"])
        r["has_playbook"] = bool(pb)
    return rows


@ui.get("/merchants/{domain}/shipping")
def merchant_shipping(domain: str, order_value: float = 250):
    pb = playbooks.get_active(domain)
    opts = (pb or {}).get("playbook", {}).get("shipping_options", {}) if pb else {}
    return {"policy": shipping_mod.merchant_policy(domain),
            "options": shipping_mod._norm(opts),
            "choice": shipping_mod.choose(domain, opts, order_value=order_value).to_dict()}


@ui.put("/merchants/{domain}")
def edit_merchant(domain: str, body: dict):
    allowed = {"name", "trusted", "return_window_days", "free_returns", "restocking_fee_pct",
               "cancel_risk", "checkout_difficulty", "notes",
               "shipping_pref", "allow_paid_shipping", "max_shipping_cost",
               "expedite_over_value", "return_method", "return_notes", "login_required"}
    patch = {k: (1 if v is True else 0 if v is False else v) for k, v in body.items() if k in allowed}
    if not patch:
        return {"ok": True}
    sets = ",".join(f"{k}=?" for k in patch)
    db.execute(f"UPDATE merchants SET {sets}, updated_at=? WHERE domain=?",
               list(patch.values()) + [time.time(), domain.lower()])
    return {"ok": True}


@ui.delete("/merchants/{domain}")
def delete_merchant(domain: str):
    db.execute("DELETE FROM merchants WHERE domain=?", (domain.lower(),))
    return {"ok": True}


@ui.get("/settings")
def get_settings():
    return {r["key"]: json.loads(r["value"]) if r["value"] else None
            for r in db.q("SELECT key,value FROM settings")}


@ui.put("/settings")
def put_settings(body: dict):
    for k, v in (body or {}).items():
        db.set_setting(k, v)
    if "auto_buy" in (body or {}):
        events.log(None, "settings", f"AUTO-BUY turned {'ON' if body['auto_buy'] else 'OFF'}",
                   level="warn" if body["auto_buy"] else "info")
    return {"ok": True}


# ======================= playbooks =======================================
@ui.get("/playbooks")
def list_playbooks(merchant: str = ""):
    sql = "SELECT id,merchant,version,created_at,status,confidence,smoke_status,smoke_detail,generated_by FROM playbooks"
    args: list = []
    if merchant:
        sql += " WHERE merchant=?"; args.append(merchant)
    sql += " ORDER BY merchant, created_at DESC"
    return db.q(sql, args)


@ui.get("/playbooks/coverage")
def playbook_coverage():
    return playbooks.coverage()


@ui.get("/playbooks/{pb_id}")
def get_playbook(pb_id: int):
    row = db.q1("SELECT * FROM playbooks WHERE id=?", (pb_id,))
    if not row:
        raise HTTPException(404, "no such playbook")
    return _j(row, "json")


@ui.post("/playbooks/{pb_id}/promote")
def promote_playbook(pb_id: int):
    playbooks.promote(pb_id)
    events.log(None, "playbook", f"playbook {pb_id} promoted to active", level="ok")
    return {"ok": True}


@ui.post("/playbooks/rollback/{merchant}")
def rollback_playbook(merchant: str):
    prev = playbooks.rollback(merchant)
    if not prev:
        raise HTTPException(400, "no earlier version to roll back to")
    events.log(None, "playbook", f"{merchant} rolled back to {prev['version']}", level="warn")
    return {"ok": True, "restored": prev["version"]}


# ======================= orders & inventory ==============================
@ui.get("/orders")
def list_orders(status: str = "", limit: int = 200):
    sql = "SELECT * FROM orders WHERE 1=1"
    args: list = []
    if status:
        sql += " AND status=?"; args.append(status)
    sql += " ORDER BY created_at DESC LIMIT ?"; args.append(min(limit, 500))
    return db.q(sql, args)


@ui.get("/orders/failed")
def failed_orders(limit: int = 100):
    return db.q("SELECT o.*, d.url, d.product_name AS deal_name FROM orders o "
                "LEFT JOIN deals d ON d.id=o.deal_id WHERE o.status='failed' "
                "ORDER BY o.created_at DESC LIMIT ?", (limit,))


@ui.get("/inventory")
def inventory():
    rows = db.q("""SELECT o.*, d.category, d.size, d.market_price, d.url
                   FROM orders o LEFT JOIN deals d ON d.id=o.deal_id
                   WHERE o.status IN ('placed','pending') ORDER BY o.created_at DESC""")
    delivered = [r for r in rows if r["ship_status"] == "delivered"]
    incoming = [r for r in rows if r["ship_status"] != "delivered"]
    def val(rs, key="market_price"):
        return round(sum(float(r[key] or 0) * int(r["qty"] or 0) for r in rs), 2)
    return {
        "delivered": delivered, "incoming": incoming,
        "units_delivered": sum(int(r["qty"] or 0) for r in delivered),
        "units_incoming": sum(int(r["qty"] or 0) for r in incoming),
        "cost_delivered": round(sum(float(r["total"] or 0) for r in delivered), 2),
        "cost_incoming": round(sum(float(r["total"] or 0) for r in incoming), 2),
        "market_delivered": val(delivered), "market_incoming": val(incoming),
    }


@ui.put("/orders/{order_id}")
def edit_order(order_id: int, body: dict):
    allowed = {"status", "order_number", "carrier", "tracking_number", "ship_status", "eta",
               "failure_reason", "failure_stage", "notes"}
    db.update("orders", order_id, {k: v for k, v in body.items() if k in allowed})
    return {"ok": True}


@ui.get("/orders/open-tracking")
def open_tracking():
    """Exactly what the ChatGPT shipping task should ask about."""
    rows = db.q("""SELECT id, merchant, order_number, carrier, tracking_number, ship_status,
                          product_name, qty, total, placed_at, eta
                   FROM orders
                   WHERE status IN ('placed','pending')
                     AND (ship_status IS NULL OR ship_status NOT IN ('delivered','cancelled'))
                   ORDER BY placed_at""")
    return {"generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "orders": rows}


# ======================= ChatGPT task prompts ============================
@ui.get("/prompts")
def list_prompts(request: Request):
    from pathlib import Path
    base = public_base(request)
    out = []
    for f in sorted((config.ROOT / "prompts").glob("*.md")):
        text = f.read_text()
        text = (text.replace("{{BASE_URL}}", base)
                    .replace("{{INGEST_TOKEN}}", config.INGEST_TOKEN or "<set INGEST_TOKEN>"))
        title = next((l.lstrip("# ").strip() for l in text.splitlines() if l.startswith("# ")), f.stem)
        out.append({"file": f.name, "title": title, "text": text})
    return out


# ======================= scheduled tasks & sessions ======================
TASK_EXPECTATIONS = {
    "recon":    {"label": "Nightly checkout recon", "every_hours": 24,
                 "prompt": "01_checkout_recon.md"},
    "shipping": {"label": "Order tracking", "every_hours": 4,
                 "prompt": "02_shipping_tracker.md"},
    "comps":    {"label": "Market comps", "every_hours": 24,
                 "prompt": "03_market_comps.md"},
    "discord":  {"label": "Discord relay", "every_hours": 0, "prompt": ""},
}


@ui.get("/tasks")
def list_tasks():
    """Health of each scheduled task: did it run, when, and did it work."""
    now = time.time()
    out = []
    for key, meta in TASK_EXPECTATIONS.items():
        last = db.q1("SELECT * FROM task_runs WHERE task=? ORDER BY ts DESC LIMIT 1", (key,))
        runs = db.q("SELECT * FROM task_runs WHERE task=? ORDER BY ts DESC LIMIT 25", (key,))
        for r in runs:
            _j(r, "errors", "detail")
        age_h = (now - last["ts"]) / 3600 if last else None
        due = meta["every_hours"]
        health = ("never" if not last else
                  "failing" if last["status"] in ("rejected", "error") else
                  "late" if due and age_h and age_h > due * 1.5 else
                  "due" if due and age_h and age_h > due else "ok")
        out.append({"task": key, **meta, "last": _j(last, "errors", "detail") if last else None,
                    "age_hours": round(age_h, 1) if age_h is not None else None,
                    "health": health, "runs": runs,
                    "run_count_7d": (db.q1(
                        "SELECT COUNT(*) n FROM task_runs WHERE task=? AND ts>?",
                        (key, now - 7 * 86400)) or {}).get("n", 0)})
    return out


@ui.get("/sessions")
def list_sessions():
    rows = db.q("""SELECT m.domain, m.name, m.login_required, s.signed_in, s.has_credentials,
                          s.last_login, s.last_checked, s.needs_manual_login, s.note
                   FROM merchants m LEFT JOIN merchant_sessions s ON s.merchant=m.domain
                   ORDER BY m.domain""")
    for r in rows:
        r["status"] = ("manual_login_needed" if r["needs_manual_login"] else
                       "signed_in" if r["signed_in"] else
                       "no_credentials" if not r["has_credentials"] else
                       "signed_out" if r["last_checked"] else "unknown")
    return rows


class SessionIn(BaseModel):
    merchant: str
    signed_in: bool = False
    has_credentials: bool = False
    needs_manual_login: bool = False
    note: str = ""


@work.post("/worker/sessions")
def report_sessions(items: list[SessionIn]):
    """The worker tells the dashboard which merchants it is still signed in to.
    Credentials themselves never leave the worker's machine."""
    now = time.time()
    for it in items:
        db.execute(
            "INSERT INTO merchant_sessions(merchant,signed_in,has_credentials,last_login,"
            "last_checked,needs_manual_login,note) VALUES(?,?,?,?,?,?,?) "
            "ON CONFLICT(merchant) DO UPDATE SET signed_in=excluded.signed_in,"
            "has_credentials=excluded.has_credentials,last_checked=excluded.last_checked,"
            "needs_manual_login=excluded.needs_manual_login,note=excluded.note,"
            "last_login=CASE WHEN excluded.signed_in=1 AND merchant_sessions.signed_in=0 "
            "THEN excluded.last_checked ELSE merchant_sessions.last_login END",
            (it.merchant.lower(), 1 if it.signed_in else 0, 1 if it.has_credentials else 0,
             now if it.signed_in else None, now, 1 if it.needs_manual_login else 0, it.note))
        if it.needs_manual_login:
            events.log(None, "session",
                       f"{it.merchant} needs a manual sign-in — run "
                       f"`python3 worker/run_worker.py --login {it.merchant}`", level="warn")
    return {"ok": True, "updated": len(items)}


@ui.get("/worker/jobs")
def worker_jobs(limit: int = 100, state: str = ""):
    sql = ("SELECT j.*, d.product_name, d.merchant, d.decision FROM job_queue j "
           "LEFT JOIN deals d ON d.id=j.deal_id WHERE 1=1")
    args: list = []
    if state:
        sql += " AND j.state=?"; args.append(state)
    sql += " ORDER BY j.created_at DESC LIMIT ?"; args.append(min(limit, 400))
    return [_j(r, "payload", "result") for r in db.q(sql, args)]


# ======================= worker ==========================================
@work.get("/worker/next")
def worker_next(worker_id: str = "local"):
    """Lease the newest still-fresh job.

    Newest first, not oldest: if the worker was offline for a few hours, the
    useful deal is the one posted two minutes ago, not the backlog. Anything
    older than the TTL is expired rather than bought — a price error from this
    morning is not a price error any more.
    """
    now = time.time()
    db.execute("UPDATE job_queue SET state='queued' WHERE state='leased' AND leased_at<?",
               (now - 300,))

    ttl = float(db.get_setting("job_ttl_seconds", 900) or 900)
    for old in db.q("SELECT * FROM job_queue WHERE state='queued' AND created_at < ?",
                    (now - ttl,)):
        db.update("job_queue", old["id"], {
            "state": "expired",
            "result": json.dumps({"reason": f"older than {ttl / 60:.0f}m — worker was offline"})})
        if old["kind"] == "place":
            events.log(old["deal_id"], "checkout",
                       f"not bought — this sat in the queue for "
                       f"{(now - old['created_at']) / 60:.0f} minutes while the worker was offline",
                       level="warn")
            db.execute("UPDATE deals SET status='expired' WHERE id=? AND status='ordering'",
                       (old["deal_id"],))

    row = db.q1("SELECT * FROM job_queue WHERE state='queued' ORDER BY "
                "CASE kind WHEN 'place' THEN 0 WHEN 'prep' THEN 1 ELSE 2 END, "
                "created_at DESC LIMIT 1")
    if not row:
        return {"job": None}
    won = db.execute_count(
        "UPDATE job_queue SET state='leased', leased_by=?, leased_at=?, attempts=attempts+1 "
        "WHERE id=? AND state='queued'", (worker_id, time.time(), row["id"]))
    if not won:                      # another worker grabbed it first
        return {"job": None}
    job = _j(row, "payload")
    deal = db.q1("SELECT * FROM deals WHERE id=?", (row["deal_id"],)) if row["deal_id"] else None
    pb = None
    if deal and deal.get("merchant"):
        active = playbooks.get_active(deal["merchant"])
        pb = active["playbook"] if active else None
    return {"job": job, "deal": deal, "playbook": pb}


class JobResult(BaseModel):
    state: str = "done"                 # done | failed
    stage: str = ""
    message: str = ""
    order_number: str | None = None
    charges: list[dict] = Field(default_factory=list)
    screenshot: str | None = None
    data: dict = Field(default_factory=dict)


@work.post("/worker/jobs/{job_id}/result")
def worker_result(job_id: int, body: JobResult):
    job = db.q1("SELECT * FROM job_queue WHERE id=?", (job_id,))
    if not job:
        raise HTTPException(404, "no such job")
    db.update("job_queue", job_id, {"state": body.state, "result": json.dumps(body.model_dump())})
    deal_id = job["deal_id"]
    deal = db.q1("SELECT * FROM deals WHERE id=?", (deal_id,)) if deal_id else None
    payload = json.loads(job["payload"] or "{}")

    if job["kind"] == "prep":
        events.log(deal_id, "checkout",
                   body.message or ("cart ready at review page" if body.state == "done"
                                    else f"cart prep failed at {body.stage}"),
                   level="ok" if body.state == "done" else "warn", **body.data)
        return {"ok": True}

    if body.state == "done":
        for ch in (body.charges or payload.get("charges", [])):
            oid = db.insert("orders", {
                "deal_id": deal_id, "created_at": time.time(),
                "merchant": deal["merchant"] if deal else payload.get("merchant"),
                "product_name": deal["product_name"] if deal else "",
                "card_id": ch.get("card_id"), "card_label": ch.get("card_label"),
                "qty": ch.get("qty"), "unit_price": ch.get("unit_price"),
                "total": ch.get("amount"), "shipping_method": ch.get("shipping_method"),
                "status": "placed", "order_number": ch.get("order_number") or body.order_number,
                "placed_at": time.time(), "ship_status": "placed",
                "raw": json.dumps(body.data)})
            left = cards_mod.commit_charge(ch.get("card_id"), float(ch.get("amount") or 0))
            events.log(deal_id, "order",
                       f"ORDER PLACED {ch.get('order_number') or body.order_number or ''} — "
                       f"{ch.get('qty')}× on {ch.get('card_label')} ${float(ch.get('amount') or 0):,.2f} "
                       f"(${left:,.2f} left on that card)", level="ok", order_id=oid)
        if deal:
            db.update("deals", deal_id, {"status": "ordered"})
    else:
        db.insert("orders", {
            "deal_id": deal_id, "created_at": time.time(),
            "merchant": deal["merchant"] if deal else payload.get("merchant"),
            "product_name": deal["product_name"] if deal else "",
            "qty": payload.get("qty"), "unit_price": payload.get("max_price"),
            "total": 0, "status": "failed", "failure_stage": body.stage,
            "failure_reason": body.message, "raw": json.dumps(body.data)})
        if deal:
            db.update("deals", deal_id, {"status": "failed", "error": body.message})
        events.log(deal_id, "order", f"ORDER FAILED at {body.stage}: {body.message}", level="error",
                   **body.data)
        if body.data.get("selector_missing") and deal:
            events.log(deal_id, "playbook",
                       f"{deal['merchant']} playbook looks out of date "
                       f"(missing '{body.data['selector_missing']}') — rolling back",
                       level="error")
            playbooks.rollback(deal["merchant"])
    return {"ok": True}


@work.post("/worker/heartbeat")
async def worker_heartbeat(request: Request):
    """The worker posts its whole state here every 30s."""
    try:
        body = await request.json()
    except Exception:
        body = {}
    if not isinstance(body, dict) or not body:
        body = dict(request.query_params)
    prev = db.get_setting("worker_last_seen", {}) or {}
    body["ts"] = time.time()
    body.setdefault("worker_id", "local")
    if prev.get("state") != "running" and body.get("state") == "running":
        events.log(None, "worker",
                   f"worker {body.get('worker_id')} v{body.get('version', '?')} connected "
                   f"({'dry run' if body.get('dry_run') else 'LIVE'})", level="ok")
    if body.get("state") == "stopping":
        events.log(None, "worker", f"worker {body.get('worker_id')} shut down", level="warn")
    db.set_setting("worker_last_seen", body)
    return {"ok": True, "server_time": time.time()}


@ui.get("/worker/status")
def worker_status():
    hb = db.get_setting("worker_last_seen", None) or None
    age = (time.time() - hb["ts"]) if hb and hb.get("ts") else None
    online = bool(age is not None and age < 120)
    queue = db.q("SELECT kind, state, COUNT(*) n FROM job_queue GROUP BY kind, state")
    waiting = sum(q["n"] for q in queue if q["state"] == "queued")
    day = time.time() - 86400
    return {
        "heartbeat": hb, "online": online,
        "seconds_since_seen": round(age) if age is not None else None,
        "state": ("online" if online else
                  "stopped" if hb and hb.get("state") == "stopping" else
                  "offline" if hb else "never_connected"),
        "queue": queue, "waiting": waiting,
        "jobs_24h": (db.q1("SELECT COUNT(*) n FROM job_queue WHERE created_at>?", (day,)) or {}).get("n", 0),
        "failed_24h": (db.q1("SELECT COUNT(*) n FROM job_queue WHERE created_at>? AND state='failed'",
                             (day,)) or {}).get("n", 0),
        "expired_24h": (db.q1("SELECT COUNT(*) n FROM job_queue WHERE created_at>? AND state='expired'",
                              (day,)) or {}).get("n", 0),
    }


# ======================= setup =========================================
@ui.post("/setup/link")
def setup_link(request: Request):
    """A one-line install command, valid for 30 minutes."""
    from ..security import make_setup_token
    base = public_base(request)
    token = make_setup_token(30)
    return {"command": f'curl -fsSL "{base}/api/worker/install.sh?t={token}" | bash',
            "expires_in_minutes": 30, "base": base}


@router.get("/worker/install.sh")
def worker_install_script(request: Request, t: str = ""):
    """Serves the macOS installer with this server's URL and worker token baked in.
    Reachable with a short-lived setup token or an admin session."""
    from fastapi.responses import PlainTextResponse
    from ..security import valid_setup_token
    if not (valid_setup_token(t) or read_session_ok(request)):
        raise HTTPException(401, "this setup link has expired — generate a new one "
                                 "from the dashboard's Setup page")
    base = public_base(request)
    script = (config.ROOT / "worker" / "install_mac.sh").read_text()
    header = "\n".join([
        "#!/usr/bin/env bash",
        f"# Generated by Deal Desk for {base}",
        f"export DEALDESK_URL={base!r}",
        f"export WORKER_TOKEN={config.WORKER_TOKEN!r}",
        f"export WORKER_BUNDLE_URL={base + '/api/worker/bundle.tar.gz'!r}",
        "",
    ])
    body = script.split("set -euo pipefail", 1)[1]
    return PlainTextResponse(header + "set -euo pipefail" + body,
                             headers={"Cache-Control": "no-store"})


@router.get("/worker/bundle.tar.gz")
def worker_bundle(request: Request, t: str = ""):
    """The worker source, so the installer doesn't need a git clone."""
    import io
    import tarfile
    from fastapi.responses import Response
    from ..security import valid_setup_token
    auth = request.headers.get("authorization", "")
    token_ok = (config.WORKER_TOKEN and auth == f"Bearer {config.WORKER_TOKEN}")
    if not (token_ok or valid_setup_token(t) or read_session_ok(request)):
        raise HTTPException(401, "not authorised")
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name in ("run_worker.py", "executor.py", "vault.py", "vault_server.py",
                     "vault.example.json", "install_mac.sh"):
            path = config.ROOT / "worker" / name
            if path.exists():
                tar.add(path, arcname=name)
    return Response(buf.getvalue(), media_type="application/gzip",
                    headers={"Cache-Control": "no-store"})


def read_session_ok(request: Request) -> bool:
    from ..security import COOKIE, read_session
    return bool(read_session(request.cookies.get(COOKIE)))


@ui.get("/setup/status")
def setup_status(request: Request):
    """The onboarding checklist — what's configured and what still isn't."""
    hb = db.get_setting("worker_last_seen", {}) or {}
    worker_online = bool(hb.get("ts") and time.time() - hb["ts"] < 120)
    cards = db.q("SELECT id, available FROM cards WHERE enabled=1")
    merchants = db.q("SELECT domain FROM merchants WHERE trusted=1")
    live_pb = db.q("SELECT DISTINCT merchant FROM playbooks WHERE status='active'")
    recon = db.q1("SELECT ts FROM task_runs WHERE task='recon' ORDER BY ts DESC LIMIT 1")
    shipn = db.q1("SELECT ts FROM task_runs WHERE task='shipping' ORDER BY ts DESC LIMIT 1")
    disc = db.q1("SELECT ts FROM task_runs WHERE task='discord' ORDER BY ts DESC LIMIT 1")
    sessions_needed = db.q("SELECT domain FROM merchants WHERE login_required=1")
    signed_in = db.q("SELECT merchant FROM merchant_sessions WHERE signed_in=1")
    deals = (db.q1("SELECT COUNT(*) n FROM deals WHERE is_test=0") or {}).get("n", 0)

    steps = [
        {"key": "merchants", "title": "Choose which merchants you'll buy from",
         "done": len(merchants) > 0, "detail": f"{len(merchants)} trusted",
         "href": "/merchants", "cta": "Open Merchants"},
        {"key": "cards", "title": "Add your cards and credit limits",
         "done": len(cards) > 0, "detail": f"{len(cards)} card(s) enabled" if cards else "none yet",
         "href": "/cards", "cta": "Add a card"},
        {"key": "filters", "title": "Set your buying filters",
         "done": bool(db.q1("SELECT id FROM filters WHERE active=1")),
         "detail": "defaults are in place — review them", "href": "/filters", "cta": "Review filters"},
        {"key": "worker", "title": "Install the checkout worker on your Mac",
         "done": worker_online, "detail": (f"{hb.get('worker_id')} online" if worker_online
                                           else "not connected"),
         "href": "/setup", "cta": "Get the install command"},
        {"key": "tasks", "title": "Schedule the ChatGPT tasks",
         "done": bool(recon and shipn),
         "detail": ("recon + tracking reporting" if recon and shipn else
                    "recon only" if recon else "tracking only" if shipn else "neither has reported"),
         "href": "/settings", "cta": "Copy the prompts"},
        {"key": "playbooks", "title": "Get a checkout playbook for each merchant",
         "done": len(live_pb) > 0,
         "detail": f"{len(live_pb)} of {len(merchants)} merchants mapped",
         "href": "/playbooks", "cta": "Open playbooks"},
        {"key": "logins", "title": "Sign in to merchants that need an account",
         "done": (not sessions_needed) or len(signed_in) >= len(sessions_needed),
         "detail": (f"{len(signed_in)}/{len(sessions_needed)} signed in" if sessions_needed
                    else "no merchant needs a login yet"),
         "href": "/merchants", "cta": "Check sign-ins"},
        {"key": "discord", "title": "Point your Discord relay at the ingest endpoint",
         "done": bool(disc), "detail": "a message has arrived" if disc else "nothing received yet",
         "href": "/settings", "cta": "See the endpoint"},
    ]
    done = sum(1 for s in steps if s["done"])
    return {"steps": steps, "done": done, "total": len(steps),
            "complete": done == len(steps), "has_deals": deals > 0,
            "auto_buy": bool(db.get_setting("auto_buy", config.AUTO_BUY))}


router.include_router(ingest)
router.include_router(ui)
router.include_router(work)
