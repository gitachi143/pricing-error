#!/usr/bin/env python3
"""Local credentials editor.

    python3 worker/vault_server.py     →  http://127.0.0.1:8765

Merchant logins and card numbers are entered HERE, on your own machine, and are
written to worker/vault.json (chmod 600). They are never sent to the dashboard
and never reach Azure — the dashboard is only told *whether* a merchant has
credentials and whether the saved browser session is still signed in.

It binds to 127.0.0.1 only, so nothing on your network can reach it.
"""
from __future__ import annotations

import json
import os
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent))
from vault import VAULT_FILE, redacted, save  # noqa: E402

HOST, PORT = "127.0.0.1", int(os.environ.get("VAULT_PORT", "8765"))

PAGE = """<!doctype html><html><head><meta charset="utf-8"><title>Deal Desk — local vault</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
 :root{--ink:#0b0b0b;--ink2:#52514e;--muted:#898781;--surface:#fcfcfb;--plane:#f9f9f7;
       --grid:#e1e0d9;--axis:#c3c2b7;--blue:#2a78d6;--good:#0ca30c;--crit:#d03b3b}
 @media(prefers-color-scheme:dark){:root{--ink:#fff;--ink2:#c3c2b7;--surface:#1a1a19;
   --plane:#0d0d0d;--grid:#2c2c2a;--axis:#383835;--blue:#3987e5}}
 *{box-sizing:border-box}
 body{font:14px system-ui,-apple-system,"Segoe UI",sans-serif;background:var(--plane);
      color:var(--ink);margin:0;padding:26px;max-width:860px;margin:0 auto}
 h1{font-size:20px;margin:0 0 3px} h2{font-size:15px;margin:22px 0 9px}
 .sub{color:var(--muted);font-size:12.5px;margin-bottom:18px}
 .card{background:var(--surface);border:1px solid var(--grid);border-radius:10px;padding:15px;
       margin-bottom:11px}
 label{display:block;font-size:11.5px;font-weight:600;color:var(--ink2);margin-bottom:3px}
 input{width:100%;padding:7px 9px;border:1px solid var(--axis);border-radius:7px;
       background:var(--plane);color:var(--ink);font:13px inherit}
 .row{display:flex;gap:9px;flex-wrap:wrap}.row>*{flex:1 1 150px}
 button{border:1px solid var(--axis);background:var(--plane);color:var(--ink);font-weight:600;
        padding:7px 13px;border-radius:7px;cursor:pointer;font:13px inherit}
 button.primary{background:var(--blue);border-color:var(--blue);color:#fff}
 button.danger{color:var(--crit);border-color:var(--crit)}
 .banner{background:color-mix(in srgb,var(--good) 12%,transparent);border:1px solid
   color-mix(in srgb,var(--good) 40%,transparent);border-radius:9px;padding:11px 13px;
   font-size:12.5px;margin-bottom:16px}
 .chip{display:inline-block;padding:2px 8px;border-radius:999px;font-size:11px;font-weight:600}
 .set{background:color-mix(in srgb,var(--good) 15%,transparent);color:var(--good)}
 .unset{background:color-mix(in srgb,var(--ink) 8%,transparent);color:var(--muted)}
 .head{display:flex;justify-content:space-between;align-items:center;margin-bottom:9px}
 .hint{color:var(--muted);font-size:11.5px;margin-top:4px}
 code{font:12px ui-monospace,Menlo,monospace;background:var(--plane);padding:1px 5px;border-radius:4px}
</style></head><body>
<h1>Local vault</h1>
<div class="sub">Runs only on this machine. Nothing here is sent to the dashboard or to Azure.</div>
<div class="banner">Saved to <code>__FILE__</code> · permissions <code>0600</code> ·
  the dashboard is told only <em>whether</em> a login exists, never what it is.</div>

<h2>Your address</h2>
<div class="card" id="addr"></div>

<h2>Merchant logins</h2>
<div class="sub" style="margin-top:-4px">One per site. The browser profile keeps you signed in;
  these are only used when that session expires.</div>
<div id="accounts"></div>
<div class="card">
  <div class="row"><input id="newMerchant" placeholder="bestbuy.com">
  <button onclick="addMerchant()">Add a merchant</button></div>
</div>

<h2>Cards</h2>
<div class="sub" style="margin-top:-4px">The number matches the card ID from the dashboard's
  Cards page.</div>
<div id="cards"></div>
<div class="card">
  <div class="row"><input id="newCard" placeholder="card id from the dashboard, e.g. 3">
  <button onclick="addCard()">Add a card</button></div>
</div>

<div style="margin:22px 0 40px"><button class="primary" onclick="saveAll()">Save vault</button>
  <span id="status" class="hint"></span></div>

<script>
let V = {accounts:{}, cards:{}, address:{}};
const ADDR = [["line1","Street"],["line2","Apt / unit"],["city","City"],["state","State"],
              ["zip","ZIP"],["country","Country"]];
const ACCT = [["email","Email"],["password","Password"],["first_name","First name"],
              ["last_name","Last name"],["phone","Phone"]];
const CARD = [["number","Card number"],["name","Name on card"],["month","Exp month"],
              ["year","Exp year"],["cvv","CVV"],["zip","Billing ZIP"]];
const esc = s => String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));

async function load(){
  const r = await fetch('/api/vault'); const d = await r.json();
  V.address = d.address || {};
  V.accounts = {}; V.cards = {};
  for (const [m,a] of Object.entries(d.accounts||{})) V.accounts[m] = {...a, password:''};
  for (const [id,c] of Object.entries(d.cards||{})) V.cards[id] = {...c, number:'', cvv:''};
  render();
}
function field(id,label,val,ph,type){return `<div><label>${label}</label>
  <input id="${id}" value="${esc(val||'')}" placeholder="${ph||''}" type="${type||'text'}"></div>`}

function render(){
  document.getElementById('addr').innerHTML = '<div class="row">' +
    ADDR.map(([k,l])=>field('addr_'+k,l,V.address[k])).join('') + '</div>';

  document.getElementById('accounts').innerHTML = Object.entries(V.accounts).map(([m,a])=>`
    <div class="card"><div class="head"><b>${esc(m)}</b>
      <span>${a.password_set?'<span class="chip set">password saved</span>':'<span class="chip unset">no password</span>'}
      <button class="danger" onclick="delAcct('${esc(m)}')">remove</button></span></div>
      <div class="row">${ACCT.map(([k,l])=>field(`acct_${m}_${k}`,l,k==='password'?'':a[k],
        k==='password'&&a.password_set?'leave blank to keep':'', k==='password'?'password':'text')).join('')}</div>
    </div>`).join('') || '<div class="card hint">No merchant logins yet.</div>';

  document.getElementById('cards').innerHTML = Object.entries(V.cards).map(([id,c])=>`
    <div class="card"><div class="head"><b>Card ID ${esc(id)}</b>
      <span>${c.number_set?`<span class="chip set">•••• ${esc(c.last4||'')}</span>`:'<span class="chip unset">no number</span>'}
      <button class="danger" onclick="delCard('${esc(id)}')">remove</button></span></div>
      <div class="row">${CARD.map(([k,l])=>field(`card_${id}_${k}`,l,
        (k==='number'||k==='cvv')?'':c[k], (k==='number'&&c.number_set)?'leave blank to keep':'',
        (k==='number'||k==='cvv')?'password':'text')).join('')}</div>
    </div>`).join('') || '<div class="card hint">No cards yet.</div>';
}
function addMerchant(){const m=document.getElementById('newMerchant').value.trim().toLowerCase();
  if(!m)return; collect(); V.accounts[m]={email:'',password:'',password_set:false}; render();}
function addCard(){const id=document.getElementById('newCard').value.trim();
  if(!id)return; collect(); V.cards[id]={number_set:false}; render();}
function delAcct(m){collect(); delete V.accounts[m]; render();}
function delCard(id){collect(); delete V.cards[id]; render();}

function collect(){
  ADDR.forEach(([k])=>{const e=document.getElementById('addr_'+k); if(e)V.address[k]=e.value;});
  for(const m of Object.keys(V.accounts)) ACCT.forEach(([k])=>{
    const e=document.getElementById(`acct_${m}_${k}`); if(e&&(k!=='password'||e.value))V.accounts[m][k]=e.value;});
  for(const id of Object.keys(V.cards)) CARD.forEach(([k])=>{
    const e=document.getElementById(`card_${id}_${k}`);
    if(e&&((k!=='number'&&k!=='cvv')||e.value))V.cards[id][k]=e.value;});
}
async function saveAll(){
  collect();
  const r = await fetch('/api/vault',{method:'PUT',headers:{'Content-Type':'application/json'},
    body:JSON.stringify(V)});
  const d = await r.json();
  document.getElementById('status').textContent = d.ok ? '  saved ✓' : ('  ' + (d.error||'failed'));
  if(d.ok) load();
}
load();
</script></body></html>"""


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, body, ctype="application/json"):
        data = body.encode() if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _load(self) -> dict:
        if VAULT_FILE.exists():
            try:
                return json.loads(VAULT_FILE.read_text())
            except Exception:
                return {}
        return {}

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/":
            return self._send(200, PAGE.replace("__FILE__", str(VAULT_FILE)), "text/html")
        if path == "/api/vault":
            return self._send(200, json.dumps(redacted(self._load())))
        self._send(404, json.dumps({"error": "not found"}))

    def do_PUT(self):
        if urlparse(self.path).path != "/api/vault":
            return self._send(404, json.dumps({"error": "not found"}))
        try:
            n = int(self.headers.get("Content-Length") or 0)
            incoming = json.loads(self.rfile.read(n) or b"{}")
            current = self._load()
            merged = {
                "address": incoming.get("address") or current.get("address", {}),
                "default_shipping": current.get("default_shipping", ""),
                "accounts": {}, "cards": {},
            }
            # blank password / card number means "keep what's already stored"
            for merchant, a in (incoming.get("accounts") or {}).items():
                old = (current.get("accounts") or {}).get(merchant, {})
                merged["accounts"][merchant] = {
                    **{k: a.get(k, old.get(k, "")) for k in
                       ("email", "first_name", "last_name", "phone")},
                    "password": a.get("password") or old.get("password", ""),
                }
            for cid, c in (incoming.get("cards") or {}).items():
                old = (current.get("cards") or {}).get(cid, {})
                merged["cards"][cid] = {
                    **{k: c.get(k, old.get(k, "")) for k in ("name", "month", "year", "zip")},
                    "number": c.get("number") or old.get("number", ""),
                    "cvv": c.get("cvv") or old.get("cvv", ""),
                }
            save(merged)
            self._send(200, json.dumps({"ok": True}))
        except Exception as e:
            self._send(400, json.dumps({"ok": False, "error": f"{type(e).__name__}: {e}"}))

    def log_message(self, *args):
        pass    # keep the console quiet


def main():
    print(f"\n  Local vault editor → http://{HOST}:{PORT}")
    print(f"  Writing to {VAULT_FILE}")
    print("  This binds to localhost only. Ctrl-C to stop.\n")
    HTTPServer((HOST, PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
