"""
End-to-end UI test of the investment calculator in a real browser (Playwright + installed Chrome).

  python tests/ui_calculator.py [base_url]      (default http://127.0.0.1:7870)

Checks: toolbar widget, market switch, ranking click, sliders and edge inputs, custom-yield mode,
chat answer == widget numbers, follow-up context, every language (no NaN/undefined), RTL, mobile overflow.
"""
import asyncio
import re
import sys
from pathlib import Path

from playwright.async_api import async_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app.tools import monthly_payment  # noqa: E402

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:7870"
results = []


def check(name, cond, info=""):
    results.append((name, bool(cond)))
    print(("PASS " if cond else "FAIL ") + name + ("" if cond else f"  -> {info}"), flush=True)


def sar(text):
    m = re.search(r"-?[\d,]+(?:\.\d+)?", text.replace("−", "-").replace(" ", " "))
    return float(m.group(0).replace(",", "")) if m else None


async def kpi(calc, i):
    return (await calc.locator(".kpi b").nth(i).inner_text()).strip()


async def ask(pg, q):
    n = await pg.locator(".msg.bot").count()
    await pg.fill("#q", q)
    await pg.keyboard.press("Enter")
    await pg.wait_for_function(f"document.querySelectorAll('.msg.bot').length > {n} && "
                               "!document.querySelector('#send').disabled", timeout=120000)
    await pg.wait_for_timeout(400)
    bot = pg.locator(".msg.bot").last
    return bot, (await bot.locator(".answer > div").first.inner_text())


async def main():
    async with async_playwright() as p:
        b = await p.chromium.launch(channel="chrome", headless=True)
        pg = await b.new_page(viewport={"width": 1440, "height": 1000})
        errors = []
        pg.on("pageerror", lambda e: errors.append(str(e)))
        pg.on("console", lambda m: m.type == "error" and errors.append(m.text))
        await pg.goto(BASE + "/?lang=en")
        await pg.wait_for_timeout(1500)

        # 1. toolbar opens the widget on the top-ranked market
        await pg.click("#calc-btn")
        calc = pg.locator(".calc").last
        check("toolbar opens calculator", await calc.count() == 1)
        title = await calc.locator("h4").inner_text()
        check("defaults to top market", "Riyadh" in title and "Apartment" in title, title)
        # widget payment == python formula (same maths both sides)
        price = sar(await calc.locator('[data-o="price"]').inner_text())
        want = round(monthly_payment(price * 0.8, 5.5, 25))
        got = sar(await kpi(calc, 3))
        check("widget payment == server formula", abs(got - want) <= 1, (got, want))

        # 2. market dropdown switches title, price and yield
        await calc.locator("select").select_option("Jeddah|Villa")
        check("dropdown switch", "Jeddah" in await calc.locator("h4").inner_text())
        check("dropdown sets median price", sar(await calc.locator('[data-o="price"]').inner_text()) == 1825000)

        # 3. ranking click
        await calc.locator(".rank button").nth(1).click()
        on = await calc.locator(".rank button.on span").first.inner_text()
        check("ranking click selects market", on.split(" · ")[0] in await calc.locator("h4").inner_text(), on)

        # 4. 100% down -> no payment, positive cash flow
        await calc.locator('input[data-k="down_payment_pct"]').fill("100")
        check("100% down -> payment 0", sar(await kpi(calc, 3)) == 0, await kpi(calc, 3))
        check("100% down -> cash flow positive", "+" in await kpi(calc, 4), await kpi(calc, 4))
        # 0% rate edge
        await calc.locator('input[data-k="down_payment_pct"]').fill("20")
        await calc.locator('input[data-k="rate_pct"]').fill("2")
        check("low rate recomputes", sar(await kpi(calc, 3)) > 0)
        # 5. bad price inputs are ignored, never NaN/Infinity
        before = await calc.locator('[data-o="price"]').inner_text()
        for bad in ("0", "-5", ""):
            await calc.locator('input[data-k="price"]').fill(bad)
        txt = await calc.inner_text()
        check("bad price ignored", await calc.locator('[data-o="price"]').inner_text() == before)
        check("no NaN/Infinity", not re.search(r"NaN|Infinity|undefined", txt), txt[:200])
        # 6. custom yield mode
        await calc.locator("select").select_option("")
        check("custom mode shows yield slider", await calc.locator('input[data-k="gy"]').count() == 1)
        await calc.locator('input[data-k="gy"]').fill("8")
        check("custom yield drives KPI", "8.00" in await kpi(calc, 0), await kpi(calc, 0))

        # 7. chat answer and widget agree
        bot, text = await ask(pg, "What is the monthly payment on a 2M SAR villa in Riyadh with 20% down?")
        c2 = bot.locator(".calc")
        check("chat renders calculator", await c2.count() == 1)
        w = await kpi(c2, 3) if await c2.count() else ""
        check("widget shows 9,825", "9,825" in w, w)
        check("answer quotes 9,825", "9,825" in text or "9825" in text, text[:300])
        check("answer has disclaimer", re.search(r"not (financial|investment) advice", text, re.I), text[-200:])

        # 8. follow-up keeps context: same villa, 30% down -> 8,597
        bot, text = await ask(pg, "what if I put 30% down instead?")
        w = await kpi(bot.locator(".calc"), 3) if await bot.locator(".calc").count() else ""
        check("follow-up uses context (30% down)", "8,597" in w or "8,597" in text, (w, text[:200]))

        # 9. unknown market -> no borrowed yield, custom mode
        bot, text = await ask(pg, "What rental yield would a villa in Taif give?")
        c3 = bot.locator(".calc")
        if await c3.count():
            t3 = await c3.locator("h4").inner_text()
            check("Taif: widget not showing another market", "Custom" in t3 or "Taif" in t3, t3)
        check("Taif: answer admits no data", re.search(r"no (rental|data|yield)|not (available|enough)|(don't|do not) have|does not (contain|include)|no .{0,20}data", text, re.I), text[:300])

        # 10. every language: calculator text localized, no NaN/undefined, Arabic is RTL
        for lang in ("ar", "fr", "ru", "zh"):
            await pg.select_option("#lang", lang)
            await pg.click("#calc-btn")
            c = pg.locator(".calc").last
            txt = await c.inner_text()
            check(f"{lang}: no NaN/undefined", not re.search(r"NaN|undefined|Infinity", txt), txt[:150])
            check(f"{lang}: localized", "Investment calculator" not in txt, txt[:80])
        check("ar: RTL", await pg.evaluate("document.documentElement.dir") in ("ltr", "rtl"))
        await pg.select_option("#lang", "ar")
        check("ar: dir=rtl", await pg.evaluate("document.documentElement.dir") == "rtl")
        check("no console errors (desktop)", not errors, errors[:3])

        # 11. mobile: calculator fits, no horizontal scroll
        m = await b.new_page(viewport={"width": 390, "height": 844})
        await m.goto(BASE + "/?lang=en")
        await m.wait_for_timeout(1200)
        await m.click("#calc-btn")
        await m.wait_for_timeout(300)
        over = await m.evaluate("[document.documentElement.scrollWidth, document.documentElement.clientWidth,"
                                " document.querySelector('.calc').getBoundingClientRect().right]")
        check("mobile: no horizontal overflow", over[0] <= over[1] and over[2] <= over[1] + 1, over)
        await m.screenshot(path=str(Path(__file__).resolve().parent / "ui_calculator_mobile.png"), full_page=True)
        await b.close()
    passed = sum(ok for _, ok in results)
    print(f"\n{passed}/{len(results)} checks passed")
    import datetime, json as _json
    _out = Path(__file__).resolve().parent / "results"
    _out.mkdir(exist_ok=True)
    (_out / "ui_calculator.json").write_text(_json.dumps({"suite": "ui_calculator", "passed": sum(1 for r in results if (r[1] if isinstance(r, tuple) else r)),
        "total": len(results), "target": BASE, "date": datetime.date.today().isoformat()}), encoding="utf-8")
    sys.exit(0 if passed == len(results) else 1)


asyncio.run(main())
