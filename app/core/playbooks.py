"""Checkout playbooks: deterministic, LLM-free instructions for one merchant.

A nightly ChatGPT scheduled task walks each site and posts one of these to
/api/ingest/playbook. The worker replays it verbatim — no model in the loop at
purchase time, which is the only way to be fast enough for a price error.

Lifecycle:  candidate --(smoke pass)--> active --(new version)--> retired
            candidate --(smoke fail)--> rolled_back  (last active stays live)
"""
from __future__ import annotations

import json
import time
from typing import Any

from .. import db

SCHEMA_VERSION = "1.1"
ACCEPTED_VERSIONS = {"1.0", "1.1"}   # 1.0 playbooks keep working, just without
                                     # shipping options or a login flow

ACTIONS = {
    "goto":        {"url"},
    "click":       {"target"},
    "fill":        {"target", "value"},
    "select":      {"target", "value"},
    "check":       {"target"},
    "uncheck":     {"target"},
    "press":       {"key"},
    "wait_for":    {"target"},
    "wait_for_url":{"contains"},
    "assert_text": {"target"},
    "assert_url":  {"contains"},
    "extract":     {"target", "as"},
    "scroll_to":   {"target"},
    "sleep":       {"ms"},
    "iframe":      {"target", "steps"},
    "set_qty":     {"target", "value"},
}

FLOWS = ["login", "add_to_cart", "open_cart", "checkout_start", "shipping", "payment",
         "review", "place_order"]
REQUIRED_FLOWS = ["add_to_cart", "checkout_start", "payment", "place_order"]

# Template variables a playbook may reference. Anything else is rejected so a
# bad playbook can't exfiltrate data it shouldn't see.
VAR_NAMESPACES = {
    "deal": {"url", "qty", "size", "sku", "product_name", "max_price"},
    "account": {"email", "password", "phone", "first_name", "last_name"},
    "address": {"line1", "line2", "city", "state", "zip", "country"},
    "card": {"number", "name", "month", "year", "cvv", "zip"},
    "shipping": {"method"},
}

MAX_STEPS = 120


def _vars_in(value: str) -> list[str]:
    import re
    return re.findall(r"\{\{\s*([a-zA-Z0-9_.]+)\s*\}\}", str(value))


def validate(pb: dict) -> tuple[bool, list[str]]:
    """Returns (ok, problems). Strict on purpose — this drives a browser that
    spends money."""
    errs: list[str] = []
    if not isinstance(pb, dict):
        return False, ["playbook must be a JSON object"]

    if str(pb.get("schema_version", "")) not in ACCEPTED_VERSIONS:
        errs.append(f"schema_version must be one of {sorted(ACCEPTED_VERSIONS)}")
    merchant = str(pb.get("merchant", "")).strip().lower()
    if not merchant or "." not in merchant:
        errs.append("merchant must be a bare domain, e.g. 'bestbuy.com'")
    if merchant.startswith("www."):
        errs.append("merchant must not include 'www.'")

    selectors = pb.get("selectors")
    if not isinstance(selectors, dict) or not selectors:
        errs.append("selectors must be a non-empty object of logical_name -> locator")
        selectors = {}
    for name, loc in (selectors or {}).items():
        if not isinstance(loc, dict):
            errs.append(f"selector '{name}' must be an object")
            continue
        if not any(k in loc for k in ("css", "xpath", "text", "role", "label", "test_id")):
            errs.append(f"selector '{name}' needs at least one of css/xpath/text/role/label/test_id")

    steps_by_flow = pb.get("steps")
    if not isinstance(steps_by_flow, dict):
        return False, errs + ["steps must be an object keyed by flow name"]
    for flow in REQUIRED_FLOWS:
        if not steps_by_flow.get(flow):
            errs.append(f"missing required flow '{flow}'")
    for flow in steps_by_flow:
        if flow not in FLOWS:
            errs.append(f"unknown flow '{flow}' (allowed: {', '.join(FLOWS)})")

    total = 0

    def check_steps(steps: Any, path: str):
        nonlocal total
        if not isinstance(steps, list):
            errs.append(f"{path} must be a list of steps")
            return
        for i, st in enumerate(steps):
            total += 1
            loc = f"{path}[{i}]"
            if not isinstance(st, dict):
                errs.append(f"{loc} must be an object"); continue
            act = st.get("action")
            if act not in ACTIONS:
                errs.append(f"{loc}: unknown action '{act}' (allowed: {', '.join(sorted(ACTIONS))})")
                continue
            for req in ACTIONS[act]:
                if req not in st:
                    errs.append(f"{loc}: action '{act}' requires '{req}'")
            tgt = st.get("target")
            if tgt and tgt not in selectors and act != "iframe":
                errs.append(f"{loc}: target '{tgt}' is not defined in selectors")
            url = str(st.get("url", ""))
            if url and not (url.startswith("https://") or url.startswith("{{")):
                errs.append(f"{loc}: url must be https:// (got '{url[:40]}')")
            for v in _vars_in(st.get("value", "")) + _vars_in(url):
                ns, _, key = v.partition(".")
                if ns not in VAR_NAMESPACES or key not in VAR_NAMESPACES[ns]:
                    errs.append(f"{loc}: unknown template variable '{{{{{v}}}}}'")
            if act == "iframe":
                check_steps(st.get("steps"), f"{loc}.steps")

    for flow, steps in steps_by_flow.items():
        check_steps(steps, flow)
    if total > MAX_STEPS:
        errs.append(f"{total} steps exceeds the {MAX_STEPS} cap — split or simplify")
    if total == 0:
        errs.append("playbook contains no steps")

    # shipping options: each named option must point at a selector we can click
    ship = pb.get("shipping_options")
    if ship is not None:
        if not isinstance(ship, dict):
            errs.append("shipping_options must be an object of name -> option")
        else:
            for name, opt in ship.items():
                if not isinstance(opt, dict):
                    errs.append(f"shipping_options.{name} must be an object"); continue
                sel = opt.get("selector")
                if not sel:
                    errs.append(f"shipping_options.{name} needs a 'selector'")
                elif sel not in selectors:
                    errs.append(f"shipping_options.{name}.selector '{sel}' is not in selectors")
                if opt.get("cost") is not None:
                    try:
                        float(opt["cost"])
                    except (TypeError, ValueError):
                        errs.append(f"shipping_options.{name}.cost must be a number")
                if opt.get("days") is not None:
                    try:
                        float(opt["days"])
                    except (TypeError, ValueError):
                        errs.append(f"shipping_options.{name}.days must be a number")

    # session_check: how the worker tells signed-in from signed-out
    sc = pb.get("session_check")
    if sc is not None:
        if not isinstance(sc, dict):
            errs.append("session_check must be an object")
        elif not (sc.get("signed_in") or sc.get("signed_out")):
            errs.append("session_check needs 'signed_in' and/or 'signed_out'")
        else:
            for k in ("signed_in", "signed_out"):
                spec = sc.get(k)
                if spec is None:
                    continue
                if not isinstance(spec, dict) or not any(
                        x in spec for x in ("css", "text", "url_contains", "test_id", "selector")):
                    errs.append(f"session_check.{k} needs css/text/url_contains/test_id/selector")
                if spec.get("selector") and spec["selector"] not in selectors:
                    errs.append(f"session_check.{k}.selector '{spec['selector']}' is not in selectors")

    if (pb.get("site_facts") or {}).get("requires_login") and not steps_by_flow.get("login"):
        errs.append("site_facts.requires_login is true but there is no 'login' flow — "
                    "either record one or set requires_login false and rely on a saved session")

    ss = pb.get("success_signals") or {}
    if not ss.get("order_confirmed"):
        errs.append("success_signals.order_confirmed is required (how we know the order went through)")

    sf = pb.get("site_facts") or {}
    if not isinstance(sf, dict):
        errs.append("site_facts must be an object")

    return (not errs), errs


def save(pb: dict, generated_by: str = "chatgpt") -> dict:
    ok, errs = validate(pb)
    merchant = str(pb.get("merchant", "")).strip().lower()
    version = str(pb.get("captured_at") or time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
    if not ok:
        return {"accepted": False, "errors": errs, "merchant": merchant}

    pb_id = db.insert("playbooks", {
        "merchant": merchant, "version": version, "created_at": time.time(),
        "status": "candidate", "confidence": float(pb.get("confidence") or 0),
        "smoke_status": "untested", "generated_by": generated_by,
        "json": json.dumps(pb, separators=(",", ":")),
    })
    # High-confidence playbooks go live immediately; the previous one is kept so
    # a mid-day site change can be rolled back in one click.
    auto = float(pb.get("confidence") or 0) >= 0.75
    if auto:
        promote(pb_id)
    return {"accepted": True, "id": pb_id, "merchant": merchant, "version": version,
            "status": "active" if auto else "candidate",
            "warnings": [] if auto else ["confidence < 0.75 — left as candidate, promote it manually"]}


def promote(pb_id: int) -> None:
    row = db.q1("SELECT merchant FROM playbooks WHERE id=?", (pb_id,))
    if not row:
        return
    db.execute("UPDATE playbooks SET status='retired' WHERE merchant=? AND status='active'",
               (row["merchant"],))
    db.execute("UPDATE playbooks SET status='active' WHERE id=?", (pb_id,))


def rollback(merchant: str) -> dict | None:
    """Demote the live playbook and restore the most recent retired one."""
    live = db.q1("SELECT * FROM playbooks WHERE merchant=? AND status='active'", (merchant,))
    prev = db.q1("SELECT * FROM playbooks WHERE merchant=? AND status='retired' "
                 "ORDER BY created_at DESC LIMIT 1", (merchant,))
    if not prev:
        return None
    if live:
        db.execute("UPDATE playbooks SET status='rolled_back' WHERE id=?", (live["id"],))
    db.execute("UPDATE playbooks SET status='active' WHERE id=?", (prev["id"],))
    return prev


def get_active(merchant: str) -> dict | None:
    row = db.q1("SELECT * FROM playbooks WHERE merchant=? AND status='active' "
                "ORDER BY created_at DESC LIMIT 1", (merchant,))
    if not row:
        return None
    try:
        row["playbook"] = json.loads(row["json"])
    except Exception:
        return None
    return row


def coverage() -> list[dict]:
    """One row per merchant: is there a live playbook, and how stale is it?"""
    rows = db.q("""
        SELECT m.domain, m.name, m.checkout_difficulty,
               p.id, p.version, p.created_at, p.confidence, p.smoke_status, p.status
        FROM merchants m
        LEFT JOIN playbooks p ON p.merchant=m.domain AND p.status='active'
        ORDER BY m.domain""")
    now = time.time()
    for r in rows:
        age_h = (now - r["created_at"]) / 3600 if r["created_at"] else None
        r["age_hours"] = round(age_h, 1) if age_h is not None else None
        r["health"] = ("missing" if not r["id"] else
                       "stale" if age_h and age_h > 36 else
                       "aging" if age_h and age_h > 26 else "fresh")
    return rows
