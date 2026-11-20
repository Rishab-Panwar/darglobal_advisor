"""Data-quality guards for the extracted project data (offline). Run after every re-scrape.

  python tests/test_data_quality.py
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scraper"))
from extract import prose_specs  # noqa: E402

DG = json.loads((ROOT / "data" / "darglobal_projects.json").read_text(encoding="utf-8"))
WS = json.loads((ROOT / "data" / "wasalt_projects.json").read_text(encoding="utf-8"))


def test_every_project_has_its_own_page():
    generic = [p["name"] for p in DG if p["url"].rstrip("/").endswith("/projects")]
    assert not generic, f"fell back to the portfolio grid (wrong photos/specs): {generic}"
    albert = next(p for p in DG if "Albert Hall" in p["name"])
    assert albert["url"].lower().endswith("/one-of-one/the-albert-hall-mansion")


def test_no_photo_is_shared_between_projects():
    owners = {}
    for p in DG + WS:
        for img in p.get("images") or []:
            owners.setdefault(img, []).append(p["name"])
    shared = {k: v for k, v in owners.items() if len(v) > 1}
    assert not shared, shared
    assert all(p.get("images") for p in DG), [p["name"] for p in DG if not p.get("images")]


def test_core_fields_coverage():
    weak = [p["name"] for p in DG if sum(1 for k in ("property_type", "unit_types", "status", "area_sqm") if p.get(k)) < 1]
    assert not weak, weak


def test_prose_parser():
    t = "Units span 109 to 722 sqm across one- to three-bedroom apartments. Under Development."
    s = prose_specs(t, "X Tower")
    assert s["area_sqm"] == "109 - 722" and s["unit_types"] == "1–3-bedroom" and s["status"] == "Under development"
    assert prose_specs("The tower offers one-, two-, and three-bedroom residences.", "Y")["unit_types"] == "1, 2 & 3-bedroom"
    assert prose_specs("UNIT TYPES 3 - 5 Bedroom Villas", "Z")["unit_types"] == "3–5-bedroom"
    assert prose_specs("Spanning 14,768 sqft. SOLD OUT", "The Pagani Penthouse") == {
        "status": "Sold Out", "area_sqm": "1372", "property_type": "Penthouse"}
    # a master-planned community's size is not a unit size; a community page's sub-project bedrooms aren't its own
    assert "area_sqm" not in prose_specs("AIDA is a 3.3 million sqm community", "Aida")
    assert "unit_types" not in prose_specs("Trump Cliff Villas, 3-bedroom residences", "Aida", community=True)


def test_implausible_listings_never_used():
    sys.path.insert(0, str(ROOT))
    from app.tools import Tools
    t = Tools(ROOT / "data")
    assert t.excluded and all(l.get("price_suspect") for l in t.excluded)
    assert not any(l.get("price_suspect") for l in t.listings)
    land = t.filter_listings(city="Riyadh", type="Land", purpose="sale", sort="price_asc")
    assert land["listings"][0]["price_sar"] > 100_000, land["listings"][0]       # not the SAR 3,500 "plot"
    rent = t.filter_listings(purpose="rent", sort="price_desc")
    assert rent["listings"][0]["price_sar"] < 20_000_000, rent["listings"][0]   # not the SAR 1.3bn/year "rent"
    assert all((l.get("bedrooms") or 0) <= 15 for l in t.listings)


def test_status_wording_is_consistent():
    allowed = {None, "Under Development", "Under Construction", "Completed", "Sold Out", "Completed & Sold Out",
               "Waiting List Open", "Ready to Move In"}
    odd = {p.get("status") for p in DG + WS} - allowed
    assert not odd, odd
    shams = next(p for p in WS if p["name"] == "Shams Al Riyadh")
    assert shams["developer"] == "Dar Al Arkan Real Estate Development"


if __name__ == "__main__":
    fns = [v for k, v in dict(globals()).items() if k.startswith("test_")]
    for f in fns:
        f()
    print(f"{len(fns)} data-quality test groups passed")
