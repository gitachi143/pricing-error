"""Turn facts into BUY / HOLD / SKIP / BLOCKED, with the reasoning attached.

Every filter here is editable from the dashboard (Filters page) — nothing is
hard-coded policy.
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass, field, asdict

from .. import db
from .product import ProductFacts
from .research import Research
from .trust import TrustResult, at_least, RANK

RISK_RANK = {"low": 0, "medium": 1, "high": 2}


@dataclass
class Decision:
    decision: str = "SKIP"          # BUY | HOLD | SKIP | BLOCKED
    score: float = 0.0              # 0-100, how good the deal is
    qty: int = 0
    unit_price: float = 0.0
    est_total: float = 0.0
    discount_pct: float | None = None
    est_profit_unit: float | None = None
    est_profit_total: float | None = None
    roi_pct: float | None = None
    passed: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


def active_filters() -> dict:
    row = db.q1("SELECT json FROM filters WHERE active=1 ORDER BY id DESC LIMIT 1")
    base = dict(db.DEFAULT_FILTERS)
    if row:
        try:
            base.update(json.loads(row["json"]))
        except Exception:
            pass
    return base


def evaluate(facts: ProductFacts, rsr: Research, trust: TrustResult,
             merchant: str, f: dict | None = None) -> Decision:
    f = f or active_filters()
    d = Decision()
    price = facts.price or 0.0
    d.unit_price = price

    def ok(msg): d.passed.append(msg)
    def bad(msg): d.failed.append(msg)

    # --- hard stops ------------------------------------------------------
    if trust.verdict == "BLOCKED":
        d.decision = "BLOCKED"
        d.failed.append(f"link blocked by trust check: {trust.reasons[0] if trust.reasons else 'unsafe'}")
        return d
    if not at_least(trust.verdict, f.get("min_trust", "TRUSTED")):
        bad(f"trust {trust.verdict} is below your minimum {f.get('min_trust')}")
    else:
        ok(f"trust {trust.verdict}")

    if price <= 0:
        d.decision = "HOLD"
        d.failed.append("no price could be read from the page — needs eyes")
        return d
    if price < float(f.get("min_price_floor", 0)):
        bad(f"${price:.2f} is below your ${f['min_price_floor']} floor (likely a parsing error)")
    if price > float(f.get("max_unit_price", 1e9)):
        bad(f"${price:.2f} over max unit price ${f['max_unit_price']}")
    else:
        ok(f"unit price ${price:.2f} within cap")

    # --- merchant / category / size gates --------------------------------
    allow_m = [m.lower() for m in f.get("allow_merchants") or []]
    deny_m = [m.lower() for m in f.get("deny_merchants") or []]
    if deny_m and merchant in deny_m:
        bad(f"{merchant} is on your deny list")
    if allow_m and merchant not in allow_m:
        bad(f"{merchant} is not on your allow list")
    elif not deny_m or merchant not in deny_m:
        ok(f"merchant {merchant} allowed")

    allow_c = f.get("allow_categories") or []
    deny_c = f.get("deny_categories") or []
    if rsr.category in deny_c:
        bad(f"category {rsr.category} is denied")
    elif allow_c and rsr.category not in allow_c:
        bad(f"category {rsr.category} not in your allow list")
    else:
        ok(f"category {rsr.category}")

    if rsr.size:
        allow_s = [str(s).upper() for s in (f.get("allow_sizes") or [])]
        deny_s = [str(s).upper() for s in (f.get("deny_sizes") or [])]
        if rsr.size.upper() in deny_s:
            bad(f"size {rsr.size} is denied")
        elif allow_s and rsr.size.upper() not in allow_s:
            bad(f"size {rsr.size} not in your allow list")
        else:
            ok(f"size {rsr.size}")

    # --- stock -----------------------------------------------------------
    if f.get("require_in_stock", True):
        if facts.availability == "out_of_stock":
            bad("out of stock")
        elif facts.availability == "unknown":
            d.notes.append("stock status unknown — worker will confirm at add-to-cart")
        else:
            ok("in stock")

    # --- returnability ---------------------------------------------------
    if f.get("block_final_sale", True) and facts.final_sale:
        bad("listing is final sale")
    if f.get("require_returnable", True):
        if not rsr.returnable:
            bad("not returnable")
        elif rsr.return_window_days < int(f.get("min_return_window_days", 0)):
            bad(f"return window {rsr.return_window_days}d < {f['min_return_window_days']}d minimum")
        else:
            ok(f"returnable, {rsr.return_window_days}d window")
    if rsr.restocking_fee_pct > float(f.get("max_restocking_fee_pct", 100)):
        bad(f"restocking fee {rsr.restocking_fee_pct:g}% too high")
    if RISK_RANK.get(rsr.cancel_risk, 1) > RISK_RANK.get(f.get("max_cancel_risk", "high"), 2):
        bad(f"merchant cancel risk {rsr.cancel_risk} exceeds your tolerance")

    # --- the money math --------------------------------------------------
    market = rsr.market_price
    if market is None:
        if f.get("allow_unknown_market", False):
            d.notes.append("no market comp — profit filters skipped by your settings")
        else:
            bad("no market comp and no MSRP on the page — can't price the upside")
    else:
        fee = float(f.get("resale_fee_pct", 12)) / 100
        ship = float(f.get("ship_cost_est", 0))
        net = market * (1 - fee) - ship
        d.est_profit_unit = round(net - price, 2)
        d.roi_pct = round((d.est_profit_unit / price) * 100, 1) if price else None
        ref = facts.list_price or market
        d.discount_pct = round((1 - price / ref) * 100, 1) if ref and ref > 0 else None

        if d.discount_pct is not None and d.discount_pct < float(f.get("min_discount_pct", 0)):
            bad(f"discount {d.discount_pct:.0f}% < {f['min_discount_pct']}% minimum")
        elif d.discount_pct is not None:
            ok(f"{d.discount_pct:.0f}% off (ref ${ref:.2f})")

        if d.est_profit_unit < float(f.get("min_profit_per_unit", 0)):
            bad(f"est. profit ${d.est_profit_unit:.2f}/unit < ${f['min_profit_per_unit']} minimum")
        else:
            ok(f"est. profit ${d.est_profit_unit:.2f}/unit after {fee*100:.0f}% fees + ${ship:g} ship")

        ratio = (market / price) if price else 0
        if ratio < float(f.get("min_resale_ratio", 0)):
            bad(f"market/price ratio {ratio:.2f}x < {f['min_resale_ratio']}x minimum")
        else:
            ok(f"worth {ratio:.2f}x what you'd pay (market ${market:.2f}, {rsr.market_price_source})")

    if rsr.demand_score < float(f.get("min_demand_score", 0)):
        bad(f"demand {rsr.demand_score:.2f} < {f['min_demand_score']} minimum")
    else:
        ok(f"demand {rsr.demand_score:.2f}")

    # --- quantity --------------------------------------------------------
    max_qty = int(f.get("max_qty_per_deal", 1))
    budget = float(f.get("max_spend_per_deal", 0))
    by_budget = int(budget // price) if price else 0
    d.qty = max(0, min(max_qty, by_budget))
    if d.qty == 0 and by_budget == 0:
        bad(f"one unit (${price:.2f}) already exceeds your ${budget} per-deal cap")
    d.est_total = round(d.qty * price, 2)

    # --- score -----------------------------------------------------------
    parts = []
    if d.discount_pct is not None:
        parts.append(("discount", min(1.0, d.discount_pct / 80), 0.30))
    if d.roi_pct is not None:
        parts.append(("roi", min(1.0, max(0.0, d.roi_pct) / 200), 0.30))
    parts.append(("demand", rsr.demand_score, 0.20))
    parts.append(("trust", RANK.get(trust.verdict, 0) / 3, 0.10))
    parts.append(("returns", 1.0 if rsr.returnable else 0.0, 0.05))
    parts.append(("cancel_risk", 1 - RISK_RANK.get(rsr.cancel_risk, 1) / 2, 0.05))
    total_w = sum(w for _, _, w in parts)
    d.score = round(100 * sum(v * w for _, v, w in parts) / total_w, 1)

    if d.est_profit_unit is not None:
        d.est_profit_total = round(d.est_profit_unit * d.qty, 2)

    # --- verdict ---------------------------------------------------------
    if d.failed:
        soft = all(any(k in msg for k in ("no market comp", "stock status unknown",
                                          "trust SUSPICIOUS", "verify before buying"))
                   for msg in d.failed)
        d.decision = "HOLD" if (soft and f.get("hold_instead_of_skip", True)) else "SKIP"
        if d.decision == "SKIP":
            d.qty = 0
            d.est_total = 0.0
    elif d.qty > 0:
        d.decision = "BUY"
    else:
        d.decision = "SKIP"
    return d
