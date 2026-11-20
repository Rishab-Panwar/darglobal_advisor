"""
Voice input end to end in real Chrome, using a WAV file as a fake microphone.

  python tests/ui_voice.py [base_url]      (default http://127.0.0.1:7870)

Covers: mic visible + labelled, recording state, auto-stop on silence, transcription, auto-send,
answer arrives, per-language hints (Arabic UI), Arabic spoken with an English UI, mic permission denied,
browser without microphone support (button hidden), Escape cancels.
"""
import asyncio
import sys
from pathlib import Path

from playwright.async_api import async_playwright

ROOT = Path(__file__).resolve().parent.parent
BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:7870"
sys.stdout.reconfigure(encoding="utf-8")
results = []


def check(name, cond, info=""):
    results.append(bool(cond))
    print(("PASS " if cond else "FAIL ") + name + ("" if cond else f"  -> {info}"), flush=True)


def chrome_args(wav=None, grant=True):
    args = ["--use-fake-device-for-media-stream"]
    if grant:
        args.append("--use-fake-ui-for-media-stream")
    if wav:
        args.append(f"--use-file-for-fake-audio-capture={wav}%noloop")
    return args


async def speak(p, ui_lang, clip, expect_words, label):
    b = await p.chromium.launch(channel="chrome", headless=True, args=chrome_args(ROOT / "tests" / "audio" / clip))
    pg = await b.new_page(viewport={"width": 1280, "height": 900})
    errors = []
    pg.on("pageerror", lambda e: errors.append(str(e)))
    await pg.goto(f"{BASE}/?lang={ui_lang}")
    await pg.wait_for_timeout(1200)
    mic = pg.locator("#mic")
    check(f"{label}: mic button visible", await mic.is_visible())
    check(f"{label}: mic has localized label", bool(await mic.get_attribute("aria-label")))
    await mic.click()
    await pg.wait_for_timeout(500)
    check(f"{label}: recording state shown", "rec" in (await mic.get_attribute("class") or ""))
    # auto-stop on silence -> transcribe -> auto-send: a user message appears without pressing anything
    await pg.wait_for_selector(".msg.user", timeout=40000)
    said = await pg.locator(".msg.user .bubble").first.inner_text()
    check(f"{label}: transcript auto-sent", all(w in said for w in expect_words), said)
    await pg.wait_for_selector(".msg.bot .foot", timeout=90000)
    ans = await pg.locator(".msg.bot .answer").first.inner_text()
    check(f"{label}: answer arrives", len(ans) > 40, ans[:120])
    check(f"{label}: mic idle again", "rec" not in (await mic.get_attribute("class") or "")
          and not await mic.is_disabled())
    check(f"{label}: no page errors", not errors, errors[:2])
    print(f"      heard: {said}")
    await b.close()


async def main():
    async with async_playwright() as p:
        await speak(p, "en", "en.wav", ["villa", "Riyadh"], "en")
        await speak(p, "ar", "ar.wav", ["دار جلوبال"], "ar UI")
        await speak(p, "en", "ar.wav", ["دار جلوبال"], "arabic speech, en UI")
        await speak(p, "fr", "fr.wav", ["Dar Global", "Espagne"], "fr")

        # permission denied -> friendly localized message, no crash
        b = await p.chromium.launch(channel="chrome", headless=True, args=chrome_args(grant=False))
        ctx = await b.new_context()
        pg = await ctx.new_page()
        await pg.add_init_script("navigator.mediaDevices.getUserMedia = () => Promise.reject(new DOMException('denied', 'NotAllowedError'))")
        await pg.goto(f"{BASE}/?lang=ar")
        await pg.wait_for_timeout(800)
        await pg.click("#mic")
        await pg.wait_for_timeout(500)
        msg = await pg.locator("#mic-msg").inner_text()
        check("denied: localized error shown", "الميكروفون" in msg, msg)
        check("denied: not stuck recording", "rec" not in (await pg.locator("#mic").get_attribute("class") or ""))
        await b.close()

        # browser without microphone APIs -> button hidden, typing still works
        b = await p.chromium.launch(channel="chrome", headless=True)
        pg = await b.new_page()
        await pg.add_init_script("Object.defineProperty(navigator, 'mediaDevices', {value: undefined})")
        await pg.goto(f"{BASE}/?lang=en")
        await pg.wait_for_timeout(800)
        check("unsupported: mic hidden", not await pg.locator("#mic").is_visible())
        await b.close()

        # Escape cancels a recording without sending anything
        b = await p.chromium.launch(channel="chrome", headless=True, args=chrome_args(ROOT / "tests" / "audio" / "en.wav"))
        pg = await b.new_page()
        await pg.goto(f"{BASE}/?lang=en")
        await pg.wait_for_timeout(800)
        await pg.click("#mic")
        await pg.wait_for_timeout(700)
        await pg.keyboard.press("Escape")
        await pg.wait_for_timeout(1500)
        check("escape: cancelled, nothing sent", await pg.locator(".msg.user").count() == 0
              and "rec" not in (await pg.locator("#mic").get_attribute("class") or ""))
        await b.close()
    print(f"\n{sum(results)}/{len(results)} checks passed")
    import datetime, json as _json
    _out = Path(__file__).resolve().parent / "results"
    _out.mkdir(exist_ok=True)
    (_out / "ui_voice.json").write_text(_json.dumps({"suite": "ui_voice", "passed": sum(1 for r in results if (r[1] if isinstance(r, tuple) else r)),
        "total": len(results), "target": BASE, "date": datetime.date.today().isoformat()}), encoding="utf-8")
    sys.exit(0 if all(results) else 1)


asyncio.run(main())
