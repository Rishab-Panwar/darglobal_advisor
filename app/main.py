import json
import logging
import os
import re
import time
from collections import OrderedDict, defaultdict, deque
from pathlib import Path

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

BASE = Path(__file__).resolve().parent
_env = BASE.parent / ".env"  # local dev convenience; in production keys come from Secret Manager
if _env.exists():
    for _line in _env.read_text(encoding="utf-8").splitlines():
        if "=" in _line and not _line.lstrip().startswith("#"):
            _k, _v = _line.split("=", 1)
            if _v.strip():
                os.environ.setdefault(_k.strip(), _v.strip().strip('"'))
DATA_DIR = Path(os.getenv("DATA_DIR", BASE.parent / "data"))

from . import guardrails, llm, stt  # noqa: E402  (reads provider keys from the env loaded above)
from .agent import answer_system, parse_plan, planner_prompt, reply_language  # noqa: E402
from .rag import Index  # noqa: E402
from .tools import INVEST_DEFAULTS, Tools  # noqa: E402

# Abuse protection for a public URL (not a provider quota): stops a script from burning the GCP credit or the
# free OpenRouter quota. Chat and voice have separate buckets, so a spoken question doesn't count twice.
RATE_LIMIT = int(os.getenv("RATE_LIMIT_PER_MIN", "20"))

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("assistant")

index = Index(DATA_DIR)
tools = Tools(DATA_DIR)
app = FastAPI(title="DarGlobal & Wasalt Assistant")
app.mount("/static", StaticFiles(directory=BASE / "static"), name="static")
_hits: dict[str, deque] = defaultdict(deque)

# Answer cache for first-turn questions (e.g. the suggested chips): saves quota and latency.
_cache: "OrderedDict[str, tuple[float, list[str]]]" = OrderedDict()
CACHE_TTL, CACHE_MAX = 6 * 3600, 300


def _cache_key(history, lang):
    if len(history) != 1:
        return None
    return lang + ":" + " ".join(re.findall(r"\w+", history[0]["content"].lower()))


class Msg(BaseModel):
    role: str
    content: str = Field(max_length=4000)


class ChatReq(BaseModel):
    messages: list[Msg] = Field(min_length=1, max_length=30)
    lang: str = Field("en", pattern="^(en|ar|fr|ru|zh)$")


def _rate_limited(ip: str, bucket: str = "chat") -> bool:
    q, now = _hits[f"{bucket}:{ip}"], time.time()
    while q and now - q[0] > 60:
        q.popleft()
    q.append(now)
    return len(q) > RATE_LIMIT


def sse(obj) -> str:
    return f"data: {json.dumps(obj, ensure_ascii=False)}\n\n"


NO_CACHE = {"Cache-Control": "no-cache, must-revalidate"}   # reviewers always get the latest page


@app.get("/")
def home():
    return FileResponse(BASE / "static" / "index.html", headers=NO_CACHE)


@app.get("/api/health")
def health():
    return {"ok": True, "pages": index.pages, "chunks": len(index.chunks),
            "listings": len(tools.listings), "listings_excluded": len(tools.excluded),
            "projects": len(tools.projects), "llm": llm.describe(),
            "stt": {"model": stt.MODEL, "location": stt.LOCATION, "fallback": f"vertex:{llm.VERTEX_MODELS[0]}"}}


@app.get("/how-it-works")
def how_it_works():
    return FileResponse(BASE / "static" / "how.html", headers=NO_CACHE)


@app.get("/api/catalog")
def catalog():
    """Options for the comparison builder: every project (with photo) and every market with listings."""
    projects = [{"name": p["name"], "source": p["source"], "location": p.get("location") or "",
                 "brand": p.get("brand_partner") or p.get("developer") or "",
                 "image": (p.get("images") or [None])[0]} for p in tools.projects]
    markets = []
    for c in tools.cities:
        for t in ("Apartment", "Villa", "Floor", "Building"):
            n = sum(1 for l in tools.listings if l["city"] == c and l["type"] == t)
            if n >= 3:
                markets.append({"city": c, "type": t, "n": n})
    return {"projects": sorted(projects, key=lambda p: (p["source"] != "darglobal", p["name"])), "markets": markets}


class CompareReq(BaseModel):
    kind: str = Field(pattern="^(projects|markets)$")
    items: list[str | dict] = Field(min_length=2, max_length=4)


@app.post("/api/compare")
def compare_direct(req: CompareReq, request: Request):
    """The comparison builder calls the tool directly: deterministic, instant, no LLM quota."""
    ip = request.headers.get("x-forwarded-for", request.client.host if request.client else "?").split(",")[0]
    if _rate_limited(ip, "compare"):
        raise HTTPException(429, "Too many requests - please wait a minute.")
    if req.kind == "projects":
        items = [i for i in req.items if isinstance(i, str) and 0 < len(i) <= 120]
    else:
        items = [{"city": str(i.get("city", ""))[:40], "type": str(i.get("type", ""))[:20]} for i in req.items if isinstance(i, dict)]
    if len(items) < 2:
        raise HTTPException(400, "Choose at least two items to compare.")
    res = tools.compare(req.kind, items)
    return {**res, "kind": "compare", "mode": res["kind"]}


@app.post("/api/stt")
async def speech_to_text(request: Request, lang: str = "en"):
    """Mic button: raw audio body (16 kHz mono WAV from the browser) -> transcript. Nothing is stored."""
    ip = request.headers.get("x-forwarded-for", request.client.host if request.client else "?").split(",")[0]
    if _rate_limited(ip, "voice"):
        raise HTTPException(429, "Too many requests - please wait a minute.")
    if lang not in stt.CODES:
        lang = "en"
    audio = await request.body()
    if len(audio) > stt.MAX_BYTES:
        raise HTTPException(413, "Recording too long.")
    if len(audio) < 2000:
        raise HTTPException(400, "No audio received.")
    mime = request.headers.get("content-type", "audio/wav").split(";")[0]
    t0 = time.time()
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(40, connect=10)) as client:
            text, engine = await stt.transcribe(client, audio, lang, tools, mime)
    except Exception as e:
        log.warning("stt failed: %s", e)
        raise HTTPException(502, "Transcription failed.")
    log.info("stt %s %.1fs %d bytes engine=%s", lang, time.time() - t0, len(audio), engine)
    return {"text": text, "engine": engine, "latency_s": round(time.time() - t0, 1)}


@app.get("/api/invest")
def invest_meta():
    """Market yield table + default assumptions for the interactive calculator."""
    return {"yields": tools.yield_table(), "defaults": INVEST_DEFAULTS}


@app.get("/api/facts")
def facts():
    """Data-derived one-liners for the loading screen, plus hero photos and headline stats."""
    dg = [p for p in tools.projects if p["source"] == "darglobal"]
    picked = DATA_DIR / "heroes.json"   # hand-picked full-resolution photos (scraper/hero_images.py)
    if picked.exists():
        heroes = [{"src": f"/static/img/hero/{h['file']}", "name": h["name"], "location": h.get("location", "")}
                  for h in json.loads(picked.read_text(encoding="utf-8"))]
    else:
        heroes = [{"src": p["images"][0], "name": p["name"], "location": p.get("location", "")}
                  for p in dg if p.get("images")][:8]
    return {"facts": tools.facts(), "heroes": heroes,
            "stats": {"projects": len(dg), "countries": len({p["country"] for p in dg}),
                      "listings": len(tools.listings), "pages": sum(index.pages.values())}}


def _strip(obj):
    """Drop image URLs from tool output before it goes into the LLM context (saves tokens)."""
    if isinstance(obj, dict):
        return {k: _strip(v) for k, v in obj.items() if k not in ("images", "image", "listing_url")}
    if isinstance(obj, list):
        return [_strip(v) for v in obj]
    return obj


def listing_card(l):
    return {"kind": "listing", "title": l["title"], "type": l["type"], "purpose": l["purpose"],
            "price_sar": l["price_sar"], "price_period": l["price_period"], "bedrooms": l.get("bedrooms"),
            "bathrooms": l.get("bathrooms"), "area_sqm": l.get("area_sqm"), "location": l["location"],
            "image": l.get("image"), "url": l.get("listing_url") or l["url"]}


def project_card(p):
    return {"kind": "project", "name": p["name"], "source": p["source"], "location": p.get("location"),
            "brand": p.get("brand_partner") or p.get("developer"), "status": p.get("status"),
            "starts_from": p.get("starts_from") or p.get("min_unit_price_sar"),
            "image": (p.get("images") or [None])[0], "url": p["url"]}


def guard(history: list[dict], lang: str) -> str | None:
    """Code-level guardrails, run before any model call. Redacts contact details in place;
    returns a fixed refusal when the latest message is an injection / prompt-extraction attempt."""
    for m in history:
        if m["role"] == "user":
            m["content"], n = guardrails.redact(m["content"])
            if n:
                log.info("redacted %d contact detail(s) before the model call", n)
    if guardrails.is_injection(history[-1]["content"]):
        return guardrails.refusal(lang)
    return None


# ---- 1. PLAN ---------------------------------------------------------------------------
async def plan_step(client: httpx.AsyncClient, history: list[dict], lang: str = "en") -> tuple[dict, bool, tuple | None]:
    """Ask candidates in order for a JSON plan; first valid plan wins. Falls back to plain retrieval."""
    convo = "\n".join(f"{m['role'].upper()}: {m['content'][:600]}" for m in history[-6:])
    fallback_q = " ".join(m["content"] for m in history if m["role"] == "user")[-400:]
    user = [{"role": "user", "content": f"Conversation:\n{convo}\n\nReturn the JSON plan."}]
    for cand in llm.candidates("plan")[:4]:
        if not llm.ready(cand):
            continue
        try:
            text = await llm.complete(client, cand, planner_prompt(tools.catalog(), reply_language(history, lang)), user,
                                      max_tokens=450)
            plan, ok = parse_plan(text, fallback_q)
            if ok:
                return plan, True, cand
            log.warning("planner %s -> unparseable plan", cand)
        except Exception as e:  # rate limit / network / provider error -> next candidate
            llm.mark_failed(cand, e)
            log.warning("planner %s failed: %s", cand, e)
    plan, _ = parse_plan("", fallback_q)
    return plan, False, None


# ---- 2. ACT ----------------------------------------------------------------------------
def act_step(plan: dict) -> tuple[str, list, list]:
    """Run tools deterministically; return (context_text, sources, trace)."""
    blocks, sources, trace = [], [], []
    if plan.get("search_query"):
        hits = index.search(plan["search_query"], k=6)
        for c in hits:
            n = len(sources) + 1
            sources.append({"n": n, "site": c.site, "url": c.url, "title": c.title})
            blocks.append(f"[{n}] ({c.site}) {c.url}\n{c.text}")
        trace.append({"tool": "search_pages", "args": {"query": plan["search_query"]},
                      "result": f"{len(hits)} passages"})
    if plan.get("listing_filter"):
        res = tools.filter_listings(**plan["listing_filter"])
        plan.setdefault("_cards", []).extend(listing_card(l) for l in res["listings"][:8])
        slim = [{k: l[k] for k in ("type", "title", "price_sar", "price_period", "bedrooms", "area_sqm", "location")}
                for l in res["listings"]]
        blocks.append("WASALT LISTINGS (structured):\n" + json.dumps(
            {"stats": res["stats"], "listings": slim, "note": res["note"]}, ensure_ascii=False))
        trace.append({"tool": "filter_listings", "args": plan["listing_filter"],
                      "result": f"{res['stats']['matches']} matches"})
        for u in sorted({l["url"] for l in res["listings"]})[:3]:
            sources.append({"n": len(sources) + 1, "site": "wasalt", "url": u, "title": "Wasalt listings"})
    if plan.get("project_filter"):
        res = tools.list_projects(**plan["project_filter"])
        blocks.append("PROJECT LIST:\n" + json.dumps(_strip(res), ensure_ascii=False)[:5000])
        by_url = {p["url"]: p for p in tools.projects}
        plan.setdefault("_cards", []).extend(project_card(by_url[p["url"]]) for p in res["projects"][:8]
                                             if p["url"] in by_url)
        trace.append({"tool": "list_projects", "args": plan["project_filter"], "result": f"{res['count']} projects"})
        for u in sorted({p["url"] for p in res["projects"]})[:4]:
            sources.append({"n": len(sources) + 1, "site": "darglobal" if "darglobal" in u else "wasalt",
                            "url": u, "title": "Project portfolio"})
    if plan.get("compare"):
        res = tools.compare(**plan["compare"])
        blocks.insert(0, "COMPARISON (computed in code):\n" + json.dumps(
            _strip({**res, "columns": [{k: v for k, v in c.items() if k != "image"} for c in res["columns"]]}),
            ensure_ascii=False)[:6000])
        trace.append({"tool": "compare", "args": plan["compare"],
                      "result": f"{len(res['columns'])} {res['kind']}" + (f", not found: {res['unresolved']}" if res["unresolved"] else "")})
        if len(res["columns"]) >= 2:      # one match is not a comparison: the answer explains what wasn't found
            plan.setdefault("_cards", []).insert(0, {**res, "kind": "compare", "mode": res["kind"]})
        for c in res["columns"]:
            if c.get("url") and len(sources) < 10:
                sources.append({"n": len(sources) + 1, "site": "darglobal" if "darglobal" in c["url"] else "wasalt",
                                "url": c["url"], "title": c.get("name") or f"{c.get('type')} in {c.get('city')}"})
    if plan.get("calc"):
        c = dict(plan["calc"])
        kind = c.pop("kind")
        if kind == "yield":
            res = tools.rental_yield(**c)
            result = f"{len(res['rows'])} market rows"
        else:
            res = tools.investment(**c)
            result = f"{res.get('gross_yield_pct', '-')}% gross, SAR {res.get('monthly_payment_sar', '-')}/mo payment"
        blocks.append("INVESTMENT CALCULATION (computed in code):\n" + json.dumps(res, ensure_ascii=False)[:4000])
        trace.append({"tool": "rental_yield" if kind == "yield" else "investment_calc", "args": plan["calc"],
                      "result": result})
        # a market comparison already shows the yields side by side; only add the widget for explicit money questions
        if not (plan.get("compare") and kind == "yield"):
            plan.setdefault("_cards", []).insert(0, {"kind": "calc", "mode": kind, "args": plan["calc"], "result": res})
    if plan.get("project"):
        res = tools.get_project(plan["project"])
        blocks.append("PROJECT LOOKUP:\n" + json.dumps(_strip(res), ensure_ascii=False)[:3500])
        best = res.get("best_match")
        trace.append({"tool": "get_project", "args": {"name": plan["project"]},
                      "result": best["name"] if best else "no match"})
        if best:
            cards = plan.setdefault("_cards", [])
            if not any(c.get("url") == best["url"] for c in cards):
                cards.insert(0, project_card(best))
            sources.append({"n": len(sources) + 1, "site": best["source"], "url": best["url"], "title": best["name"]})
    return ("\n\n".join(blocks) or "(no relevant results)"), sources, trace


# ---- 3. ANSWER (streamed) -------------------------------------------------------------
@app.post("/api/chat")
async def chat(req: ChatReq, request: Request):
    if not llm.candidates():
        raise HTTPException(500, "No LLM provider key is configured on the server.")
    ip = request.headers.get("x-forwarded-for", request.client.host if request.client else "?").split(",")[0]
    if _rate_limited(ip):
        raise HTTPException(429, "Too many requests - please wait a minute.")
    history = [{"role": m.role, "content": m.content} for m in req.messages if m.role in ("user", "assistant")][-8:]
    while history and history[0]["role"] != "user":  # providers require the first turn to be the user's
        history.pop(0)
    if not history:
        raise HTTPException(400, "The conversation must contain a user message.")

    lang = req.lang
    blocked = guard(history, lang)
    if blocked:
        log.warning("guardrail blocked q=%r", history[-1]["content"][:120])

        async def refuse():
            yield sse({"trace": {"plan_ok": True, "planner_model": "", "steps": [
                {"tool": "guardrail", "args": {"check": "prompt_injection"}, "result": "blocked before any model call"}]},
                "sources": [], "cards": [], "followups": []})
            yield sse({"model": "guardrail"})
            yield sse({"delta": blocked})
            yield sse({"done": True, "latency_s": 0.0})
            yield "data: [DONE]\n\n"
        return StreamingResponse(refuse(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"})
    key = _cache_key(history, lang)
    hit = _cache.get(key) if key else None
    if hit and time.time() - hit[0] < CACHE_TTL:
        async def replay():
            for ev in hit[1]:
                yield ev
            yield sse({"done": True, "latency_s": 0.0, "cached": True})
            yield "data: [DONE]\n\n"
        log.info("cache hit q=%r", key[:80])
        return StreamingResponse(replay(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"})

    async def stream():
        events = []  # recorded for the cache
        t0 = time.time()
        async with httpx.AsyncClient(timeout=httpx.Timeout(90, connect=15)) as client:
            yield sse({"phase": "plan"})
            plan, ok, planner = await plan_step(client, history, lang)
            context, sources, trace = act_step(plan)
            planner_name = f"{planner[0]}:{planner[1]}" if planner else ""
            ev_trace = sse({"trace": {"plan_ok": ok, "planner_model": planner_name, "steps": trace},
                            "sources": sources, "cards": plan.get("_cards", [])[:8],
                            "followups": plan.get("followups", [])})
            events.append(ev_trace)
            yield ev_trace
            log.info("q=%r plan_ok=%s planner=%s steps=%s", history[-1]["content"][:120], ok, planner_name,
                     [s["tool"] for s in trace])

            yield sse({"phase": "write"})
            system = answer_system(context, history, lang)
            # Try candidates in order (the planner's first); skip any that error, refuse or stay empty.
            # the answer has its own model order (it may differ from the planner's: see VERTEX_PLANNER_MODELS)
            order = llm.candidates("answer")
            answered = False
            for cand in order:
                if not llm.ready(cand):
                    continue
                got = 0
                try:
                    async for delta in llm.stream(client, cand, system, history):
                        out = ([sse({"model": f"{cand[0]}:{cand[1]}"})] if got == 0 else []) + [sse({"delta": delta})]
                        got += 1
                        for o in out:
                            events.append(o)
                            yield o
                except Exception as e:  # provider error, rate limit, refusal
                    llm.mark_failed(cand, e)
                    log.warning("answer %s failed after %d chunks: %s", cand, got, e)
                    if got:  # failed mid-stream: tell the client to discard the partial answer
                        yield sse({"reset": True})
                        got = 0
                if got:
                    answered = True
                    break
                del events[1:]  # keep only the trace event
            if not answered:
                yield sse({"error": "All models are busy right now - please retry in a few seconds."})
            elif key:
                _cache[key] = (time.time(), events)
                while len(_cache) > CACHE_MAX:
                    _cache.popitem(last=False)
        yield sse({"done": True, "latency_s": round(time.time() - t0, 1)})
        yield "data: [DONE]\n\n"

    return StreamingResponse(stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
