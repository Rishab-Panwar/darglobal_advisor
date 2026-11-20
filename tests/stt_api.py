"""
Speech-to-text tests against a running server + the Gemini fallback in-process.

  python tests/stt_api.py [base_url]      (default http://127.0.0.1:7870)

Clips in tests/audio/ were generated with Google Cloud Text-to-Speech (one per UI language).
"""
import asyncio
import os
import sys
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8")
BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:7870"
AUDIO = ROOT / "tests" / "audio"

# spoken text -> words that must survive transcription (brand and city names are the hard part)
EXPECT = {
    "en": ["villa", "Riyadh"],                       # "Show me four bedroom villas for sale in Riyadh under 3M riyals"
    "ar": ["دار جلوبال", "عمان"],                     # "What are Dar Global's projects in Oman?"
    "fr": ["Dar Global", "Espagne"],                  # "Which Dar Global projects in Spain?"
    "ru": ["Dar Global", "Дуба"],                     # "Which Dar Global projects are in Dubai?"
    "zh": ["利雅得", "公寓"],                          # "Which apartments are for sale in Riyadh?"
}
results = []


def check(name, cond, info=""):
    results.append(bool(cond))
    print(("PASS " if cond else "FAIL ") + name + ("" if cond else f"  -> {info}"), flush=True)


def post(c, lang, data, ctype="audio/wav"):
    return c.post(f"{BASE}/api/stt", params={"lang": lang}, content=data, headers={"Content-Type": ctype}, timeout=60)


def main():
    with httpx.Client() as c:
        for lang, words in EXPECT.items():
            r = post(c, lang, (AUDIO / f"{lang}.wav").read_bytes())
            d = r.json() if r.status_code == 200 else {}
            text = d.get("text", "")
            check(f"{lang}: transcribed with brand/city names", r.status_code == 200 and all(w in text for w in words),
                  (r.status_code, text))
            check(f"{lang}: engine is Chirp 3", d.get("engine") == "google-stt:chirp_3", d.get("engine"))
            print(f"      {d.get('latency_s')}s  {text}")
        # Arabic spoken while the UI is in English / French
        for ui in ("en", "fr"):
            text = post(c, ui, (AUDIO / "ar.wav").read_bytes()).json().get("text", "")
            check(f"arabic speech with {ui} UI", "دار جلوبال" in text, text)
        # edge cases
        check("empty body -> 400", post(c, "en", b"").status_code == 400)
        check("oversize -> 413", post(c, "en", b"\0" * 2_600_000).status_code == 413)
        check("unknown lang falls back", post(c, "xx", (AUDIO / "en.wav").read_bytes()).status_code == 200)
        silence = (AUDIO / "en.wav").read_bytes()[:44] + b"\0" * 64000      # valid WAV header, 2 s of silence
        r = post(c, "en", silence)
        check("silence -> empty text, no model call", r.status_code == 200 and not r.json().get("text", "").strip() and r.json().get("engine") == "silence",
              (r.status_code, r.text[:120]))

    # Gemini fallback path (in-process, Chirp disabled)
    os.environ["STT_FORCE_FALLBACK"] = "1"
    from app import main as app_main, stt

    async def fb():
        async with httpx.AsyncClient(timeout=60) as c:
            return await stt.transcribe(c, (AUDIO / "fr.wav").read_bytes(), "fr", app_main.tools)
    text, engine = asyncio.run(fb())
    check("fallback: Gemini transcribes when Chirp is unavailable", engine.startswith("vertex:") and "Espagne" in text,
          (engine, text))

    async def fb_silence():
        quiet = (AUDIO / "en.wav").read_bytes()[:44] + bytes(64000)
        async with httpx.AsyncClient(timeout=60) as c:
            return await stt.transcribe(c, quiet, "en", app_main.tools)
    text, engine = asyncio.run(fb_silence())
    check("fallback: silence never reaches Gemini (no invented transcript)", text == "" and engine == "silence",
          (engine, text))
    print(f"\n{sum(results)}/{len(results)} checks passed")
    sys.exit(0 if all(results) else 1)


if __name__ == "__main__":
    main()
