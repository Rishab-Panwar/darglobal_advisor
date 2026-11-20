"""
Comparison feature end to end in real Chrome.

  python tests/ui_compare.py [base_url]      (default http://127.0.0.1:7870)

Covers: project comparison (photos, "price on request", unresolved names), market comparison (numbers match the
tool, best markers only within the same property type, bars), differences-only toggle, open-in-calculator,
"+ Compare" selection tray from project cards, follow-up context, Arabic RTL, mobile overflow, no NaN/undefined.
"""
import asyncio
import re
import sys
from pathlib import Path

from playwright.async_api import async_playwright

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8")
from app.tools import Tools  # noqa: E402

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:7870"
SHOTS = ROOT / "tests"
T = Tools(ROOT / "data")
results = []


def check(name, cond, info=""):
    results.append(bool(cond))
    print(("PASS " if cond else "FAIL ") + name + ("" if cond else f"  -> {info}"), flush=True)


async def ask(pg, q):
    n = await pg.locator(".msg.bot").count()
    await pg.fill("#q", q)
    await pg.keyboard.press("Enter")
    await pg.wait_for_function(f"document.querySelectorAll('.msg.bot').length > {n} && !document.querySelector('#send').disabled",
                               timeout=120000)
    await pg.wait_for_timeout(300)
    bot = pg.locator(".msg.bot").last
    return bot, await bot.locator(".answer > div").first.inner_text()


async def main():
    async with async_playwright() as p:
        b = await p.chromium.launch(channel="chrome", headless=True)
        pg = await b.new_page(viewport={"width": 1440, "height": 1300})
        errors = []
        pg.on("pageerror", lambda e: errors.append(str(e)))
        await pg.goto(BASE + "/?lang=en")
        await pg.wait_for_timeout(1200)

        # 1. projects
        bot, text = await ask(pg, "Compare Trump Cliff Villas and Marea")
        cmp = bot.locator(".cmp")
        check("projects: comparison table rendered", await cmp.count() == 1)
        heads = await cmp.locator("thead .col-nm").all_inner_texts()
        check("projects: both resolved", len(heads) == 2 and any("Trump Cliff" in h for h in heads) and any("Marea" in h for h in heads), heads)
        check("projects: photos shown", await cmp.locator("thead img").count() == 2)
        check("projects: DG price shown as on request", "Price on request" in await cmp.inner_text())
        check("projects: answer mentions both", "Trump" in text and "Marea" in text, text[:200])
        check("projects: no duplicate project cards", await bot.locator(".cards .card").count() == 0)
        txt = await cmp.inner_text()
        check("projects: no NaN/undefined", not re.search(r"NaN|undefined|Infinity", txt), txt[:200])
        await pg.screenshot(path=str(SHOTS / "ui_compare_projects.png"))

        # 2. differences-only toggle hides identical rows
        rows_all = await cmp.locator("tbody tr:visible").count()
        await cmp.locator("[data-diff]").check()
        rows_diff = await cmp.locator("tbody tr:visible").count()
        same = await cmp.locator("tbody tr.same").count()
        check("toggle: identical rows hidden", rows_diff == rows_all - same and (same == 0 or rows_diff < rows_all), (rows_all, rows_diff, same))

        # 3. markets: numbers come from code
        bot, text = await ask(pg, "Riyadh vs Jeddah apartments: which is cheaper?")
        cmp = bot.locator(".cmp")
        check("markets: table rendered", await cmp.count() == 1)
        ref = T.compare("markets", cities=["Riyadh", "Jeddah"], type="Apartment")
        t = await cmp.inner_text()
        for c in ref["columns"]:
            check(f"markets: {c['city']} median price matches tool", f"{c['median_price_sar']:,}" in t, c["median_price_sar"])
        cheaper = min(ref["columns"], key=lambda c: c["median_price_sar"])["city"]
        check("markets: answer names the cheaper city", cheaper in text, text[:200])
        check("markets: best markers present", await cmp.locator("td.best").count() >= 1)
        check("markets: bars present", await cmp.locator("td .bar").count() >= 2)
        # open in calculator
        await cmp.locator("[data-calc]").first.click()
        await pg.wait_for_timeout(400)
        calc = bot.locator(".calc")
        check("markets: open in calculator", await calc.count() == 1 and "Riyadh" in await calc.locator("h4").inner_text())
        await pg.screenshot(path=str(SHOTS / "ui_compare_markets.png"))

        # 4. mixed types: best markers only within the same type
        bot, _ = await ask(pg, "Compare Riyadh and Dammam for apartments and villas")
        cmp = bot.locator(".cmp")
        ref = T.compare("markets", cities=["Riyadh", "Dammam"])
        cols = await cmp.locator("thead .tagc").all_inner_texts()
        check("mixed: 4 market columns", len(cols) == 4, cols)
        check("mixed: winners never cross types", all(len({ref['columns'][i]['type'] for i in v}) == len(v)
                                                       for v in ref["winners"].values()), ref["winners"])

        # 5. + Compare from project cards -> tray -> comparison
        bot, _ = await ask(pg, "Which Dar Global projects are in Oman?")
        picks = bot.locator(".card .pick")
        check("tray: project cards have + Compare", await picks.count() >= 2)
        await picks.nth(0).click()
        await picks.nth(1).click()
        check("tray: shows 2 selected, button enabled", await pg.locator("#tray.on").count() == 1 and not await pg.locator("#tray-go").is_disabled())
        check("tray: clicking pick does not open the link", len(b.contexts[0].pages) == 1)
        await pg.click("#tray-go")
        await pg.wait_for_function("!document.querySelector('#send').disabled && document.querySelectorAll('.cmp').length >= 4", timeout=120000)
        check("tray: comparison produced", await pg.locator(".msg.bot").last.locator(".cmp thead .col-nm").count() == 2)
        check("tray: cleared after use", await pg.locator("#tray.on").count() == 0)

        # 6. follow-up context: "and Dubai?" adds to the previous market comparison
        bot, _ = await ask(pg, "Riyadh vs Jeddah villas")
        bot, _ = await ask(pg, "add Dammam to that comparison")
        cities = await bot.locator(".cmp thead .col-nm").all_inner_texts()
        check("follow-up: Dammam added to comparison", any("Dammam" in c for c in cities) and len(cities) >= 3, cities)

        # 7. unknown project is reported, not invented
        bot, text = await ask(pg, "Compare Trump Cliff Villas with Atlantis The Palm")
        miss = await bot.locator(".cmp .miss").count()
        check("unknown: reported as not found", miss == 1 or re.search(r"Atlantis[^.]{0,160}\b(not|no|cannot|isn't|doesn't|don't)\b|\b(not|no|cannot|don't)\b[^.]{0,160}Atlantis", text, re.I), text[:200])
        check("unknown: no internal jargon", not re.search(r"tool results|provided (data|context)", text, re.I), text[:200])
        check("unknown: no one-column table", await bot.locator(".cmp").count() == 0, await bot.locator(".cmp").count())
        check("no page errors (desktop)", not errors, errors[:2])

        # 8. Arabic + mobile
        m = await b.new_page(viewport={"width": 390, "height": 844})
        await m.goto(BASE + "/?lang=ar")
        await m.wait_for_timeout(1000)
        bot, text = await ask(m, "قارن بين شقق الرياض وجدة")
        cmp = bot.locator(".cmp")
        check("ar: table rendered", await cmp.count() == 1)
        t = await cmp.inner_text()
        check("ar: labels localized", "متوسط السعر المطلوب" in t, t[:120])
        check("ar: answer in Arabic", re.search(r"[؀-ۿ]", text) is not None)
        over = await m.evaluate("[document.documentElement.scrollWidth, document.documentElement.clientWidth]")
        check("mobile: page does not scroll sideways (table scrolls inside)", over[0] <= over[1], over)
        await cmp.scroll_into_view_if_needed()
        await m.screenshot(path=str(SHOTS / "ui_compare_mobile_ar.png"), full_page=False)
        await b.close()
    print(f"\n{sum(results)}/{len(results)} checks passed")
    import datetime, json as _json
    _out = Path(__file__).resolve().parent / "results"
    _out.mkdir(exist_ok=True)
    (_out / "ui_compare.json").write_text(_json.dumps({"suite": "ui_compare", "passed": sum(1 for r in results if (r[1] if isinstance(r, tuple) else r)),
        "total": len(results), "target": BASE, "date": datetime.date.today().isoformat()}), encoding="utf-8")
    sys.exit(0 if all(results) else 1)


asyncio.run(main())
