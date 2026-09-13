# Deal Desk

A price-error desk. A link lands in chat; a few hundred milliseconds later you know whether the
domain is real, what the product is, what it's worth, whether you can return it, which card should
pay for it — and the cart is already sitting one click from paid.

```
python3 -m pip install -r requirements.txt
./run_local.sh                  # http://127.0.0.1:8080
```

The password is in `.env` (`ADMIN_PASSWORD_HASH`); regenerate any time with
`python3 tools/mkpass.py`. It starts empty — the Overview page carries a setup checklist that walks
you through cards, merchants, the ChatGPT tasks, and the worker, and ticks each item off as it
detects it's actually done.

---

## The idea

The thing that kills you on a pricing error is the 40 seconds between "link posted" and "order
placed". Almost all of that is checkout: finding the button, filling the address, typing the card.
So checkout doesn't wait for the research — the two run at the same time:

```
link → extract → trust check ─┬─ research: fetch page → category/size/demand/market → score
                              └─ checkout: load playbook → add to cart → fill everything → PARK
                                                      ↓
                              decision arrives → BUY: click  ·  HOLD: wait  ·  SKIP: drop the cart
```

By the time the numbers come back, the only thing left to do is press the button. The home page
draws this; `app/core/pipeline.py` implements it.

## What each piece does

| Piece | File | What it's for |
|---|---|---|
| Link extraction | `app/core/links.py` | markdown, `<angle>`, backticked, bare domains; strips tracking junk |
| **Trust check** | `app/core/trust.py` | is this domain actually the merchant it looks like? |
| Product facts | `app/core/product.py` | JSON-LD → OpenGraph → microdata → DOM, in one fetch |
| Research | `app/core/research.py` | category, size, demand, market comp, return policy |
| Decision | `app/core/decision.py` | your filters, with a pass/fail reason for every one |
| Card allocation | `app/core/cards.py` | per-site rules, fill a card to its limit, roll to the next |
| **Shipping choice** | `app/core/shipping.py` | your per-merchant speed/cost policy → the option it clicks |
| Playbooks | `app/core/playbooks.py` | store/validate/version the recorded checkout steps |
| Worker | `worker/run_worker.py` | drives the browser; runs on your machine, not the server |
| Local vault | `worker/vault_server.py` | localhost-only editor for merchant logins and card numbers |
| Mac installer | `worker/install_mac.sh` | one command: install, autostart, self-restart |

### The pages

| Page | What you do there |
|---|---|
| Overview | the diagram, live counters, the auto-buy switch |
| **Test bench** | paste a link or a whole Discord message, watch every stage run, buy nothing |
| Console | every link, decision, order, failure, and the raw log |
| Inventory | incoming vs delivered, cost vs market value |
| **Tasks & worker** | did the ChatGPT tasks run? is the worker alive? what did each job do? |
| **Merchants** | per-site shipping speed, cost ceiling, return policy, whether it needs a login |
| Cards & limits | cards, per-site card order, and a dry-run allocator |
| Buying filters | every rule that decides buy vs skip |
| Checkout playbooks | coverage, versions, promote/roll back, paste one in by hand |
| Link trust | paste any URL and see exactly why it passed or failed |
| Settings | spend caps, tokens, copy-paste setup for the ChatGPT tasks |
| **Set up your Mac** | the install command, worker status, and the everyday commands |

### The trust check is the important one

Exact-domain matching only — a merchant counts as trusted if its registrable domain is on your list,
character for character. On top of that it catches:

| Attack | Example | Verdict |
|---|---|---|
| Cyrillic/Greek homograph | `tаrget.com` (Cyrillic а), `nιke.com` (Greek ι) | BLOCKED |
| Punycode | `xn--trget-9ye.com` | BLOCKED (shown decoded) |
| Mixed script in one label | Latin + Cyrillic together | BLOCKED |
| Leet / typo squat | `amaz0n.com`, `wallmart.com`, `bestbuv.com` | BLOCKED |
| Subdomain bait | `target.com.secure-checkout.ru` | BLOCKED |
| Credential bait | `https://target.com@evil.ru/…` | BLOCKED |
| Brand in an unrelated domain | `bestbuy-clearance.shop` | BLOCKED |
| Real brand, different ccTLD | `costco.co.uk` when you only listed `.com` | SUSPICIOUS → you decide |
| Shortener | `bit.ly/…` | resolved first, then judged on the destination |

A blocked link is never fetched and never queued. In the dashboard, non-ASCII characters in a domain
are underlined in red so the table can't show you the same thing the attacker wanted you to see.

## The two ChatGPT scheduled tasks

Checkout flows change constantly, so a model maps them nightly and the worker replays the result with
no model in the loop. The prompts are ready to paste, with your URL and token already filled in —
**Settings → ChatGPT scheduled tasks**, or read them in `prompts/`.

| Task | Cadence | Produces | Lands at |
|---|---|---|---|
| `01_checkout_recon.md` | nightly | a playbook per merchant | `POST /api/ingest/playbook` |
| `02_shipping_tracker.md` | every ~4h | carrier/tracking/ETA/cancellations | `POST /api/ingest/shipping` |
| `03_market_comps.md` | daily | real resale prices | `POST /api/ingest/comps` |

`/tasks` shows whether each one is actually running: last run, what it said, what it rejected, and a
health flag that goes **late** when the shipping task hasn't reported in 6 hours or **failing** when
a playbook gets rejected. The same page shows the worker's heartbeat, its queue, and a step-by-step
log of every checkout job it ran.

If your ChatGPT setup can't make HTTP calls, each task prints one fenced JSON block instead — paste
it into **Playbooks → Paste a playbook**.

Playbook **schema 1.1** adds three things to what the recon task captures: `shipping_options` (every
option the site offers, with cost and delivery days), a `login` flow, and a `session_check` that
tells the worker whether the saved session is still signed in. Version 1.0 playbooks keep working —
they just can't do per-merchant shipping. A test asserts that the example in the prompt validates
against the real validator, so the two can't drift.

Playbooks are validated hard before they're stored (known actions only, every target must resolve to
a declared selector, https-only URLs, a fixed set of template variables). Confidence ≥ 0.75 goes live
immediately and the previous version is retired, not deleted — so when a site changes at 2pm and a
purchase fails, the server rolls that merchant back to the last known-good version automatically.

## The test bench

`/testbench` is the page to use before trusting any of this. Paste a link — or a whole messy Discord
message with two links and a spoof in it — and it runs the real pipeline end to end:

- the trust verdict and every reason behind it
- the product facts it could actually read
- the filters that passed and failed, with numbers
- **the shipping option it would pick, and why**
- **which cards would pay, in what order**
- whether there's a live playbook and whether you're still signed in

Dry run is on by default: nothing is added to a cart, no card is touched, no job is queued. Test
deals are tagged separately and `Clear test history` removes them without touching real ones.

## Per-merchant shipping and returns

Everything on `/merchants` is yours to set from your own research — the bot never guesses a policy.

**Shipping.** Pick `cheapest`, `standard`, `expedited`, `fastest`, or `pickup`; decide whether paid
shipping is allowed at all; set a hard ceiling on what you'll pay; and optionally set an
*auto-expedite threshold* — an order value above which speed is worth paying for regardless, because
on a price error a slow ship is often a cancelled ship.

The nightly recon task records what each site actually offers (label, cost, delivery days, whether
it's a pickup option). Your policy plus those options decide the click. The page shows you the
options it found and marks the one it would choose, and every choice carries its reasoning:

```
2-day  $9.99  →  "fastest within your $10.00 cap"
Standard free →  "fastest free option — paid shipping is turned off for this merchant"
Store pickup  →  "you asked for pickup but this merchant doesn't offer it here — fell back to Standard"
```

If a merchant has no recorded options it says so and leaves the site default rather than guessing.

**Returns.** Window, restocking fee, whether returns are free, how you actually return it (free
label / mail at your cost / in store / carrier pickup), how often that merchant voids price errors,
and a free-text box for whatever you learn. The return window and cancel risk feed straight into the
buying filters.

## Merchant logins

Sign-ins live in a **persistent browser profile** (`~/.dealdesk-profile`), so you log in once and
stay logged in for weeks — the worker doesn't retype a password on every run.

Credentials themselves never leave your machine, exactly like the card numbers. Enter them locally:

```bash
python3 worker/vault_server.py      # → http://127.0.0.1:8765, binds to localhost only
```

Then do the first sign-in by hand, so 2FA and any challenge are handled by a human:

```bash
python3 worker/run_worker.py --login bestbuy.com
```

A browser opens, you sign in, press Enter, and the profile keeps the cookies.

After that the worker checks `session_check` from the playbook before each run. If the session is
still good — the normal case — it costs one DOM check and carries on. If it has lapsed and you've
stored a password, it replays the `login` flow. If that fails (2FA, a challenge), it stops, marks the
merchant **needs a manual sign-in**, and both the Merchants and Tasks pages show it — so you find out
before a deal lands, not during one.

The dashboard is told three booleans per merchant: has credentials, is signed in, needs manual login.
Never the password.

## Cards and limits

The dashboard stores **nickname, network, last 4, credit limit, remaining credit** — never a card
number. Per site you set which card to try first and the shipping method; unlisted cards stay as
fallbacks in the global order.

When an order is bigger than what's left on a card, it buys as many units as fit, then rolls the
remainder onto the next card, and writes the new remaining balance back after each success. Set a
statement day and the limit restores itself on that date. Use **Cards → What would happen?** to
dry-run any scenario.

The real card numbers live in `worker/vault.json` (chmod 600) or your macOS Keychain, on the machine
that runs the worker. See `worker/vault.example.json`.

## Running the worker

Open **Set up your Mac** in the dashboard and copy the one command it gives you:

```bash
curl -fsSL "https://<your-app>.azurewebsites.net/api/worker/install.sh?t=<code>" | bash
```

That link is signed and expires in 30 minutes; it carries your server URL and worker token, so
nothing has to be typed. The installer:

1. installs to `~/Library/Application Support/DealDesk` — nothing outside that folder, no `sudo`
2. builds its own Python environment and downloads a private Chromium (your Chrome is untouched)
3. writes the config and an empty vault at `0600`
4. registers a **launchd agent** — `RunAtLoad` so it starts when you log in, `KeepAlive` so macOS
   restarts it if it ever dies
5. starts it in **dry run**

It's idempotent: run it again any time to upgrade or repair. Then:

```bash
dealdesk status          # running? plus the last 15 log lines
dealdesk logs            # follow it live
dealdesk vault           # enter cards and merchant logins (localhost only)
dealdesk login <site>    # first sign-in to a merchant, by hand
dealdesk live            # turn OFF dry run — it will start paying
dealdesk uninstall       # stop it and remove the agent
```

**Two independent switches** have to be on before money moves: `dealdesk live` on your Mac, and
Auto-buy in the dashboard. Before paying, the worker re-reads the total on the review page and
refuses if it's above the price the deal was approved at — so a fixed price error costs you nothing.

### When the connection drops

The worker is built to be left alone:

- **Server unreachable** (wifi drops, Azure restarts, laptop wakes) → exponential backoff up to 60s
  with jitter, one log line rather than a flood, and a `reconnected after 45s offline` when it's
  back. The reconnect count shows on the Setup page.
- **A result can't be delivered** — which matters most when an order was already placed — it retries
  three times, then spools the result to disk and flushes it on reconnect. A dropped connection
  never loses the record of a purchase.
- **Laptop slept** → on wake it notices the time jump and drops any parked carts rather than
  resuming a stale checkout.
- **Killed or crashed** → launchd restarts it within 10 seconds.
- **Logged out / shut down** → SIGTERM is handled, browsers are closed cleanly, and the dashboard
  gets a final `stopping` heartbeat so it shows *stopped* rather than *offline*.

### Does the Mac need to stay on?

Only for buying. The dashboard keeps vetting, researching and logging around the clock. With the Mac
closed, deals still land in the Console — they just aren't purchased, and anything queued more than
15 minutes is dropped rather than bought late. For real 24/7, a cheap Mac mini at home beats a cloud
VM: the worker wants a residential connection.

## Feeding it from Discord

Your Discord listener only has to do one thing:

```bash
curl -X POST $DEALDESK_URL/api/ingest/message \
  -H "Authorization: Bearer $INGEST_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"message": "🔥 https://www.target.com/p/… $39 was $399", "source": "discord:#pricing-errors"}'
```

The response already contains the decision:

```json
{"handled": 1, "elapsed_ms": 412,
 "deals": [{"id": 7, "merchant": "target.com", "decision": "BUY", "qty": 3, "score": 93.6}]}
```

## Deploying

Already deployed and running at
**https://dealdesk-thsh6d7emcano.azurewebsites.net** (Central US, B1).

To push new code, or to stand up a second instance:

```bash
az login
./infra/deploy_azure.sh          # re-run any time; reuses the same resources and password
```

App Service (Linux, Python 3.12) + Key Vault for the secrets + a managed identity to read them.
SQLite lives on `/home`, which Azure persists. Details, cost and the region-policy gotcha are in
[`infra/README.md`](infra/README.md).

## Tests

```bash
python3 -m pytest tests/ -q      # 113 tests
```

Covering the spoof catalogue above, card allocation and roll-over, every buying filter, every
shipping-policy branch, playbook validation for both schema versions, scheduled-task health,
session tracking, the dry-run guarantee that the test bench queues nothing, the setup checklist,
the signed install link (including that an expired one is refused and that the installer never
hard-codes a secret), and the full message-to-decision path including "checkout prep started before
the page even came back".

## One thing to know about running research in the cloud

Plenty of retailers refuse or stall requests coming from datacenter IP ranges, Azure included. When
that happens the pipeline degrades the way you'd want — it keeps the name it parsed from the URL,
writes `fetch failed: ReadTimeout` to the console, and returns **HOLD: "no price could be read from
the page — needs eyes"** instead of inventing a number. It never guesses a price.

Live example from the deployed instance, on an REI link:

```
[ok   ] trust     TRUSTED (1.00) — rei.com
[info ] product   instant name from URL: "Yeti Tundra Cooler"
[warn ] product   fetch failed: ReadTimeout
[warn ] decision  HOLD · score 0 · no price could be read from the page — needs eyes
```

Merchants that do answer (most of them) go through normally. For the ones that don't, the checkout
worker on your own connection can still see the page — so you get the deal in the console, you just
have to eyeball the price. If it becomes a recurring problem for a merchant you care about, the fix
is to run the whole app on a box with a residential connection rather than to fight the block.

## What it deliberately doesn't do

- **No CAPTCHA solving, no bot-wall evasion, no fingerprint spoofing.** If a site blocks automation
  the run fails, says where, and moves on. Getting blocked is a normal outcome, not a bug to engineer
  around.
- **No card numbers on the server.** Even if the Azure app were compromised, there's nothing there to
  take but limits and last-4s.
- **No model in the purchase path.** Research and recon use models; pressing Buy doesn't.

One thing worth knowing: retailers routinely cancel pricing-error orders after the fact, and
automated checkout is against most retailers' terms of service. The merchant table has a
`cancel_risk` column and the filters have a `max_cancel_risk` setting because that's a real cost you
should be pricing in, not a hypothetical.
