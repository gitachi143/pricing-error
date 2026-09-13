"""A playbook drives a browser that spends money — validation has to be strict."""
import copy

from app import db
from app.core import playbooks

GOOD = {
    "schema_version": "1.0", "merchant": "bestbuy.com", "captured_at": "2026-09-13T03:41:00Z",
    "confidence": 0.9,
    "site_facts": {"guest_checkout": False, "max_qty_per_order": 3},
    "selectors": {
        "add_to_cart": {"test_id": "add-to-cart-button"},
        "checkout_btn": {"role": "button", "label": "Checkout"},
        "card_number": {"css": "#cc-number"},
        "place_order": {"role": "button", "label": "Place Your Order"},
        "order_number": {"css": ".order-number"},
    },
    "steps": {
        "add_to_cart": [{"action": "goto", "url": "{{deal.url}}"},
                        {"action": "click", "target": "add_to_cart"}],
        "checkout_start": [{"action": "click", "target": "checkout_btn"}],
        "payment": [{"action": "fill", "target": "card_number", "value": "{{card.number}}"}],
        "place_order": [{"action": "click", "target": "place_order"},
                        {"action": "extract", "target": "order_number", "as": "order_number"}],
    },
    "success_signals": {"order_confirmed": {"css": ".order-number"}},
}


def test_a_well_formed_playbook_is_accepted_and_goes_live():
    res = playbooks.save(copy.deepcopy(GOOD))
    assert res["accepted"] and res["status"] == "active"
    assert playbooks.get_active("bestbuy.com")["version"] == GOOD["captured_at"]


def test_low_confidence_is_stored_but_not_made_live():
    pb = copy.deepcopy(GOOD); pb["confidence"] = 0.4
    res = playbooks.save(pb)
    assert res["accepted"] and res["status"] == "candidate"
    assert playbooks.get_active("bestbuy.com") is None


def test_unknown_action_is_rejected():
    pb = copy.deepcopy(GOOD)
    pb["steps"]["payment"] = [{"action": "eval_js", "target": "card_number"}]
    ok, errs = playbooks.validate(pb)
    assert not ok and any("unknown action" in e for e in errs)


def test_target_must_exist_in_selectors():
    pb = copy.deepcopy(GOOD)
    pb["steps"]["payment"] = [{"action": "click", "target": "ghost_button"}]
    ok, errs = playbooks.validate(pb)
    assert not ok and any("not defined in selectors" in e for e in errs)


def test_unknown_template_variable_is_rejected():
    pb = copy.deepcopy(GOOD)
    pb["steps"]["payment"][0]["value"] = "{{vault.everything}}"
    ok, errs = playbooks.validate(pb)
    assert not ok and any("unknown template variable" in e for e in errs)


def test_non_https_url_is_rejected():
    pb = copy.deepcopy(GOOD)
    pb["steps"]["add_to_cart"][0]["url"] = "javascript:alert(1)"
    ok, errs = playbooks.validate(pb)
    assert not ok and any("must be https" in e for e in errs)


def test_missing_required_flow_is_rejected():
    pb = copy.deepcopy(GOOD); del pb["steps"]["place_order"]
    ok, errs = playbooks.validate(pb)
    assert not ok and any("place_order" in e for e in errs)


def test_missing_success_signal_is_rejected():
    pb = copy.deepcopy(GOOD); del pb["success_signals"]
    ok, errs = playbooks.validate(pb)
    assert not ok and any("order_confirmed" in e for e in errs)


def test_new_version_retires_the_old_one_and_can_roll_back():
    playbooks.save(copy.deepcopy(GOOD))
    v2 = copy.deepcopy(GOOD); v2["captured_at"] = "2026-09-14T03:40:00Z"
    playbooks.save(v2)
    assert playbooks.get_active("bestbuy.com")["version"] == v2["captured_at"]
    restored = playbooks.rollback("bestbuy.com")
    assert restored["version"] == GOOD["captured_at"]
    assert playbooks.get_active("bestbuy.com")["version"] == GOOD["captured_at"]


def test_coverage_flags_merchants_with_no_playbook():
    cov = {c["domain"]: c for c in playbooks.coverage()}
    assert cov["nike.com"]["health"] == "missing"


# --- schema 1.1: shipping options, login flow, session check ---------------
V11 = {**GOOD, "schema_version": "1.1",
       "selectors": {**GOOD["selectors"],
                     "ship_std": {"text": "Standard"}, "ship_2d": {"text": "2-day"},
                     "email": {"css": "#email"}, "pw": {"css": "#pw"},
                     "account_menu": {"css": ".account-name"}},
       "shipping_options": {
           "standard": {"label": "Standard (free)", "cost": 0, "days": 4, "selector": "ship_std"},
           "two_day": {"label": "2-day", "cost": 9.99, "days": 2, "selector": "ship_2d"}},
       "session_check": {"signed_in": {"selector": "account_menu"},
                         "signed_out": {"url_contains": "/signin"}},
       "steps": {**GOOD["steps"],
                 "login": [{"action": "fill", "target": "email", "value": "{{account.email}}"},
                           {"action": "fill", "target": "pw", "value": "{{account.password}}"}]}}


def test_schema_11_with_shipping_and_login_is_accepted():
    ok, errs = playbooks.validate(copy.deepcopy(V11))
    assert ok, errs


def test_old_10_playbooks_still_validate():
    ok, errs = playbooks.validate(copy.deepcopy(GOOD))
    assert ok, errs


def test_shipping_option_pointing_at_a_missing_selector_is_rejected():
    pb = copy.deepcopy(V11)
    pb["shipping_options"]["two_day"]["selector"] = "does_not_exist"
    ok, errs = playbooks.validate(pb)
    assert not ok and any("is not in selectors" in e for e in errs)


def test_shipping_option_without_a_selector_is_rejected():
    pb = copy.deepcopy(V11)
    del pb["shipping_options"]["standard"]["selector"]
    ok, errs = playbooks.validate(pb)
    assert not ok and any("needs a 'selector'" in e for e in errs)


def test_non_numeric_shipping_cost_is_rejected():
    pb = copy.deepcopy(V11)
    pb["shipping_options"]["two_day"]["cost"] = "about ten bucks"
    ok, errs = playbooks.validate(pb)
    assert not ok and any("cost must be a number" in e for e in errs)


def test_session_check_needs_something_checkable():
    pb = copy.deepcopy(V11)
    pb["session_check"] = {"signed_in": {"vibes": "good"}}
    ok, errs = playbooks.validate(pb)
    assert not ok and any("session_check.signed_in needs" in e for e in errs)


def test_requires_login_without_a_login_flow_is_rejected():
    pb = copy.deepcopy(V11)
    pb["site_facts"] = {"requires_login": True}
    del pb["steps"]["login"]
    ok, errs = playbooks.validate(pb)
    assert not ok and any("no 'login' flow" in e for e in errs)


def test_login_flow_may_only_use_account_variables():
    pb = copy.deepcopy(V11)
    pb["steps"]["login"][0]["value"] = "{{card.number}}"     # allowed var, wrong place, still parses
    ok, _ = playbooks.validate(pb)
    assert ok
    pb["steps"]["login"][0]["value"] = "{{secrets.everything}}"
    ok, errs = playbooks.validate(pb)
    assert not ok and any("unknown template variable" in e for e in errs)
