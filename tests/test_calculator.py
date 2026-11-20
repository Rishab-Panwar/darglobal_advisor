"""Calculator maths and edge cases (python -m pytest tests/ or python tests/test_calculator.py)."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from app.tools import Tools, monthly_payment, project_investment  # noqa: E402

t = Tools(ROOT / "data")


def test_payment_formula():
    assert round(monthly_payment(1_600_000, 5.5, 25)) == 9825   # textbook amortisation
    assert abs(monthly_payment(1_200_000, 0, 10) - 10_000) < 1e-6
    assert monthly_payment(0, 5, 25) == 0
    assert monthly_payment(1e6, 5, 0) == 0


def test_full_cash_purchase():
    r = project_investment(1_000_000, 6, down_payment_pct=100)
    assert r["loan_sar"] == 0 and r["monthly_payment_sar"] == 0
    assert r["monthly_cash_flow_sar"] == round(1_000_000 * 0.06 * 0.85 / 12)


def test_net_yield_and_upfront_include_rett():
    r = project_investment(1_000_000, 6)
    assert r["net_yield_pct"] == round(60_000 * 0.85 / 1_050_000 * 100, 2)
    assert r["upfront_cash_sar"] == 200_000 + 50_000
    assert r["total_interest_sar"] > 0


def test_zero_yield_and_string_inputs():
    r = project_investment(1_000_000, 0)
    assert r["payback_years"] is None and r["est_annual_rent_sar"] == 0
    r = project_investment("2000000", "4.5", down_payment_pct="30", rate_pct="6", years="20")
    assert r["assumptions"]["years"] == 20 and r["loan_sar"] == 1_400_000


def test_investment_defaults_and_missing_data():
    r = t.investment(city="Riyadh", type="Villa")
    assert r["price_sar"] == r["market"]["median_sale_price_sar"]
    r = t.investment(price=1_000_000, city="Taif", type="Villa")   # no rent data there
    assert r["market"] is None and r.get("note") and r["monthly_payment_sar"] > 0
    assert "error" in t.investment()


def test_name_normalisation():
    assert t.rental_yield("riyadh", "villas")["rows"][0]["type"] == "Villa"
    assert t._city("al-khobar") == "Al Khobar"
    assert t.rental_yield("Jeddah", "Land")["rows"] == []           # land has no meaningful yield
    assert len(t.rental_yield("Riyadh", "Apartment", "three")["rows"]) == 1
    assert t.rental_yield("Riyadh", "Apartment", 3.0)["rows"][0]["bedrooms"] == 3
    assert t.rental_yield("Riyadh", "Villa", 12)["rows"][0]["bedrooms"] is None  # falls back when too few


def test_ranking_excludes_thin_samples():
    tab = t.yield_table()
    assert tab and all(r["confidence"] != "low" for r in tab)
    assert all(a["gross_yield_pct"] >= b["gross_yield_pct"] for a, b in zip(tab, tab[1:]))
    assert all(2 <= r["gross_yield_pct"] <= 12 for r in tab)
    direct = t.rental_yield("Makkah", "Villa")["rows"]
    assert direct and direct[0]["confidence"] == "low"


if __name__ == "__main__":
    fns = [v for k, v in dict(globals()).items() if k.startswith("test_")]
    for f in fns:
        f()
    print(f"{len(fns)} test groups passed")
