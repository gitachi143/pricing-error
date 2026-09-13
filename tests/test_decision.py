"""Filters decide what gets bought. Each one should be independently provable."""
import json

from app import db
from app.core.decision import evaluate, active_filters
from app.core.product import ProductFacts
from app.core.research import Research
from app.core.trust import TrustResult


def facts(**kw):
    base = dict(url="https://target.com/p/x", name="Sony WH-1000XM5", brand="Sony",
                price=39.99, list_price=399.99, availability="in_stock", currency="USD")
    base.update(kw)
    return ProductFacts(**base)


def rsr(**kw):
    base = dict(category="electronics_audio", size="", demand_score=0.8, market_price=248.0,
                market_price_source="comp:ebay", returnable=True, return_window_days=90,
                free_returns=True, restocking_fee_pct=0.0, cancel_risk="medium")
    base.update(kw)
    return Research(**base)


TRUSTED = TrustResult(url="https://target.com/p/x", verdict="TRUSTED", score=1.0,
                      registrable="target.com")


def test_a_real_price_error_is_a_buy():
    d = evaluate(facts(), rsr(), TRUSTED, "target.com")
    assert d.decision == "BUY"
    assert d.qty >= 1 and d.est_profit_unit > 100 and d.score > 70


def test_blocked_link_never_reaches_the_maths():
    bad = TrustResult(url="x", verdict="BLOCKED", reasons=["homograph"], registrable="evil.ru")
    d = evaluate(facts(), rsr(), bad, "evil.ru")
    assert d.decision == "BLOCKED" and d.qty == 0


def test_thin_margin_is_skipped():
    d = evaluate(facts(price=230.0), rsr(), TRUSTED, "target.com")
    assert d.decision == "SKIP"
    assert any("profit" in f or "ratio" in f or "discount" in f for f in d.failed)


def test_non_returnable_is_skipped_when_you_require_returns():
    d = evaluate(facts(), rsr(returnable=False, return_window_days=0), TRUSTED, "target.com")
    assert d.decision == "SKIP" and any("returnable" in f for f in d.failed)


def test_final_sale_listing_is_skipped():
    d = evaluate(facts(final_sale=True), rsr(), TRUSTED, "target.com")
    assert any("final sale" in f for f in d.failed)


def test_out_of_stock_is_skipped():
    d = evaluate(facts(availability="out_of_stock"), rsr(), TRUSTED, "target.com")
    assert any("out of stock" in f for f in d.failed)


def test_denied_category_is_skipped():
    d = evaluate(facts(name="$500 Gift Card"), rsr(category="gift_card"), TRUSTED, "target.com")
    assert any("gift_card" in f for f in d.failed)


def test_no_market_comp_becomes_a_hold_not_a_silent_skip():
    d = evaluate(facts(list_price=None), rsr(market_price=None, market_price_source="unknown"),
                 TRUSTED, "target.com")
    assert d.decision == "HOLD"
    assert any("market comp" in f for f in d.failed)


def test_quantity_is_capped_by_the_spend_limit():
    f = active_filters(); f["max_spend_per_deal"] = 100; f["max_qty_per_deal"] = 10
    d = evaluate(facts(price=39.99), rsr(), TRUSTED, "target.com", f)
    assert d.qty == 2                                   # 2 × 39.99 fits in 100, 3 doesn't


def test_suspicious_trust_holds_for_a_human():
    susp = TrustResult(url="x", verdict="SUSPICIOUS", score=0.6, registrable="costco.co.uk",
                       reasons=["same brand name as costco.com"])
    d = evaluate(facts(), rsr(), susp, "costco.co.uk")
    assert d.decision == "HOLD"


def test_reasons_are_always_human_readable():
    d = evaluate(facts(), rsr(), TRUSTED, "target.com")
    assert all(isinstance(x, str) and len(x) > 5 for x in d.passed)
    assert any("$" in x for x in d.passed)              # the money reasons show numbers


def test_edited_filters_change_the_outcome():
    db.execute("UPDATE filters SET json=? WHERE active=1",
               (json.dumps({**db.DEFAULT_FILTERS, "min_profit_per_unit": 500}),))
    d = evaluate(facts(), rsr(), TRUSTED, "target.com")
    assert d.decision == "SKIP" and any("500" in f for f in d.failed)
