"""
Provider-agnostic LLM layer.

A *candidate* is (provider, model). Candidates are tried in order; a candidate is skipped
when it errors (429/5xx/network), refuses, or returns empty content. A candidate that was
rate-limited (HTTP 429) is put on a short cooldown so later requests don't waste a round-trip.

Providers (order set by LLM_ORDER, default "vertex,openrouter,anthropic": Gemini first for quality and reliability,
the free OpenRouter models as the backup):
  openrouter - free models via OpenRouter (needs OPENROUTER_API_KEY)
  vertex     - Gemini on Google Vertex AI (needs VERTEX_PROJECT; keyless: on Cloud Run the
               service account's token comes from the metadata server, locally from gcloud)
  anthropic  - Claude via the Anthropic SDK (needs ANTHROPIC_API_KEY; dormant otherwise)
"""
import asyncio
import json
import logging
import os
import shutil
import subprocess
import time
from typing import AsyncIterator

import anthropic
import httpx

log = logging.getLogger("assistant.llm")

# ---- OpenRouter -------------------------------------------------------------------------
OPENROUTER_KEY = os.getenv("OPENROUTER_API_KEY", "")
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
OPENROUTER_MODELS = [m.strip() for m in os.getenv(
    "OPENROUTER_MODELS",
    # chosen by benchmark: fast + reliable JSON with reasoning disabled
    "nvidia/nemotron-3.5-super-vl:free,nvidia/nemotron-3-ultra-550b-a55b:free,particle-ai/particle-1:free",
).split(",") if m.strip()]
NO_REASONING = {"enabled": False}  # free reasoning models otherwise burn the token budget "thinking"

# ---- Vertex AI (Gemini) -----------------------------------------------------------------
VERTEX_PROJECT = os.getenv("VERTEX_PROJECT", "")
VERTEX_LOCATION = os.getenv("VERTEX_LOCATION", "global")
VERTEX_MODELS = [m.strip() for m in os.getenv(
    "VERTEX_MODELS", "gemini-3-flash-preview,gemini-3.1-flash-lite").split(",") if m.strip()]
VERTEX_THINKING = os.getenv("VERTEX_THINKING", "minimal")
# optional: a different model order for the planning call (e.g. a stronger planner with a faster writer)
VERTEX_PLANNER_MODELS = [m.strip() for m in os.getenv("VERTEX_PLANNER_MODELS", "").split(",") if m.strip()] or VERTEX_MODELS  # grounded Q&A needs little reasoning
_METADATA_TOKEN = "http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/token"
_token: dict = {"value": "", "exp": 0.0}

# ---- Anthropic (Claude) -----------------------------------------------------------------
ANTHROPIC_KEY = os.getenv("ANTHROPIC_API_KEY", "")
CLAUDE_MODEL = os.getenv("CLAUDE_MODEL", "claude-opus-5-5")
CLAUDE_EFFORT = os.getenv("CLAUDE_EFFORT", "low")  # chat-speed answers; thinking stays adaptive
_claude = anthropic.AsyncAnthropic(api_key=ANTHROPIC_KEY, max_retries=1, timeout=60) if ANTHROPIC_KEY else None
CLAUDE_EXTRA = {"betas": ["server-side-fallback-2026-07-01"], "fallbacks": "default"}  # refusal safety net

ORDER = [p.strip() for p in os.getenv("LLM_ORDER", "vertex,openrouter,anthropic").split(",") if p.strip()]
COOLDOWN_S = int(os.getenv("RATE_LIMIT_COOLDOWN_S", "120"))
_cooldown: dict[tuple[str, str], float] = {}


class RateLimited(RuntimeError):
    pass


class DailyQuota(RateLimited):
    """OpenRouter's free tier is capped per account per day ("free-models-per-day"). Every free model shares that cap,
    so the whole provider is skipped until the reset time instead of being retried every two minutes."""
    def __init__(self, provider: str, until: float):
        super().__init__(f"{provider} daily quota used up until {time.strftime('%H:%M UTC', time.gmtime(until))}")
        self.provider, self.until = provider, until


def _next_midnight_utc(now: float | None = None) -> float:
    now = time.time() if now is None else now
    return (int(now) // 86400 + 1) * 86400


def _daily_quota(r: httpx.Response, body: str) -> DailyQuota | None:
    """Recognise OpenRouter's daily-cap 429 (as opposed to a short burst limit)."""
    text = body.lower()
    if "per-day" not in text and "per day" not in text and "daily" not in text:
        return None
    reset = r.headers.get("x-ratelimit-reset")
    try:
        until = float(reset) / (1000 if float(reset) > 1e11 else 1)   # OpenRouter sends epoch milliseconds
    except (TypeError, ValueError):
        until = 0
    if not time.time() < until < time.time() + 2 * 86400:
        until = _next_midnight_utc()
    return DailyQuota("openrouter", until)


def _all(role: str = "answer") -> list[tuple[str, str]]:
    out = []
    for p in ORDER:
        if p == "openrouter" and OPENROUTER_KEY:
            out += [("openrouter", m) for m in OPENROUTER_MODELS]
        elif p == "vertex" and VERTEX_PROJECT:
            out += [("vertex", m) for m in (VERTEX_PLANNER_MODELS if role == "plan" else VERTEX_MODELS)]
        elif p == "anthropic" and _claude:
            out.append(("anthropic", CLAUDE_MODEL))
    return out


def candidates(role: str = "answer") -> list[tuple[str, str]]:
    """Configured candidates in order, skipping any on rate-limit cooldown (unless all are)."""
    now = time.time()
    every = _all(role)
    ready = [c for c in every if _cooldown.get(c, 0) <= now]
    return ready or every


def ready(cand: tuple[str, str]) -> bool:
    """False while a candidate is paused, e.g. right after a sibling model reported the account-wide daily cap."""
    now = time.time()
    if _cooldown.get(cand, 0) <= now:
        return True
    return not any(_cooldown.get(c, 0) <= now for c in _all())   # everything paused: still make a best-effort attempt


def mark_failed(cand: tuple[str, str], err: Exception) -> None:
    if isinstance(err, DailyQuota):                       # one account-wide cap: park every model of that provider
        for c in _all():
            if c[0] == err.provider:
                _cooldown[c] = max(_cooldown.get(c, 0), err.until)
        log.info("%s", err)
    elif isinstance(err, RateLimited):
        _cooldown[cand] = time.time() + COOLDOWN_S
        log.info("cooldown %ss for %s", COOLDOWN_S, cand)


def describe() -> dict:
    now = time.time()
    return {"order": [f"{p}:{m}" for p, m in _all()],
            "cooling_down": [f"{p}:{m}" for (p, m), t in _cooldown.items() if t > now],
            "cooling_until": {f"{p}:{m}": time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(t))
                              for (p, m), t in _cooldown.items() if t > now},
            "openrouter_key": bool(OPENROUTER_KEY), "vertex": bool(VERTEX_PROJECT),
            "anthropic_key": bool(ANTHROPIC_KEY)}


def _check(r: httpx.Response, body: str = "") -> None:
    if r.status_code == 429:
        raise _daily_quota(r, body) or RateLimited("HTTP 429")
    if r.status_code == 401:
        _token["exp"] = 0  # expired/revoked token: fetch a fresh one on the next call
    if r.status_code != 200:
        raise RuntimeError(f"HTTP {r.status_code}")


# ---- helpers ----------------------------------------------------------------------------
def _or_headers():
    return {"Authorization": f"Bearer {OPENROUTER_KEY}",
            "HTTP-Referer": os.getenv("APP_URL", "https://property-assistant.run.app"),
            "X-Title": "DarGlobal Wasalt Assistant"}


async def _vertex_token(client: httpx.AsyncClient) -> str:
    """Service-account token from the metadata server (Cloud Run), else local gcloud credentials."""
    if _token["value"] and time.time() < _token["exp"] - 60:
        return _token["value"]
    try:
        r = await client.get(_METADATA_TOKEN, headers={"Metadata-Flavor": "Google"}, timeout=3)
        r.raise_for_status()
        j = r.json()
        _token.update(value=j["access_token"], exp=time.time() + j["expires_in"], src="metadata")
    except Exception:
        gcloud = shutil.which("gcloud") or shutil.which("gcloud.cmd")
        if not gcloud:
            raise RuntimeError("no Vertex credentials (not on GCP and gcloud not found)")
        out = await asyncio.to_thread(subprocess.run, [gcloud, "auth", "print-access-token"],
                                      capture_output=True, text=True, timeout=30)
        _token.update(value=out.stdout.strip(), exp=time.time() + 600, src="gcloud")  # gcloud may hand back a nearly-expired token
    return _token["value"]


def _vertex_url(model: str, stream: bool) -> str:
    host = "aiplatform.googleapis.com" if VERTEX_LOCATION == "global" else f"{VERTEX_LOCATION}-aiplatform.googleapis.com"
    verb = "streamGenerateContent?alt=sse" if stream else "generateContent"
    return (f"https://{host}/v1/projects/{VERTEX_PROJECT}/locations/{VERTEX_LOCATION}"
            f"/publishers/google/models/{model}:{verb}")


def _vertex_body(system: str, messages: list[dict], max_tokens: int, temperature: float) -> dict:
    return {"systemInstruction": {"parts": [{"text": system}]},
            "contents": [{"role": "model" if m["role"] == "assistant" else "user", "parts": [{"text": m["content"]}]}
                         for m in messages],
            "generationConfig": {"maxOutputTokens": max_tokens, "temperature": temperature,
                                 "thinkingConfig": {"thinkingLevel": VERTEX_THINKING}}}


def _vertex_text(ev: dict) -> str:
    cand = (ev.get("candidates") or [{}])[0]
    return "".join(p.get("text", "") for p in cand.get("content", {}).get("parts", []) if not p.get("thought"))


def _claude_text(msg) -> str:
    if msg.stop_reason == "refusal":
        return ""
    return "".join(b.text for b in msg.content if b.type == "text")


# ---- one-shot completion (planner) ------------------------------------------------
async def complete(client: httpx.AsyncClient, cand: tuple[str, str], system: str,
                   messages: list[dict], max_tokens: int = 350) -> str:
    provider, model = cand
    if provider == "openrouter":
        r = await client.post(OPENROUTER_URL, headers=_or_headers(), timeout=25, json={
            "model": model, "temperature": 0, "max_tokens": max_tokens, "reasoning": NO_REASONING,
            "messages": [{"role": "system", "content": system}] + messages})
        _check(r, r.text[:2000] if r.status_code != 200 else "")
        return r.json()["choices"][0]["message"].get("content") or ""
    if provider == "vertex":
        tok = await _vertex_token(client)
        r = await client.post(_vertex_url(model, False), headers={"Authorization": f"Bearer {tok}"}, timeout=25,
                              json=_vertex_body(system, messages, max_tokens * 3, 0))
        _check(r)
        return _vertex_text(r.json())
    # anthropic: thinking is adaptive (always on for Opus 5.5) and counts toward max_tokens
    msg = await _claude.beta.messages.create(
        model=model, max_tokens=4000, system=system, messages=messages,
        output_config={"effort": CLAUDE_EFFORT}, **CLAUDE_EXTRA)
    return _claude_text(msg)


# ---- streamed answer ------------------------------------------------------------------
async def stream(client: httpx.AsyncClient, cand: tuple[str, str], system: str,
                 messages: list[dict]) -> AsyncIterator[str]:
    provider, model = cand
    if provider == "openrouter":
        payload = {"model": model, "messages": [{"role": "system", "content": system}] + messages,
                   "stream": True, "temperature": 0.2, "max_tokens": 1000, "reasoning": NO_REASONING}
        async with client.stream("POST", OPENROUTER_URL, json=payload, headers=_or_headers()) as r:
            if r.status_code != 200:     # read the error body so a daily-cap 429 can be told apart from a burst limit
                _check(r, (await r.aread()).decode("utf-8", "ignore")[:2000])
            async for line in r.aiter_lines():
                if not line.startswith("data: ") or line == "data: [DONE]":
                    continue
                try:
                    ev = json.loads(line[6:])
                except json.JSONDecodeError:
                    continue
                if ev.get("error"):
                    raise RuntimeError(str(ev["error"])[:200])
                delta = (ev.get("choices") or [{}])[0].get("delta", {}).get("content")
                if delta:
                    yield delta
        return
    if provider == "vertex":
        tok = await _vertex_token(client)
        async with client.stream("POST", _vertex_url(model, True), headers={"Authorization": f"Bearer {tok}"},
                                 json=_vertex_body(system, messages, 2000, 0.2)) as r:
            _check(r)
            async for line in r.aiter_lines():
                if not line.startswith("data: "):
                    continue
                try:
                    ev = json.loads(line[6:])
                except json.JSONDecodeError:
                    continue
                if ev.get("error"):
                    raise RuntimeError(str(ev["error"])[:200])
                text = _vertex_text(ev)
                if text:
                    yield text
        return
    async with _claude.beta.messages.stream(
            model=model, max_tokens=8000, system=system, messages=messages,
            output_config={"effort": CLAUDE_EFFORT}, **CLAUDE_EXTRA) as s:
        async for text in s.text_stream:
            yield text
        final = await s.get_final_message()
        if final.stop_reason == "refusal":
            raise RuntimeError("refusal")
