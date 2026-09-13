"""The link check is the only thing standing between a Discord message and your card."""
from app.core.links import SHORTENERS, extract_urls, slug_guess
from app.core.trust import check_url

KNOWN = {"amazon.com", "walmart.com", "target.com", "bestbuy.com", "nike.com",
         "costco.com", "newegg.com", "rei.com"}


def verdict(url):
    return check_url(url, KNOWN, SHORTENERS).verdict


class TestSpoofs:
    def test_cyrillic_homograph_is_blocked(self):
        r = check_url("https://www.tаrget.com/p/deal", KNOWN, SHORTENERS)   # Cyrillic 'а'
        assert r.verdict == "BLOCKED"
        assert r.impersonates == "target.com"
        assert any("mixed-script" in x for x in r.reasons)

    def test_greek_homograph_is_blocked(self):
        r = check_url("https://nιke.com/launch", KNOWN, SHORTENERS)         # Greek iota
        assert r.verdict == "BLOCKED"
        assert r.impersonates == "nike.com"

    def test_punycode_is_decoded_and_flagged(self):
        r = check_url("https://xn--trget-9ye.com/p/deal", KNOWN, SHORTENERS)
        assert r.verdict == "BLOCKED"
        assert r.unicode_host != r.host          # we show the user what it renders as

    def test_both_representations_are_reported(self):
        """The UI has to be able to show the unambiguous form — a spoof and the
        real domain look identical until you punycode-encode them."""
        from app.core.trust import skeleton
        spoof = check_url("https://www.t\u0430rget.com/p/x", KNOWN, SHORTENERS)
        real = check_url("https://www.target.com/p/x", KNOWN, SHORTENERS)
        # same glyphs on screen, different bytes underneath
        assert skeleton(spoof.unicode_host) == skeleton(real.unicode_host)
        assert spoof.unicode_host != real.unicode_host
        # the punycode form is the one a human can actually tell apart
        assert spoof.ascii_host.startswith("www.xn--")
        assert real.ascii_host == "www.target.com"

    def test_leetspeak_typosquat(self):
        assert verdict("https://amaz0n.com/dp/B01") == "BLOCKED"

    def test_doubled_letter_typosquat(self):
        assert verdict("https://wallmart.com/ip/tv") == "BLOCKED"

    def test_one_char_typosquat(self):
        assert verdict("https://bestbuv.com/site/tv") == "BLOCKED"

    def test_subdomain_bait(self):
        r = check_url("https://target.com.secure-checkout.ru/p/deal", KNOWN, SHORTENERS)
        assert r.verdict == "BLOCKED"
        assert r.registrable == "secure-checkout.ru"

    def test_credential_bait(self):
        r = check_url("https://target.com@evil.ru/p/deal", KNOWN, SHORTENERS)
        assert r.verdict == "BLOCKED"
        assert r.registrable == "evil.ru"

    def test_brand_in_unrelated_domain(self):
        assert verdict("https://bestbuy-clearance.shop/tv") == "BLOCKED"

    def test_raw_ip_host(self):
        assert verdict("http://192.168.4.9:8080/checkout") == "BLOCKED"


class TestLegitimate:
    def test_exact_match_is_trusted(self):
        r = check_url("https://www.target.com/p/sony-headphones/-/A-867", KNOWN, SHORTENERS)
        assert r.verdict == "TRUSTED"
        assert r.registrable == "target.com"

    def test_deep_path_and_query_are_fine(self):
        assert verdict("https://www.bestbuy.com/site/x/6534489.p?skuId=6534489") == "TRUSTED"

    def test_unknown_but_clean_domain_is_unknown_not_blocked(self):
        assert verdict("https://somerandomstore.com/p/item") == "UNKNOWN"

    def test_brand_cctld_needs_a_human(self):
        # costco.co.uk is probably real, but it is not the domain you allowlisted
        assert verdict("https://costco.co.uk/p/item") == "SUSPICIOUS"

    def test_shortener_needs_resolution(self):
        r = check_url("https://bit.ly/3xYz", KNOWN, SHORTENERS)
        assert r.needs_resolution and r.verdict != "TRUSTED"


class TestExtraction:
    def test_pulls_link_out_of_a_messy_message(self):
        msg = "🔥🔥 PRICE ERROR https://www.target.com/p/x/-/A-86718543?utm_source=discord $39!!"
        assert extract_urls(msg) == ["https://www.target.com/p/x/-/A-86718543"]

    def test_markdown_and_angle_brackets(self):
        assert extract_urls("see [deal](<https://www.bestbuy.com/site/x.p>) now") == \
               ["https://www.bestbuy.com/site/x.p"]

    def test_bare_domain_without_scheme(self):
        assert extract_urls("walmart.com/ip/Dyson-V15/123 lol")[0].startswith("https://walmart.com/ip/")

    def test_multiple_links_deduped_in_order(self):
        out = extract_urls("https://a.com/x https://b.com/y https://a.com/x")
        assert out == ["https://a.com/x", "https://b.com/y"]

    def test_instant_name_from_slug(self):
        assert slug_guess("https://www.target.com/p/sony-wh-1000xm5-headphones/-/A-867") == \
               "Sony Wh 1000Xm5 Headphones"
