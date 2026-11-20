"""
Comparison builder (top-bar "Compare" button) in real Chrome.

  python tests/ui_compare_builder.py [base_url]      (default http://127.0.0.1:7870)

Covers: open from the top bar, type-to-search with highlighting, keyboard selection, browse-all dropdown with
photos, no duplicate picks, validation, instant result without any chat/LLM call, add/remove fields (values kept),
markets tab (numbers equal the tool's, type list follows the city), verdict hand-off to chat, Arabic RTL, mobile.
"""
import asyncio
import re
import sys
import time
from pathlib import Path

from playwright.async_api import async_playwright

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8")
from app.tools import Tools  # noqa: E402

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:7870"
T = Tools(ROOT / "data")
results = []


def check(name, cond, info=""):
    results.append(bool(cond))
    print(("PASS " if cond else "FAIL ") + name + ("" if cond else f"  -> {info}"), flush=True)


async def main():
    async with async_playwright() as p:
        b = await p.chromium.launch(channel="chrome", headless=True)
        pg = await b.new_page(viewport={"width": 1440, "height": 1200})
        errors, chats = [], []
        pg.on("pageerror", lambda e: errors.append(str(e)))
        pg.on("request", lambda r: "/api/chat" in r.url and chats.append(r.url))
        await pg.goto(BASE + "/?lang=en")
        await pg.wait_for_timeout(1000)

        # 1. open from the top bar
        await pg.click("#cmp-btn")
        box = pg.locator(".cmpb").last
        await box.locator(".slot").first.wait_for()
        check("builder opens with two fields", await box.locator(".slot").count() == 2)
        slot1, slot2 = box.locator(".slot").nth(0), box.locator(".slot").nth(1)

        # 2. type to search + keyboard pick
        await slot1.locator("input").fill("trump cl")
        await pg.wait_for_timeout(200)
        opts = await slot1.locator("li[role=option]").all_inner_texts()
        check("search filters the list", any("Trump Cliff Villas" in o for o in opts) and len(opts) <= 3, opts)
        check("match is highlighted", await slot1.locator("li mark").count() >= 1)
        await slot1.locator("input").press("ArrowDown")
        await slot1.locator("input").press("Enter")
        check("keyboard selection", await slot1.get_attribute("data-value") == "Trump Cliff Villas")
        check("preview shows photo + name", await slot1.locator(".pv img").count() == 1 and "Trump Cliff" in await slot1.locator(".pv").inner_text())

        # 3. browse all + no duplicates
        await slot2.locator(".tg").click()
        await pg.wait_for_timeout(200)
        n_all = await slot2.locator("li[role=option]").count()
        check("browse-all lists every project", n_all == len(T.projects), (n_all, len(T.projects)))
        check("options have photos", await slot2.locator("li[role=option] img").count() >= 40)
        check("group headers shown", await slot2.locator("li.grp").count() == 2)
        dis = slot2.locator("li[aria-disabled=true]")
        check("already-picked project is disabled", await dis.count() == 1 and "Trump Cliff" in await dis.inner_text())
        await slot2.locator("li[role=option]", has_text="Marea").first.click()
        check("click selection", (await slot2.get_attribute("data-value") or "").startswith("Marea"))

        # 4. instant result, no chat/LLM call
        t0 = time.time()
        await box.locator(".run").click()
        await box.locator(".out .cmp").wait_for()
        dt = time.time() - t0
        check("result is instant (< 2 s)", dt < 2, round(dt, 2))
        check("no chat request was made", not chats, chats)
        check("table has both projects", await box.locator(".out .cmp thead .col-nm").count() == 2)
        check("price on request shown", "Price on request" in await box.locator(".out .cmp").inner_text())
        await pg.screenshot(path=str(ROOT / "tests" / "ui_builder_projects.png"))

        # 5. add / remove keep values
        await box.locator(".add").click()
        await pg.wait_for_timeout(200)
        check("add a third field", await box.locator(".slot").count() == 3)
        s3 = box.locator(".slot").nth(2)
        await s3.locator("input").fill("shams")
        await pg.wait_for_timeout(150)
        await s3.locator("input").press("Enter")      # single match -> Enter picks it
        check("enter picks the single match", await s3.get_attribute("data-value") == "Shams Al Riyadh")
        await box.locator(".run").click()
        await pg.wait_for_function("document.querySelector('.cmpb .out .cmp thead') && document.querySelectorAll('.cmpb .out .cmp thead .col-nm').length === 3")
        check("three-way comparison", await box.locator(".out .cmp thead .col-nm").count() == 3)
        await box.locator(".slot").nth(1).locator(".rm").click()
        await pg.wait_for_timeout(300)
        vals = [await box.locator(".slot").nth(i).get_attribute("data-value") for i in range(await box.locator(".slot").count())]
        check("remove keeps the other picks", vals == ["Trump Cliff Villas", "Shams Al Riyadh"], vals)

        # 6. validation
        await box.locator(".slot").nth(1).locator("input").fill("zzzz")
        await pg.wait_for_timeout(150)
        check("no-match message", "No matching project" in await box.locator(".slot").nth(1).inner_text())
        await box.locator(".run").click()
        check("needs two items", "at least two" in await box.locator(".hint").inner_text())

        # 7. markets tab
        await box.locator(".seg button[data-m=markets]").click()
        await pg.wait_for_timeout(300)
        c1 = await box.locator(".slot").nth(0).locator("[data-city]").input_value()
        c2 = await box.locator(".slot").nth(1).locator("[data-city]").input_value()
        check("markets prefilled Riyadh vs Jeddah", (c1, c2) == ("Riyadh", "Jeddah"), (c1, c2))
        await box.locator(".run").click()
        await pg.wait_for_function("document.querySelector('.cmpb .out .cmp') && document.querySelector('.cmpb .out .cmp').innerText.includes('Median asking price')")
        ref = T.compare("markets", [{"city": "Riyadh", "type": "Apartment"}, {"city": "Jeddah", "type": "Apartment"}])
        txt = await box.locator(".out .cmp").inner_text()
        check("market numbers equal the tool's", all(f"{c['median_price_sar']:,}" in txt for c in ref["columns"]))
        await box.locator(".slot").nth(1).locator("[data-city]").select_option("Al Khobar")
        types = await box.locator(".slot").nth(1).locator("[data-type] option").all_inner_texts()
        check("type list follows the city", len(types) >= 2 and all("(" in t for t in types[1:]), types)
        check("still no chat request", not chats, chats)

        # 8. verdict hands off to chat (one LLM call)
        await box.locator(".slot").nth(1).locator("[data-city]").select_option("Jeddah")
        await box.locator(".slot").nth(1).locator("[data-type]").select_option("Apartment")
        await box.locator(".run").click()
        await box.locator(".verdict").wait_for()
        await box.locator(".verdict").click()
        await pg.wait_for_function("document.querySelectorAll('.msg.user').length === 1 && !document.querySelector('#send').disabled", timeout=120000)
        said = await pg.locator(".msg.user .bubble").inner_text()
        check("verdict asks the advisor", "Compare" in said and "Riyadh" in said and "Jeddah" in said, said)
        check("no page errors (desktop)", not errors, errors[:2])

        # 8b. typing a full name without clicking counts; one match out of two is reported, not tabled
        await pg.click("#new")
        await pg.click("#cmp-btn")
        nb = pg.locator(".cmpb").last
        await nb.locator(".slot").first.wait_for()
        await nb.locator(".slot").nth(0).locator("input").fill("DG1")
        await nb.locator(".slot").nth(1).locator("input").fill("Fairway Villas")
        check("full name closes the list (Compare button not covered)",
              await nb.locator(".slot").nth(1).locator(".cb.open").count() == 0)
        await nb.locator(".run").click(timeout=5000)
        await nb.locator(".out .cmp").wait_for(timeout=5000)
        check("typed full names count as picks", await nb.locator(".out .cmp thead .col-nm").count() == 2)
        r = await pg.evaluate("""fetch('/api/compare', {method:'POST', headers:{'Content-Type':'application/json'},
            body: JSON.stringify({kind:'projects', items:['DG1','Atlantis The Palm']})}).then(r => r.json())""")
        check("one match out of two -> reported as not found", len(r["columns"]) == 1 and r["unresolved"] == ["Atlantis The Palm"])
        # new conversation clears the + Compare tray
        bot = pg.locator(".msg.bot")
        await pg.click("#new")
        check("new conversation clears state", await pg.locator("#tray.on").count() == 0 and await pg.locator(".msg").count() == 0)

        # 9. Arabic RTL + mobile
        m = await b.new_page(viewport={"width": 390, "height": 844})
        await m.goto(BASE + "/?lang=ar")
        await m.wait_for_timeout(900)
        await m.click("#cmp-btn")
        mb = m.locator(".cmpb").last
        await mb.locator(".slot").first.wait_for()
        check("ar: builder localized", "ماذا تود أن تقارن" in await mb.inner_text())
        await mb.locator(".slot").nth(0).locator("input").fill("Trump")
        await m.wait_for_timeout(150)
        await mb.locator(".slot").nth(0).locator("li[role=option]").first.click()
        await mb.locator(".slot").nth(1).locator(".tg").click()
        await m.wait_for_timeout(150)
        await mb.locator(".slot").nth(1).locator("li[role=option]:not([aria-disabled=true])").nth(3).click()
        await mb.locator(".run").click()
        await mb.locator(".out .cmp").wait_for()
        over = await m.evaluate("[document.documentElement.scrollWidth, document.documentElement.clientWidth]")
        check("mobile: no sideways page scroll", over[0] <= over[1], over)
        check("ar: dir=rtl", await m.evaluate("document.documentElement.dir") == "rtl")
        await mb.locator(".slots").scroll_into_view_if_needed()
        await m.screenshot(path=str(ROOT / "tests" / "ui_builder_mobile_ar.png"))
        await b.close()
    print(f"\n{sum(results)}/{len(results)} checks passed")
    import datetime, json as _json
    _out = Path(__file__).resolve().parent / "results"
    _out.mkdir(exist_ok=True)
    (_out / "ui_compare_builder.json").write_text(_json.dumps({"suite": "ui_compare_builder", "passed": sum(1 for r in results if (r[1] if isinstance(r, tuple) else r)),
        "total": len(results), "target": BASE, "date": datetime.date.today().isoformat()}), encoding="utf-8")
    sys.exit(0 if all(results) else 1)


asyncio.run(main())
