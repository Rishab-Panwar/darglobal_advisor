"""Compare tool: name resolution, market statistics, best-value rules (offline, no LLM)."""
import statistics
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from app.tools import Tools  # noqa: E402

t = Tools(ROOT / "data")


def test_project_resolution():
    r = t.compare("projects", ["Trump Cliff Villas", "Marea by Missoni", "Atlantis The Palm"])
    names = [c["name"] for c in r["columns"]]
    assert names[0] == "Trump Cliff Villas" and names[1].startswith("Marea"), names
    assert r["unresolved"] == ["Atlantis The Palm"]                  # never guess a project we don't have
    assert r["note"] and "on request" in r["note"]                   # Dar Global publishes no prices
    assert t.compare("projects", ["Marea", "marea, interiors by missoni"])["unresolved"]  # same project twice -> one column


def test_project_fields():
    c = t.compare("projects", ["Marea", "Shams Al Riyadh"])["columns"]
    assert c[0]["area_min"] == 147 and c[0]["area_max"] == 382 and c[0]["price_published"] is False
    assert c[1]["source"] == "wasalt" and c[1]["starting_price_sar"] > 0 and c[1]["country"] == "Saudi Arabia"


def test_market_stats_match_raw_data():
    m = t.market_stats("Riyadh", "Apartment")
    sale = [l["price_sar"] for l in t.listings if l["city"] == "Riyadh" and l["type"] == "Apartment" and l["purpose"] == "sale"]
    assert m["median_price_sar"] == round(statistics.median(sale)) and m["n_sale"] == len(sale)
    assert m["p25_price_sar"] <= m["median_price_sar"] <= m["p75_price_sar"]
    assert m["gross_yield_pct"] == t._yield_row("Riyadh", "Apartment")["gross_yield_pct"]   # same maths as the calculator
    assert m["cheapest"]["price_sar"] == min(sale)


def test_winners_same_type_only_and_no_low_confidence():
    r = t.compare("markets", cities=["Riyadh", "Dammam"])                 # apartments + villas, Dammam apt is low-confidence
    cols = r["columns"]
    for idx in r["winners"].values():
        assert len({cols[i]["type"] for i in idx}) == len(idx)            # at most one winner per type
        assert all(cols[i]["confidence"] != "low" for i in idx)
    r = t.compare("markets", cities=["Riyadh", "Jeddah"], type="Apartment")
    cheaper = min(range(2), key=lambda i: r["columns"][i]["median_price_sar"])
    assert r["winners"]["median_price_sar"] == [cheaper]


def test_unknown_market_and_limits():
    r = t.compare("markets", cities=["Taif", "Riyadh"], type="Villa")
    assert r["unresolved"] == ["Villa in Taif"] and len(r["columns"]) == 1
    r = t.compare("markets", cities=["Riyadh", "Jeddah", "Dammam", "Madinah", "Makkah"])
    assert len(r["columns"]) == 4                                        # table stays readable


if __name__ == "__main__":
    fns = [v for k, v in dict(globals()).items() if k.startswith("test_")]
    for f in fns:
        f()
    print(f"{len(fns)} compare test groups passed")
