"""Domain trust engine.

Answers one question: *is this link really the merchant it looks like?*

Attacks it catches:
  - IDN homograph  (tаrget.com with a Cyrillic 'а', аmazon.com, nіke.com)
  - punycode       (xn--trget-9ye.com)
  - mixed script   (one label containing both Latin and Greek/Cyrillic)
  - typosquat      (amaz0n.com, wallmart.com, bestbuy-deals.com)
  - subdomain bait (target.com.secure-checkout.ru/...)
  - credential bait(https://target.com@evil.ru/p/...)
  - raw IP hosts, odd ports, http://, high-risk TLDs, unresolved shorteners

Verdicts: TRUSTED (exact match, known merchant) | UNKNOWN (real domain, not on
your list) | SUSPICIOUS (needs a human) | BLOCKED (do not touch).
"""
from __future__ import annotations

import ipaddress
import re
import unicodedata
from dataclasses import dataclass, field
from urllib.parse import urlsplit

# --- confusable folding --------------------------------------------------
# Unicode characters that render like an ASCII letter. Folding these gives a
# "skeleton"; if a skeleton equals a real brand but the raw domain does not,
# the domain is impersonating that brand.
CONFUSABLES = {
    # Cyrillic
    "а": "a", "б": "b", "в": "b", "е": "e", "ѕ": "s", "і": "i", "ј": "j", "к": "k",
    "м": "m", "н": "h", "о": "o", "р": "p", "с": "c", "т": "t", "у": "y", "х": "x",
    "һ": "h", "ԁ": "d", "ԛ": "q", "ԝ": "w", "ӏ": "l", "ї": "i", "ё": "e", "ү": "y",
    "ғ": "f", "ԍ": "g", "ѵ": "v", "ѡ": "w", "ұ": "y", "ә": "a", "ӡ": "3", "ѐ": "e",
    "А": "A", "В": "B", "Е": "E", "К": "K", "М": "M", "Н": "H", "О": "O", "Р": "P",
    "С": "C", "Т": "T", "У": "Y", "Х": "X", "І": "I", "Ј": "J", "Ѕ": "S",
    # Greek
    "α": "a", "β": "b", "γ": "y", "δ": "d", "ε": "e", "ζ": "z", "η": "n", "θ": "o",
    "ι": "i", "κ": "k", "λ": "l", "μ": "u", "ν": "v", "ξ": "e", "ο": "o", "π": "n",
    "ρ": "p", "σ": "o", "ς": "c", "τ": "t", "υ": "u", "φ": "o", "χ": "x", "ψ": "y",
    "ω": "w", "Α": "A", "Β": "B", "Γ": "T", "Ε": "E", "Ζ": "Z", "Η": "H", "Ι": "I",
    "Κ": "K", "Μ": "M", "Ν": "N", "Ο": "O", "Ρ": "P", "Τ": "T", "Υ": "Y", "Χ": "X",
    # Armenian / Georgian / Cherokee / Hebrew lookalikes
    "օ": "o", "ո": "n", "ս": "u", "ա": "w", "գ": "q", "զ": "q", "ի": "h", "ռ": "n",
    "Ꭰ": "D", "Ꭺ": "A", "Ꮃ": "W", "Ꮮ": "L", "Ꮯ": "C", "Ꮖ": "P", "Ꮕ": "N", "Ꭼ": "E",
    "ᴏ": "o", "ᴀ": "a", "ᴇ": "e", "ʀ": "r", "ɢ": "g", "ʟ": "l", "ɪ": "i", "ɴ": "n",
    # math / styled latin
    "𝐚": "a", "𝗮": "a", "𝘢": "a", "𝙖": "a", "𝒂": "a", "𝕒": "a", "ａ": "a",
    "ｅ": "e", "ｏ": "o", "ｐ": "p", "ｃ": "c", "ｉ": "i", "ｎ": "n", "ｔ": "t",
    "ı": "i", "ł": "l", "ø": "o", "đ": "d", "ƅ": "b", "ϲ": "c", "ϳ": "j", "ѳ": "o",
}

# Second pass, only for typo-squat detection (never for script-spoof detection).
LEET = {"0": "o", "1": "l", "3": "e", "4": "a", "5": "s", "7": "t", "8": "b", "9": "g", "$": "s"}

HIGH_RISK_TLDS = {
    "zip", "mov", "top", "xyz", "icu", "cyou", "sbs", "cfd", "click", "link", "rest",
    "tk", "ml", "ga", "cf", "gq", "buzz", "monster", "quest", "bar", "casa", "shop",
    "store", "online", "site", "website", "live", "fun", "su", "ru", "cn", "cc", "ws",
}

# Multi-label public suffixes we care about (enough for retail; extend freely).
MULTI_SUFFIXES = {
    "co.uk", "org.uk", "ac.uk", "gov.uk", "me.uk", "ltd.uk", "plc.uk",
    "com.au", "net.au", "org.au", "co.nz", "net.nz", "co.jp", "ne.jp", "or.jp",
    "com.br", "com.mx", "com.ar", "com.co", "co.in", "com.cn", "com.hk", "com.tw",
    "com.sg", "com.my", "com.tr", "com.ua", "com.pl", "co.kr", "co.za", "com.ph",
    "co.il", "com.vn", "com.sa", "com.eg", "co.th", "com.pe", "com.ec", "com.uy",
}

BRAND_TOKENS = {
    "amazon", "walmart", "target", "bestbuy", "costco", "homedepot", "lowes", "nike",
    "adidas", "macys", "nordstrom", "newegg", "bhphotovideo", "rei", "zappos", "kohls",
    "samsclub", "staples", "gamestop", "dickssportinggoods", "apple", "samsung", "sony",
    "dell", "lego", "sephora", "ulta", "wayfair", "chewy", "ebay", "paypal", "shopify",
    "dyson", "lululemon", "patagonia", "microsoft", "playstation", "xbox", "steam",
}


@dataclass
class TrustResult:
    url: str
    host: str = ""
    registrable: str = ""
    merchant: str = ""
    verdict: str = "UNKNOWN"
    score: float = 1.0
    reasons: list[str] = field(default_factory=list)
    impersonates: str = ""
    is_shortener: bool = False
    needs_resolution: bool = False
    unicode_host: str = ""
    ascii_host: str = ""

    def to_dict(self) -> dict:
        return {
            "url": self.url, "host": self.host, "registrable": self.registrable,
            "merchant": self.merchant, "verdict": self.verdict, "score": round(self.score, 3),
            "reasons": self.reasons, "impersonates": self.impersonates,
            "is_shortener": self.is_shortener, "needs_resolution": self.needs_resolution,
            "unicode_host": self.unicode_host, "ascii_host": self.ascii_host,
        }


def to_unicode_host(host: str) -> str:
    """Decode punycode labels so we can inspect what the user actually sees."""
    out = []
    for label in host.split("."):
        if label.lower().startswith("xn--"):
            try:
                out.append(label.encode("ascii").decode("idna"))
                continue
            except Exception:
                pass
        out.append(label)
    return ".".join(out)


def to_ascii_host(host: str) -> str:
    """The punycode form — what actually goes on the wire. For a spoofed domain
    this is the only representation that can't be mistaken for the real thing."""
    out = []
    for label in host.split("."):
        try:
            out.append(label.encode("idna").decode("ascii") if any(ord(c) > 127 for c in label)
                       else label)
        except Exception:
            out.append(label)
    return ".".join(out)


def scripts_of(text: str) -> set[str]:
    names = set()
    for ch in text:
        if not ch.isalpha():
            continue
        try:
            name = unicodedata.name(ch)
        except ValueError:
            names.add("UNKNOWN")
            continue
        names.add(name.split(" ")[0])
    return names


def skeleton(text: str, leet: bool = False) -> str:
    """Fold a string to its ASCII lookalike skeleton."""
    text = unicodedata.normalize("NFKC", text).lower()
    out = []
    for ch in text:
        if ch in CONFUSABLES:
            out.append(CONFUSABLES[ch].lower())
            continue
        if leet and ch in LEET:
            out.append(LEET[ch])
            continue
        # strip diacritics: é -> e
        decomp = unicodedata.normalize("NFKD", ch)
        base = "".join(c for c in decomp if not unicodedata.combining(c))
        out.append(base.lower() if base else ch)
    s = "".join(out)
    if leet:
        s = s.replace("rn", "m").replace("vv", "w").replace("cl", "d")
        s = re.sub(r"(.)\1+", r"\1", s)  # amazzon -> amazon
    return s


def registrable_domain(host: str) -> str:
    """eTLD+1 (no PSL download; curated multi-label list covers retail)."""
    host = host.lower().strip(".")
    try:
        ipaddress.ip_address(host)
        return host
    except ValueError:
        pass
    parts = host.split(".")
    if len(parts) <= 2:
        return host
    if ".".join(parts[-2:]) in MULTI_SUFFIXES and len(parts) >= 3:
        return ".".join(parts[-3:])
    return ".".join(parts[-2:])


def levenshtein(a: str, b: str, cap: int = 3) -> int:
    if abs(len(a) - len(b)) > cap:
        return cap + 1
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        if min(cur) > cap:
            return cap + 1
        prev = cur
    return prev[-1]


def check_url(url: str, known_domains: set[str] | None = None,
              shorteners: set[str] | None = None) -> TrustResult:
    """Pure function — no network. Feed it your merchant allowlist."""
    known = {d.lower() for d in (known_domains or set())}
    shorteners = shorteners or set()
    res = TrustResult(url=url)
    parts = urlsplit(url if "://" in url else "https://" + url)
    host = (parts.hostname or "").lower().strip(".")
    res.host = host
    if not host:
        res.verdict, res.score = "BLOCKED", 0.0
        res.reasons.append("no host in url")
        return res

    uni = to_unicode_host(host)
    res.unicode_host = uni
    res.ascii_host = to_ascii_host(uni)
    res.registrable = registrable_domain(host)
    reg_uni = registrable_domain(uni)
    label = reg_uni.split(".")[0]
    tld = reg_uni.rsplit(".", 1)[-1]

    def hit(points: float, reason: str):
        res.score -= points
        res.reasons.append(reason)

    # --- structural red flags -------------------------------------------
    if parts.username or parts.password or "@" in (parts.netloc or "").split("?")[0]:
        hit(1.0, "credential-bait: '@' in authority — the real host is after the @")
    try:
        ipaddress.ip_address(host)
        hit(0.9, "host is a raw IP address, not a domain")
    except ValueError:
        pass
    if parts.port and parts.port not in (80, 443):
        hit(0.4, f"non-standard port :{parts.port}")
    if parts.scheme == "http":
        hit(0.15, "plain http (no TLS)")
    if len(host) > 60 or host.count(".") > 4:
        hit(0.2, f"unusually deep hostname ({host.count('.') + 1} labels)")

    # --- script / homograph ---------------------------------------------
    puny = [l for l in host.split(".") if l.startswith("xn--")]
    non_ascii = any(ord(c) > 127 for c in uni)
    if puny or non_ascii:
        hit(0.35, f"internationalised domain ({'punycode ' + ','.join(puny) if puny else 'non-ASCII'}) "
                  f"→ renders as “{uni}”")
    for lbl in uni.split("."):
        sc = scripts_of(lbl)
        if len(sc) > 1 and "LATIN" in sc:
            hit(0.9, f"mixed-script label “{lbl}” ({'+'.join(sorted(sc))}) — classic homograph attack")
            break

    # --- impersonation --------------------------------------------------
    skel = skeleton(reg_uni)
    skel_leet = skeleton(reg_uni, leet=True)
    skel_label = skeleton(label)
    known_all = known | {f"{b}.com" for b in BRAND_TOKENS}

    is_exact_known = reg_uni in known_all or res.registrable in known_all
    if not is_exact_known:
        for real in sorted(known_all, key=lambda d: (len(d), d)):
            real_label = real.split(".")[0]
            if skel == real:
                res.impersonates = real
                hit(1.5, f"folds to {real} once lookalike characters are normalised "
                         f"(“{reg_uni}” → “{skel}”) but is NOT {real}")
                break
            if skel_leet == skeleton(real, leet=True):
                res.impersonates = real
                hit(1.5, f"character-swap impersonation of {real} "
                         f"(“{reg_uni}” → “{skel_leet}”)")
                break
            if skel_label == real_label:
                # e.g. costco.co.uk when only costco.com is allowlisted: often the
                # real brand abroad, sometimes a squat. Human decides.
                res.impersonates = real
                hit(0.4, f"same brand name as {real} but a different domain "
                         f"({reg_uni}) — verify before buying")
                break
            if len(real_label) >= 5 and len(skel_label) >= 5:
                d = levenshtein(skel_label, real_label, cap=2)
                if d == 1:
                    res.impersonates = real
                    hit(0.9, f"one character away from {real} (typosquat)")
                    break
                if d == 2 and abs(len(skel_label) - len(real_label)) <= 2:
                    res.impersonates = real
                    hit(0.55, f"two characters away from {real} (possible typosquat)")
                    break

    # brand token buried in subdomain or hyphenated domain
    if not res.impersonates and not is_exact_known:
        sub = host[: -len(res.registrable)].strip(".") if host.endswith(res.registrable) else ""
        sub_skel = skeleton(sub, leet=True)
        for b in BRAND_TOKENS | {d.split(".")[0] for d in known}:
            if len(b) < 4:
                continue
            if b in sub_skel and res.registrable not in known_all:
                res.impersonates = f"{b}.com"
                hit(1.0, f"“{b}” appears in the subdomain but the real domain is "
                         f"{res.registrable} — subdomain bait")
                break
            if b in skel_leet.split(".")[0] and reg_uni not in known_all and b != skel_label:
                res.impersonates = f"{b}.com"
                hit(0.7, f"brand “{b}” embedded in an unrelated domain ({reg_uni})")
                break

    # --- reputation -------------------------------------------------------
    if tld in HIGH_RISK_TLDS and (res.impersonates or non_ascii or puny):
        hit(0.5, f"high-risk TLD .{tld} on a brand-lookalike domain")
    elif tld in HIGH_RISK_TLDS and res.registrable not in known:
        hit(0.2, f"high-risk TLD .{tld}")

    if res.registrable in shorteners or res.host in shorteners:
        res.is_shortener = True
        res.needs_resolution = True
        hit(0.3, "URL shortener — destination unknown until resolved")

    # --- verdict ----------------------------------------------------------
    exact = res.registrable in known
    if exact:
        res.merchant = res.registrable
        res.reasons.insert(0, f"exact match on allowlisted merchant {res.registrable}")
        res.score += 0.5

    res.score = max(0.0, min(1.0, res.score))
    if res.impersonates and res.score < 0.55:
        res.verdict = "BLOCKED"
    elif res.score < 0.35:
        res.verdict = "BLOCKED"
    elif res.score < 0.7:
        res.verdict = "SUSPICIOUS"
    elif exact and not res.needs_resolution:
        res.verdict = "TRUSTED"
    elif res.needs_resolution:
        res.verdict = "UNKNOWN"
    else:
        res.verdict = "UNKNOWN"
        if not res.reasons:
            res.reasons.append("domain looks structurally fine but is not on your merchant list")
    return res


RANK = {"BLOCKED": 0, "SUSPICIOUS": 1, "UNKNOWN": 2, "TRUSTED": 3}


def at_least(verdict: str, minimum: str) -> bool:
    return RANK.get(verdict, 0) >= RANK.get(minimum, 3)
