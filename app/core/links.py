"""Pull URLs out of a raw chat message and normalise them.

Discord messages are messy: markdown links, <angle brackets>, backticks,
trailing punctuation, bare domains, tracking junk.
"""
import re
from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode

MD_LINK = re.compile(r"\[[^\]]*\]\(\s*(<?)(?P<url>[^)\s>]+)\1\s*\)")
ANGLE = re.compile(r"<(?P<url>(?:https?://|www\.)[^>\s]+)>")
BARE = re.compile(r"(?P<url>https?://[^\s<>\"'`\]\)]+)", re.I)
# domain.tld/path with no scheme — common in deal posts
NAKED = re.compile(
    r"(?<![@\w./-])(?P<url>(?:[a-z0-9¡-￿](?:[a-z0-9¡-￿-]*[a-z0-9¡-￿])?\.)+"
    r"[a-z¡-￿]{2,24}(?:/[^\s<>\"'`\]\)]*)?)",
    re.I,
)

TRAILING = ".,;:!?'\"`*_"
CLOSERS = {")": "(", "]": "[", "}": "{"}

# Tracking params that are safe to drop (never affects the product page).
JUNK_PARAMS = {
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content", "utm_id",
    "fbclid", "gclid", "msclkid", "igshid", "mc_cid", "mc_eid", "ref_", "_branch_match_id",
    "srsltid", "irclickid", "irgwc", "cjevent", "affid", "aff_id", "sourceid",
}

SHORTENERS = {
    "bit.ly", "tinyurl.com", "t.co", "goo.gl", "ow.ly", "buff.ly", "is.gd", "cutt.ly",
    "rebrand.ly", "shorturl.at", "rb.gy", "s.click.aliexpress.com", "amzn.to", "a.co",
    "ebay.us", "tiny.cc", "lnkd.in", "trib.al", "shorte.st", "linktr.ee", "dis.gd",
    "sovrn.co", "howl.link", "fave.co", "shop-links.co", "go.magik.ly", "bhpho.to",
    "ftx.link", "lt.dlvr.it", "share.temu.com", "click.linksynergy.com", "redirect.viglink.com",
}


def _trim(url: str) -> str:
    """Strip trailing punctuation without eating balanced brackets."""
    while url and url[-1] in TRAILING:
        url = url[:-1]
    while url and url[-1] in CLOSERS:
        opener = CLOSERS[url[-1]]
        if url.count(opener) >= url.count(url[-1]):
            break
        url = url[:-1]
        while url and url[-1] in TRAILING:
            url = url[:-1]
    return url


def normalise(url: str) -> str:
    url = _trim(url.strip().strip("`"))
    if not re.match(r"^[a-z][a-z0-9+.-]*://", url, re.I):
        url = "https://" + url.lstrip("/")
    parts = urlsplit(url)
    host = parts.netloc.strip().rstrip(".")
    qs = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
          if k.lower() not in JUNK_PARAMS]
    return urlunsplit((parts.scheme.lower(), host, parts.path or "/", urlencode(qs), ""))


def extract_urls(message: str) -> list[str]:
    """Ordered, de-duplicated URLs found in a message. Markdown/angle forms win."""
    if not message:
        return []
    found: list[str] = []
    consumed: list[tuple[int, int]] = []

    for pat in (MD_LINK, ANGLE):
        for m in pat.finditer(message):
            found.append(m.group("url"))
            consumed.append(m.span())

    def overlaps(span):
        return any(span[0] >= a and span[1] <= b for a, b in consumed)

    for pat in (BARE, NAKED):
        for m in pat.finditer(message):
            if overlaps(m.span()):
                continue
            found.append(m.group("url"))
            consumed.append(m.span())

    out, seen = [], set()
    for raw in found:
        try:
            u = normalise(raw)
        except Exception:
            continue
        parts = urlsplit(u)
        if "." not in parts.netloc or len(parts.netloc) < 4:
            continue
        if u.lower() not in seen:
            seen.add(u.lower())
            out.append(u)
    return out


def is_shortener(host: str) -> bool:
    host = host.lower().lstrip("www.")
    return host in SHORTENERS


def slug_guess(url: str) -> str:
    """Instant product-name guess from the URL path — zero network calls.
    Runs before any fetch so the UI has something in ~0ms."""
    path = urlsplit(url).path
    segs = [s for s in path.split("/") if s]
    if not segs:
        return ""
    skip = {"p", "dp", "ip", "product", "products", "item", "items", "shop", "buy",
            "pd", "site", "gp", "en", "us", "en-us", "catalog", "sku", "detail", "pdp"}
    best = ""
    for seg in segs:
        seg = re.sub(r"\.(html?|aspx?|php)$", "", seg, flags=re.I)
        if seg.lower() in skip or re.fullmatch(r"[0-9A-Z\-]{6,}", seg) or seg.isdigit():
            continue
        words = re.split(r"[-_+]", seg)
        words = [w for w in words if w and not w.isdigit() and len(w) > 1]
        if len(" ".join(words)) > len(best):
            best = " ".join(words)
    best = re.sub(r"\s+", " ", best).strip()
    return best.title() if best and best.islower() else best
