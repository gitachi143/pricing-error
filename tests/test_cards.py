"""Card allocation: fill one card to its limit, roll the rest to the next."""
from app import db
from app.core import cards


def mk(nick, avail, limit=10000, prio=100, **kw):
    return db.insert("cards", {"nickname": nick, "network": "visa", "last4": nick[-4:].rjust(4, "0"),
                               "credit_limit": limit, "available": avail, "priority": prio,
                               "enabled": 1, "max_per_txn": kw.get("max_per_txn", 0),
                               "daily_cap": kw.get("daily_cap", 0)})


def test_single_card_covers_everything():
    mk("Visa1", 5000)
    p = cards.plan_purchase("target.com", 100, 3)
    assert p.qty_planned == 3 and len(p.charges) == 1 and p.total == 300


def test_fills_first_card_then_rolls_to_the_next():
    a = mk("Amex", 250, prio=10)
    b = mk("Citi", 5000, prio=20)
    p = cards.plan_purchase("target.com", 100, 4)
    assert [c.qty for c in p.charges] == [2, 2]          # 250 fits two units, rest rolls
    assert p.charges[0].card_id == a and p.charges[1].card_id == b
    assert p.qty_planned == 4


def test_skips_a_card_that_cannot_cover_one_unit():
    mk("Nearly full", 40, prio=10)
    mk("Fresh", 5000, prio=20)
    p = cards.plan_purchase("target.com", 100, 2)
    assert len(p.charges) == 1 and p.charges[0].qty == 2
    assert any("not enough for one unit" in (c.get("skipped") or "") for c in p.considered)


def test_site_rule_beats_global_priority():
    first = mk("Global first", 5000, prio=1)
    special = mk("Best Buy card", 5000, prio=99)
    db.insert("card_rules", {"merchant": "bestbuy.com", "card_id": special, "rank": 0,
                             "enabled": 1, "shipping_method": "2-day"})
    p = cards.plan_purchase("bestbuy.com", 100, 1)
    assert p.charges[0].card_id == special
    assert p.charges[0].shipping_method == "2-day"
    # ...but a different merchant still uses the global order
    assert cards.plan_purchase("target.com", 100, 1).charges[0].card_id == first


def test_tax_is_counted_against_the_limit():
    mk("Tight", 215)
    p = cards.plan_purchase("target.com", 100, 3, tax_rate=0.08)   # 108 per unit
    assert p.qty_planned == 1                                      # only one fits in 215... plus change
    assert p.charges[0].amount == 108.0


def test_max_per_transaction_cap():
    mk("Capped", 5000, max_per_txn=250)
    p = cards.plan_purchase("target.com", 100, 5)
    assert p.charges[0].qty == 2 and p.qty_planned == 2


def test_disabled_and_cooling_down_cards_are_skipped():
    import time
    hot = mk("Hot", 5000, prio=5)
    db.execute("UPDATE cards SET cooldown_until=? WHERE id=?", (time.time() + 600, hot))
    ok = mk("Ok", 5000, prio=10)
    p = cards.plan_purchase("target.com", 100, 1)
    assert p.charges[0].card_id == ok
    assert any("cooling down" in (c.get("skipped") or "") for c in p.considered)


def test_no_credit_anywhere_reports_why():
    mk("Empty", 10)
    p = cards.plan_purchase("target.com", 100, 2)
    assert p.qty_planned == 0 and "single unit" in p.shortfall_reason


def test_commit_and_release_move_the_limit():
    cid = mk("Visa", 1000, limit=1000)
    assert cards.commit_charge(cid, 300) == 700
    assert cards.release_charge(cid, 300) == 1000
    assert cards.release_charge(cid, 5000) == 1000       # never exceeds the limit


def test_daily_cap_stops_further_spend():
    mk("Card", 5000)
    p = cards.plan_purchase("target.com", 100, 5, daily_cap=250)
    assert p.qty_planned == 2
