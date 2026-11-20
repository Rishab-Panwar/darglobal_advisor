"""
Image pass: revisit already-scraped pages and record image URLs (no new pages are crawled).

  python scraper/images.py      -> data/images.json

- Wasalt listing pages: every property card's photo + its own detail link (matched to listings later
  by the title slug and price in scraper/extract.py).
- Dar Global and Wasalt project pages: the first large hero/gallery images.

Same polite rules as scrape.py: real Chrome with the persistent profile, 1.5-3.5 s between pages,
and a human completes any bot check. Images are hot-linked from the sites' CDNs, not re-hosted.
"""
import asyncio
import json
import random
import sys
from pathlib import Path

from playwright.async_api import async_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent))
from scrape import PROFILE, wait_for_human  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"

CARDS_JS = r"""() => {
  const seen = new Map();
  document.querySelectorAll('a[href*="/property/"]').forEach(a => {
    const img = a.querySelector('img');
    if (!img) return;
    const src = img.currentSrc || img.src || img.dataset.src || '';
    if (!/imagedelivery|cdn\.wasalt/.test(src) || seen.has(a.href)) return;
    // climb to the card container that also holds the price / title text
    let el = a, text = '';
    for (let i = 0; i < 6 && el; i++, el = el.parentElement) {
      text = (el.innerText || '').trim();
      if (/SAR|\d{2,3},\d{3}/.test(text) && text.length > 30) break;
    }
    seen.set(a.href, {href: a.href, src, text: text.slice(0, 400)});
  });
  // the page's current text too, so listings and photos come from the same snapshot (inventory rotates daily)
  const hidden = [...document.querySelectorAll('header,footer,nav')];
  hidden.forEach(e => e.style.display = 'none');
  const text = document.body.innerText;
  hidden.forEach(e => e.style.display = '');
  return {cards: [...seen.values()], text};
}"""

HERO_JS = r"""(host) => {
  const out = [];
  for (const i of document.images) {
    const s = i.currentSrc || i.src || '';
    if (!s.includes(host) || /logo|icon|\.svg/i.test(s)) continue;
    if (i.naturalWidth >= 700 && !out.includes(s)) out.push(s);
    if (out.length >= 4) break;
  }
  const og = document.querySelector('meta[property="og:image"]');
  if (og && og.content && og.content.startsWith('http') && !out.includes(og.content)) out.push(og.content);
  return out;
}"""


async def visit(page, url):
    await page.goto(url, wait_until="domcontentloaded", timeout=60000)
    await page.wait_for_timeout(2000)
    if not await wait_for_human(page, url):
        return False
    for _ in range(12):  # scroll to trigger lazy-loaded images
        await page.mouse.wheel(0, 1400)
        await page.wait_for_timeout(450)
    await page.wait_for_timeout(800)
    return True


async def main():
    out = DATA / "images.json"
    res = json.loads(out.read_text(encoding="utf-8")) if out.exists() else {}
    res.setdefault("cards", {}), res.setdefault("projects", {}), res.setdefault("page_text", {})
    res["cards"] = {u: c for u, c in res["cards"].items() if u in res["page_text"]}  # re-visit pages saved without text
    listings = json.loads((DATA / "listings.json").read_text(encoding="utf-8"))
    dg = json.loads((DATA / "darglobal_projects.json").read_text(encoding="utf-8"))
    ws = json.loads((DATA / "wasalt_projects.json").read_text(encoding="utf-8"))
    jobs = [("cards", u, None) for u in sorted({l["url"] for l in listings})]
    jobs += [("projects", p["url"], "cdn.darglobal.co.uk") for p in dg]
    jobs += [("projects", p["url"], "imagedelivery.net") for p in ws]
    jobs = [j for j in jobs if j[1] not in res[j[0]]]  # resume
    print(f"{len(jobs)} pages to visit")

    async with async_playwright() as p:
        ctx = await p.chromium.launch_persistent_context(
            str(PROFILE), channel="chrome", headless=False, locale="en-GB",
            viewport={"width": 1366, "height": 900}, args=["--disable-blink-features=AutomationControlled"])
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        for n, (kind, url, host) in enumerate(jobs, 1):
            try:
                if not await visit(page, url):
                    print("challenge not cleared, skipping", url)
                    continue
                if kind == "cards":
                    snap = await page.evaluate(CARDS_JS)
                    got, res["page_text"][url] = snap["cards"], snap["text"]
                else:
                    got = await page.evaluate(HERO_JS, host)
                res[kind][url] = got
                print(f"[{n}/{len(jobs)}] {len(got):3d} images  {url}", flush=True)
                out.write_text(json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")
            except Exception as e:
                print("error", url, e)
            await page.wait_for_timeout(random.randint(1500, 3500))
        await ctx.close()


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    asyncio.run(main())
