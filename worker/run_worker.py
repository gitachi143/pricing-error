#!/usr/bin/env python3
"""Checkout worker — runs on YOUR machine, not on Azure.

It polls the dashboard for jobs and drives a real browser:

    prep   → open the product page, add to cart, fill shipping + payment,
             stop at the review screen and hold the tab open
    place  → finish the order (re-using the tab prep left open, if it's still there)

Nothing here talks to a model. It replays last night's playbook verbatim, which
is why it takes seconds instead of a minute.

    pip install -r requirements-worker.txt
    python3 -m playwright install chromium
    DEALDESK_URL=https://your-app.azurewebsites.net WORKER_TOKEN=… python3 worker/run_worker.py

Sign-ins are kept in a persistent browser profile (~/.dealdesk-profile), so you
log in once per merchant and stay logged in. To do that first sign-in by hand:

    python3 worker/run_worker.py --login bestbuy.com

Safety:
  * Starts in DRY-RUN. Everything runs except the final "place order" click.
    Set WORKER_DRY_RUN=0 when you're ready to spend money.
  * A price guard re-reads the total on the review page and refuses to pay if the
    error has already been fixed.
  * It makes no attempt to disguise itself as a human. If a site blocks
    automation, the run fails, says so, and moves on.
"""
from __future__ import annotations

import json
import os
import platform
import random
import signal
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))


def _load_config():
    """Read the config file launchd points us at, so the agent doesn't depend on
    a shell profile being loaded."""
    path = os.environ.get("DEALDESK_CONFIG") or str(Path(__file__).resolve().parent / "config.env")
    if not Path(path).exists():
        return
    for line in Path(path).read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


_load_config()

import httpx
from executor import (RunLog, StepError, check_failure_signals, check_session,
                      locate, money_on_page, run_steps)
from vault import card_for, context_for, has_credentials, load as load_vault

VERSION = "1.2"
BASE = os.environ.get("DEALDESK_URL", "http://127.0.0.1:8080").rstrip("/")
TOKEN = os.environ.get("WORKER_TOKEN", "")
WORKER_ID = os.environ.get("WORKER_ID", os.uname().nodename)
DRY_RUN = os.environ.get("WORKER_DRY_RUN", "1") not in ("0", "false", "no")
HEADLESS = os.environ.get("WORKER_HEADLESS", "0") in ("1", "true", "yes")
POLL_SECONDS = float(os.environ.get("WORKER_POLL", "1.5"))
PROFILE_DIR = Path(os.environ.get("WORKER_PROFILE", Path.home() / ".dealdesk-profile"))
SESSION_SWEEP_MINUTES = float(os.environ.get("WORKER_SESSION_SWEEP", "60"))

HTTP = httpx.Client(base_url=BASE, headers={"Authorization": f"Bearer {TOKEN}"}, timeout=30)
PARKED: dict[int, dict] = {}       # deal_id -> {"context":…, "page":…, "at":…}
FLOWS_TO_REVIEW = ["add_to_cart", "open_cart", "checkout_start", "shipping", "payment", "review"]

STATS = {"started_at": time.time(), "jobs_done": 0, "jobs_failed": 0, "orders_placed": 0,
         "last_job_at": None, "last_job": "", "last_error": "", "reconnects": 0,
         "browser_ok": None}
RUNNING = True


def log(msg: str):
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}", flush=True)


def _shutdown(signum, _frame):
    """launchd sends SIGTERM on logout/restart. Finish cleanly so a half-done
    checkout isn't left holding a browser."""
    global RUNNING
    RUNNING = False
    log(f"signal {signum} — shutting down")


signal.signal(signal.SIGTERM, _shutdown)
signal.signal(signal.SIGINT, _shutdown)


def heartbeat(stopping: bool = False):
    """Tell the dashboard everything it needs to show a real status page."""
    HTTP.post("/api/worker/heartbeat", json={
        "worker_id": WORKER_ID,
        "version": VERSION,
        "state": "stopping" if stopping else "running",
        "dry_run": DRY_RUN,
        "headless": HEADLESS,
        "uptime_seconds": int(time.time() - STATS["started_at"]),
        "jobs_done": STATS["jobs_done"],
        "jobs_failed": STATS["jobs_failed"],
        "orders_placed": STATS["orders_placed"],
        "last_job": STATS["last_job"],
        "last_job_at": STATS["last_job_at"],
        "last_error": STATS["last_error"],
        "reconnects": STATS["reconnects"],
        "browser_ok": STATS["browser_ok"],
        "parked_carts": len(PARKED),
        "host": platform.node(),
        "os": f"{platform.system()} {platform.mac_ver()[0] or platform.release()}",
        "python": platform.python_version(),
        "profile": str(PROFILE_DIR),
    })


def report(job_id: int, state: str, stage: str, message: str, **extra):
    if state == "done":
        STATS["jobs_done"] += 1
        STATS["orders_placed"] += len([c for c in extra.get("charges", [])
                                       if c.get("order_number") not in (None, "", "DRYRUN")])
    else:
        STATS["jobs_failed"] += 1
        STATS["last_error"] = f"{stage}: {message}"[:200]
    for attempt in range(3):
        try:
            HTTP.post(f"/api/worker/jobs/{job_id}/result",
                      json={"state": state, "stage": stage, "message": message,
                            "order_number": extra.pop("order_number", None),
                            "charges": extra.pop("charges", []), "data": extra})
            return
        except Exception as e:
            # An order may already be placed — this result must not be lost to a
            # dropped connection.
            if attempt == 2:
                log(f"could not report job {job_id} after 3 tries: {e}")
                _spool(job_id, state, stage, message, extra)
            else:
                time.sleep(2 * (attempt + 1))


def _spool(job_id, state, stage, message, extra):
    """Last resort: write the result next to the worker so it isn't lost."""
    try:
        path = PROFILE_DIR.parent / "unreported_results.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a") as fh:
            fh.write(json.dumps({"ts": time.time(), "job_id": job_id, "state": state,
                                 "stage": stage, "message": message, "data": extra}) + "\n")
        log(f"result for job {job_id} written to {path}")
    except Exception:
        pass


def flush_spool():
    """Send anything that was written while the server was unreachable."""
    path = PROFILE_DIR.parent / "unreported_results.jsonl"
    if not path.exists():
        return
    try:
        lines = [l for l in path.read_text().splitlines() if l.strip()]
        kept = []
        for line in lines:
            try:
                r = json.loads(line)
                HTTP.post(f"/api/worker/jobs/{r['job_id']}/result",
                          json={"state": r["state"], "stage": r["stage"],
                                "message": r["message"], "data": r.get("data", {})})
            except Exception:
                kept.append(line)
        if kept:
            path.write_text("\n".join(kept) + "\n")
        else:
            path.unlink()
            log("flushed queued job results to the server")
    except Exception:
        pass


def report_session(merchant: str, signed_in: bool, has_creds: bool,
                   manual: bool = False, note: str = ""):
    try:
        HTTP.post("/api/worker/sessions", json=[{
            "merchant": merchant, "signed_in": signed_in, "has_credentials": has_creds,
            "needs_manual_login": manual, "note": note}])
    except Exception as e:
        log(f"could not report session for {merchant}: {e}")


def new_page(pw):
    """One persistent browser profile keeps you logged in between runs."""
    try:
        ctx = pw.chromium.launch_persistent_context(
            user_data_dir=str(PROFILE_DIR), headless=HEADLESS,
            viewport={"width": 1440, "height": 900})
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.set_default_timeout(12000)
        STATS["browser_ok"] = True
        return ctx, page
    except Exception as e:
        STATS["browser_ok"] = False
        STATS["last_error"] = f"browser failed to launch: {type(e).__name__}"
        raise


# --------------------------------------------------------------------------
# sign-in
# --------------------------------------------------------------------------
def ensure_signed_in(page, playbook: dict, vault: dict, merchant: str,
                     log_obj: RunLog) -> bool:
    """Returns True if we're good to proceed. Raises StepError if we're locked out.

    The happy path costs one DOM check: the saved profile is still signed in and
    we carry on. Only when that fails do we type a password.
    """
    needs = bool((playbook.get("site_facts") or {}).get("requires_login"))
    has_check = bool(playbook.get("session_check"))
    if not needs and not has_check:
        return True

    creds = has_credentials(vault, merchant)
    state = check_session(page, playbook, log_obj)

    if state is True:
        report_session(merchant, True, creds)
        return True
    if state is None and not needs:
        return True     # can't tell, site doesn't demand it — carry on

    if not playbook.get("steps", {}).get("login") or not creds:
        report_session(merchant, False, creds, manual=True,
                       note="no saved session" + ("" if creds else " and no stored credentials"))
        raise StepError(
            f"not signed in to {merchant} and "
            + ("no login flow in the playbook" if not playbook.get("steps", {}).get("login")
               else "no credentials in the local vault")
            + f" — run: python3 worker/run_worker.py --login {merchant}",
            stage="login")

    log_obj.stage = "login"
    log_obj.add(f"— login ({merchant}) —")
    ctx_vars = context_for(vault, {"merchant": merchant}, {"payload": {}}, None)
    try:
        run_steps(page, playbook["steps"]["login"], playbook.get("selectors", {}),
                  ctx_vars, log_obj)
    except StepError as e:
        report_session(merchant, False, creds, manual=True, note=str(e)[:180])
        raise StepError(f"automatic sign-in to {merchant} failed ({e}) — this is usually 2FA or a "
                        f"challenge. Run: python3 worker/run_worker.py --login {merchant}",
                        stage="login")

    if check_session(page, playbook, log_obj) is False:
        report_session(merchant, False, creds, manual=True,
                       note="login flow ran but the session check still says signed out")
        raise StepError(f"signed in to {merchant} but the session check still fails — "
                        f"run: python3 worker/run_worker.py --login {merchant}", stage="login")
    report_session(merchant, True, creds, note="signed in automatically")
    return True


def manual_login(merchant: str):
    """Open a real browser window and let a human sign in — 2FA, captcha, whatever.
    The profile keeps the cookies afterwards."""
    from playwright.sync_api import sync_playwright
    vault = load_vault()
    pb = None
    try:
        pb = HTTP.get("/api/playbooks", params={"merchant": merchant}).json()
        pb = pb[0] if pb else None
        if pb:
            pb = HTTP.get(f"/api/playbooks/{pb['id']}").json().get("json")
    except Exception:
        pass

    url = f"https://www.{merchant}/"
    if pb:
        url = ((pb.get("site_facts") or {}).get("login_url")
               or (pb.get("site_facts") or {}).get("cart_url") or url)

    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            user_data_dir=str(PROFILE_DIR), headless=False,
            viewport={"width": 1440, "height": 900})
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto(url, wait_until="domcontentloaded")
        print(f"\n  A browser window is open at {url}")
        print(f"  Sign in to {merchant} by hand (do the 2FA, tick 'remember me').")
        input("  Press Enter here when you're signed in… ")
        state = None
        if pb:
            state = check_session(page, pb, RunLog())
        creds = has_credentials(vault, merchant)
        if state is False:
            print(f"  ⚠ The session check still says signed out. The playbook's "
                  f"session_check may be stale — tonight's recon will refresh it.")
        else:
            print(f"  ✓ Saved. {merchant} should stay signed in from now on.")
        report_session(merchant, state is not False, creds,
                       manual=(state is False), note="manual sign-in")
        ctx.close()


def sweep_sessions(pw, vault: dict):
    """Periodically check which merchants we're still signed in to, so the
    dashboard can warn you before a deal lands rather than during one."""
    try:
        merchants = [m["domain"] for m in HTTP.get("/api/sessions").json()
                     if m.get("login_required")]
    except Exception:
        return
    if not merchants:
        return
    ctx, page = new_page(pw)
    try:
        for merchant in merchants[:12]:
            try:
                pb = HTTP.get("/api/playbooks", params={"merchant": merchant}).json()
                pb = [p for p in pb if p["status"] == "active"]
                if not pb:
                    continue
                full = HTTP.get(f"/api/playbooks/{pb[0]['id']}").json().get("json") or {}
                if not full.get("session_check"):
                    continue
                page.goto((full.get("site_facts") or {}).get("cart_url")
                          or f"https://www.{merchant}/", wait_until="domcontentloaded")
                state = check_session(page, full)
                report_session(merchant, state is True, has_credentials(vault, merchant),
                               manual=(state is False and not has_credentials(vault, merchant)),
                               note="periodic check")
            except Exception as e:
                log(f"session sweep {merchant}: {type(e).__name__}")
    finally:
        _close(ctx)


# --------------------------------------------------------------------------
# shipping
# --------------------------------------------------------------------------
def apply_shipping(page, playbook: dict, choice: dict, log_obj: RunLog):
    """Click the shipping option the server picked from your merchant settings."""
    if not choice or not choice.get("selector"):
        if choice and choice.get("reason"):
            log_obj.add(f"shipping: {choice['reason']}")
        return
    try:
        locate(page, playbook.get("selectors", {}), choice["selector"]).first.click(timeout=8000)
        log_obj.add(f"shipping: chose {choice.get('label') or choice['selector']} "
                    f"({'free' if not choice.get('cost') else '$%.2f' % choice['cost']}) "
                    f"— {choice.get('reason', '')}")
    except Exception as e:
        # Not fatal: the site default still ships the item, it just may be slower.
        log_obj.add(f"shipping: could not select {choice.get('label')} "
                    f"({type(e).__name__}) — leaving the site default")


def run_flows(page, playbook: dict, ctx_vars: dict, flows: list[str], log_obj: RunLog,
              shipping_choice: dict | None = None):
    selectors = playbook.get("selectors", {})
    steps = playbook.get("steps", {})
    for flow in flows:
        if not steps.get(flow):
            if flow == "shipping" and shipping_choice:
                apply_shipping(page, playbook, shipping_choice, log_obj)
            continue
        log_obj.stage = flow
        log_obj.add(f"— {flow} —")
        run_steps(page, steps[flow], selectors, ctx_vars, log_obj)
        if flow == "shipping" and shipping_choice:
            apply_shipping(page, playbook, shipping_choice, log_obj)
        sig = check_failure_signals(page, playbook, log_obj)
        if sig:
            raise StepError(sig, stage=flow)


# --------------------------------------------------------------------------
# jobs
# --------------------------------------------------------------------------
def handle_prep(pw, job, deal, playbook, vault):
    """Get the cart to the review page while research is still running."""
    deal_id = deal["id"]
    merchant = deal.get("merchant", "")
    log_obj = RunLog()
    ctx, page = new_page(pw)
    try:
        vars_ = context_for(vault, deal, job, None)
        payload = job.get("payload", {})
        page.goto(payload.get("url") or deal.get("url"), wait_until="domcontentloaded")
        ensure_signed_in(page, playbook, vault, merchant, log_obj)
        run_flows(page, playbook, vars_, FLOWS_TO_REVIEW[:-1], log_obj,
                  shipping_choice=payload.get("shipping"))
        PARKED[deal_id] = {"context": ctx, "page": page, "at": time.time()}
        report(job["id"], "done", "review", "cart parked at the review page — one click from paid",
               lines=log_obj.lines[-12:])
        log(f"deal {deal_id}: parked and waiting")
    except StepError as e:
        _close(ctx)
        report(job["id"], "failed", e.stage or log_obj.stage, str(e),
               selector_missing=e.selector, lines=log_obj.lines[-12:])
        log(f"deal {deal_id}: prep failed — {e}")
    except Exception as e:
        _close(ctx)
        report(job["id"], "failed", log_obj.stage, f"{type(e).__name__}: {e}",
               lines=log_obj.lines[-12:])


def handle_place(pw, job, deal, playbook, vault):
    """Finish the purchase, once per card in the payment plan."""
    deal_id = deal["id"]
    merchant = deal.get("merchant", "")
    payload = job.get("payload", {})
    charges = payload.get("charges", [])
    ship = payload.get("shipping")
    placed, log_obj = [], RunLog()

    for i, charge in enumerate(charges):
        card = card_for(vault, charge["card_id"], charge.get("card_label", ""))
        vars_ = context_for(vault, deal, job, card)
        vars_["deal"]["qty"] = str(charge["qty"])
        if ship and ship.get("label"):
            vars_["shipping"]["method"] = ship["label"]
        parked = PARKED.pop(deal_id, None) if i == 0 else None
        reuse = bool(parked and time.time() - parked["at"] < 900)
        ctx, page = (parked["context"], parked["page"]) if reuse else new_page(pw)
        try:
            if reuse:
                log_obj.add(f"re-using the cart parked {int(time.time() - parked['at'])}s ago")
                run_flows(page, playbook, vars_, ["payment"], log_obj)
            else:
                page.goto(payload.get("url") or deal.get("url"), wait_until="domcontentloaded")
                ensure_signed_in(page, playbook, vault, merchant, log_obj)
                run_flows(page, playbook, vars_, FLOWS_TO_REVIEW, log_obj, shipping_choice=ship)

            # Price guard: the error may already be fixed. Never pay more than planned.
            selectors = playbook.get("selectors", {})
            total = money_on_page(page, selectors)
            ship_cost = float((ship or {}).get("cost") or 0)
            cap = float(payload.get("max_price") or 0) * int(charge["qty"]) * 1.35 + ship_cost
            if total and cap and total > cap:
                raise StepError(
                    f"review page shows ${total:,.2f}, above the ${cap:,.2f} guard rail — "
                    f"the price error is gone, not buying", stage="review")
            if total:
                log_obj.add(f"review total ${total:,.2f} (guard ${cap:,.2f})")

            if DRY_RUN:
                log_obj.add("DRY RUN — stopping before the place-order click")
                placed.append({**charge, "order_number": "DRYRUN",
                               "shipping_method": (ship or {}).get("label", ""),
                               "shipping_cost": ship_cost})
            else:
                log_obj.stage = "place_order"
                run_steps(page, playbook["steps"]["place_order"], selectors, vars_, log_obj)
                num = log_obj.extracted.get("order_number", "")
                placed.append({**charge, "order_number": num,
                               "shipping_method": (ship or {}).get("label", ""),
                               "shipping_cost": ship_cost})
                log(f"deal {deal_id}: ORDER PLACED {num} on {charge['card_label']}")
        except StepError as e:
            report(job["id"], "failed", e.stage or log_obj.stage, str(e),
                   selector_missing=e.selector, card=charge.get("card_label"),
                   lines=log_obj.lines[-15:], placed_so_far=placed)
            _close(ctx)
            return
        except Exception as e:
            report(job["id"], "failed", log_obj.stage, f"{type(e).__name__}: {e}",
                   lines=log_obj.lines[-15:], placed_so_far=placed)
            _close(ctx)
            return
        finally:
            _close(ctx)

    report(job["id"], "done", "place_order",
           ("DRY RUN — " if DRY_RUN else "") +
           f"{sum(c['qty'] for c in placed)} unit(s) across {len(placed)} card(s)"
           + (f" · {ship['label']}" if ship and ship.get("label") else ""),
           charges=placed,
           order_number=placed[0].get("order_number") if placed else None,
           lines=log_obj.lines[-15:])


def _close(ctx):
    try:
        ctx.close()
    except Exception:
        pass


def main():
    if "--login" in sys.argv:
        idx = sys.argv.index("--login")
        if idx + 1 >= len(sys.argv):
            raise SystemExit("usage: python3 worker/run_worker.py --login <merchant.com>")
        if not TOKEN:
            raise SystemExit("Set WORKER_TOKEN (it's in the server's .env).")
        manual_login(sys.argv[idx + 1].lower().strip())
        return

    if not TOKEN:
        raise SystemExit("Set WORKER_TOKEN (it's in the server's .env, or config.env here).")
    vault = load_vault()
    from playwright.sync_api import sync_playwright
    log(f"worker {WORKER_ID} v{VERSION} → {BASE}")
    log(f"  {'DRY RUN — nothing will be paid for' if DRY_RUN else 'LIVE — this will spend money'}"
        f" · {'headless' if HEADLESS else 'visible browser'}")
    log(f"  profile: {PROFILE_DIR} (sign-ins persist here)")

    try:
        flush_spool()
    except Exception:
        pass

    last_beat = 0.0
    last_sweep = 0.0
    last_tick = time.time()
    backoff = 0.0
    offline_since = None

    with sync_playwright() as pw:
        while RUNNING:
            try:
                # Laptop slept and woke: drop any parked carts, they're stale.
                now = time.time()
                if now - last_tick > 180 and PARKED:
                    log(f"woke after {int(now - last_tick)}s asleep — dropping "
                        f"{len(PARKED)} parked cart(s)")
                    for st in list(PARKED.values()):
                        _close(st.get("context"))
                    PARKED.clear()
                last_tick = now

                if now - last_beat > 30 or offline_since:
                    heartbeat()              # raises while the server is still down
                    last_beat = now
                    if offline_since:
                        STATS["reconnects"] += 1
                        log(f"reconnected after {int(now - offline_since)}s offline")
                        offline_since = None
                        flush_spool()
                        heartbeat()          # resend so the count is current straight away
                    backoff = 0.0

                if now - last_sweep > SESSION_SWEEP_MINUTES * 60:
                    last_sweep = now
                    sweep_sessions(pw, vault)

                r = HTTP.get("/api/worker/next", params={"worker_id": WORKER_ID}).json()
                job = r.get("job")
                if not job:
                    time.sleep(POLL_SECONDS)
                    continue

                deal, playbook = r.get("deal"), r.get("playbook")
                log(f"job {job['id']} · {job['kind']} · deal {job.get('deal_id')} "
                    f"· {(deal or {}).get('merchant', '?')}")
                STATS["last_job_at"] = time.time()
                STATS["last_job"] = f"{job['kind']} · {(deal or {}).get('merchant', '?')}"
                if not playbook:
                    report(job["id"], "failed", "playbook",
                           "no live playbook for this merchant")
                    continue
                if job["kind"] == "prep":
                    handle_prep(pw, job, deal, playbook, vault)
                elif job["kind"] == "place":
                    handle_place(pw, job, deal, playbook, vault)
                else:
                    report(job["id"], "done", "", f"ignored job kind {job['kind']}")

            except (httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadTimeout,
                    httpx.RemoteProtocolError, httpx.PoolTimeout) as e:
                # The server is unreachable — wifi dropped, Azure is restarting,
                # the laptop just woke up. Back off, keep trying, say so once.
                if offline_since is None:
                    offline_since = time.time()
                    log(f"server unreachable ({type(e).__name__}) — retrying with backoff")
                STATS["last_error"] = f"{type(e).__name__}"
                backoff = min(60.0, (backoff or 1.0) * 1.8)
                time.sleep(backoff + random.uniform(0, backoff * 0.25))

            except httpx.HTTPStatusError as e:
                code = e.response.status_code if e.response is not None else 0
                if code == 401:
                    log("401 from the server — WORKER_TOKEN is wrong. Re-run the installer "
                        "with a fresh link from the Setup page.")
                    time.sleep(60)
                else:
                    log(f"server said {code}; retrying shortly")
                    time.sleep(10)

            except KeyboardInterrupt:
                break
            except Exception as e:
                STATS["last_error"] = f"{type(e).__name__}: {e}"
                log(f"loop error: {type(e).__name__}: {e}")
                time.sleep(5)

    for st in list(PARKED.values()):
        _close(st.get("context"))
    try:
        heartbeat(stopping=True)
    except Exception:
        pass
    log("stopped")


if __name__ == "__main__":
    main()
