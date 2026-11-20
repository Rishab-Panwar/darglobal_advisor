"""
Full-resolution candidates for the hero panel (the tall photo on the left of the desktop layout).

The project-card photos are resized to 1400 px wide, which looks soft when a tall panel scales them up on a
high-DPI screen, and some project pages lead with logo artwork rather than a photo. This pass downloads each
project's original images through the same real-Chrome session, keeps only large ones (>= 1800 px wide), and writes
a contact sheet for a person to choose from. The chosen ones go in data/heroes.json (name, location, file).

  python scraper/hero_images.py            -> scraper/hero_candidates/*.jpg + contact_sheet.jpg + candidates.json
"""
import asyncio
import hashlib
import io
import json
import sys
from pathlib import Path

from PIL import Image, ImageDraw
from playwright.async_api import async_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent))
from scrape import PROFILE, wait_for_human  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "scraper" / "hero_candidates"
MIN_W = 1800


async def main():
    OUT.mkdir(exist_ok=True)
    img = json.loads((ROOT / "data" / "images.json").read_text(encoding="utf-8"))
    dg = json.loads((ROOT / "data" / "darglobal_projects.json").read_text(encoding="utf-8"))
    seen_on = {}
    for lst in img["projects"].values():
        for u in set(lst):
            seen_on[u] = seen_on.get(u, 0) + 1
    jobs = [(p, u) for p in dg for u in img["projects"].get(p["url"], []) if seen_on.get(u) == 1]
    print(f"{len(jobs)} original images to check")
    cands = []
    async with async_playwright() as pw:
        ctx = await pw.chromium.launch_persistent_context(str(PROFILE), channel="chrome", headless=False,
                                                          args=["--disable-blink-features=AutomationControlled"])
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        try:
            await page.goto("https://darglobal.co.uk/projects", wait_until="commit", timeout=90000)
            await page.wait_for_timeout(3000)
            await wait_for_human(page, "darglobal")
        except Exception as e:
            print("warm-up:", e)
        for p, u in jobs:
            try:
                r = await page.goto(u, wait_until="load", timeout=45000)
                if not r or r.status != 200:
                    raise RuntimeError(f"HTTP {r.status if r else '?'}")
                im = Image.open(io.BytesIO(await r.body())).convert("RGB")
                w, h = im.size
                print(f"{w:5d}x{h:<5d} {p['name'][:40]:40s} {u.rsplit('/', 1)[-1][:50]}", flush=True)
                if w >= MIN_W:
                    if w > 2600:
                        im = im.resize((2600, round(h * 2600 / w)), Image.LANCZOS)
                    name = hashlib.sha1(u.encode()).hexdigest()[:12] + ".jpg"
                    im.save(OUT / name, "JPEG", quality=88, optimize=True, progressive=True)
                    cands.append({"file": name, "src": u, "name": p["name"], "location": p.get("location", ""),
                                  "w": w, "h": h})
            except Exception as e:
                print("skip", u, e, flush=True)
            await page.wait_for_timeout(500)
        await ctx.close()
    (OUT / "candidates.json").write_text(json.dumps(cands, indent=1, ensure_ascii=False), encoding="utf-8")
    # contact sheet: numbered thumbnails so a person can pick real architectural photos (not logos or renders of text)
    tw, th, cols = 320, 200, 5
    rows = (len(cands) + cols - 1) // cols
    sheet = Image.new("RGB", (cols * (tw + 8), max(1, rows) * (th + 30)), (18, 18, 20))
    d = ImageDraw.Draw(sheet)
    for i, c in enumerate(cands):
        t = Image.open(OUT / c["file"]); t.thumbnail((tw, th))
        x, y = (i % cols) * (tw + 8), (i // cols) * (th + 30)
        sheet.paste(t, (x, y))
        d.text((x + 4, y + th + 6), f"#{i} {c['name'][:30]} {c['w']}x{c['h']}", fill=(232, 211, 164))
    sheet.save(OUT / "contact_sheet.jpg", quality=85)
    print(f"{len(cands)} candidates >= {MIN_W}px  ->  {OUT / 'contact_sheet.jpg'}")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    asyncio.run(main())
