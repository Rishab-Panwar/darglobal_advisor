"""
Turn scraped Wasalt page text into structured listing records (data/listings.json).

Listing cards on search pages render as consecutive lines, e.g.
    150,000
    yearly                       <- optional (rentals)
    Villa 300 SQM Facing East with 8 Bedrooms
    Al-Noor, North Jeddah, Jeddah
Project pages list units as
    Ref No: ERJQ-2020 / Unit No: 014 / Land 1920 SQM / ... / 17,760,000
"""
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"

PRICE = re.compile(r"^\s*(\d{1,3}(?:,\d{3})+|\d{4,})\s*$")
PERIOD = re.compile(r"^(yearly|monthly|daily|weekly)$", re.I)
TITLE = re.compile(
    r"^(?P<type>Villa|Apartment|Floor|Land|Building|Duplex|Townhouse|Rest house|Farm|Palace|Chalet|"
    r"Studio|Office|Shop|Showroom|Warehouse|Commercial|Room)\b(?P<rest>.*)$", re.I)
SQM = re.compile(r"([\d.,]+)\s*SQM", re.I)
BEDS = re.compile(r"(\d+)\s*Bedrooms?", re.I)
FACING = re.compile(r"Facing\s+([A-Za-z ]+?)(?:\s+on|\s+with|$)", re.I)
STREET = re.compile(r"on\s+(\d+)m\s+Width\s+Street", re.I)


CITY_ALIASES = {"aldammam": "Dammam", "makkah al mukarramah": "Makkah", "alttayif": "Taif",
                "khobar": "Al Khobar", "al-khobar": "Al Khobar", "khamis mushait": "Khamis Mushait"}


def city_name(c):
    return CITY_ALIASES.get(c.strip().lower(), c.strip())


def num(s):
    try:
        return float(s.replace(",", ""))
    except (ValueError, AttributeError):
        return None


def page_purpose(url, title):
    s = f"{url} {title}".lower()
    return "rent" if "rent" in s else "sale"


def parse_cards(rec):
    lines = [l.strip() for l in rec["text"].split("\n") if l.strip()]
    out = []
    for i, ln in enumerate(lines):
        m = TITLE.match(ln)
        if not m or not SQM.search(ln) and not BEDS.search(ln):
            continue
        # price is 1-2 lines above the title (rent pages put "yearly" in between)
        price, period = None, None
        for j in (i - 1, i - 2):
            if j >= 0 and PERIOD.match(lines[j]):
                period = lines[j].lower()
            elif j >= 0 and PRICE.match(lines[j]) and price is None:
                price = num(lines[j])
        if price is None:
            continue
        loc = lines[i + 1] if i + 1 < len(lines) else ""
        parts = [p.strip() for p in loc.split(",")]
        if len(parts) < 2:
            continue
        rest = m.group("rest")
        # cards show icon counts after the location: bedrooms, then bathrooms (small integers)
        counts = []
        for w in lines[i + 2:i + 4]:
            if w.isdigit() and int(w) < 40:
                counts.append(int(w))
            else:
                break
        beds = int(BEDS.search(ln).group(1)) if BEDS.search(ln) else (counts[0] if counts else None)
        baths = counts[1] if len(counts) > 1 else None
        if m.group("type").lower() == "land":
            beds = baths = None
        out.append({
            "source": "wasalt",
            "purpose": "rent" if period else page_purpose(rec["url"], rec.get("title", "")),
            "type": m.group("type").title(),
            "title": ln,
            "price_sar": price,
            "price_period": period or "total",
            "area_sqm": num(SQM.search(ln).group(1)) if SQM.search(ln) else None,
            "bedrooms": beds,
            "bathrooms": baths,
            "facing": FACING.search(rest).group(1).strip() if FACING.search(rest) else None,
            "street_width_m": int(STREET.search(rest).group(1)) if STREET.search(rest) else None,
            "district": parts[0],
            "city": city_name(parts[-1]),
            "location": loc,
            "url": rec["url"],
        })
    return out


def parse_project(rec):
    lines = [l.strip() for l in rec["text"].split("\n") if l.strip()]
    name = rec.get("h1") or rec.get("title", "").split(" For Sale")[0]
    name = name.split("|")[0].strip()
    # header: "<district>, <area>, <city>Map view"
    loc = next((l.replace("Map view", "").strip() for l in lines if l.endswith("Map view")), "")
    developer = None
    for k, l in enumerate(lines):
        if l == "Starts From" and k + 3 < len(lines):
            developer = lines[k + 3]
    status = next((l for l in lines if l in ("Under Construction", "Ready", "Off Plan", "Completed")), None)
    starts = None
    if "Starts From" in lines:
        starts = lines[lines.index("Starts From") + 1]
    about = ""
    if "About Project" in lines:
        idx = len(lines) - 1 - lines[::-1].index("About Project")
        about = lines[idx + 1] if idx + 1 < len(lines) else ""
    units = []
    for k, l in enumerate(lines):
        if l.startswith("Ref No:"):
            window = lines[k:k + 12]
            title = next((w for w in window if TITLE.match(w)), None)
            price = next((num(w) for w in window if PRICE.match(w) and num(w) and num(w) > 10000), None)
            if title and price:
                units.append({"unit_title": title, "price_sar": price,
                              "area_sqm": num(SQM.search(title).group(1)) if SQM.search(title) else None})
    prices = [u["price_sar"] for u in units]
    return {
        "source": "wasalt", "name": name, "location": loc,
        "city": loc.split(",")[-1].strip() if loc else None,
        "developer": developer, "status": status, "starts_from": starts,
        "units_listed": len(units),
        "min_unit_price_sar": min(prices) if prices else None,
        "max_unit_price_sar": max(prices) if prices else None,
        "sample_units": units[:5], "about": about[:800], "url": rec["url"],
    }


COUNTRIES = [("UAE", "United Arab Emirates"), ("UNITED ARAB EMIRATES", "United Arab Emirates"),
             ("SPAIN", "Spain"), ("QATAR", "Qatar"), ("OMAN", "Oman"), ("LONDON", "United Kingdom"),
             ("ENGLAND", "United Kingdom"), ("UNITED KINGDOM", "United Kingdom"), ("KSA", "Saudi Arabia"),
             ("SAUDI ARABIA", "Saudi Arabia"), ("RIYADH", "Saudi Arabia"), ("JEDDAH", "Saudi Arabia")]
BRANDS = ["Trump", "Pagani", "Aston Martin", "Missoni", "Lamborghini", "Fendi Casa", "Elie Saab", "Mouawad",
          "Marriott", "W Residences", "Nickelodeon", "Mulliner"]


def fix_case(s):
    for a, b in ((r"\bUae\b", "UAE"), (r"\bKsa\b", "KSA"), (r"\bRak\b", "RAK"), (r"\bDg1\b", "DG1"),
                 (r"\bBy\b", "by"), (r"\bOf\b", "of"), (r"\bAt\b", "at"), (r"\bThe\b(?!^)", "The")):
        s = re.sub(a, b, s)
    return s


def _words(s):
    return set(re.findall(r"[a-z0-9]+", s.lower())) - {"the", "by", "at", "of", "and", "darglobal", "dar", "global"}


def match_page(name, pages):
    """Find the project's own page: exact H1 first, then best word overlap with H1/title/slug."""
    overrides = {"AIDA": "/landing-page/aida", "TRUMP MANSIONS": "/rayana/trump-mansions",
                 # the page slug is upper-case and singular ("THE-ALBERT-HALL-MANSION"), so word matching missed it
                 "7&8 ALBERT HALL MANSIONS": "/one-of-one/the-albert-hall-mansion"}
    if name.upper() in overrides:
        hit = next((p for p in pages if p["url"].rstrip("/").lower().endswith(overrides[name.upper()])), None)
        if hit:
            return hit
    skip = ("/projects", "/partners/", "/press/", "/blog", "/insights")
    cands = [p for p in pages if not any(s in p["url"] for s in skip)]
    for p in cands:
        if p.get("h1", "").split(" | ")[0].strip().upper() == name.upper():
            return p
    nw = _words(name)
    best, score = None, 0.0
    for p in cands:
        pw = _words(f"{p.get('h1', '')} {p['url'].rstrip('/').split('/')[-1]}")
        j = len(nw & pw) / len(nw | pw) if nw | pw else 0
        if j > score:
            best, score = p, j
    return best if score >= 0.5 else None


SPEC_LABELS = {"PROPERTY TYPE": "property_type", "UNIT TYPE": "unit_types", "STATUS": "status",
               "AREA (SQM)": "area_sqm", "BENEFITS": "benefits", "STARTING PRICE": "starting_price",
               "STARTING FROM": "starting_price", "PRICE": "starting_price", "HANDOVER": "handover",
               "COMPLETION": "handover", "PAYMENT PLAN": "payment_plan", "BEDROOMS": "unit_types",
               "UNITS": "unit_types", "UNIT TYPES": "unit_types", "AREA(SQM)": "area_sqm", "AREA (SQ M)": "area_sqm"}


WORDNUM = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8}
_N = r"(\d|one|two|three|four|five|six|seven|eight)"
STATUS_RE = re.compile(r"\b(sold out|completed|ready to move in|under (?:development|construction)|coming soon|"
                       r"launching soon|join the waiting list)\b", re.I)
TYPE_WORDS = ["Penthouse", "Mansion", "Villas", "Villa", "Townhouses", "Residences", "Apartments", "Plots", "Hotel"]


def prose_specs(text, name, community=False):
    """Fallback for pages without a spec table ("One of One" collection, landing pages): read status, bedrooms,
    unit size and property type from the prose. Only used for fields the spec table didn't provide."""
    head, out = text[:5000], {}
    m = STATUS_RE.search(head)
    if m:
        st = m.group(1).lower()
        out["status"] = "Waiting list open" if "waiting" in st else st.capitalize().replace("Sold out", "Sold Out")
    # bedrooms: "one- to three-bedroom" is a range (1-3); "one-, two-, and three-bedroom" is a list (1, 2, 3)
    beds_txt = None
    m = re.search(r"((?:\b(?:\d|one|two|three|four|five|six|seven|eight)\b[\s,\-–]*(?:and|&|or|to)?[\s,\-–]*)+)bed(?:room)?s?\b",
                  head, re.I)
    if m:
        seg = m.group(1)
        nums = [int(x) if x.isdigit() else WORDNUM[x.lower()]
                for x in re.findall(r"\b(\d|one|two|three|four|five|six|seven|eight)\b", seg, re.I)]
        if len(nums) == 2 and re.search(r"\bto\b|–|\s-\s", seg, re.I) and nums[0] < nums[1]:
            beds_txt = f"{nums[0]}–{nums[1]}-bedroom"
        elif nums:
            uniq = sorted(set(nums))
            beds_txt = (", ".join(map(str, uniq[:-1])) + " & " if len(uniq) > 1 else "") + f"{uniq[-1]}-bedroom"
    if beds_txt and not community:      # a community page mentions its sub-projects' bedrooms: not its own spec
        out["unit_types"] = beds_txt
    # sizes: keep "from 109 to 722 sqm" as a range; convert sq ft; ignore community-scale areas
    sizes = []
    unit_re = r"(sq\.?\s?ft|sqft|square feet|sq\.?\s?m\b|sqm|m²|square met)"
    for a, b, unit in re.findall(r"(?<!million )(?<![\d.])(?:([\d,]+(?:\.\d+)?)\s*(?:to|-|–)\s*)?([\d,]+(?:\.\d+)?)\s*" + unit_re,
                                 head, re.I):
        for x in (a, b):
            if not x:
                continue
            v = float(x.replace(",", ""))
            if "f" in unit.lower():
                v = v / 10.764
            if 30 <= v <= 5000:          # a home, not a master-planned community (AIDA is 3.3 million m²)
                sizes.append(round(v))
    if sizes and not community:
        out["area_sqm"] = f"{min(sizes)} - {max(sizes)}" if min(sizes) != max(sizes) else str(sizes[0])
    if community and re.search(r"master[- ]planned|community", head, re.I):
        out["property_type"] = "Master-planned community"
        return out
    for w in TYPE_WORDS:                 # the project's own name is the most reliable signal, then the opening text
        if re.search(rf"\b{w}\b", name, re.I) or re.search(rf"\b{w}\b", head[:600], re.I):
            out["property_type"] = w
            break
    return out


def project_specs(page, name=""):
    lines = [l.strip() for l in page["text"].split("\n") if l.strip()]
    specs = {}
    for k, l in enumerate(lines[:-1]):
        key = SPEC_LABELS.get(l.upper())
        if key and key not in specs and len(lines[k + 1]) < 120 and lines[k + 1].upper() not in SPEC_LABELS:
            specs[key] = lines[k + 1]
    community = "/landing-page/" in page["url"]
    from_text = {k: v for k, v in prose_specs(page["text"], name, community).items() if k not in specs}
    specs.update(from_text)
    if from_text:
        specs["specs_from_text"] = sorted(from_text)     # provenance: read from prose, not from a spec table
    desc = next((l for l in lines[:40] if len(l) > 160), "")
    specs["description"] = desc[:900]
    return specs


def darglobal_projects():
    """Parse the portfolio grid on darglobal.co.uk/projects: alternating LOCATION / NAME lines."""
    pages = [json.loads(l) for l in (DATA / "darglobal.jsonl").open(encoding="utf-8")]
    page_by_slug = {p["url"].rstrip("/").split("/")[-1]: p["url"] for p in pages}
    proj = next((p for p in pages if p["url"].rstrip("/").endswith("/projects")), None)
    if not proj:
        return []
    lines = [l.strip() for l in proj["text"].split("\n") if l.strip()]
    start = lines.index("Saudi Arabia") + 1 if "Saudi Arabia" in lines else 0
    end = next((i for i, l in enumerate(lines) if l.startswith("REGISTER YOUR INTEREST")), len(lines))
    out, i = [], start
    while i < end - 1:
        loc, name = lines[i], lines[i + 1]
        country = next((c for key, c in COUNTRIES if key in loc.upper()), None)
        if country and name.isupper():
            tagline = lines[i + 2] if i + 2 < end and not lines[i + 2].isupper() else ""
            title = fix_case(name.title())
            loc = fix_case(loc.title()).replace(" | ", ", ")
            brand = next((b for b in BRANDS if b.lower() in f"{name} {tagline}".lower()), None)
            page = match_page(name, pages)
            rec = {"source": "darglobal", "name": title, "location": loc, "country": country,
                   "brand_partner": brand, "tagline": tagline,
                   "url": page["url"] if page else "https://darglobal.co.uk/projects"}
            if page:
                rec.update(project_specs(page, title))
            out.append(rec)
            i += 3 if tagline else 2
        else:
            i += 1
    return out


def _slug(s):
    return re.sub(r"[^a-z0-9]+", "-", (s or "").lower().replace(".", "")).strip("-")


def attach_images(listings, dg, ws):
    """Join data/images.json (from scraper/images.py) onto listings and projects.
    A Wasalt card's detail URL ends with the listing title slug + id, so a listing matches a card on the
    same page by title slug; the price in the card text breaks ties."""
    f = DATA / "images.json"
    if not f.exists():
        return
    img = json.loads(f.read_text(encoding="utf-8"))
    pools = {}
    for page, cards in img.get("cards", {}).items():
        for c in cards:
            slug = re.sub(r"-\d+$", "", c["href"].rstrip("/").rsplit("/", 1)[-1])
            prices = {float(x.replace(",", "")) for x in re.findall(r"\d{1,3}(?:,\d{3})+", c["text"])}
            pools.setdefault(page, []).append({**c, "slug": slug, "prices": prices})
    hit = 0
    for l in listings:
        pool = pools.get(l["url"], [])
        want = _slug(l["title"])
        cands = [c for c in pool if c["slug"] == want] or [c for c in pool if want and want in c["slug"]]
        best = next((c for c in cands if l["price_sar"] in c["prices"]), cands[0] if len(cands) == 1 else None)
        if best:
            l["image"], l["listing_url"] = best["src"], best["href"]
            pool.remove(best)
            hit += 1
    # project photos: self-hosted copies (scraper/fetch_images.py) because Dar Global's CDN blocks cross-origin use
    cf = DATA / "image_cache.json"
    local = json.loads(cf.read_text(encoding="utf-8")) if cf.exists() else {}
    # an image that appears on several project pages is a shared banner/logo (e.g. the "One of One"
    # collection artwork), not a photo of that project
    seen_on = {}
    for lst in img.get("projects", {}).values():
        for u in set(lst):
            seen_on[u] = seen_on.get(u, 0) + 1
    for p in dg + ws:
        if p["url"].rstrip("/").endswith("/projects"):
            continue                     # the portfolio grid shows *other* projects' photos: never borrow them
        mine = [local[u] for u in img.get("projects", {}).get(p["url"], []) if u in local and seen_on.get(u, 0) == 1]
        if mine:
            p["images"] = mine
    print(f"images: {hit}/{len(listings)} listings, {sum(1 for p in dg + ws if p.get('images'))} projects")


STATUS_CANON = {"under development": "Under Development", "under construction": "Under Construction",
                "completed": "Completed", "sold out": "Sold Out", "completed and sold out": "Completed & Sold Out",
                "waiting list open": "Waiting List Open", "ready to move in": "Ready to Move In"}
DEVELOPER_EN = {"دار الأركان للتطوير العقاري": "Dar Al Arkan Real Estate Development"}   # Dar Global's parent company


def tidy_projects(dg, ws):
    """One spelling per status; translate the one developer name that matters for cross-site questions."""
    for p in dg + ws:
        if p.get("status"):
            p["status"] = STATUS_CANON.get(re.sub(r"\s+", " ", p["status"].strip().lower()), p["status"].strip())
        if p.get("developer") in DEVELOPER_EN:
            p["developer_ar"], p["developer"] = p["developer"], DEVELOPER_EN[p["developer"]]


def flag_implausible(listings):
    """Some Wasalt listings carry prices no market produces (a 400 m² plot for sale at SAR 3,500; a plot for rent at
    SAR 1.3 billion a year): agent typos or per-m² prices typed into the total field. They are kept but flagged, and
    the app leaves them out of statistics, filters, cards and comparisons."""
    n = 0
    for l in listings:
        price, area = l["price_sar"], l.get("area_sqm") or 0
        annual = price * (12 if l["price_period"] == "monthly" else 1)
        why = None
        if l["purpose"] == "sale" and area and price / area < 150:
            why = f"sale price SAR {price / area:,.0f}/m²"
        elif l["purpose"] == "rent" and (annual > 20_000_000 or (area and annual / area > 10_000)):
            why = f"rent SAR {annual:,.0f}/year"
        elif l["purpose"] == "rent" and annual < 1000:
            why = f"rent SAR {annual:,.0f}/year"
        if why:
            l["price_suspect"] = why
            n += 1
        if (l.get("bedrooms") or 0) > 15:                 # "Apartment 600 SQM with 45 Bedrooms"
            l["bedrooms_raw"], l["bedrooms"] = l["bedrooms"], None
    return n


def main():
    dg = darglobal_projects() if (DATA / "darglobal.jsonl").exists() else []
    (DATA / "darglobal_projects.json").write_text(json.dumps(dg, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"{len(dg)} Dar Global projects")
    listings, projects, seen = [], [], set()
    # the image pass (scraper/images.py) re-reads listing pages; prefer that newer text so every
    # listing's photo and detail link come from the same snapshot of the page
    img = DATA / "images.json"
    fresh = json.loads(img.read_text(encoding="utf-8")).get("page_text", {}) if img.exists() else {}
    for line in (DATA / "wasalt.jsonl").open(encoding="utf-8"):
        rec = json.loads(line)
        if rec["url"] in fresh:
            rec["text"] = fresh[rec["url"]]
        if "/project/" in rec["url"]:
            projects.append(parse_project(rec))
            continue
        for c in parse_cards(rec):
            key = (c["title"], c["price_sar"], c["location"])
            if key not in seen:
                seen.add(key)
                listings.append(c)
    attach_images(listings, dg, projects)
    tidy_projects(dg, projects)
    print(f"{flag_implausible(listings)} listings flagged as implausible (kept, excluded from statistics)")
    (DATA / "darglobal_projects.json").write_text(json.dumps(dg, ensure_ascii=False, indent=1), encoding="utf-8")
    (DATA / "listings.json").write_text(json.dumps(listings, ensure_ascii=False, indent=1), encoding="utf-8")
    (DATA / "wasalt_projects.json").write_text(json.dumps(projects, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"{len(listings)} listings, {len(projects)} projects")


if __name__ == "__main__":
    main()
