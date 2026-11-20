"""Deterministic tools the agent can call. Pure Python over the extracted JSON - no LLM involved."""
import json
import re
import statistics
from difflib import SequenceMatcher
from pathlib import Path


def _load(path: Path):
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else []


FACT = re.compile(r"\d|bedroom|located|situated|minutes from|overlooking|collection of", re.I)
SIZE = re.compile(r"sq ?m|sqft|sq ft|m²|square (?:feet|met)", re.I)


def _highlight(text, has_area=False):
    """Most informative 1-2 sentences for a comparison cell: prefer sentences carrying facts (numbers, bedrooms,
    sizes, location features) over pure marketing lines such as "These are no ordinary interiors"."""
    if not text:
        return None
    sents = [x.strip() for x in re.split(r"(?<=[.!؟?])\s+", text.strip()) if len(x.strip()) > 25]
    if has_area:   # the size row is already filled: don't quote a second, possibly conflicting, size figure
        sents = [x for x in sents if not SIZE.search(x)] or sents
    if not sents:
        return text[:240]
    pick = next((x for x in sents if FACT.search(x)), sents[0])
    out = pick
    nxt = sents[sents.index(pick) + 1] if sents.index(pick) + 1 < len(sents) else None
    if len(out) < 130 and nxt and len(out) + len(nxt) < 260:
        out += " " + nxt
    return out[:260]


ICONIC = ("Trump", "Pagani", "Lamborghini", "Missoni", "Elie Saab", "Aston Martin")

# ---- investment maths: defaults are explicit, user-adjustable assumptions (shown in the UI) ----
INVEST_DEFAULTS = {
    "down_payment_pct": 20,   # Saudi first-home buyers can finance up to 90%; 20% is a common choice
    "rate_pct": 5.5,          # assumed annual home-finance rate (APR); varies by bank and profile
    "years": 25,
    "costs_pct": 15,          # share of rent lost to maintenance, vacancy and management
    "purchase_costs_pct": 5,  # Saudi Real Estate Transaction Tax (RETT) is 5% of the price
}
YIELD_TYPES = ("Apartment", "Villa", "Floor", "Building")  # land has no rent to speak of
MIN_N = 3


def monthly_payment(loan: float, rate_pct: float, years: int) -> float:
    """Standard amortising-loan payment: M = L*r / (1 - (1+r)^-n), r monthly."""
    n = int(years) * 12
    r = float(rate_pct) / 100 / 12
    if loan <= 0 or n <= 0:
        return 0.0
    return loan / n if r == 0 else loan * r / (1 - (1 + r) ** -n)


def project_investment(price, gross_yield_pct, down_payment_pct=None, rate_pct=None, years=None,
                       costs_pct=None, purchase_costs_pct=None) -> dict:
    d = INVEST_DEFAULTS
    down_payment_pct = d["down_payment_pct"] if down_payment_pct is None else float(down_payment_pct)
    rate_pct = d["rate_pct"] if rate_pct is None else float(rate_pct)
    years = d["years"] if years is None else int(years)
    costs_pct = d["costs_pct"] if costs_pct is None else float(costs_pct)
    purchase_costs_pct = d["purchase_costs_pct"] if purchase_costs_pct is None else float(purchase_costs_pct)
    price, gross_yield_pct = float(price), float(gross_yield_pct or 0)
    rent = price * gross_yield_pct / 100
    net_rent = rent * (1 - costs_pct / 100)
    down = price * down_payment_pct / 100
    loan = price - down
    pay = monthly_payment(loan, rate_pct, years)
    upfront = down + price * purchase_costs_pct / 100
    cash_flow = net_rent / 12 - pay
    return {
        "price_sar": round(price), "assumptions": {"down_payment_pct": down_payment_pct, "rate_pct": rate_pct,
                                                  "years": years, "costs_pct": costs_pct,
                                                  "purchase_costs_pct": purchase_costs_pct},
        "est_annual_rent_sar": round(rent), "est_monthly_rent_sar": round(rent / 12),
        "gross_yield_pct": round(gross_yield_pct, 2),
        "net_yield_pct": round(net_rent / (price * (1 + purchase_costs_pct / 100)) * 100, 2),
        "down_payment_sar": round(down), "loan_sar": round(loan), "upfront_cash_sar": round(upfront),
        "monthly_payment_sar": round(pay), "total_interest_sar": round(pay * years * 12 - loan),
        "monthly_cash_flow_sar": round(cash_flow),
        "cash_on_cash_pct": round(cash_flow * 12 / upfront * 100, 2) if upfront else None,
        "payback_years": round(price / net_rent, 1) if net_rent else None,
    }


class Tools:
    def __init__(self, data_dir: Path):
        loaded = _load(data_dir / "listings.json")
        # listings flagged as implausible by scraper/extract.py (e.g. a plot "for rent" at SAR 1.3bn/year) are kept
        # on disk for transparency but never used for answers, statistics, cards or comparisons
        self.excluded = [l for l in loaded if l.get("price_suspect")]
        self.listings = [l for l in loaded if not l.get("price_suspect")]
        self.projects = _load(data_dir / "wasalt_projects.json") + _load(data_dir / "darglobal_projects.json")
        self.cities = sorted({l["city"] for l in self.listings})
        self.types = sorted({l["type"] for l in self.listings})

    # ---- filter_listings ---------------------------------------------------
    def filter_listings(self, city=None, purpose=None, type=None, district=None,
                        min_price=None, max_price=None, min_bedrooms=None, max_bedrooms=None,
                        min_area=None, max_area=None, sort="price_asc", limit=8, **_):
        def ok(l):
            if city and city.lower() not in l["city"].lower():
                return False
            if purpose and l["purpose"] != purpose:
                return False
            if type and l["type"].lower() != str(type).lower():
                return False
            if district and district.lower() not in l["location"].lower():
                return False
            p, b, a = l["price_sar"], l["bedrooms"], l["area_sqm"]
            if min_price is not None and p < float(min_price): return False
            if max_price is not None and p > float(max_price): return False
            if min_bedrooms is not None and (b is None or b < int(min_bedrooms)): return False
            if max_bedrooms is not None and (b is None or b > int(max_bedrooms)): return False
            if min_area is not None and (a is None or a < float(min_area)): return False
            if max_area is not None and (a is None or a > float(max_area)): return False
            return True

        rows = [l for l in self.listings if ok(l)]
        key = {"price_asc": lambda l: l["price_sar"], "price_desc": lambda l: -l["price_sar"],
               "area_desc": lambda l: -(l["area_sqm"] or 0), "area_asc": lambda l: l["area_sqm"] or 1e12,
               "price_per_sqm_asc": lambda l: l["price_sar"] / l["area_sqm"] if l["area_sqm"] else 1e12}
        rows.sort(key=key.get(sort, key["price_asc"]))
        prices = [r["price_sar"] for r in rows]
        stats = {"matches": len(rows)}
        if prices:
            stats.update(min_price=min(prices), median_price=statistics.median(prices), max_price=max(prices))
            ppsqm = [r["price_sar"] / r["area_sqm"] for r in rows if r["area_sqm"]]
            if ppsqm:
                stats["median_price_per_sqm"] = round(statistics.median(ppsqm))
        limit = max(1, min(int(limit or 8), 15))
        return {"stats": stats, "listings": rows[:limit],
                "note": "Sample of listings scraped from Wasalt search pages (first pages only), not the full inventory."
                        + (f" {len(self.excluded)} listings with implausible prices are excluded from all figures."
                           if self.excluded else "")}

    # ---- get_project ---------------------------------------------------------
    def get_project(self, name=None, **_):
        if not name or not self.projects:
            return {"projects": [p["name"] for p in self.projects]}
        n = name.lower()
        scored = sorted(self.projects, key=lambda p: -max(
            SequenceMatcher(None, n, p["name"].lower()).ratio(),
            1.0 if n in p["name"].lower() or p["name"].lower() in n else 0))
        return {"best_match": scored[0], "other_projects": [p["name"] for p in scored[1:6]]}

    # ---- list_projects --------------------------------------------------------
    def list_projects(self, source=None, country=None, city=None, brand=None, **_):
        def ok(p):
            if source and p["source"] != source:
                return False
            if country and country.lower() not in (p.get("country") or "").lower() \
                    and country.lower() not in (p.get("location") or "").lower():
                return False
            if city and city.lower() not in (p.get("location") or "").lower():
                return False
            if brand and brand.lower() not in f"{p.get('brand_partner') or ''} {p['name']}".lower():
                return False
            return True
        rows = [p for p in self.projects if ok(p)]
        slim = [{k: p.get(k) for k in ("source", "name", "location", "country", "brand_partner", "developer",
                                       "starts_from", "status", "url") if p.get(k)} for p in rows]
        return {"count": len(rows), "projects": slim}

    # ---- rental yield ------------------------------------------------------------
    def _yield_row(self, city, type, bedrooms=None):
        """Gross yield = median annual rent / median sale price, same city and type (and bedrooms when
        there is enough data). Medians, not per-m2 ratios: listed areas on rentals are often plot sizes."""
        def pick(purpose, beds):
            return [l for l in self.listings if l["city"] == city and l["type"] == type and l["purpose"] == purpose
                    and (beds is None or l.get("bedrooms") == beds)]
        matched = bedrooms is not None
        sale, rent = pick("sale", bedrooms), pick("rent", bedrooms)
        if matched and (len(sale) < MIN_N or len(rent) < MIN_N):
            matched, sale, rent = False, pick("sale", None), pick("rent", None)
        if len(sale) < MIN_N or len(rent) < MIN_N:
            return None
        annual = [l["price_sar"] * (12 if l["price_period"] == "monthly" else 1) for l in rent]
        mr, mp = statistics.median(annual), statistics.median(l["price_sar"] for l in sale)
        n = min(len(sale), len(rent))
        return {"city": city, "type": type, "bedrooms": bedrooms if matched else None,
                "median_annual_rent_sar": round(mr), "median_sale_price_sar": round(mp),
                "gross_yield_pct": round(mr / mp * 100, 2), "price_to_rent_years": round(mp / mr, 1),
                "n_sale": len(sale), "n_rent": len(rent),
                "confidence": "high" if n >= 15 else "medium" if n >= 6 else "low"}

    def _city(self, city):
        if not city:
            return None
        c = city.lower().replace("al-", "al ")
        return next((x for x in self.cities if x.lower() == c or c in x.lower() or x.lower() in c), None)

    def _type(self, type):
        if not type:
            return None
        t = str(type).lower().rstrip("s")
        return next((x for x in YIELD_TYPES if x.lower() == t), None)

    def yield_table(self):
        """Markets for comparisons/rankings. Low-confidence rows (<6 sales or rentals) are left out so a
        thin sample can't top a "best city" answer; they are still returned for a direct city+type query."""
        rows = [self._yield_row(c, t) for c in self.cities for t in YIELD_TYPES]
        return sorted([r for r in rows if r and r["confidence"] != "low"], key=lambda r: -r["gross_yield_pct"])

    def rental_yield(self, city=None, type=None, bedrooms=None, **_):
        asked_city, asked_type = city, type
        city, type = self._city(city), self._type(type)
        # an unknown city/type must never silently widen to other markets (e.g. Taif -> Dammam)
        if (asked_city and not city) or (asked_type and not type):
            return {"rows": [], "note": f"No rental-yield data for {asked_type or ''} in {asked_city or 'Saudi Arabia'} "
                    f"in the Wasalt sample. Markets with data: {', '.join(sorted({r['city'] for r in self.yield_table()}))}."}
        try:
            beds = int(float(bedrooms)) if bedrooms not in (None, "") else None
        except (TypeError, ValueError):
            beds = None
        if city and type:
            rows = [r for r in [self._yield_row(city, type, beds)] if r]
        else:
            rows = [r for r in self.yield_table() if (not city or r["city"] == city) and (not type or r["type"] == type)]
        return {"rows": rows, "method": "gross yield = median annual rent / median asking price, same city and "
                "property type, from the Wasalt listing sample (asking prices, not transactions)",
                "note": "Estimates from a sample, not a forecast or financial advice."}

    def investment(self, price=None, city=None, type=None, bedrooms=None, gross_yield_pct=None, **kw):
        """Mortgage + cash-flow projection for a purchase price, using the market yield for city/type."""
        if price is None and not (city and type):
            return {"error": "need a price, or a city and property type with enough data"}
        y = self.rental_yield(city, type, bedrooms) if (city or type) else {"rows": []}
        row = y["rows"][0] if y["rows"] else None
        if price is None and row:
            price = row["median_sale_price_sar"]
        if price is None:
            return {"error": "need a price, or a city and property type with enough data"}
        gy = float(gross_yield_pct) if gross_yield_pct else (row["gross_yield_pct"] if row else None)
        out = project_investment(price, gy or 0, **{k: kw.get(k) for k in INVEST_DEFAULTS if kw.get(k) is not None})
        out["market"] = row
        if not gy:
            out["note"] = (y.get("note") or "No city and property type given, so there is no market rent to apply.") + \
                " Only the financing figures (payment, interest, upfront cash) are meaningful."
        return out

    # ---- compare --------------------------------------------------------------------
    def _match_project(self, name):
        """Fuzzy project lookup returning (project, score); used to resolve names the user typed or spoke."""
        n = (name or "").lower().strip()
        if not n:
            return None, 0.0
        def score(p):
            pn = p["name"].lower()
            s = SequenceMatcher(None, n, pn).ratio()
            if n in pn or pn in n:
                s = max(s, 0.9)
            words = set(re.findall(r"\w+", n)) - {"the", "by", "at", "of", "and", "villas", "residences"}
            if words and words <= set(re.findall(r"\w+", pn)):
                s = max(s, 0.85)
            return s
        best = max(self.projects, key=score)
        return best, score(best)

    def _project_column(self, p):
        nums = [float(x) for x in re.findall(r"\d+(?:\.\d+)?", (p.get("area_sqm") or "").replace(",", ""))]
        text = p.get("description") or p.get("about") or ""
        summary = _highlight(text, has_area=bool(nums))
        sar = p.get("min_unit_price_sar")
        return {
            "name": p["name"], "source": p["source"], "url": p["url"], "image": (p.get("images") or [None])[0],
            "brand": p.get("brand_partner") or p.get("developer"),
            "location": p.get("location"), "country": p.get("country") or ("Saudi Arabia" if p["source"] == "wasalt" else None),
            "property_type": p.get("property_type"), "unit_types": p.get("unit_types"),
            "area_sqm": p.get("area_sqm"), "area_min": min(nums) if nums else None, "area_max": max(nums) if nums else None,
            "status": p.get("status"),
            "starting_price_sar": float(sar) if sar else None,
            "starting_price_text": p.get("starts_from") or p.get("starting_price"),
            "price_published": bool(sar or p.get("starting_price")),
            "units_listed": int(float(p["units_listed"])) if p.get("units_listed") else None,
            "summary": summary,
        }

    def market_stats(self, city, type):
        """Sale and rent statistics for one city + property type, from the Wasalt sample."""
        sane = {"Apartment": (25, 600), "Villa": (100, 3000), "Floor": (50, 800), "Building": (100, 20000)}.get(type, (20, 50000))
        sale = [l for l in self.listings if l["city"] == city and l["type"] == type and l["purpose"] == "sale"]
        rent = [l for l in self.listings if l["city"] == city and l["type"] == type and l["purpose"] == "rent"]
        if not sale and not rent:
            return None
        def pct(vals, q):
            vals = sorted(vals)
            return vals[min(len(vals) - 1, int(round(q * (len(vals) - 1))))] if vals else None
        prices = [l["price_sar"] for l in sale]
        sized = [l for l in sale if l.get("area_sqm") and sane[0] <= l["area_sqm"] <= sane[1]]
        beds = [l["bedrooms"] for l in sale if l.get("bedrooms")]
        annual = [l["price_sar"] * (12 if l["price_period"] == "monthly" else 1) for l in rent]
        y = self._yield_row(city, type)
        cheapest = min(sale, key=lambda l: l["price_sar"]) if sale else None
        return {
            "city": city, "type": type, "n_sale": len(sale), "n_rent": len(rent),
            "median_price_sar": round(statistics.median(prices)) if prices else None,
            "p25_price_sar": round(pct(prices, .25)) if prices else None,
            "p75_price_sar": round(pct(prices, .75)) if prices else None,
            "median_price_per_sqm": round(statistics.median(l["price_sar"] / l["area_sqm"] for l in sized)) if len(sized) >= MIN_N else None,
            "median_area_sqm": round(statistics.median(l["area_sqm"] for l in sized)) if len(sized) >= MIN_N else None,
            "median_bedrooms": statistics.median(beds) if beds else None,
            "median_annual_rent_sar": round(statistics.median(annual)) if len(annual) >= MIN_N else None,
            "gross_yield_pct": y["gross_yield_pct"] if y else None,
            "confidence": y["confidence"] if y else ("low" if min(len(sale), len(rent)) < 6 else "medium"),
            "cheapest": {"title": cheapest["title"], "price_sar": cheapest["price_sar"], "location": cheapest["location"],
                         "url": cheapest.get("listing_url") or cheapest["url"]} if cheapest else None,
        }

    # direction of "better" per metric, used to mark winners in code (never by the model)
    BETTER = {"median_price_sar": "low", "median_price_per_sqm": "low", "gross_yield_pct": "high",
              "median_area_sqm": "high", "starting_price_sar": "low", "area_max": "high"}

    def _winners(self, cols, keys):
        """Index of the best column per metric. Markets are only ranked against the same property type
        (an apartment is not 'cheaper' than a villa) and low-confidence samples never win."""
        out = {}
        groups = {}
        for i, c in enumerate(cols):
            groups.setdefault(c.get("type", "_"), []).append(i)
        for k in keys:
            for idx in groups.values():
                vals = [(i, cols[i].get(k)) for i in idx
                        if isinstance(cols[i].get(k), (int, float)) and cols[i].get("confidence") != "low"]
                if len(vals) >= 2 and len({v for _, v in vals}) > 1:
                    pick = (min if self.BETTER[k] == "low" else max)(vals, key=lambda iv: iv[1])
                    out.setdefault(k, []).append(pick[0])
        return out

    def compare(self, kind=None, items=None, cities=None, type=None, **_):
        """Side-by-side comparison of 2-4 projects or markets (city + property type)."""
        items = [i for i in (items or []) if i][:4]
        if kind == "projects" or (kind is None and items and not cities):
            cols, unresolved, seen = [], [], set()
            for name in items:
                p, s = self._match_project(str(name))
                if not p or s < 0.55 or p["url"] in seen:
                    unresolved.append(str(name))
                    continue
                seen.add(p["url"])
                cols.append(self._project_column(p))
            note = None
            if any(c["source"] == "darglobal" for c in cols):
                note = "Dar Global does not publish prices on its website; prices are available on request."
            return {"kind": "projects", "columns": cols, "unresolved": unresolved,
                    "winners": self._winners(cols, ["starting_price_sar", "area_max"]), "note": note}
        # markets: explicit [{"city","type"}] items, or cities x type (both main types when type is missing)
        pairs = []   # (resolved city, resolved type, label as asked)
        for it in items:
            if isinstance(it, dict):
                t = self._type(it.get("type")) or self._type(type)
                pairs.append((self._city(it.get("city")), t, f"{it.get('type') or type or ''} {it.get('city')}".strip()))
        for c in (cities or []):
            types = [self._type(type)] if type else ["Apartment", "Villa"]
            pairs += [(self._city(c), t, f"{type or t or ''} in {c}".strip()) for t in types]
        cols, unresolved, seen = [], [], set()
        for c, t, label in pairs:
            if (c, t) in seen or len(cols) >= 4:
                continue
            seen.add((c, t))
            m = self.market_stats(c, t) if c and t else None
            (cols.append(m) if m else unresolved.append(label))
        return {"kind": "markets", "columns": cols, "unresolved": unresolved,
                "winners": self._winners(cols, ["median_price_sar", "median_price_per_sqm", "gross_yield_pct", "median_area_sqm"]),
                "method": "Medians of Wasalt asking prices (sample, not transactions); yield = median annual rent / median price.",
                "note": "Estimates from a listing sample, not financial advice."}

    # ---- facts for the loading screen (computed from the data, so every number is true) ----
    def facts(self) -> list[dict]:
        def med(rows, key=lambda l: l["price_sar"]):
            vals = [key(l) for l in rows if key(l)]
            return round(statistics.median(vals)) if len(vals) >= 3 else None

        def rows(city=None, purpose=None, type=None, period=None):
            return [l for l in self.listings if (not city or l["city"] == city) and (not purpose or l["purpose"] == purpose)
                    and (not type or l["type"] == type) and (not period or l["price_period"] == period)]

        dg = [p for p in self.projects if p["source"] == "darglobal"]
        ws = [p for p in self.projects if p["source"] == "wasalt" and p.get("min_unit_price_sar")]
        out = [
            {"k": "dg_portfolio", "n": len(dg), "c": len({p["country"] for p in dg})},
            {"k": "dg_brands", "list": ", ".join(sorted({p["brand_partner"] for p in dg if p.get("brand_partner")},
                                                   key=lambda b: (b not in ICONIC, b))[:6])},
            {"k": "dg_lse"},
            {"k": "ws_auctions"},
            {"k": "ws_sample", "n": len(self.listings), "c": len(self.cities)},
        ]
        add = lambda k, v, **kw: v and out.append({"k": k, "v": v, **kw})
        add("rent_apartment", med(rows("Riyadh", "rent", "Apartment", "yearly")), city="Riyadh")
        add("rent_apartment", med(rows("Jeddah", "rent", "Apartment", "yearly")), city="Jeddah")
        add("sale_villa", med(rows("Riyadh", "sale", "Villa")), city="Riyadh")
        add("sale_villa", med(rows("Jeddah", "sale", "Villa")), city="Jeddah")
        for city in ("Riyadh", "Jeddah", "Dammam"):
            add("sqm_apartment", med(rows(city, "sale", "Apartment"),
                                     key=lambda l: l["area_sqm"] and l["price_sar"] / l["area_sqm"]), city=city)
        villas = [l for l in rows(purpose="sale", type="Villa") if l.get("area_sqm")]
        if villas:
            big = max(villas, key=lambda l: l["area_sqm"])
            add("biggest_villa", round(big["area_sqm"]), city=big["city"])
        if ws:
            p = min(ws, key=lambda p: p["min_unit_price_sar"])
            add("ws_project_entry", round(p["min_unit_price_sar"]), name=p["name"], city=p.get("city") or "")
        return out

    def catalog(self):
        dg = [p for p in self.projects if p["source"] == "darglobal"]
        return {"cities": self.cities, "types": self.types,
                "projects": [p["name"] for p in self.projects],
                "dg_countries": sorted({p["country"] for p in dg}),
                "brands": sorted({p["brand_partner"] for p in dg if p.get("brand_partner")})}
