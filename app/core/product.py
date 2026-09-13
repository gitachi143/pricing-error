"""Get the product facts out of a page as fast as possible.

Order of operations matters for a price error: the URL slug gives a name in
~0ms, then one HTTP fetch fills in price/brand/size/stock from structured data
(JSON-LD → OpenGraph → microdata → visible DOM). No LLM in the hot path.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field, asdict
from urllib.parse import urlsplit

import httpx
from bs4 import BeautifulSoup

from .. import config
from .links import slug_guess

PRICE_RE = re.compile(r"(?:(?P<cur>[$€£¥])\s*)?(?P<num>\d{1,3}(?:,\d{3})*(?:\.\d{1,2})?|\d+(?:\.\d{1,2})?)")
CUR_SYMBOL = {"$": "USD", "€": "EUR", "£": "GBP", "¥": "JPY"}
SIZE_RE = re.compile(
    r"\b(?:size[:\s]*)?("
    r"XX?S|X?S|M|X?L|XX?L|3XL|4XL|"                       # apparel
    r"(?:\d{1,2}(?:\.5)?)\s?(?:US|M|W|D|EE)?(?=\s?(?:men|women|shoe|size|$))|"  # shoe
    r"\d{2}\s?(?:inch|in|\"|-inch)|"                       # tv / monitor
    r"\d{1,2}\s?(?:TB|GB)|"                                # storage
    r"(?:twin|full|queen|king|california king)"            # bedding
    r")\b", re.I)

FINAL_SALE_MARKERS = [
    "final sale", "no returns", "non-returnable", "not eligible for return",
    "all sales final", "cannot be returned", "nonrefundable", "non-refundable",
]

IN_STOCK_MARKERS = ["instock", "in stock", "available", "add to cart", "add to bag"]
OOS_MARKERS = ["outofstock", "out of stock", "sold out", "unavailable", "discontinued",
               "backorder", "back order", "pre-order", "preorder", "notify me"]


@dataclass
class ProductFacts:
    url: str
    name: str = ""
    brand: str = ""
    sku: str = ""
    gtin: str = ""
    size: str = ""
    color: str = ""
    price: float | None = None
    list_price: float | None = None
    currency: str = "USD"
    availability: str = ""        # in_stock | out_of_stock | unknown
    image: str = ""
    description: str = ""
    final_sale: bool = False
    source: str = ""              # jsonld | opengraph | microdata | dom | slug
    http_status: int = 0
    fetch_ms: int = 0
    blocked: bool = False         # bot wall / 403 / captcha page
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


def _num(text) -> float | None:
    if text is None:
        return None
    if isinstance(text, (int, float)):
        return float(text)
    m = PRICE_RE.search(str(text).replace(",", ""))
    if not m:
        return None
    try:
        return float(m.group("num"))
    except ValueError:
        return None


def _walk_jsonld(node, out: list):
    if isinstance(node, dict):
        t = node.get("@type") or node.get("type")
        types = [t] if isinstance(t, str) else (t or [])
        if any(str(x).lower() in ("product", "productgroup", "individualproduct") for x in types):
            out.append(node)
        for v in node.values():
            _walk_jsonld(v, out)
    elif isinstance(node, list):
        for v in node:
            _walk_jsonld(v, out)


def parse_html(html: str, url: str) -> ProductFacts:
    f = ProductFacts(url=url)
    soup = BeautifulSoup(html, "lxml")
    text_lower = html.lower()

    # 1) JSON-LD Product — the richest and most reliable source
    products: list = []
    for tag in soup.find_all("script", type=lambda v: v and "ld+json" in v.lower()):
        raw = tag.string or tag.get_text() or ""
        try:
            _walk_jsonld(json.loads(raw.strip()), products)
        except Exception:
            # some sites emit several concatenated objects or trailing commas
            for chunk in re.findall(r"\{.*?\}(?=\s*[,\]\}]|\s*$)", raw, re.S)[:20]:
                try:
                    _walk_jsonld(json.loads(chunk), products)
                except Exception:
                    pass

    if products:
        p = max(products, key=lambda d: len(json.dumps(d)))
        f.source = "jsonld"
        f.name = str(p.get("name") or "")[:300]
        brand = p.get("brand")
        f.brand = (brand.get("name") if isinstance(brand, dict) else str(brand or ""))[:120]
        f.sku = str(p.get("sku") or p.get("mpn") or "")[:80]
        f.gtin = str(p.get("gtin13") or p.get("gtin12") or p.get("gtin") or p.get("gtin14") or "")[:40]
        f.color = str(p.get("color") or "")[:60]
        f.size = str(p.get("size") or "")[:60]
        f.description = re.sub(r"\s+", " ", str(p.get("description") or ""))[:600]
        img = p.get("image")
        if isinstance(img, list):
            img = img[0] if img else ""
        f.image = (img.get("url") if isinstance(img, dict) else str(img or ""))[:500]
        offers = p.get("offers")
        if isinstance(offers, list):
            offers = offers[0] if offers else {}
        if isinstance(offers, dict):
            f.price = _num(offers.get("price") or offers.get("lowPrice") or
                           (offers.get("priceSpecification") or {}).get("price"))
            f.currency = str(offers.get("priceCurrency") or "USD")[:3].upper() or "USD"
            avail = str(offers.get("availability") or "").lower()
            if any(m in avail for m in OOS_MARKERS):
                f.availability = "out_of_stock"
            elif any(m in avail for m in IN_STOCK_MARKERS):
                f.availability = "in_stock"

    # 2) OpenGraph / meta fallbacks
    def meta(*keys) -> str:
        for k in keys:
            tag = soup.find("meta", attrs={"property": k}) or soup.find("meta", attrs={"name": k}) \
                or soup.find("meta", attrs={"itemprop": k})
            if tag and tag.get("content"):
                return tag["content"].strip()
        return ""

    if not f.name:
        f.name = meta("og:title", "twitter:title", "title")[:300]
        if f.name:
            f.source = f.source or "opengraph"
    if f.price is None:
        f.price = _num(meta("product:price:amount", "og:price:amount", "price", "twitter:data1"))
        if f.price is not None:
            f.source = f.source or "opengraph"
    if not f.brand:
        f.brand = meta("product:brand", "og:brand", "brand")[:120]
    if not f.image:
        f.image = meta("og:image", "twitter:image")[:500]
    if not f.description:
        f.description = meta("og:description", "description")[:600]
    cur = meta("product:price:currency", "og:price:currency")
    if cur:
        f.currency = cur[:3].upper()

    # 3) Visible DOM fallback
    if not f.name:
        h1 = soup.find("h1")
        if h1:
            f.name = re.sub(r"\s+", " ", h1.get_text(" ", strip=True))[:300]
            f.source = f.source or "dom"
        elif soup.title:
            f.name = re.sub(r"\s+", " ", soup.title.get_text(strip=True))[:300]
            f.source = f.source or "dom"
    if f.price is None:
        for sel in ('[data-testid*="price" i]', '[class*="price" i]', '[id*="price" i]',
                    '[itemprop="price"]'):
            for el in soup.select(sel)[:8]:
                v = _num(el.get("content") or el.get_text(" ", strip=True))
                if v and 0.01 <= v <= 100000:
                    f.price = v
                    f.source = f.source or "dom"
                    break
            if f.price is not None:
                break

    # list / was price, for discount math
    for pat in [r'"(?:wasPrice|listPrice|regularPrice|msrp|strikethrough[Pp]rice)"\s*:\s*"?\$?([\d,.]+)',
                r'(?:was|reg\.?|list|msrp)[:\s]*\$\s?([\d,]+\.?\d{0,2})']:
        m = re.search(pat, html, re.I)
        if m:
            v = _num(m.group(1))
            if v and (f.price is None or v > f.price):
                f.list_price = v
                break

    # stock + returnability signals from raw text
    if not f.availability:
        if any(m in text_lower for m in ("out of stock", "sold out", '"outofstock"')):
            f.availability = "out_of_stock"
        elif any(m in text_lower for m in ("add to cart", "add to bag", '"instock"')):
            f.availability = "in_stock"
        else:
            f.availability = "unknown"
    f.final_sale = any(m in text_lower for m in FINAL_SALE_MARKERS)

    # size, if structured data didn't carry one
    if not f.size:
        m = SIZE_RE.search(f.name or "")
        if m:
            f.size = m.group(1).strip()

    # bot-wall detection
    if re.search(r"(are you a human|captcha|access denied|unusual traffic|"
                 r"enable javascript and cookies|px-captcha|request blocked)", text_lower):
        f.blocked = True
        f.notes.append("page looks like a bot wall — facts may be incomplete")

    f.name = re.sub(r"\s*[|\-–]\s*(Target|Walmart\.com|Best Buy|Amazon\.com|Costco)\s*$", "",
                    f.name, flags=re.I).strip()
    return f


async def fetch_product(url: str, client: httpx.AsyncClient | None = None) -> ProductFacts:
    """One fetch, structured-data first. Always returns facts — never raises."""
    import time
    t0 = time.perf_counter()
    facts = ProductFacts(url=url, name=slug_guess(url), source="slug")
    if not config.ALLOW_NETWORK:
        facts.notes.append("network disabled (ALLOW_NETWORK=0)")
        return facts
    own = client is None
    client = client or httpx.AsyncClient(
        timeout=config.FETCH_TIMEOUT, follow_redirects=True,
        headers={"User-Agent": config.FETCH_UA,
                 "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                 "Accept-Language": "en-US,en;q=0.9"})
    try:
        r = await client.get(url)
        parsed = parse_html(r.text, str(r.url))
        parsed.http_status = r.status_code
        if r.status_code in (403, 429) or r.status_code >= 500:
            parsed.blocked = r.status_code in (403, 429)
            parsed.notes.append(f"HTTP {r.status_code} from merchant")
        if not parsed.name:
            parsed.name = slug_guess(url) or facts.name
            parsed.source = parsed.source or "slug"
        facts = parsed
        facts.url = url
    except Exception as e:
        facts.notes.append(f"fetch failed: {type(e).__name__}: {e}")
    finally:
        if own:
            await client.aclose()
    facts.fetch_ms = int((time.perf_counter() - t0) * 1000)
    return facts


async def resolve_redirects(url: str) -> tuple[str, list[str]]:
    """Follow a shortener to its destination so trust can be judged on the real host."""
    if not config.ALLOW_NETWORK:
        return url, ["network disabled"]
    chain: list[str] = []
    try:
        async with httpx.AsyncClient(timeout=6.0, follow_redirects=True,
                                     headers={"User-Agent": config.FETCH_UA}) as c:
            r = await c.get(url)
            chain = [str(h.url) for h in r.history] + [str(r.url)]
            return str(r.url), chain
    except Exception as e:
        return url, [f"redirect resolution failed: {type(e).__name__}"]


def merchant_of(url: str) -> str:
    from .trust import registrable_domain
    return registrable_domain(urlsplit(url).hostname or "")
