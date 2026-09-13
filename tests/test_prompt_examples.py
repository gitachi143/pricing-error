"""The JSON examples we hand ChatGPT must satisfy the validators we hand the API.

If these drift apart, the nightly task produces output that gets rejected at 3am
and nobody notices until a deal is missed.
"""
import json
import re
from pathlib import Path

import pytest

from app import config
from app.api.routes import CompIn, ShippingBatch
from app.core import playbooks

PROMPTS = config.ROOT / "prompts"


def blocks(filename: str) -> list:
    text = (PROMPTS / filename).read_text()
    out = []
    for raw in re.findall(r"```json\n(.*?)```", text, re.S):
        try:
            out.append(json.loads(raw))
        except json.JSONDecodeError as e:
            pytest.fail(f"{filename} contains a ```json block that isn't valid JSON: {e}")
    return out


def test_recon_prompt_example_is_a_valid_playbook():
    pbs = [b for b in blocks("01_checkout_recon.md") if isinstance(b, dict) and "steps" in b]
    assert pbs, "the recon prompt should show at least one complete playbook"
    for pb in pbs:
        ok, errs = playbooks.validate(pb)
        assert ok, f"the example we give ChatGPT would be rejected: {errs}"


def test_recon_example_declares_shipping_options_pointing_at_real_selectors():
    pb = [b for b in blocks("01_checkout_recon.md") if isinstance(b, dict) and "steps" in b][0]
    opts = pb.get("shipping_options") or {}
    assert opts, "the example must demonstrate shipping_options"
    assert any(o.get("pickup") for o in opts.values()), "show a pickup option too"
    for name, o in opts.items():
        assert o["selector"] in pb["selectors"], f"{name} points at a missing selector"


def test_recon_example_has_a_login_flow_and_session_check():
    pb = [b for b in blocks("01_checkout_recon.md") if isinstance(b, dict) and "steps" in b][0]
    assert pb["steps"].get("login"), "show the login flow"
    assert pb.get("session_check", {}).get("signed_in")
    blob = json.dumps(pb["steps"]["login"])
    assert "{{account.email}}" in blob and "{{account.password}}" in blob
    assert "@" not in blob.replace("{{account.email}}", ""), "no literal credentials in the example"


def test_shipping_prompt_example_matches_the_ingest_schema():
    for b in blocks("02_shipping_tracker.md"):
        if isinstance(b, dict) and "updates" in b:
            ShippingBatch(**b)          # raises if the shape is wrong
            for u in b["updates"]:
                assert u["status"] in {"placed", "label_created", "in_transit", "out_for_delivery",
                                       "delivered", "exception", "cancelled", "unknown"}
            return
    pytest.fail("the shipping prompt should show a full payload")


def test_comps_prompt_example_matches_the_ingest_schema():
    for b in blocks("03_market_comps.md"):
        if isinstance(b, list) and b and isinstance(b[0], dict) and "market_price" in b[0]:
            for item in b:
                CompIn(**item)
            return
    pytest.fail("the comps prompt should show a full payload")


def test_prompts_reference_endpoints_that_exist():
    from app.main import app
    routes = {getattr(r, "path", "") for r in app.routes}
    for f in PROMPTS.glob("*.md"):
        for path in re.findall(r"/api/[a-z0-9/_-]+", f.read_text()):
            assert path in routes, f"{f.name} points at {path}, which this app does not serve"
