"""Fill the database with realistic-looking data so you can see the dashboard
working before a single real deal comes in.   python3 tools/seed_demo.py
Wipes nothing except previous demo rows."""
import json
import random
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app import db
from app.core import events, playbooks

db.init_db()
now = time.time()

# ---- cards -------------------------------------------------------------
if not db.q1("SELECT id FROM cards"):
    ids = {}
    for nick, net, l4, lim, avail, prio, day in [
        ("Amex Gold", "amex", "1003", 12000, 1010, 10, 14),
        ("Chase Sapphire", "visa", "4421", 9000, 3310, 20, 3),
        ("Citi Double Cash", "mastercard", "7788", 15000, 15000, 30, 22),
        ("Discover it", "discover", "9012", 4000, 4000, 40, 8),
    ]:
        ids[nick] = db.insert("cards", {
            "nickname": nick, "network": net, "last4": l4, "credit_limit": lim,
            "available": avail, "priority": prio, "statement_day": day, "enabled": 1,
            "max_per_txn": 0, "daily_cap": 0, "updated_at": now})
    db.insert("card_rules", {"merchant": "bestbuy.com", "card_id": ids["Chase Sapphire"],
                             "rank": 0, "enabled": 1, "shipping_method": "Free 2-day"})
    db.insert("card_rules", {"merchant": "target.com", "card_id": ids["Citi Double Cash"],
                             "rank": 0, "enabled": 1, "shipping_method": "Standard"})
    db.insert("card_rules", {"merchant": "*", "card_id": ids["Citi Double Cash"],
                             "rank": 0, "enabled": 1, "shipping_method": ""})

# ---- playbooks ---------------------------------------------------------
def demo_playbook(domain, hours_old, conf=0.9):
    return {
        "merchant": domain,
        "captured_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now - hours_old * 3600)),
        "schema_version": "1.1",
        "confidence": conf, "generated_by": "chatgpt-recon-task",
        "site_facts": {"guest_checkout": domain != "bestbuy.com", "max_qty_per_order": 3,
                       "cvv_required_at_checkout": True, "payment_in_iframe": domain == "walmart.com",
                       "requires_login": domain in ("bestbuy.com", "target.com"),
                       "cart_url": f"https://www.{domain}/cart"},
        "selectors": {"add_to_cart": {"test_id": "add-to-cart"}, "checkout_btn": {"role": "button", "label": "Checkout"},
                      "card_number": {"css": "#cc-number"}, "cvv_input": {"css": "#cc-cvv"},
                      "place_order": {"role": "button", "label": "Place order"},
                      "order_number": {"css": ".order-confirmation-number"},
                      "order_total": {"css": "[data-test='orderTotal']"},
                      "email_input": {"css": "#email"}, "password_input": {"css": "#password"},
                      "signin_btn": {"role": "button", "label": "Sign in"},
                      "account_menu": {"css": "[data-test='accountName']"},
                      "ship_standard": {"text": "Standard"}, "ship_2day": {"text": "2-day"},
                      "ship_overnight": {"text": "Next day"}, "ship_pickup": {"text": "Store pickup"}},
        "shipping_options": {
            "standard":  {"label": "Standard (free, 3-5 days)", "cost": 0, "days": 4, "selector": "ship_standard"},
            "two_day":   {"label": "2-day", "cost": 9.99, "days": 2, "selector": "ship_2day"},
            "overnight": {"label": "Next day", "cost": 24.99, "days": 1, "selector": "ship_overnight"},
            "pickup":    {"label": "Store pickup", "cost": 0, "days": 0, "selector": "ship_pickup",
                          "pickup": True}},
        "session_check": {"signed_in": {"selector": "account_menu"},
                          "signed_out": {"url_contains": "/signin"}},
        "steps": {"login": [{"action": "fill", "target": "email_input", "value": "{{account.email}}"},
                            {"action": "fill", "target": "password_input", "value": "{{account.password}}"},
                            {"action": "click", "target": "signin_btn"},
                            {"action": "wait_for", "target": "account_menu"}],
                  "add_to_cart": [{"action": "goto", "url": "{{deal.url}}"},
                                  {"action": "wait_for", "target": "add_to_cart"},
                                  {"action": "click", "target": "add_to_cart"}],
                  "checkout_start": [{"action": "click", "target": "checkout_btn"}],
                  "payment": [{"action": "fill", "target": "card_number", "value": "{{card.number}}"},
                              {"action": "fill", "target": "cvv_input", "value": "{{card.cvv}}"}],
                  "review": [{"action": "assert_text", "target": "order_total", "contains": "$"}],
                  "place_order": [{"action": "click", "target": "place_order"},
                                  {"action": "wait_for", "target": "order_number"},
                                  {"action": "extract", "target": "order_number", "as": "order_number"}]},
        "success_signals": {"order_confirmed": {"css": ".order-confirmation-number"}},
        "failure_signals": [{"name": "out_of_stock", "text": "Sold out", "meaning": "gone before checkout"},
                            {"name": "card_declined", "text": "declined", "meaning": "try the next card"}],
    }

if not db.q1("SELECT id FROM playbooks"):
    for dom, age, conf in [("target.com", 6, 0.94), ("bestbuy.com", 7, 0.88),
                           ("walmart.com", 31, 0.79), ("homedepot.com", 9, 0.91),
                           ("rei.com", 52, 0.7)]:
        r = playbooks.save(demo_playbook(dom, age, conf))
        if r.get("accepted"):
            db.execute("UPDATE playbooks SET created_at=? WHERE id=?", (now - age * 3600, r["id"]))

# ---- deals + orders ----------------------------------------------------
DEMO = [
    # name, brand, merchant, cat, size, price, market, list, decision, trust, qty, score
    ("Sony WH-1000XM5 Wireless Headphones – Black", "Sony", "target.com", "electronics_audio", "",
     39.99, 248.0, 399.99, "BUY", "TRUSTED", 3, 93.6),
    ("LEGO Icons Eiffel Tower 10307", "LEGO", "bestbuy.com", "toys_lego", "",
     89.99, 512.5, 679.99, "BUY", "TRUSTED", 2, 91.2),
    ("DeWalt 20V MAX XR Hammer Drill Kit", "DeWalt", "homedepot.com", "tools", "",
     59.00, 229.0, 299.00, "BUY", "TRUSTED", 4, 88.1),
    ("Dyson V15 Detect Absolute", "Dyson", "walmart.com", "appliance_small", "",
     189.00, 549.0, 749.99, "HOLD", "TRUSTED", 0, 76.4),
    ("Nike Air Max 1 '86 OG", "Nike", "nike.com", "sneakers", "10.5",
     44.97, 165.0, 150.00, "HOLD", "TRUSTED", 0, 71.9),
    ("Samsung 65\" S90D OLED TV", "Samsung", "bestbuy.com", "electronics_tv", "65 inch",
     1299.99, 1450.0, 1899.99, "SKIP", "TRUSTED", 0, 44.2),
    ("Yeti Tundra 45 Cooler", "YETI", "rei.com", "outdoor", "",
     174.99, 240.0, 325.00, "SKIP", "TRUSTED", 0, 52.8),
    ("Sony WH-1000XM5 Headphones", "Sony", "tаrget.com", "electronics_audio", "",
     None, None, None, "BLOCKED", "BLOCKED", 0, 0.0),
    ("Apple AirPods Pro 2", "Apple", "amazon.com", "electronics_audio", "",
     19.99, 189.0, 249.00, "BUY", "TRUSTED", 2, 95.1),
]

if not db.q1("SELECT id FROM deals"):
    for i, (name, brand, merch, cat, size, price, market, listp, dec, trust, qty, score) in enumerate(DEMO):
        created = now - (i * 2700 + random.randint(60, 900))
        disc = round((1 - price / listp) * 100, 1) if price and listp else None
        profit = round((market * 0.88 - 9) - price, 2) if market and price else None
        reasons = {"passed": ["trust " + trust, f"category {cat}", "returnable, 90d window"],
                   "failed": [] if dec == "BUY" else
                             (["no market comp and no MSRP on the page — can't price the upside"]
                              if dec == "HOLD" else
                              ["est. profit below your minimum"] if dec == "SKIP" else
                              ["folds to target.com once lookalike characters are normalised"]),
                   "notes": []}
        did = db.insert("deals", {
            "created_at": created, "source": "discord:#pricing-errors",
            "raw_message": f"🚨 {name} {price if price else ''}", "url": f"https://www.{merch}/p/{i}",
            "merchant": merch, "trust_verdict": trust, "trust_score": 1.0 if trust == "TRUSTED" else 0.0,
            "trust_reasons": json.dumps(["exact match on allowlisted merchant " + merch] if trust == "TRUSTED"
                                        else ["mixed-script label — classic homograph attack"]),
            "product_name": name, "brand": brand, "size": size, "category": cat,
            "listed_price": price, "currency": "USD", "market_price": market,
            "market_price_source": "comp:ebay-sold" if market else "unknown",
            "demand_score": round(random.uniform(0.55, 0.92), 2), "returnable": 1,
            "return_window_days": 90, "discount_pct": disc, "est_profit_unit": profit,
            "score": score, "decision": dec, "decision_reasons": json.dumps(reasons),
            "target_qty": qty, "latency_ms": random.randint(280, 950),
            "status": {"BUY": "ordered", "HOLD": "hold", "SKIP": "skipped", "BLOCKED": "skipped"}[dec],
            "raw_product": json.dumps({"availability": "in_stock", "source": "jsonld"})})
        events.log(did, "intake", f"link from discord:#pricing-errors: https://www.{merch}/p/{i}")
        events.log(did, "trust", f"{trust} — {merch}", level="ok" if trust == "TRUSTED" else "error")
        events.log(did, "product", f"“{name}” · {brand} · ${price} · in_stock · via jsonld in 412ms", level="ok")
        events.log(did, "decision", f"{dec} · score {score} · {qty}× ${price or 0}", 
                   level="ok" if dec == "BUY" else "warn" if dec == "HOLD" else "info")

        if dec == "BUY":
            cards = db.q("SELECT * FROM cards ORDER BY priority")
            card = cards[i % len(cards)]
            ship = ["placed", "in_transit", "out_for_delivery", "delivered", "delivered"][i % 5]
            oid = db.insert("orders", {
                "deal_id": did, "created_at": created + 8, "merchant": merch, "product_name": name,
                "card_id": card["id"], "card_label": f"{card['nickname']} ·{card['last4']}",
                "qty": qty, "unit_price": price, "total": round(qty * price * 1.06, 2),
                "shipping_method": "Standard", "status": "placed",
                "order_number": f"{merch[:3].upper()}-{random.randint(10**9, 10**10):010d}",
                "placed_at": created + 8, "carrier": random.choice(["UPS", "FedEx", "USPS"]),
                "tracking_number": f"1Z{random.randint(10**10, 10**11)}",
                "ship_status": ship, "eta": time.strftime("%Y-%m-%d", time.localtime(now + 86400 * 2)),
                "delivered_at": created + 200000 if ship == "delivered" else None,
                "last_tracking_update": now - 3600})
            # keep the displayed "available" consistent with the demo orders
            db.execute("UPDATE cards SET available=MAX(0, available-?), updated_at=? WHERE id=?",
                       (round(qty * price * 1.06, 2), now, card["id"]))
            events.log(did, "order", f"ORDER PLACED — {qty}× on {card['nickname']} ·{card['last4']}", level="ok")
            events.log(did, "shipping", f"{name} → {ship.replace('_', ' ')}",
                       level="ok" if ship == "delivered" else "info")

    # one failed order, because they happen
    d = db.q1("SELECT * FROM deals WHERE decision='BUY' ORDER BY id DESC LIMIT 1")
    db.insert("orders", {"deal_id": d["id"], "created_at": now - 5400, "merchant": d["merchant"],
                         "product_name": d["product_name"], "qty": 2, "unit_price": d["listed_price"],
                         "total": 0, "status": "failed", "failure_stage": "payment",
                         "failure_reason": "card declined — Amex Gold ·1003 over limit, rolled to next card",
                         "raw": "{}"})
    events.log(d["id"], "order", "ORDER FAILED at payment: card declined", level="error")

# ---- per-merchant shipping & return policy ------------------------------
POLICIES = [
    # domain, pref, paid?, cap, expedite_over, return_method, login?
    ("target.com",    "expedited", 1, 15,  150, "in_store",   1),
    ("bestbuy.com",   "expedited", 1, 10,  200, "in_store",   1),
    ("walmart.com",   "cheapest",  0, 0,   0,   "in_store",   0),
    ("homedepot.com", "standard",  0, 0,   0,   "in_store",   0),
    ("rei.com",       "cheapest",  0, 0,   0,   "mail_free",  0),
    ("amazon.com",    "fastest",   1, 12,  100, "carrier_pickup", 0),
    ("costco.com",    "cheapest",  0, 0,   0,   "in_store",   0),
    ("newegg.com",    "cheapest",  0, 0,   0,   "mail_paid",  0),
]
for dom, pref, paid, cap, exp, rm, login in POLICIES:
    db.execute("UPDATE merchants SET shipping_pref=?, allow_paid_shipping=?, max_shipping_cost=?, "
               "expedite_over_value=?, return_method=?, login_required=?, updated_at=? WHERE domain=?",
               (pref, paid, cap, exp, rm, login, now, dom))
db.execute("UPDATE merchants SET return_notes=? WHERE domain='bestbuy.com'",
           ("15 days for most things, 30 for members. Opened electronics get inspected — keep the box.",))

# ---- worker session state -----------------------------------------------
for dom, signed, creds, manual, note in [
        ("target.com", 1, 1, 0, "signed in automatically"),
        ("bestbuy.com", 0, 1, 1, "2FA challenge — needs a manual sign-in")]:
    db.execute("INSERT OR REPLACE INTO merchant_sessions(merchant,signed_in,has_credentials,"
               "last_login,last_checked,needs_manual_login,note) VALUES(?,?,?,?,?,?,?)",
               (dom, signed, creds, now - 7200 if signed else None, now - 900, manual, note))

# ---- scheduled task history ---------------------------------------------
if not db.q1("SELECT id FROM task_runs"):
    runs = [
        ("recon", "chatgpt-recon-task", "ok", "target.com 2026-09-13T03:41:00Z", 1, None, 8),
        ("recon", "chatgpt-recon-task", "ok", "bestbuy.com 2026-09-13T03:44:00Z", 1, None, 8),
        ("recon", "chatgpt-recon-task", "rejected",
         "rei.com — rejected", 0, ["steps.payment[1]: target 'cvv_box' is not defined in selectors"], 8),
        ("shipping", "chatgpt-shipping-task", "ok", "3 order(s) updated", 3, None, 1),
        ("shipping", "chatgpt-shipping-task", "partial", "2 order(s) updated, 1 unmatched", 2,
         ["BBY01-806123456789"], 5),
        ("comps", "chatgpt-comps", "ok", "24 comp(s) refreshed", 24, None, 11),
        ("discord", "discord:#pricing-errors", "ok", "1 link(s): target.com → BUY", 1, None, 0.3),
    ]
    for task, src, status, summary, items, errs, hours_ago in runs:
        db.insert("task_runs", {"ts": now - hours_ago * 3600, "task": task, "source": src,
                                "status": status, "summary": summary, "items": items,
                                "errors": json.dumps(errs) if errs else None})

print("Demo data loaded. Sign in and look around.")
