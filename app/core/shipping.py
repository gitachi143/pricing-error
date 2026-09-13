"""Pick the shipping option at checkout.

You set the policy per merchant (Merchants page); the nightly recon task records
what options that merchant actually offers; this picks one and tells you why.

Merchant policy fields:
  shipping_pref        cheapest | standard | expedited | fastest | pickup
  allow_paid_shipping  may we pay for speed at all
  max_shipping_cost    hard ceiling on what we'll pay (0 = no ceiling)
  expedite_over_value  order value above which speed is worth paying for (0 = never)
"""
from __future__ import annotations

from dataclasses import dataclass, asdict

from .. import db

PREFS = ("cheapest", "standard", "expedited", "fastest", "pickup")


@dataclass
class ShippingChoice:
    key: str = ""              # the playbook's option key, e.g. "two_day"
    label: str = ""            # what it says on the site
    selector: str = ""         # which selector the worker clicks
    cost: float = 0.0
    days: float | None = None
    reason: str = ""
    fallback: bool = False     # we couldn't honour the preference exactly

    def to_dict(self) -> dict:
        return asdict(self)


def merchant_policy(merchant: str) -> dict:
    row = db.q1("SELECT * FROM merchants WHERE domain=?", (merchant,)) or {}
    return {
        "shipping_pref": row.get("shipping_pref") or "cheapest",
        "allow_paid_shipping": bool(row.get("allow_paid_shipping")),
        "max_shipping_cost": float(row.get("max_shipping_cost") or 0),
        "expedite_over_value": float(row.get("expedite_over_value") or 0),
    }


def _norm(options: dict) -> list[dict]:
    out = []
    for key, o in (options or {}).items():
        if not isinstance(o, dict):
            continue
        out.append({
            "key": key,
            "label": str(o.get("label") or key),
            "selector": str(o.get("selector") or ""),
            "cost": float(o.get("cost") or 0),
            "days": None if o.get("days") is None else float(o.get("days")),
            "pickup": bool(o.get("pickup")) or "pickup" in key.lower(),
        })
    return out


def _affordable(opts: list[dict], policy: dict) -> list[dict]:
    """What we're allowed to pay for."""
    free = [o for o in opts if o["cost"] <= 0]
    if not policy["allow_paid_shipping"]:
        return free or opts
    cap = policy["max_shipping_cost"]
    paid = [o for o in opts if o["cost"] <= cap] if cap > 0 else opts
    return paid or free or opts


def _fastest(opts: list[dict]) -> dict:
    return min(opts, key=lambda o: (o["days"] if o["days"] is not None else 99, o["cost"]))


def _cheapest(opts: list[dict]) -> dict:
    return min(opts, key=lambda o: (o["cost"], o["days"] if o["days"] is not None else 99))


def choose(merchant: str, options: dict, order_value: float = 0.0,
           override: str = "") -> ShippingChoice:
    opts = _norm(options)
    if not opts:
        return ShippingChoice(reason="this merchant's playbook lists no shipping options — "
                                     "the site's default will be used", fallback=True)

    policy = merchant_policy(merchant)
    pref = (override or policy["shipping_pref"] or "cheapest").lower()

    # An order big enough to be worth protecting gets upgraded regardless of the
    # default preference — a slow ship is a cancelled ship on a price error.
    upgraded = False
    if (policy["expedite_over_value"] > 0 and order_value >= policy["expedite_over_value"]
            and pref in ("cheapest", "standard")):
        pref, upgraded = "expedited", True

    usable = _affordable(opts, policy)

    if pref == "pickup":
        pick = next((o for o in opts if o["pickup"]), None)
        if pick:
            return ShippingChoice(**_c(pick), reason="store pickup, per your setting for this merchant")
        chosen = _cheapest(usable)
        return ShippingChoice(**_c(chosen), fallback=True,
                              reason=f"you asked for pickup but this merchant doesn't offer it here — "
                                     f"fell back to {chosen['label']}")

    ground = [o for o in usable if not o["pickup"]] or usable

    if pref == "standard":
        std = next((o for o in ground if o["key"] == "standard"), None) or _cheapest(ground)
        return ShippingChoice(**_c(std), reason=f"standard shipping ({_money(std['cost'])})")

    if pref in ("expedited", "fastest"):
        if not policy["allow_paid_shipping"]:
            chosen = _fastest([o for o in ground if o["cost"] <= 0] or ground)
            return ShippingChoice(
                **_c(chosen), fallback=True,
                reason=f"fastest free option ({chosen['label']}) — paid shipping is turned off "
                       f"for this merchant")
        chosen = _fastest(ground)
        why = (f"fastest within your {_money(policy['max_shipping_cost'])} cap"
               if policy["max_shipping_cost"] > 0 else "fastest available")
        if upgraded:
            why = (f"order is {_money(order_value)}, over your "
                   f"{_money(policy['expedite_over_value'])} expedite threshold — upgraded to "
                   f"{chosen['label']}")
        return ShippingChoice(**_c(chosen), reason=why, fallback=upgraded)

    chosen = _cheapest(ground)
    return ShippingChoice(**_c(chosen),
                          reason=f"cheapest option ({_money(chosen['cost'])}, "
                                 f"{_days(chosen['days'])})")


def _c(o: dict) -> dict:
    return {"key": o["key"], "label": o["label"], "selector": o["selector"],
            "cost": o["cost"], "days": o["days"]}


def _money(v: float) -> str:
    return "free" if not v else f"${v:,.2f}"


def _days(d) -> str:
    if d is None:
        return "unknown speed"
    if d <= 0:
        return "same day"
    return f"{d:g} day{'s' if d != 1 else ''}"


def summary(merchant: str) -> dict:
    """What the Merchants page shows for one merchant."""
    from .playbooks import get_active
    pb = get_active(merchant)
    options = (pb or {}).get("playbook", {}).get("shipping_options", {}) if pb else {}
    policy = merchant_policy(merchant)
    return {"policy": policy, "options": _norm(options),
            "example": choose(merchant, options, order_value=250).to_dict()}
