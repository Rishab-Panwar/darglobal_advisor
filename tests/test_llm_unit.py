"""Model chain: order, daily-quota detection and cooldowns (offline, no network)."""
import asyncio
import sys
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from app import llm  # noqa: E402

DAILY = ('{"error":{"message":"Rate limit exceeded: free-models-per-day. Add 10 credits to unlock 1000 free model '
         'requests per day","code":429}}')


def _client(status, body, headers=None):
    return httpx.AsyncClient(transport=httpx.MockTransport(lambda req: httpx.Response(status, text=body, headers=headers or {})))


def _setup():
    llm._cooldown.clear()
    llm.OPENROUTER_KEY, llm.VERTEX_PROJECT = "test-key", "test-project"
    llm.ORDER = ["vertex", "openrouter", "anthropic"]


def test_gemini_is_first():
    _setup()
    c = llm.candidates()
    assert c[0][0] == "vertex" and c[len(llm.VERTEX_MODELS)][0] == "openrouter", c


def test_daily_quota_parks_every_openrouter_model_until_reset():
    _setup()
    reset_ms = int((time.time() + 3 * 3600) * 1000)
    async def run():
        async with _client(429, DAILY, {"x-ratelimit-reset": str(reset_ms)}) as c:
            try:
                await llm.complete(c, ("openrouter", llm.OPENROUTER_MODELS[0]), "s", [{"role": "user", "content": "x"}])
            except llm.DailyQuota as e:
                return e
    err = asyncio.run(run())
    assert isinstance(err, llm.DailyQuota) and abs(err.until - reset_ms / 1000) < 1
    llm.mark_failed(("openrouter", llm.OPENROUTER_MODELS[0]), err)
    left = llm.candidates()
    assert all(p != "openrouter" for p, _ in left), left            # no free model retried before the reset
    assert left and left[0][0] == "vertex"


def test_daily_quota_detected_while_streaming_and_without_reset_header():
    _setup()
    async def run():
        async with _client(429, DAILY) as c:
            try:
                async for _ in llm.stream(c, ("openrouter", llm.OPENROUTER_MODELS[1]), "s", [{"role": "user", "content": "x"}]):
                    pass
            except llm.DailyQuota as e:
                return e
    err = asyncio.run(run())
    assert isinstance(err, llm.DailyQuota)
    assert err.until == llm._next_midnight_utc()                     # falls back to the next midnight UTC


def test_burst_limit_only_pauses_one_model_briefly():
    _setup()
    async def run():
        async with _client(429, '{"error":{"message":"Rate limit exceeded: too many requests per minute"}}') as c:
            try:
                await llm.complete(c, ("openrouter", llm.OPENROUTER_MODELS[0]), "s", [{"role": "user", "content": "x"}])
            except llm.RateLimited as e:
                return e
    err = asyncio.run(run())
    assert type(err) is llm.RateLimited
    llm.mark_failed(("openrouter", llm.OPENROUTER_MODELS[0]), err)
    left = [m for p, m in llm.candidates() if p == "openrouter"]
    assert llm.OPENROUTER_MODELS[0] not in left and len(left) == len(llm.OPENROUTER_MODELS) - 1
    assert llm._cooldown[("openrouter", llm.OPENROUTER_MODELS[0])] - time.time() <= llm.COOLDOWN_S + 1


def test_ready_skips_parked_but_never_leaves_nothing_to_try():
    _setup()
    llm.mark_failed(("openrouter", llm.OPENROUTER_MODELS[0]), llm.DailyQuota("openrouter", time.time() + 3600))
    assert not llm.ready(("openrouter", llm.OPENROUTER_MODELS[1]))      # sibling parked at once, same request
    assert llm.ready(("vertex", llm.VERTEX_MODELS[0]))
    for c in llm._all():                                                # everything paused -> best effort
        llm._cooldown[c] = time.time() + 3600
    assert llm.ready(("vertex", llm.VERTEX_MODELS[0]))


if __name__ == "__main__":
    fns = [v for k, v in dict(globals()).items() if k.startswith("test_")]
    for f in fns:
        f()
    print(f"{len(fns)} model-chain test groups passed")
