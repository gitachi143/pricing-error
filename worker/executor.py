"""Replays a playbook with Playwright. Deterministic: no model, no improvisation.

If a selector is missing the run stops and says which one — the server uses that
to roll the merchant back to the previous playbook version.
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass, field


class StepError(Exception):
    def __init__(self, msg: str, selector: str = "", stage: str = ""):
        super().__init__(msg)
        self.selector = selector
        self.stage = stage


@dataclass
class RunLog:
    stage: str = ""
    lines: list[str] = field(default_factory=list)
    extracted: dict = field(default_factory=dict)

    def add(self, msg: str):
        self.lines.append(f"{time.strftime('%H:%M:%S')} {msg}")
        print(f"    {msg}")


def render(value, ctx: dict) -> str:
    """Substitute {{namespace.key}} from the context."""
    def sub(m):
        ns, _, key = m.group(1).strip().partition(".")
        return str(ctx.get(ns, {}).get(key, ""))
    return re.sub(r"\{\{([^}]+)\}\}", sub, str(value))


def locate(page, selectors: dict, name: str):
    spec = selectors.get(name)
    if not spec:
        raise StepError(f"selector '{name}' is not defined in this playbook", selector=name)
    if "test_id" in spec:
        return page.get_by_test_id(spec["test_id"])
    if "role" in spec:
        return page.get_by_role(spec["role"], name=spec.get("label") or spec.get("text"))
    if "label" in spec:
        return page.get_by_label(spec["label"])
    if "css" in spec:
        return page.locator(spec["css"])
    if "xpath" in spec:
        return page.locator("xpath=" + spec["xpath"])
    if "text" in spec:
        return page.get_by_text(spec["text"], exact=False)
    raise StepError(f"selector '{name}' has no usable locator", selector=name)


def run_steps(page, steps: list, selectors: dict, ctx: dict, log: RunLog,
              timeout_ms: int = 12000) -> None:
    for st in steps or []:
        act = st.get("action")
        target = st.get("target")
        optional = bool(st.get("optional"))
        to = int(st.get("timeout_ms") or timeout_ms)
        try:
            if act == "goto":
                url = render(st["url"], ctx)
                page.goto(url, timeout=to * 2, wait_until="domcontentloaded")
                log.add(f"goto {url}")
            elif act == "sleep":
                page.wait_for_timeout(int(st["ms"])); log.add(f"sleep {st['ms']}ms")
            elif act == "press":
                page.keyboard.press(st["key"]); log.add(f"press {st['key']}")
            elif act == "wait_for_url":
                page.wait_for_url(f"**{render(st['contains'], ctx)}**", timeout=to)
                log.add(f"url now contains {st['contains']}")
            elif act == "assert_url":
                want = render(st["contains"], ctx)
                if want not in page.url:
                    raise StepError(f"expected URL to contain '{want}', got {page.url}")
                log.add(f"url ok ({want})")
            elif act == "iframe":
                spec = selectors.get(target) or {}
                frame = page.frame_locator(spec.get("css") or spec.get("xpath") or target)
                run_steps(frame, st.get("steps", []), selectors, ctx, log, to)
            else:
                el = locate(page, selectors, target)
                if act == "wait_for":
                    el.first.wait_for(state="visible", timeout=to); log.add(f"saw {target}")
                elif act == "click":
                    el.first.click(timeout=to); log.add(f"clicked {target}")
                elif act == "fill":
                    val = render(st["value"], ctx)
                    el.first.fill(val, timeout=to)
                    shown = "•" * len(val) if target and any(
                        k in target for k in ("card", "cvv", "password")) else val
                    log.add(f"filled {target} = {shown}")
                elif act in ("select", "set_qty"):
                    el.first.select_option(render(st["value"], ctx), timeout=to)
                    log.add(f"set {target} = {render(st['value'], ctx)}")
                elif act == "check":
                    el.first.check(timeout=to); log.add(f"checked {target}")
                elif act == "uncheck":
                    el.first.uncheck(timeout=to); log.add(f"unchecked {target}")
                elif act == "scroll_to":
                    el.first.scroll_into_view_if_needed(timeout=to); log.add(f"scrolled to {target}")
                elif act == "assert_text":
                    txt = el.first.inner_text(timeout=to)
                    want = render(st.get("contains", ""), ctx)
                    if want and want not in txt:
                        raise StepError(f"{target} says '{txt[:60]}', expected to contain '{want}'")
                    log.add(f"{target} = '{txt[:60]}'")
                elif act == "extract":
                    txt = el.first.inner_text(timeout=to).strip()
                    log.extracted[st["as"]] = txt
                    log.add(f"extracted {st['as']} = '{txt[:60]}'")
                else:
                    raise StepError(f"unsupported action '{act}'")
        except StepError:
            if optional:
                log.add(f"skipped optional {act} {target or ''}")
                continue
            raise
        except Exception as e:
            if optional:
                log.add(f"skipped optional {act} {target or ''} ({type(e).__name__})")
                continue
            raise StepError(f"{act} {target or ''} failed: {type(e).__name__}: {str(e)[:180]}",
                            selector=target or "", stage=log.stage)


def check_failure_signals(page, playbook: dict, log: RunLog) -> str | None:
    for sig in playbook.get("failure_signals", []) or []:
        try:
            if "css" in sig and page.locator(sig["css"]).count():
                return f"{sig['name']}: {sig.get('meaning', '')}"
            if "text" in sig and page.get_by_text(sig["text"], exact=False).count():
                return f"{sig['name']}: {sig.get('meaning', '')}"
        except Exception:
            continue
    return None


def _matches(page, spec: dict, selectors: dict) -> bool:
    """Does this page match a session_check / signal spec?"""
    try:
        if spec.get("url_contains"):
            return spec["url_contains"] in page.url
        if spec.get("selector"):
            return locate(page, selectors, spec["selector"]).first.is_visible(timeout=3000)
        if spec.get("test_id"):
            return page.get_by_test_id(spec["test_id"]).first.is_visible(timeout=3000)
        if spec.get("css"):
            return page.locator(spec["css"]).first.is_visible(timeout=3000)
        if spec.get("text"):
            return page.get_by_text(spec["text"], exact=False).first.is_visible(timeout=3000)
    except Exception:
        return False
    return False


def check_session(page, playbook: dict, log: RunLog | None = None) -> bool | None:
    """True = signed in, False = signed out, None = the playbook can't tell us.

    A saved browser profile normally keeps you signed in for weeks; this is how
    we find out when it hasn't.
    """
    sc = playbook.get("session_check") or {}
    if not sc:
        return None
    selectors = playbook.get("selectors", {})
    if sc.get("signed_in") and _matches(page, sc["signed_in"], selectors):
        if log:
            log.add("session: signed in")
        return True
    if sc.get("signed_out") and _matches(page, sc["signed_out"], selectors):
        if log:
            log.add("session: signed OUT")
        return False
    # only a signed_in probe was given and it didn't match
    if sc.get("signed_in") and not sc.get("signed_out"):
        if log:
            log.add("session: signed-in marker not found, assuming signed out")
        return False
    return None


def money_on_page(page, selectors: dict, name: str = "order_total") -> float | None:
    if name not in selectors:
        return None
    try:
        txt = locate(page, selectors, name).first.inner_text(timeout=4000)
        m = re.search(r"([\d,]+\.\d{2})", txt)
        return float(m.group(1).replace(",", "")) if m else None
    except Exception:
        return None
