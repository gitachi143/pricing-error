"""Per-merchant shipping policy → the option the worker actually clicks."""
import time

from app import db
from app.core import shipping

OPTIONS = {
    "standard":  {"label": "Standard (free, 3-5 days)", "cost": 0,     "days": 4, "selector": "ship_std"},
    "two_day":   {"label": "2-day",                     "cost": 9.99,  "days": 2, "selector": "ship_2d"},
    "overnight": {"label": "Next day",                  "cost": 24.99, "days": 1, "selector": "ship_ovn"},
    "pickup":    {"label": "Store pickup",              "cost": 0,     "days": 0, "selector": "ship_pu",
                  "pickup": True},
}


def set_policy(**kw):
    db.execute("INSERT OR REPLACE INTO merchants(domain,name,trusted,updated_at) VALUES(?,?,1,?)",
               ("shop.com", "Shop", time.time()))
    for k, v in kw.items():
        db.execute(f"UPDATE merchants SET {k}=? WHERE domain='shop.com'",
                   (1 if v is True else 0 if v is False else v,))


def test_cheapest_is_the_default():
    set_policy()
    c = shipping.choose("shop.com", OPTIONS)
    assert c.cost == 0 and c.key in ("standard", "pickup")


def test_expedited_is_ignored_when_paid_shipping_is_off():
    set_policy(shipping_pref="expedited", allow_paid_shipping=False)
    c = shipping.choose("shop.com", OPTIONS)
    assert c.cost == 0 and c.fallback
    assert "paid shipping is turned off" in c.reason


def test_expedited_picks_the_fastest_you_allow():
    set_policy(shipping_pref="expedited", allow_paid_shipping=True, max_shipping_cost=0)
    c = shipping.choose("shop.com", OPTIONS)
    assert c.key == "pickup" or c.days <= 1


def test_cost_ceiling_is_respected():
    set_policy(shipping_pref="fastest", allow_paid_shipping=True, max_shipping_cost=10)
    c = shipping.choose("shop.com", OPTIONS)
    assert c.key == "two_day" and c.cost == 9.99      # overnight is over the cap


def test_big_orders_upgrade_themselves():
    set_policy(shipping_pref="cheapest", allow_paid_shipping=True, max_shipping_cost=15,
               expedite_over_value=200)
    slow = shipping.choose("shop.com", OPTIONS, order_value=50)
    fast = shipping.choose("shop.com", OPTIONS, order_value=900)
    assert slow.cost == 0
    assert fast.key == "two_day" and "expedite threshold" in fast.reason


def test_pickup_preference():
    set_policy(shipping_pref="pickup")
    assert shipping.choose("shop.com", OPTIONS).key == "pickup"


def test_pickup_falls_back_when_unavailable():
    set_policy(shipping_pref="pickup")
    c = shipping.choose("shop.com", {k: v for k, v in OPTIONS.items() if k != "pickup"})
    assert c.fallback and "doesn't offer it" in c.reason and c.cost == 0


def test_no_options_in_playbook_is_reported_not_guessed():
    set_policy()
    c = shipping.choose("shop.com", {})
    assert c.fallback and c.selector == "" and "no shipping options" in c.reason


def test_per_deal_override_beats_the_merchant_setting():
    set_policy(shipping_pref="cheapest", allow_paid_shipping=True, max_shipping_cost=30)
    c = shipping.choose("shop.com", OPTIONS, override="fastest")
    assert c.key == "overnight"


def test_every_choice_explains_itself():
    for pref in shipping.PREFS:
        set_policy(shipping_pref=pref, allow_paid_shipping=True, max_shipping_cost=30)
        c = shipping.choose("shop.com", OPTIONS, order_value=100)
        assert len(c.reason) > 10, f"{pref} gave no reason"
