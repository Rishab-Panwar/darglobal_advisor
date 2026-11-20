"""
Speech-to-text for the mic button.

Primary:  Google Cloud Speech-to-Text v2, model **chirp_3**, EU multi-region endpoint.
          - language hint = the UI language + Arabic (Chirp accepts at most two), so a Saudi user speaking
            Arabic with the UI in English/French/... is still transcribed correctly
          - phrase boosting with our own vocabulary (Dar Global, Wasalt, project and city names, incl. Arabic,
            Chinese and Russian spellings): without it French heard "Dar Global" as "d'art global"
Fallback: Gemini on Vertex AI transcribes the same audio if Chirp errors or returns nothing.

Keyless: the Cloud Run service account's token (roles/speech.client, roles/aiplatform.user).
Audio is sent to Google Cloud under our project, transcribed and discarded; nothing is stored.
"""
import base64
import logging
import os
import re

import httpx

from . import llm

log = logging.getLogger("assistant.stt")

LOCATION = os.getenv("STT_LOCATION", "eu")
MODEL = os.getenv("STT_MODEL", "chirp_3")
MAX_BYTES = 2_500_000  # ~75 s of 16 kHz mono 16-bit WAV; the browser stops at 30 s anyway
CODES = {"en": "en-US", "ar": "ar-SA", "fr": "fr-FR", "ru": "ru-RU", "zh": "cmn-Hans-CN"}
LANG_NAMES = {"en": "English", "ar": "Arabic", "fr": "French", "ru": "Russian", "zh": "Simplified Chinese"}
LOCAL_CITIES = {
    "ar": ["الرياض", "جدة", "الدمام", "مكة", "المدينة المنورة", "الخبر", "عمان", "دبي", "دار جلوبال", "وصلت", "دار الأركان"],
    "zh": ["利雅得", "吉达", "达曼", "麦加", "麦地那", "迪拜", "阿曼", "马斯喀特"],
    "ru": ["Эр-Рияд", "Джидда", "Даммам", "Мекка", "Медина", "Дубай", "Оман", "Маскат"],
    "fr": ["Riyad", "Djeddah", "Dubaï", "Oman", "Mascate"],
}


def language_codes(lang: str) -> list[str]:
    return ["ar-SA", "en-US"] if lang == "ar" else [CODES.get(lang, "en-US"), "ar-SA"]


def phrases(tools, lang: str) -> list[str]:
    """A short, curated boost list. Measured: boosting all ~70 project names diluted the boost and French
    went back to hearing "d'art global"; 12-20 phrases keep brand names right. Project names don't need
    boosting: the planner fuzzy-matches them anyway."""
    # boosting is sensitive to the exact mix of phrases (measured: "brands only" fails, this list works), so
    # fix_brands() below is the safety net that makes the result deterministic
    out = ["Dar Global", "Wasalt", "Dar Al Arkan"] + LOCAL_CITIES.get(lang, []) + ["Riyadh", "Jeddah", "Dubai", "Oman"]
    return list(dict.fromkeys(out))


# post-ASR normalisation: known mishearings of our brand names -> canonical spelling (any engine, any language)
BRAND_FIXES = [
    (re.compile(r"\b(?:d['’]\s?art|dare|dhar|darr?)[\s-]*glob(?:al|ale|el)\b", re.I), "Dar Global"),
    (re.compile(r"\bdarglobal\b", re.I), "Dar Global"),
    (re.compile(r"\b(?:was+[\s-]?salt|wa[\s-]salt|wassalt|wasselt|wasalt)\b", re.I), "Wasalt"),
    (re.compile(r"\bdar[\s-]?al[\s-]?ark[ae]n\b", re.I), "Dar Al Arkan"),
    (re.compile(r"دا\s?جلوبال|دار\s?[غق]لوبال|دارجلوبال"), "دار جلوبال"),
    # mixed script when Arabic is spoken with an English UI ("دار Global", "Dar جلوبال")
    (re.compile(r"دار\s*Global\b|\bDar\s*جلوبال", re.I), "دار جلوبال"),
    (re.compile(r"وصل\s*t\b|\bWasal\s*ت"), "وصلت"),
    (re.compile(r"\bДар\s?Глоб[ае]л\w*", re.I), "Dar Global"),
    (re.compile(r"达尔\s?环球|达尔\s?全球"), "Dar Global"),
]


def fix_brands(text: str) -> str:
    for pat, good in BRAND_FIXES:
        text = pat.sub(good, text)
    return text


def is_silent(wav: bytes, threshold: float = 0.005) -> bool:
    """True when a 16-bit PCM WAV has no speech-level energy. Checked before any model call: given silence,
    the Gemini fallback once 'transcribed' an invented sentence about Dar Global."""
    if len(wav) < 1000 or wav[:4] != b"RIFF":
        return False                       # not a plain WAV: let the engines decide
    pcm = memoryview(wav[44:]).cast("h") if (len(wav) - 44) % 2 == 0 else memoryview(wav[44:-1]).cast("h")
    if not len(pcm):
        return True
    step = max(1, len(pcm) // 16000)       # sample ~16k points
    peak_rms = 0.0
    win = 1600                             # 0.1 s windows at 16 kHz
    for start in range(0, len(pcm), win * step):
        chunk = pcm[start:start + win * step:step]
        if len(chunk):
            rms = (sum(x * x for x in chunk) / len(chunk)) ** 0.5 / 32768
            peak_rms = max(peak_rms, rms)
    return peak_rms < threshold


async def _chirp(client: httpx.AsyncClient, audio: bytes, lang: str, vocab: list[str]) -> str:
    tok = await llm._vertex_token(client)
    host = "speech.googleapis.com" if LOCATION == "global" else f"{LOCATION}-speech.googleapis.com"
    r = await client.post(
        f"https://{host}/v2/projects/{llm.VERTEX_PROJECT}/locations/{LOCATION}/recognizers/_:recognize",
        # local user credentials need a quota project; the Cloud Run service account doesn't (and may lack
        # serviceusage.services.use), so the header is only sent for gcloud-issued tokens
        headers={"Authorization": f"Bearer {tok}",
                 **({"x-goog-user-project": llm.VERTEX_PROJECT} if llm._token.get("src") == "gcloud" else {})},
        timeout=30,
        json={"config": {"model": MODEL, "languageCodes": language_codes(lang), "autoDecodingConfig": {},
                         "adaptation": {"phraseSets": [{"inlinePhraseSet": {
                             "phrases": [{"value": p, "boost": 10} for p in vocab]}}]}},
              "content": base64.b64encode(audio).decode()})
    if r.status_code == 401:
        llm._token["exp"] = 0
    if r.status_code != 200:
        raise RuntimeError(f"chirp HTTP {r.status_code}: {r.text[:160]}")
    return " ".join(a["alternatives"][0]["transcript"].strip()
                    for a in r.json().get("results", []) if a.get("alternatives")).strip()


async def _gemini(client: httpx.AsyncClient, audio: bytes, lang: str, mime: str) -> str:
    tok = await llm._vertex_token(client)
    model = llm.VERTEX_MODELS[0]
    r = await client.post(llm._vertex_url(model, False), headers={"Authorization": f"Bearer {tok}"}, timeout=30, json={
        "contents": [{"role": "user", "parts": [
            {"inlineData": {"mimeType": mime, "data": base64.b64encode(audio).decode()}},
            {"text": f"Transcribe the speech in this audio verbatim. It is most likely {LANG_NAMES.get(lang, 'English')} "
                     "or Arabic. If you hear the names, spell them 'Dar Global' and 'Wasalt'. Output only the words "
                     "actually spoken. Never add, complete or invent words. If there is no clear speech, output nothing."}]}],
        "generationConfig": {"temperature": 0, "maxOutputTokens": 400, "thinkingConfig": {"thinkingLevel": "minimal"}}})
    if r.status_code != 200:
        raise RuntimeError(f"gemini HTTP {r.status_code}")
    return llm._vertex_text(r.json()).strip()


async def transcribe(client: httpx.AsyncClient, audio: bytes, lang: str, tools, mime: str = "audio/wav") -> tuple[str, str]:
    """Returns (text, engine). Raises only if every engine fails."""
    if not llm.VERTEX_PROJECT:
        raise RuntimeError("speech-to-text needs VERTEX_PROJECT")
    if is_silent(audio):
        return "", "silence"
    errors = []
    if os.getenv("STT_FORCE_FALLBACK") != "1":
        try:
            # an empty result from Chirp means "no speech" and is final: falling back to an LLM on silence
            # invites a hallucinated transcript. Gemini is only used when Chirp itself fails.
            return fix_brands(await _chirp(client, audio, lang, phrases(tools, lang))), f"google-stt:{MODEL}"
        except Exception as e:
            errors.append(str(e))
            log.warning("chirp failed, falling back to Gemini: %s", e)
    try:
        return fix_brands(await _gemini(client, audio, lang, mime)), f"vertex:{llm.VERTEX_MODELS[0]}"
    except Exception as e:
        errors.append(str(e))
        raise RuntimeError("; ".join(errors))
