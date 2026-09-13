"""Which card pays, in what order, and what happens when one runs out.

Rules you set per site (Cards page) beat the global priority order. When a card
can't cover the whole order we buy as many units as fit on it, then roll to the
next card automatically — and the remaining limit is written back after each
successful charge.

No card numbers live here. The dashboard stores nickname / network / last4 /
limit only; the PAN stays in the local worker's vault (worker/vault.py).
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field, asdict

from .. import db


@dataclass
class Charge:
    card_id: int
    card_label: str
    qty: int
    unit_price: float
    amount: float          # what actually hits the card, tax included
    shipping_method: str = ""
    reason: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Plan:
    charges: list[Charge] = field(default_factory=list)
    qty_planned: int = 0
    qty_requested: int = 0
    total: float = 0.0
    shortfall_reason: str = ""
    considered: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"charges": [c.to_dict() for c in self.charges], "qty_planned": self.qty_planned,
                "qty_requested": self.qty_requested, "total": round(self.total, 2),
                "shortfall_reason": self.shortfall_reason, "considered": self.considered}


def _label(card: dict) -> str:
    return f"{card['nickname']} ·{card['last4'] or '????'}"


def spent_today(card_id: int | None = None) -> float:
    since = time.time() - 86400
    if card_id:
        row = db.q1("SELECT COALESCE(SUM(total),0) t FROM orders "
                    "WHERE card_id=? AND created_at>? AND status IN ('placed','pending')",
                    (card_id, since))
    else:
        row = db.q1("SELECT COALESCE(SUM(total),0) t FROM orders "
                    "WHERE created_at>? AND status IN ('placed','pending')", (since,))
    return float(row["t"] if row else 0)


def eligible_cards(merchant: str) -> list[dict]:
    """Site rules first (in rank order), then any remaining enabled cards by priority."""
    now = time.time()
    rules = db.q(
        "SELECT c.*, r.rank, r.shipping_method FROM card_rules r JOIN cards c ON c.id=r.card_id "
        "WHERE r.merchant=? AND r.enabled=1 AND c.enabled=1 ORDER BY r.rank ASC, c.priority ASC",
        (merchant,))
    if not rules:
        rules = db.q(
            "SELECT c.*, r.rank, r.shipping_method FROM card_rules r JOIN cards c ON c.id=r.card_id "
            "WHERE r.merchant='*' AND r.enabled=1 AND c.enabled=1 ORDER BY r.rank ASC, c.priority ASC")
    seen = {c["id"] for c in rules}
    rest = [c for c in db.q("SELECT *, 9999 AS rank, '' AS shipping_method FROM cards "
                            "WHERE enabled=1 ORDER BY priority ASC, id ASC") if c["id"] not in seen]
    out = []
    for c in rules + rest:
        if (c.get("cooldown_until") or 0) > now:
            c = dict(c); c["_skip"] = f"cooling down until {time.strftime('%H:%M', time.localtime(c['cooldown_until']))}"
        out.append(c)
    return out


def plan_purchase(merchant: str, unit_price: float, qty: int,
                  tax_rate: float = 0.0, shipping_cost: float = 0.0,
                  daily_cap: float | None = None) -> Plan:
    """Split `qty` units across cards, filling each card as far as it goes."""
    plan = Plan(qty_requested=qty)
    if qty <= 0 or unit_price <= 0:
        plan.shortfall_reason = "nothing to buy"
        return plan

    unit_cost = unit_price * (1 + tax_rate)
    remaining = qty
    global_left = None
    if daily_cap:
        global_left = max(0.0, daily_cap - spent_today())
        if global_left <= 0:
            plan.shortfall_reason = f"daily spend cap ${daily_cap:,.0f} already reached"
            return plan

    for card in eligible_cards(merchant):
        if remaining <= 0:
            break
        note = {"card": _label(card), "available": round(float(card["available"] or 0), 2)}
        if card.get("_skip"):
            note["skipped"] = card["_skip"]; plan.considered.append(note); continue

        capacity = float(card["available"] or 0)
        if card["max_per_txn"]:
            capacity = min(capacity, float(card["max_per_txn"]))
        if card["daily_cap"]:
            capacity = min(capacity, max(0.0, float(card["daily_cap"]) - spent_today(card["id"])))
        if global_left is not None:
            capacity = min(capacity, global_left)

        # first unit on a card also carries shipping
        if capacity < unit_cost + shipping_cost:
            note["skipped"] = f"only ${capacity:,.2f} left — not enough for one unit at ${unit_cost:,.2f}"
            plan.considered.append(note)
            continue

        units = int((capacity - shipping_cost) // unit_cost)
        units = min(units, remaining)
        amount = round(units * unit_cost + shipping_cost, 2)
        reason = ("covers the whole order" if units == remaining
                  else f"fills this card to ~${capacity - amount:,.2f} left, rolling {remaining - units} unit(s) to the next card")
        plan.charges.append(Charge(card_id=card["id"], card_label=_label(card), qty=units,
                                   unit_price=unit_price, amount=amount,
                                   shipping_method=card.get("shipping_method") or "", reason=reason))
        plan.total += amount
        remaining -= units
        if global_left is not None:
            global_left -= amount
        note["takes"] = units
        note["amount"] = amount
        plan.considered.append(note)

    plan.qty_planned = qty - remaining
    if remaining > 0:
        plan.shortfall_reason = (f"only {plan.qty_planned}/{qty} units fit on available credit"
                                 if plan.qty_planned else
                                 "no card has enough remaining credit for a single unit")
    return plan


def commit_charge(card_id: int, amount: float) -> float:
    """Decrement remaining credit after a successful order. Returns new available."""
    card = db.q1("SELECT available FROM cards WHERE id=?", (card_id,))
    if not card:
        return 0.0
    new = max(0.0, float(card["available"] or 0) - float(amount))
    db.execute("UPDATE cards SET available=?, updated_at=? WHERE id=?", (new, time.time(), card_id))
    return new


def release_charge(card_id: int, amount: float) -> float:
    card = db.q1("SELECT available, credit_limit FROM cards WHERE id=?", (card_id,))
    if not card:
        return 0.0
    new = min(float(card["credit_limit"] or 0) or 1e12, float(card["available"] or 0) + float(amount))
    db.execute("UPDATE cards SET available=?, updated_at=? WHERE id=?", (new, time.time(), card_id))
    return new


def mark_declined(card_id: int, minutes: int = 45) -> None:
    db.execute("UPDATE cards SET cooldown_until=?, updated_at=? WHERE id=?",
               (time.time() + minutes * 60, time.time(), card_id))


def refresh_statement_limits() -> list[str]:
    """Restore `available` to the full limit on each card's statement day."""
    today = int(time.strftime("%d"))
    out = []
    for c in db.q("SELECT * FROM cards WHERE statement_day=?", (today,)):
        last = time.strftime("%Y-%m-%d", time.localtime(c["updated_at"] or 0))
        if last != time.strftime("%Y-%m-%d"):
            db.execute("UPDATE cards SET available=credit_limit, updated_at=? WHERE id=?",
                       (time.time(), c["id"]))
            out.append(f"{_label(c)} limit restored to ${c['credit_limit']:,.0f}")
    return out
