"""
Self-host project photos (downloaded through the same real-Chrome session as the scraper: Dar Global's
bot protection rejects plain HTTP clients).

Dar Global's CDN sends `Cross-Origin-Resource-Policy: same-origin`, so browsers refuse to show its
images on another site. For the 47 project cards (and the hero panel) we download the first two images
per project once, resize them to max 1400 px wide WebP, and serve them from app/static/img/.
Wasalt listing photos (Cloudflare Images) allow cross-origin use and stay hot-linked.

  python scraper/fetch_images.py      -> app/static/img/p/*.webp + data/image_cache.json
"""
import asyncio
import hashlib
import io
import json
import sys
from pathlib import Path

from PIL import Image
from playwright.async_api import async_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent))
from scrape import PROFILE, wait_for_human  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
OUT = ROOT / "app" / "static" / "img" / "p"


async def main():
    OUT.mkdir(parents=True, exist_ok=True)
    cache_f = DATA / "image_cache.json"
    cache = json.loads(cache_f.read_text(encoding="utf-8")) if cache_f.exists() else {}
    img = json.loads((DATA / "images.json").read_text(encoding="utf-8"))
    urls = [u for imgs in img.get("projects", {}).values() for u in imgs[:2]]
    todo = [u for u in dict.fromkeys(urls) if not (cache.get(u) and (ROOT / "app" / cache[u].lstrip("/")).exists())]
    print(f"{len(todo)} images to fetch")
    async with async_playwright() as p:
        ctx = await p.chromium.launch_persistent_context(
            str(PROFILE), channel="chrome", headless=False, args=["--disable-blink-features=AutomationControlled"])
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        try:  # warm the session; a human clears any bot check in the window
            await page.goto("https://darglobal.co.uk/projects", wait_until="commit", timeout=90000)
            await page.wait_for_timeout(3000)
            await wait_for_human(page, "darglobal")
        except Exception as e:
            print("warm-up:", e)
        for u in todo:
            try:
                r = await page.goto(u, wait_until="load", timeout=45000)
                if not r or r.status != 200:
                    raise RuntimeError(f"HTTP {r.status if r else '?'}")
                im = Image.open(io.BytesIO(await r.body())).convert("RGB")
                if im.width > 1400:
                    im = im.resize((1400, round(im.height * 1400 / im.width)), Image.LANCZOS)
                name = hashlib.sha1(u.encode()).hexdigest()[:12] + ".webp"
                im.save(OUT / name, "WEBP", quality=78, method=6)
                cache[u] = f"/static/img/p/{name}"
                print(f"{(OUT / name).stat().st_size // 1024:4d} KB  {u}", flush=True)
            except Exception as e:
                print("skip", u, e, flush=True)
            await page.wait_for_timeout(700)  # polite
        await ctx.close()
    cache_f.write_text(json.dumps(cache, indent=1), encoding="utf-8")


if __name__ == "__main__":
    asyncio.run(main())
