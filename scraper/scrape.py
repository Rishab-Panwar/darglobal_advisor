"""
Polite crawler for publicly available pages on darglobal.co.uk and wasalt.sa.

Both sites sit behind bot protection (Imperva/hCaptcha and Cloudflare), so this
drives a real, visible Chrome window with a persistent profile. If a human
verification appears, solve it yourself in the window; the crawler waits and
then continues in the same verified session. It never solves CAPTCHAs itself.

Usage:
    python scraper/scrape.py darglobal --max 80
    python scraper/scrape.py wasalt --max 80
Output: data/<site>.jsonl  (one JSON object per page: url, title, description, text)
"""
import argparse
import asyncio
import json
import random
import re
from pathlib import Path
from urllib.parse import urldefrag, urlparse

from playwright.async_api import async_playwright

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
PROFILE = ROOT / "scraper" / ".chrome-profile"

SITES = {
    "darglobal": {
        "seeds": ["https://www.darglobal.co.uk/"],
        "allow": lambda u: urlparse(u).netloc.endswith("darglobal.co.uk"),
    },
    "wasalt": {
        "seeds": [
            "https://wasalt.sa/en",
            "https://wasalt.sa/en/corporate",
            "https://wasalt.sa/en/sale/search?countryId=1&propertyFor=sale&type=residential&project=true",
            "https://wasalt.sa/en/properties-for-sale-in-riyadh",
            "https://wasalt.sa/en/properties-for-sale-in-jeddah",
            "https://wasalt.sa/en/properties-for-rent-in-riyadh",
            "https://wasalt.sa/en/properties-for-rent-in-jeddah",
            "https://wasalt.sa/en/villas-for-sale-in-riyadh",
            "https://wasalt.sa/en/apartments-for-sale-in-riyadh",
            "https://wasalt.sa/en/apartments-for-rent-in-riyadh",
            "https://wasalt.sa/en/lands-for-sale-in-riyadh",
            "https://wasalt.sa/llms.txt",
        ],
        # English public pages only; respect robots.txt (/search is disallowed)
        "allow": lambda u: urlparse(u).netloc == "wasalt.sa"
        and urlparse(u).path.startswith("/en")
        and "/search" not in urlparse(u).path,
    },
}

SKIP_EXT = re.compile(r"\.(pdf|jpe?g|png|gif|svg|webp|mp4|zip|docx?|xlsx?)$", re.I)
CHALLENGE = re.compile(r"Just a moment|Performing security verification|Additional security check|Incapsula", re.I)


def norm(u: str) -> str:
    u = urldefrag(u)[0].replace("://www.", "://")
    return u.rstrip("/") if urlparse(u).path not in ("", "/") else u


async def wait_for_human(page, label: str):
    """If a bot challenge is showing, wait (up to 5 min) for the user to solve it."""
    for i in range(60):
        body = ""
        try:
            body = await page.inner_text("body")
        except Exception:
            pass
        # judge by visible text only: Imperva injects an "_Incapsula_Resource" script into every page
        if not CHALLENGE.search(body[:2000]) and len(body.strip()) > 200:
            return True
        if i == 0:
            print(f"[{label}] Verification page detected - please complete it in the Chrome window...", flush=True)
        await page.wait_for_timeout(5000)
    return False


async def crawl(site: str, max_pages: int):
    cfg = SITES[site]
    DATA.mkdir(exist_ok=True)
    out = DATA / f"{site}.jsonl"
    seen, queue, records = set(), [norm(s) for s in cfg["seeds"]], []
    # resume: keep pages already saved by a previous (interrupted) run
    # (pages saved without their outgoing links are re-fetched so the frontier can be rebuilt)
    if out.exists():
        for line in out.open(encoding="utf-8"):
            r = json.loads(line)
            u = norm(r["url"])
            if "links" not in r or u in seen:
                continue
            records.append(r)
            seen.add(u)
            queue.extend(l for l in r["links"] if l not in seen)
        print(f"Resuming with {len(records)} pages already saved, {len(queue)} queued")
    with out.open("w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    sink = out.open("a", encoding="utf-8")

    async with async_playwright() as p:
        ctx = await p.chromium.launch_persistent_context(
            str(PROFILE), channel="chrome", headless=False, locale="en-GB",
            viewport={"width": 1366, "height": 900},
            args=["--disable-blink-features=AutomationControlled"],
        )
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()

        while queue and len(records) < max_pages:
            if page.is_closed():
                page = await ctx.new_page()
            url = queue.pop(0)
            if url in seen:
                continue
            seen.add(url)
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=60000)
                await page.wait_for_timeout(2500)
                if not await wait_for_human(page, site):
                    print("  gave up waiting for verification:", url)
                    continue
                # scroll to trigger lazy-loaded content
                for _ in range(4):
                    await page.mouse.wheel(0, 2500)
                    await page.wait_for_timeout(600)

                data = await page.evaluate(
                    """() => {
                        // innerText on the live (rendered) body keeps line breaks and skips
                        // scripts/styles; header/footer chrome is dropped to cut noise.
                        const hidden = [...document.querySelectorAll('header,footer,nav')];
                        hidden.forEach(e => e.dataset.prevDisplay = e.style.display);
                        hidden.forEach(e => e.style.display = 'none');
                        const text = document.body.innerText;
                        hidden.forEach(e => e.style.display = e.dataset.prevDisplay || '');
                        const md = document.querySelector('meta[name="description"]');
                        return {
                          title: document.title,
                          description: md ? md.content : '',
                          h1: [...document.querySelectorAll('h1')].map(e => e.innerText.trim()).join(' | '),
                          text: text,
                          links: [...new Set([...document.querySelectorAll('a[href]')].map(a => a.href))],
                        };
                    }"""
                )
                text = re.sub(r"\n\s*\n+", "\n", data["text"]).strip()
                final = norm(page.url)
                links = []
                for l in data["links"]:
                    l = norm(l)
                    if cfg["allow"](l) and not SKIP_EXT.search(urlparse(l).path):
                        links.append(l)
                        if l not in seen:
                            queue.append(l)
                if final != url and final in seen:  # redirect to a page we already have
                    continue
                seen.add(final)
                rec = {"site": site, "url": page.url, "title": data["title"],
                       "description": data["description"], "h1": data["h1"], "text": text,
                       "links": sorted(set(links))}
                records.append(rec)
                sink.write(json.dumps(rec, ensure_ascii=False) + "\n")
                sink.flush()
                print(f"[{len(records)}/{max_pages}] {len(text):6d} chars  {page.url}", flush=True)
            except Exception as e:
                print("  error:", url, e)

            await asyncio.sleep(random.uniform(1.5, 3.5))  # be polite

        await ctx.close()
    sink.close()
    print(f"Saved {len(records)} pages -> {out}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("site", choices=SITES)
    ap.add_argument("--max", type=int, default=80)
    a = ap.parse_args()
    asyncio.run(crawl(a.site, a.max))
