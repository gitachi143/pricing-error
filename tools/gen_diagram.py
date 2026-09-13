"""Generates app/static/diagram.svg — the 'how this works' picture on the home
page. Coordinates are computed, not hand-tuned, so the layout stays honest."""
from pathlib import Path
from html import escape

W, H = 1280, 852
out: list[str] = []

def node(x, y, w, h, title, sub="", accent="var(--s1)", dashed=False, kind="box"):
    rx = h / 2 if kind == "pill" else 10
    dash = ' stroke-dasharray="5 4"' if dashed else ""
    out.append(f'<g><rect x="{x}" y="{y}" width="{w}" height="{h}" rx="{rx}" '
               f'fill="var(--raised)" stroke="{accent if dashed else "var(--axis)"}"'
               f' stroke-width="1"{dash}/>')
    if kind == "box":
        out.append(f'<rect x="{x}" y="{y}" width="4" height="{h}" rx="2" fill="{accent}"/>')
    ty = y + (h / 2 + 4.5) if not sub else y + 21
    out.append(f'<text x="{x + 16}" y="{ty}" font-size="13.5" font-weight="600" '
               f'fill="var(--ink)">{escape(title)}</text>')
    for i, line in enumerate(sub.split("\n") if sub else []):
        out.append(f'<text x="{x + 16}" y="{y + 39 + i * 14}" font-size="11.5" '
                   f'fill="var(--ink-2)">{escape(line)}</text>')
    out.append("</g>")
    return (x, y, w, h)

def arrow(a, b, side="r", label="", color="var(--axis)", dashed=False, bend=None, lx=0, ly=-7):
    """a,b are node tuples. side: r(ight) l(eft) d(own) u(p)."""
    ax, ay, aw, ah = a; bx, by, bw, bh = b
    if side == "r":  p0 = (ax + aw, ay + ah / 2); p1 = (bx, by + bh / 2)
    elif side == "l": p0 = (ax, ay + ah / 2); p1 = (bx + bw, by + bh / 2)
    elif side == "d": p0 = (ax + aw / 2, ay + ah); p1 = (bx + bw / 2, by)
    else:             p0 = (ax + aw / 2, ay); p1 = (bx + bw / 2, by + bh)
    if bend == "hv":   d = f"M{p0[0]},{p0[1]} H{p1[0]} V{p1[1]}"
    elif bend == "vh": d = f"M{p0[0]},{p0[1]} V{p1[1]} H{p1[0]}"
    else:              d = f"M{p0[0]},{p0[1]} L{p1[0]},{p1[1]}"
    dash = ' stroke-dasharray="5 4"' if dashed else ""
    out.append(f'<path d="{d}" fill="none" stroke="{color}" stroke-width="1.6"{dash} '
               f'marker-end="url(#ah)"/>')
    if label:
        mx, my = (p0[0] + p1[0]) / 2 + lx, (p0[1] + p1[1]) / 2 + ly
        out.append(f'<text x="{mx}" y="{my}" font-size="10.5" font-weight="600" text-anchor="middle" '
                   f'fill="var(--muted)">{escape(label)}</text>')

def route(pts, label="", color="var(--axis)", dashed=False, marker="ah",
          label_at=None, label_dy=-6):
    """Explicit polyline between waypoints — used where a straight elbow would
    run through another box."""
    d = f"M{pts[0][0]},{pts[0][1]} " + " ".join(f"L{x},{y}" for x, y in pts[1:])
    dash = ' stroke-dasharray="5 4"' if dashed else ""
    out.append(f'<path d="{d}" fill="none" stroke="{color}" stroke-width="1.6"{dash} '
               f'marker-end="url(#{marker})"/>')
    if label:
        lx, ly = label_at or ((pts[0][0] + pts[-1][0]) / 2, pts[0][1] + label_dy)
        out.append(f'<text x="{lx}" y="{ly + label_dy}" font-size="10.5" font-weight="600" '
                   f'text-anchor="middle" fill="{color}">{escape(label)}</text>')


def band(x, y, w, h, label, color="var(--s1)"):
    out.append(f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="12" fill="none" '
               f'stroke="{color}" stroke-opacity=".35" stroke-dasharray="3 5"/>')
    out.append(f'<text x="{x + 13}" y="{y + 17}" font-size="10.5" font-weight="700" '
               f'letter-spacing=".09em" fill="{color}">{escape(label.upper())}</text>')

def tick(x, y, text, color="var(--muted)"):
    out.append(f'<text x="{x}" y="{y}" font-size="10.5" font-weight="700" fill="{color}" '
               f'font-family="var(--mono)">{escape(text)}</text>')

# ================= band 1: intake =================
band(24, 22, 1232, 112, "1 · intake — runs in single-digit milliseconds", "var(--s1)")
discord = node(44, 52, 178, 62, "Discord message", "your #pricing-errors feed\nPOST /api/ingest/message",
               accent="var(--s7)", dashed=True)
extract = node(262, 52, 168, 62, "Extract link", "markdown, <angle>, bare\nstrips tracking params")
slug    = node(470, 52, 178, 62, "Name it instantly", "product name parsed from\nthe URL slug — 0 network")
trust   = node(688, 52, 196, 62, "Trust the domain?", "homoglyph · punycode · typo\nsubdomain bait · shorteners",
               accent="var(--s3)")
blocked = node(936, 52, 152, 62, "BLOCKED", "never fetched,\nlogged with the reason", accent="var(--critical)")
arrow(discord, extract); arrow(extract, slug); arrow(slug, trust)
arrow(trust, blocked, label="fake", color="var(--critical)")
tick(1106, 78, "spoof stops here", "var(--critical)")

# ================= band 2: the parallel part =================
band(24, 150, 1232, 268, "2 · two tracks at once — this is where the seconds come from", "var(--s2)")
fork = node(44, 262, 58, 46, "fork", accent="var(--s2)", kind="pill")
out.append(f'<path d="M884,83 H{44+29} V262" fill="none" stroke="var(--axis)" stroke-width="1.6" marker-end="url(#ah)"/>')

# lane A — research
out.append('<text x="128" y="196" font-size="11" font-weight="700" fill="var(--s1)" '
           'letter-spacing=".07em">RESEARCH TRACK</text>')
fetch  = node(128, 206, 186, 66, "Fetch the page", "JSON-LD → OpenGraph → DOM\nprice · stock · final sale")
rsrch  = node(346, 206, 206, 66, "Research it", "category · size · demand\nmarket comp · return policy")
dec    = node(584, 206, 186, 66, "Score vs your filters", "discount · profit · ROI\ndemand · returnability", accent="var(--s4)")
arrow(fork, fetch, label=""); arrow(fetch, rsrch); arrow(rsrch, dec)

# lane B — checkout
out.append('<text x="128" y="316" font-size="11" font-weight="700" fill="var(--s2)" '
           'letter-spacing=".07em">CHECKOUT TRACK  (starts before the decision exists)</text>')
pbk    = node(128, 326, 186, 66, "Playbook + session", "last night's recorded steps;\nstill signed in? (saved profile)", accent="var(--s2)")
cart   = node(346, 326, 206, 66, "Cart + details filled", "add to cart, size, qty,\naddress + card entered", accent="var(--s2)")
armed  = node(584, 326, 186, 66, "Parked at review", "one click from paid,\nnothing charged yet", accent="var(--s2)")
out.append(f'<path d="M73,308 V359 H128" fill="none" stroke="var(--axis)" stroke-width="1.6" marker-end="url(#ah)"/>')
arrow(pbk, cart); arrow(cart, armed)

join = node(812, 262, 58, 46, "join", accent="var(--s2)", kind="pill")
arrow(dec, join, bend="hv"); arrow(armed, join, bend="hv")
tick(900, 262, "BUY  → click it now")
tick(900, 284, "HOLD → wait for you")
tick(900, 306, "SKIP → drop the cart")

# ================= band 3: money =================
band(24, 434, 1232, 176, "3 \u00b7 paying for it", "var(--s3)")
gate  = node(44, 492, 134, 80, "Decision", "BUY \u00b7 HOLD\nSKIP \u00b7 BLOCKED", accent="var(--s4)")
alloc = node(202, 492, 200, 80, "Pick the card", "your per-site rule order,\nfill one to its limit,\nthen roll to the next", accent="var(--s3)")
ship  = node(426, 492, 178, 80, "Pick shipping", "your per-merchant rule:\nfree, expedited, pickup\n\u2014 within your cost cap", accent="var(--s3)")
place = node(628, 492, 186, 80, "Place the order", "replays the playbook,\nre-reads the total first", accent="var(--s3)")
rec   = node(838, 492, 166, 80, "Order recorded", "limit decremented\nconsole line written")
fail  = node(1028, 492, 188, 80, "Failed orders", "stage + reason kept,\nplaybook rolled back\nif the site changed", accent="var(--critical)")
arrow(join, gate, bend="vh"); arrow(gate, alloc); arrow(alloc, ship); arrow(ship, place)
arrow(place, rec)
route([(721, 572), (721, 598), (1122, 598), (1122, 574)], color="var(--critical)",
      label="blocked \u00b7 out of stock \u00b7 card declined \u00b7 price already fixed",
      label_at=(920, 598), label_dy=-16)

# ================= band 4: after =================
band(24, 646, 1232, 176, "4 \u00b7 after the click", "var(--s7)")
ship  = node(44, 690, 228, 76, "Shipping status", "carrier \u00b7 tracking \u00b7 ETA\nrefreshed every few hours",
             accent="var(--s7)")
inv   = node(316, 690, 210, 76, "Inventory", "incoming vs delivered,\ncost and market value", accent="var(--s7)")
cons  = node(570, 690, 196, 76, "Console", "every link, decision,\nand order, with reasons")
route([(921, 572), (921, 624), (158, 624), (158, 688)])
arrow(ship, inv); arrow(inv, cons)

# the two ChatGPT scheduled tasks feeding in
gpt1 = node(1014, 648, 200, 64, "Nightly recon task", "walks each checkout,\nposts a fresh playbook",
            accent="var(--s5)", dashed=True)
gpt2 = node(1014, 722, 200, 64, "Tracking task", "reads confirmations,\nposts status updates",
            accent="var(--s5)", dashed=True)
route([(1014, 680), (994, 680), (994, 424), (221, 424), (221, 394)],
      color="var(--s5)", dashed=True, marker="ah5")
out.append('<text x="600" y="418" font-size="10.5" font-weight="600" fill="var(--s5)" '
           'text-anchor="middle">playbook JSON \u2192 /api/ingest/playbook (nightly, per merchant)</text>')
route([(1014, 754), (994, 754), (994, 802), (158, 802), (158, 768)],
      color="var(--s5)", dashed=True, marker="ah5")
out.append('<text x="576" y="796" font-size="10.5" font-weight="600" fill="var(--s5)" '
           'text-anchor="middle">status JSON \u2192 /api/ingest/shipping (every few hours)</text>')

svg = f'''<svg viewBox="0 0 {W} {H}" xmlns="http://www.w3.org/2000/svg" role="img"
     aria-label="How the deal bot works, end to end" style="width:100%;height:auto;font-family:var(--font)">
  <defs>
    <marker id="ah" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse">
      <path d="M0,1 L9,5 L0,9 z" fill="var(--axis)"/></marker>
    <marker id="ah5" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse">
      <path d="M0,1 L9,5 L0,9 z" fill="var(--s5)"/></marker>
  </defs>
  {"".join(out)}
</svg>'''

Path("app/static/diagram.svg").write_text(svg)
print(f"diagram.svg written ({len(svg)} bytes)")
