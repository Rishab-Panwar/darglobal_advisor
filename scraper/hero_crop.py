"""
Make the hero-panel photos: 3:4 portrait crops (1500x2000) of hand-picked, full-resolution project photos.

  python scraper/hero_crop.py           (after scraper/hero_images.py; downloads originals again only if needed)

PICKS maps a contact-sheet index -> horizontal focus (0 = left edge, 1 = right edge), chosen by eye so the building
sits in the middle of a tall, narrow panel. Interiors, logos, maps and lifestyle shots were left out.
"""
import asyncio
import io
import json
import sys
from pathlib import Path

from PIL import Image
from playwright.async_api import async_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent))
from scrape import PROFILE  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
CAND = ROOT / "scraper" / "hero_candidates"
OUT = ROOT / "app" / "static" / "img" / "hero"
PICKS = {108: 0.43, 13: 0.5, 66: 0.5, 43: 0.42, 52: 0.47, 104: 0.5, 48: 0.6, 56: 0.5, 37: 0.5}
TARGET = (1500, 2000)


def crop(im, fx):
    W, H = im.size
    tw = round(H * 3 / 4)
    if tw > W:                                   # portrait source: trim top/bottom instead
        th = round(W * 4 / 3)
        im = im.crop((0, (H - th) // 2, W, (H - th) // 2 + th))
        W, H = im.size
        tw = W
    x = min(max(0, round(fx * W - tw / 2)), W - tw)
    c = im.crop((x, 0, x + tw, H))
    return c.resize(TARGET, Image.LANCZOS) if c.height > TARGET[1] else c


async def originals(urls):
    out = {}
    async with async_playwright() as pw:
        ctx = await pw.chromium.launch_persistent_context(str(PROFILE), channel="chrome", headless=False,
                                                          args=["--disable-blink-features=AutomationControlled"])
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        for u in urls:
            r = await page.goto(u, wait_until="load", timeout=60000)
            out[u] = Image.open(io.BytesIO(await r.body())).convert("RGB")
            await page.wait_for_timeout(600)
        await ctx.close()
    return out


def main():
    cands = json.loads((CAND / "candidates.json").read_text(encoding="utf-8"))
    OUT.mkdir(parents=True, exist_ok=True)
    # the candidate files were capped at 2600 px wide; wide panoramas need the original to reach 2000 px tall
    need = [cands[i]["src"] for i in PICKS if min(cands[i]["h"], 2600 * cands[i]["h"] / cands[i]["w"]) < TARGET[1]]
    full = asyncio.run(originals(need)) if need else {}
    heroes = []
    for i, fx in PICKS.items():
        c = cands[i]
        im = full.get(c["src"]) or Image.open(CAND / c["file"]).convert("RGB")
        out = crop(im, fx)
        name = c["file"].replace(".jpg", ".webp")
        out.save(OUT / name, "WEBP", quality=84, method=6)
        heroes.append({"file": name, "name": c["name"], "location": c["location"], "size": f"{out.width}x{out.height}"})
        print(f"#{i:3d} {c['name'][:40]:40s} {out.size} {(OUT / name).stat().st_size // 1024} KB")
    (ROOT / "data" / "heroes.json").write_text(json.dumps(heroes, indent=1, ensure_ascii=False), encoding="utf-8")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    main()
