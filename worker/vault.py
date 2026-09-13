"""Local secret store for the checkout worker.

Card numbers, your address and site logins live here — on your machine, never on
the server. The dashboard only ever knows nickname / network / last4 / limits.

Lookup order:
  1. macOS Keychain item  `dealdesk-vault`  (recommended)
  2. worker/vault.json    (chmod 600)
"""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

HERE = Path(__file__).resolve().parent
VAULT_FILE = HERE / "vault.json"
KEYCHAIN_SERVICE = "dealdesk-vault"


def _from_keychain() -> dict | None:
    try:
        out = subprocess.run(["security", "find-generic-password", "-s", KEYCHAIN_SERVICE, "-w"],
                             capture_output=True, text=True, timeout=8)
        if out.returncode == 0 and out.stdout.strip():
            return json.loads(out.stdout)
    except Exception:
        pass
    return None


def load() -> dict:
    data = _from_keychain()
    if data is None and VAULT_FILE.exists():
        mode = VAULT_FILE.stat().st_mode & 0o777
        if mode & 0o077:
            print(f"[vault] WARNING: {VAULT_FILE} is world/group readable ({oct(mode)}). "
                  f"Run: chmod 600 '{VAULT_FILE}'")
        data = json.loads(VAULT_FILE.read_text())
    if data is None:
        raise SystemExit(
            f"No vault found. Create {VAULT_FILE} (see vault.example.json) and chmod 600 it, or store "
            f"the same JSON in the Keychain:\n"
            f"  security add-generic-password -s {KEYCHAIN_SERVICE} -a $USER -w \"$(cat vault.json)\"")
    return data


def save(data: dict) -> None:
    """Write the vault back to disk with tight permissions."""
    VAULT_FILE.write_text(json.dumps(data, indent=2))
    os.chmod(VAULT_FILE, 0o600)


def has_credentials(vault: dict, merchant: str) -> bool:
    acct = (vault.get("accounts") or {}).get(merchant)
    if not acct:
        return False
    return bool(acct.get("email") and acct.get("password"))


def redacted(vault: dict) -> dict:
    """What is safe to display — never the password itself."""
    out = {"address": vault.get("address", {}), "accounts": {}, "cards": {},
           "default_shipping": vault.get("default_shipping", "")}
    for merchant, a in (vault.get("accounts") or {}).items():
        out["accounts"][merchant] = {
            "email": a.get("email", ""),
            "password_set": bool(a.get("password")),
            "first_name": a.get("first_name", ""), "last_name": a.get("last_name", ""),
            "phone": a.get("phone", ""),
        }
    for cid, c in (vault.get("cards") or {}).items():
        out["cards"][cid] = {"last4": str(c.get("number", ""))[-4:],
                             "name": c.get("name", ""),
                             "expires": f"{c.get('month', '')}/{c.get('year', '')}",
                             "number_set": bool(c.get("number")),
                             "cvv_set": bool(c.get("cvv"))}
    return out


def card_for(vault: dict, card_id: int, card_label: str = "") -> dict:
    """Match a dashboard card to its real details by id, then by last4."""
    cards = vault.get("cards", {})
    if str(card_id) in cards:
        return cards[str(card_id)]
    last4 = card_label.split("·")[-1].strip() if "·" in card_label else ""
    for c in cards.values():
        if last4 and str(c.get("number", ""))[-4:] == last4:
            return c
    raise KeyError(f"no vault entry for card id={card_id} ({card_label}). "
                   f"Add it to vault.json under cards.{card_id}")


def context_for(vault: dict, deal: dict, job: dict, card: dict | None) -> dict:
    """Flatten everything a playbook may reference into {{namespace.key}} values."""
    merchant = (deal or {}).get("merchant", "")
    accounts = vault.get("accounts", {})
    acct = accounts.get(merchant, accounts.get("default", {}))
    addr = vault.get("address", {})
    payload = job.get("payload", {})
    return {
        "deal": {
            "url": payload.get("url") or (deal or {}).get("final_url") or (deal or {}).get("url", ""),
            "qty": str(payload.get("qty", 1)), "size": (deal or {}).get("size") or "",
            "sku": (deal or {}).get("sku") or "", "product_name": (deal or {}).get("product_name") or "",
            "max_price": str(payload.get("max_price", "")),
        },
        "account": {k: str(acct.get(k, "")) for k in
                    ("email", "password", "phone", "first_name", "last_name")},
        "address": {k: str(addr.get(k, "")) for k in
                    ("line1", "line2", "city", "state", "zip", "country")},
        "card": {k: str((card or {}).get(k, "")) for k in
                 ("number", "name", "month", "year", "cvv", "zip")},
        "shipping": {"method": payload.get("shipping_method") or vault.get("default_shipping", "")},
    }
