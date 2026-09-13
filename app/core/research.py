"""Light research on a product: category, size, demand, what it's really worth,
and whether you can send it back. Fast + explainable — every number carries the
reason it exists, because you have to trust this in 20 seconds.
"""
from __future__ import annotations

import math
import re
import time
from dataclasses import dataclass, field, asdict

from .. import config, db
from .product import ProductFacts, SIZE_RE

# --- taxonomy ------------------------------------------------------------
CATEGORY_KEYWORDS: dict[str, list[str]] = {
    "gaming_console": ["playstation", "ps5", "xbox series", "nintendo switch", "steam deck", "console"],
    "gaming_accessory": ["dualsense", "controller", "gaming headset", "capture card", "gamecube"],
    "electronics_audio": ["headphone", "earbud", "airpod", "soundbar", "speaker", "wh-1000", "bose",
                          "sonos", "turntable", "amplifier", "subwoofer"],
    "electronics_tv": ["oled", "qled", "4k tv", "smart tv", "television", "projector", "neo qled"],
    "electronics_computer": ["laptop", "macbook", "monitor", "ssd", "nvme", "graphics card", "rtx",
                             "geforce", "radeon", "cpu", "ryzen", "motherboard", "desktop", "ipad",
                             "tablet", "chromebook", "keyboard", "mouse", "router"],
    "electronics_phone": ["iphone", "galaxy s", "pixel", "smartphone", "phone case", "smartwatch",
                          "apple watch"],
    "camera": ["camera", "lens", "dslr", "mirrorless", "gopro", "drone", "gimbal"],
    "sneakers": ["sneaker", "jordan", "yeezy", "dunk", "air max", "new balance", "air force 1",
                 "running shoe", "trainers", "samba", "gazelle"],
    "apparel": ["shirt", "hoodie", "jacket", "pants", "jeans", "dress", "sweater", "coat", "shorts",
                "leggings", "socks", "tee", "puffer", "fleece"],
    "outdoor": ["tent", "sleeping bag", "backpack", "hiking", "kayak", "cooler", "yeti", "camp",
                "climbing", "ski", "snowboard"],
    "home_kitchen": ["cookware", "skillet", "dutch oven", "knife set", "blender", "air fryer",
                     "instant pot", "espresso", "coffee maker", "mixer", "le creuset", "cast iron"],
    "appliance_large": ["refrigerator", "washer", "dryer", "dishwasher", "range", "freezer", "hvac"],
    "appliance_small": ["vacuum", "dyson", "roomba", "humidifier", "purifier", "steamer", "fan"],
    "tools": ["drill", "impact driver", "milwaukee", "dewalt", "ryobi", "makita", "tool set",
              "saw", "wrench", "generator", "pressure washer"],
    "toys_lego": ["lego", "toy", "funko", "action figure", "puzzle", "board game", "doll", "nerf"],
    "beauty": ["fragrance", "perfume", "cologne", "serum", "moisturizer", "makeup", "lipstick",
               "shampoo", "skincare"],
    "baby": ["stroller", "car seat", "diaper", "crib", "bassinet", "baby monitor"],
    "furniture": ["sofa", "desk", "chair", "mattress", "bed frame", "dresser", "bookcase", "table"],
    "jewelry_watch": ["watch", "ring", "necklace", "bracelet", "diamond", "gold", "seiko", "citizen"],
    "sports": ["dumbbell", "treadmill", "peloton", "golf", "bike", "bicycle", "weights", "kettlebell",
               "racket", "paddle"],
    "auto": ["tire", "car battery", "dash cam", "floor mat", "motor oil", "wiper"],
    "grocery": ["coffee beans", "protein powder", "snack", "cereal", "supplement", "vitamin"],
    "gift_card": ["gift card", "egift", "e-gift", "giftcard"],
    "digital": ["download", "digital code", "subscription", "license key", "streaming"],
    "perishable": ["fresh", "frozen", "produce", "meat", "dairy", "flowers"],
}

# Rough resale-vs-MSRP factors, used only when no real comp exists.
RESALE_FACTOR = {
    "gaming_console": 0.85, "gaming_accessory": 0.55, "electronics_audio": 0.62,
    "electronics_tv": 0.58, "electronics_computer": 0.60, "electronics_phone": 0.68,
    "camera": 0.65, "sneakers": 0.80, "apparel": 0.40, "outdoor": 0.55,
    "home_kitchen": 0.55, "appliance_large": 0.45, "appliance_small": 0.55,
    "tools": 0.65, "toys_lego": 0.70, "beauty": 0.50, "baby": 0.50,
    "furniture": 0.35, "jewelry_watch": 0.45, "sports": 0.50, "auto": 0.50,
    "grocery": 0.45, "gift_card": 0.90, "digital": 0.30, "perishable": 0.10, "other": 0.45,
}

# How fast the category moves if you need to flip it.
CATEGORY_VELOCITY = {
    "gaming_console": 0.95, "toys_lego": 0.85, "sneakers": 0.85, "electronics_computer": 0.8,
    "tools": 0.8, "electronics_audio": 0.75, "electronics_phone": 0.8, "camera": 0.7,
    "appliance_small": 0.7, "home_kitchen": 0.65, "electronics_tv": 0.6, "outdoor": 0.6,
    "gaming_accessory": 0.6, "beauty": 0.6, "sports": 0.55, "baby": 0.5, "apparel": 0.45,
    "jewelry_watch": 0.45, "auto": 0.45, "appliance_large": 0.4, "furniture": 0.3,
    "grocery": 0.4, "gift_card": 0.9, "digital": 0.2, "perishable": 0.05, "other": 0.4,
}

HOT_BRANDS = {
    "apple": 0.95, "sony": 0.85, "nintendo": 0.92, "playstation": 0.92, "xbox": 0.8,
    "lego": 0.9, "dyson": 0.88, "bose": 0.8, "samsung": 0.78, "nike": 0.82, "jordan": 0.95,
    "adidas": 0.7, "yeti": 0.85, "stanley": 0.85, "milwaukee": 0.9, "dewalt": 0.85,
    "le creuset": 0.85, "lululemon": 0.85, "patagonia": 0.8, "garmin": 0.8, "gopro": 0.72,
    "nvidia": 0.92, "asus": 0.7, "lg": 0.7, "bissell": 0.6, "shark": 0.65, "vitamix": 0.8,
    "hoka": 0.8, "on running": 0.78, "new balance": 0.8, "meta": 0.75, "anker": 0.7,
    "kitchenaid": 0.8, "weber": 0.75, "traeger": 0.75, "ninja": 0.7, "roomba": 0.7,
}

HYPE_WORDS = {
    "limited edition": 0.12, "exclusive": 0.08, "collab": 0.12, "retro": 0.06, "og": 0.04,
    "sold out": 0.10, "hard to find": 0.10, "discontinued": 0.08, "anniversary": 0.06,
    "special edition": 0.08, "bundle": 0.04,
}


@dataclass
class Research:
    category: str = "other"
    category_confidence: float = 0.0
    size: str = ""
    size_kind: str = ""
    demand_score: float = 0.5
    demand_reasons: list[str] = field(default_factory=list)
    market_price: float | None = None
    market_price_source: str = "unknown"   # comp | msrp | list_price | manual | unknown
    market_confidence: float = 0.0
    returnable: bool = True
    return_window_days: int = 0
    free_returns: bool = True
    restocking_fee_pct: float = 0.0
    cancel_risk: str = "medium"
    return_notes: list[str] = field(default_factory=list)
    elapsed_ms: int = 0

    def to_dict(self) -> dict:
        return asdict(self)


def categorize(facts: ProductFacts) -> tuple[str, float]:
    hay = " ".join(filter(None, [facts.name, facts.brand, facts.description])).lower()
    if not hay.strip():
        return "other", 0.0
    best, best_hits = "other", 0
    for cat, words in CATEGORY_KEYWORDS.items():
        hits = sum(2 if w in (facts.name or "").lower() else 1 for w in words if w in hay)
        if hits > best_hits:
            best, best_hits = cat, hits
    return best, min(1.0, best_hits / 3)


def detect_size(facts: ProductFacts) -> tuple[str, str]:
    if facts.size:
        raw = facts.size
    else:
        m = SIZE_RE.search(f"{facts.name} {facts.description}")
        raw = m.group(1) if m else ""
    raw = raw.strip()
    if not raw:
        return "", ""
    low = raw.lower()
    if re.fullmatch(r"(xx?s|s|m|l|xx?l|3xl|4xl)", low):
        kind = "apparel"
    elif re.search(r"\d{1,2}(\.5)?\s?(us|m|w|d|ee)?$", low):
        kind = "shoe"
    elif "tb" in low or "gb" in low:
        kind = "capacity"
    elif "inch" in low or '"' in low or re.fullmatch(r"\d{2}", low):
        kind = "screen"
    elif low in ("twin", "full", "queen", "king", "california king"):
        kind = "bedding"
    else:
        kind = "other"
    return raw.upper() if kind in ("apparel",) else raw, kind


def demand(facts: ProductFacts, category: str) -> tuple[float, list[str]]:
    reasons: list[str] = []
    hay = f"{facts.name} {facts.brand} {facts.description}".lower()
    base = CATEGORY_VELOCITY.get(category, 0.4)
    score = base
    reasons.append(f"{category.replace('_', ' ')} baseline velocity {base:.2f}")

    brand_hit = None
    for b, v in HOT_BRANDS.items():
        if b in hay:
            if brand_hit is None or v > brand_hit[1]:
                brand_hit = (b, v)
    if brand_hit:
        score = 0.45 * score + 0.55 * brand_hit[1]
        reasons.append(f"strong brand signal: {brand_hit[0]} ({brand_hit[1]:.2f})")

    for w, bump in HYPE_WORDS.items():
        if w in hay:
            score += bump
            reasons.append(f"“{w}” in listing (+{bump:.2f})")

    if facts.availability == "out_of_stock":
        score += 0.05
        reasons.append("already out of stock elsewhere (+0.05)")

    # operator overrides from the dashboard
    for row in db.q("SELECT key,value FROM settings WHERE key LIKE 'demand_override:%'"):
        token = row["key"].split(":", 1)[1].lower()
        if token and token in hay:
            try:
                import json
                val = float(json.loads(row["value"]))
                score = val
                reasons.append(f"manual override for “{token}” → {val:.2f}")
            except Exception:
                pass
    return max(0.0, min(1.0, score)), reasons


def _comp_key(facts: ProductFacts) -> str:
    base = (facts.gtin or facts.sku or facts.name or "").lower()
    return re.sub(r"[^a-z0-9]+", " ", base).strip()[:120]


def market_price(facts: ProductFacts, category: str) -> tuple[float | None, str, float]:
    """Best available estimate of what the item is actually worth.

    comp      – a real observed price (fed in by the nightly comps task or by you)
    msrp      – derived from the page's own strike-through / list price
    unknown   – no basis; the decision engine treats this as a hard stop
    """
    key = _comp_key(facts)
    if key:
        row = db.q1(
            "SELECT market_price, source, sample_size, updated_at FROM comps "
            "WHERE key=? ORDER BY updated_at DESC LIMIT 1", (key,))
        if row and row["market_price"]:
            age_days = (time.time() - (row["updated_at"] or 0)) / 86400
            conf = 0.9 if age_days < 14 else 0.7 if age_days < 45 else 0.5
            return float(row["market_price"]), f"comp:{row['source']}", conf
    if facts.list_price and facts.price and facts.list_price > facts.price:
        factor = RESALE_FACTOR.get(category, 0.45)
        return round(facts.list_price * factor, 2), "msrp", 0.55
    return None, "unknown", 0.0


def returnability(merchant: str, facts: ProductFacts) -> dict:
    row = db.q1("SELECT * FROM merchants WHERE domain=?", (merchant,))
    notes: list[str] = []
    if row:
        window = row["return_window_days"] or 0
        free = bool(row["free_returns"])
        fee = float(row["restocking_fee_pct"] or 0)
        risk = row["cancel_risk"] or "medium"
        notes.append(f"{row['name'] or merchant}: {window}d window"
                     f"{', free returns' if free else ', returns not free'}"
                     f"{f', {fee:g}% restocking' if fee else ''}")
    else:
        window, free, fee, risk = 0, False, 0.0, "high"
        notes.append("merchant not in your return-policy table — treat as no-returns until you add it")
    returnable = window > 0
    if facts.final_sale:
        returnable = False
        notes.append("page says FINAL SALE / no returns")
    return {"returnable": returnable, "return_window_days": window, "free_returns": free,
            "restocking_fee_pct": fee, "cancel_risk": risk, "return_notes": notes}


def research(facts: ProductFacts, merchant: str) -> Research:
    t0 = time.perf_counter()
    cat, cat_conf = categorize(facts)
    size, size_kind = detect_size(facts)
    dem, dem_reasons = demand(facts, cat)
    mp, src, conf = market_price(facts, cat)
    ret = returnability(merchant, facts)
    r = Research(category=cat, category_confidence=cat_conf, size=size, size_kind=size_kind,
                 demand_score=dem, demand_reasons=dem_reasons, market_price=mp,
                 market_price_source=src, market_confidence=conf, **ret)
    r.elapsed_ms = int((time.perf_counter() - t0) * 1000)
    return r
