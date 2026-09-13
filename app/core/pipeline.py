"""The hot path.

A message arrives. Within a few milliseconds we know the URL, whether the domain
is real, and roughly what the product is. Then two tracks run *at the same time*:

    research track  ──►  fetch page ──► category/size/demand/market ──► decision
    checkout track  ──►  worker opens the page, adds to cart, fills shipping +
                         payment, and stops one click short of paying

Whichever finishes first waits for the other. If the decision is BUY the cart is
already sitting at the review page and the worker only has to click. If it's
SKIP or HOLD the cart gets abandoned. That's where the seconds come from.
"""
from __future__ import annotations

import asyncio
import json
import time

from .. import config, db
from . import cards as cards_mod
from . import events, playbooks
from . import shipping as shipping_mod
from .decision import active_filters, evaluate
from .links import SHORTENERS, extract_urls, slug_guess
from .product import fetch_product, merchant_of, resolve_redirects
from .research import research
from .trust import check_url


def known_domains() -> set[str]:
    return {r["domain"] for r in db.q("SELECT domain FROM merchants WHERE trusted=1")}


def _shorteners() -> set[str]:
    extra = db.get_setting("extra_shorteners", []) or []
    return SHORTENERS | set(extra)


async def process_message(raw_message: str, source: str = "discord",
                          url_override: str | None = None, dry_run: bool = False) -> list[dict]:
    """Handle one chat message. Returns one result per link found."""
    urls = [url_override] if url_override else extract_urls(raw_message)
    if not urls:
        events.log(None, "intake", "message had no links", level="warn",
                   message_preview=raw_message[:160])
        return []
    return list(await asyncio.gather(
        *[process_link(u, raw_message, source, dry_run=dry_run) for u in urls]))


async def process_link(url: str, raw_message: str = "", source: str = "discord",
                       dry_run: bool = False) -> dict:
    """dry_run: run every stage and record the result, but never touch a cart or
    a card. This is what the Test bench uses."""
    t0 = time.perf_counter()

    # ---- t+0ms: name guess + trust, both pure CPU ------------------------
    trust = check_url(url, known_domains(), _shorteners())
    instant_name = slug_guess(url)
    merchant = trust.registrable

    deal_id = db.insert("deals", {
        "created_at": time.time(), "source": source, "raw_message": raw_message[:2000],
        "url": url, "merchant": merchant, "trust_verdict": trust.verdict,
        "trust_score": trust.score, "trust_reasons": json.dumps(trust.reasons),
        "product_name": instant_name, "status": "new", "decision": "PENDING",
        "is_test": 1 if dry_run else 0,
    })
    events.log(deal_id, "intake", f"link from {source}: {url}", url=url)
    events.log(deal_id, "trust", f"{trust.verdict} ({trust.score:.2f}) — {merchant}",
               level="ok" if trust.verdict == "TRUSTED" else
               "error" if trust.verdict == "BLOCKED" else "warn",
               reasons=trust.reasons, impersonates=trust.impersonates)
    if instant_name:
        events.log(deal_id, "product", f"instant name from URL: “{instant_name}”")

    # ---- shorteners: resolve before trusting anything --------------------
    if trust.needs_resolution:
        final_url, chain = await resolve_redirects(url)
        if final_url != url:
            trust = check_url(final_url, known_domains(), _shorteners())
            merchant = trust.registrable
            db.update("deals", deal_id, {
                "final_url": final_url, "merchant": merchant, "trust_verdict": trust.verdict,
                "trust_score": trust.score, "trust_reasons": json.dumps(trust.reasons)})
            events.log(deal_id, "trust", f"shortener resolved → {final_url} ({trust.verdict})",
                       level="ok" if trust.verdict == "TRUSTED" else "warn", chain=chain)
            url = final_url

    if trust.verdict == "BLOCKED":
        db.update("deals", deal_id, {
            "status": "skipped", "decision": "BLOCKED",
            "decision_reasons": json.dumps(trust.reasons),
            "latency_ms": int((time.perf_counter() - t0) * 1000)})
        events.log(deal_id, "decision", "BLOCKED — unsafe link, nothing was fetched", level="error",
                   reasons=trust.reasons)
        return _deal_out(deal_id)

    # ---- the two parallel tracks ----------------------------------------
    db.update("deals", deal_id, {"status": "researching"})
    filters = active_filters()
    prep_task = asyncio.create_task(
        _prepare_checkout(deal_id, url, merchant, filters, dry_run=dry_run))
    research_task = asyncio.create_task(_research_track(deal_id, url, merchant, filters))

    decision, facts, rsr = await research_task
    prep = await prep_task

    # ---- money: which cards, how many units ------------------------------
    plan = None
    if decision.decision == "BUY":
        plan = cards_mod.plan_purchase(
            merchant, decision.unit_price, decision.qty,
            tax_rate=float(filters.get("tax_rate", 0)),
            daily_cap=float(db.get_setting("daily_spend_cap", config.MAX_SPEND_PER_DAY) or 0))
        if plan.qty_planned == 0:
            decision.decision = "HOLD"
            decision.failed.append(plan.shortfall_reason or "no card could cover this")
            events.log(deal_id, "payment", f"HOLD — {plan.shortfall_reason}", level="warn",
                       considered=plan.considered)
        else:
            decision.qty = plan.qty_planned
            decision.est_total = round(plan.total, 2)
            events.log(deal_id, "payment",
                       " + ".join(f"{c.qty}× on {c.card_label} (${c.amount:,.2f})" for c in plan.charges),
                       level="ok", plan=plan.to_dict())
            pb_active = playbooks.get_active(merchant)
            ship_opts = (pb_active or {}).get("playbook", {}).get("shipping_options", {})
            ship = shipping_mod.choose(merchant, ship_opts, order_value=decision.est_total)
            events.log(deal_id, "shipping",
                       f"{ship.label or 'site default'} — {ship.reason}",
                       level="warn" if ship.fallback else "ok", choice=ship.to_dict())
            if plan.shortfall_reason:
                events.log(deal_id, "payment", plan.shortfall_reason, level="warn")

    latency = int((time.perf_counter() - t0) * 1000)
    db.update("deals", deal_id, {
        "product_name": facts.name or instant_name, "brand": facts.brand, "sku": facts.sku or facts.gtin,
        "size": rsr.size, "category": rsr.category, "listed_price": facts.price,
        "currency": facts.currency, "market_price": rsr.market_price,
        "market_price_source": rsr.market_price_source, "demand_score": rsr.demand_score,
        "returnable": 1 if rsr.returnable else 0, "return_window_days": rsr.return_window_days,
        "discount_pct": decision.discount_pct, "est_profit_unit": decision.est_profit_unit,
        "score": decision.score, "decision": decision.decision,
        "decision_reasons": json.dumps({"passed": decision.passed, "failed": decision.failed,
                                        "notes": decision.notes}),
        "target_qty": decision.qty, "latency_ms": latency,
        "raw_product": json.dumps(facts.to_dict(), default=str),
        "status": {"BUY": "ready", "HOLD": "hold", "SKIP": "skipped",
                   "BLOCKED": "skipped"}[decision.decision],
    })
    events.log(deal_id, "decision",
               f"{decision.decision} · score {decision.score:.0f} · "
               f"{decision.qty}× ${decision.unit_price:,.2f} = ${decision.est_total:,.2f} · {latency}ms",
               level="ok" if decision.decision == "BUY" else
                     "warn" if decision.decision == "HOLD" else "info",
               passed=decision.passed, failed=decision.failed, notes=decision.notes)

    # ---- hand off to the worker -----------------------------------------
    auto_buy = bool(db.get_setting("auto_buy", config.AUTO_BUY))
    if dry_run:
        events.log(deal_id, "checkout",
                   "DRY RUN — nothing was added to a cart and no card was touched", level="warn")
    elif decision.decision == "BUY" and plan and plan.charges:
        if auto_buy:
            _enqueue(deal_id, "place", {
                "url": url, "merchant": merchant, "qty": decision.qty, "size": rsr.size,
                "max_price": decision.unit_price * 1.05,
                "charges": [c.to_dict() for c in plan.charges],
                "shipping": ship.to_dict(),
                "prep_job_id": prep.get("job_id"),
            })
            events.log(deal_id, "checkout", "queued for purchase — worker will place the order",
                       level="ok")
            db.update("deals", deal_id, {"status": "ordering"})
        else:
            events.log(deal_id, "checkout",
                       "AUTO-BUY is OFF — cart is prepared, press Buy on the deal page to finish",
                       level="warn")
    elif prep.get("job_id"):
        db.execute("UPDATE job_queue SET state='done', result=? WHERE id=? AND state IN ('queued','leased')",
                   (json.dumps({"abandoned": decision.decision}), prep["job_id"]))
        events.log(deal_id, "checkout", f"cart abandoned ({decision.decision})")

    return _deal_out(deal_id)


async def _research_track(deal_id: int, url: str, merchant: str, filters: dict):
    facts = await fetch_product(url)
    events.log(deal_id, "product",
               f"“{facts.name}” · {facts.brand or 'no brand'} · "
               f"${facts.price if facts.price is not None else '?'} · {facts.availability} "
               f"· via {facts.source} in {facts.fetch_ms}ms",
               level="warn" if facts.blocked or facts.price is None else "ok",
               facts=facts.to_dict())
    for n in facts.notes:
        events.log(deal_id, "product", n, level="warn")

    rsr = research(facts, merchant)
    events.log(deal_id, "research",
               f"{rsr.category} · size {rsr.size or 'n/a'} · demand {rsr.demand_score:.2f} · "
               f"market ${rsr.market_price if rsr.market_price else '?'} ({rsr.market_price_source}) · "
               f"returns {rsr.return_window_days}d",
               demand=rsr.demand_reasons, returns=rsr.return_notes, elapsed_ms=rsr.elapsed_ms)

    trust = check_url(url, known_domains(), _shorteners())
    decision = evaluate(facts, rsr, trust, merchant, filters)
    return decision, facts, rsr


async def _prepare_checkout(deal_id: int, url: str, merchant: str, filters: dict,
                            dry_run: bool = False) -> dict:
    """Fire the prep job the instant we know the domain is safe — this runs while
    research is still going."""
    pb = playbooks.get_active(merchant)
    if not pb:
        events.log(deal_id, "checkout",
                   f"no live playbook for {merchant} — nightly recon task hasn't mapped it yet",
                   level="warn")
        return {"ready": False, "reason": "no playbook"}
    if dry_run:
        events.log(deal_id, "checkout",
                   f"dry run — would have prepped the cart with playbook {pb['version']}")
        return {"ready": False, "reason": "dry run"}
    job_id = _enqueue(deal_id, "prep", {
        "url": url, "merchant": merchant, "playbook_id": pb["id"],
        "qty": int(filters.get("max_qty_per_deal", 1)),
        "stop_at": "review",
    })
    events.log(deal_id, "checkout",
               f"cart prep started in parallel using playbook {pb['version']} "
               f"(conf {pb['confidence']:.2f}) — will stop at the review page",
               level="ok", job_id=job_id)
    return {"ready": True, "job_id": job_id, "playbook_id": pb["id"]}


def _enqueue(deal_id: int, kind: str, payload: dict) -> int:
    return db.insert("job_queue", {
        "created_at": time.time(), "deal_id": deal_id, "kind": kind,
        "payload": json.dumps(payload), "state": "queued"})


def _deal_out(deal_id: int) -> dict:
    row = db.q1("SELECT * FROM deals WHERE id=?", (deal_id,)) or {}
    for k in ("trust_reasons", "decision_reasons", "raw_product"):
        if row.get(k):
            try:
                row[k] = json.loads(row[k])
            except Exception:
                pass
    return row
