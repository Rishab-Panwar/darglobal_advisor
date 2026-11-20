"""
Evaluation harness.

  python eval/run_eval.py              # retrieval only (offline, no API key): hit@k + MRR per retriever
  python eval/run_eval.py --e2e        # + end-to-end: planner tool choice & answer keyword checks (needs OPENROUTER_API_KEY)

questions.json items:
  {"q": "...", "urls": ["substring of a relevant URL", ...],      # retrieval ground truth (any match = hit)
   "tool": "search_pages|filter_listings|get_project",              # expected tool for e2e
   "expect": ["keyword", ...]}                                      # answer must mention (case-insensitive, any-of groups via '|')
"""
import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.rag import Index  # noqa: E402

QUESTIONS = json.loads((ROOT / "eval" / "questions.json").read_text(encoding="utf-8"))


def retrieval_eval(index: Index, ks=(1, 3, 6)):
    rows = [q for q in QUESTIONS if q.get("urls")]
    results = {}
    for mode in index.modes():
        hits = {k: 0 for k in ks}
        rr = 0.0
        for q in rows:
            got = [c.url for c in index.search(q["q"], k=max(ks), mode=mode)]
            rank = next((i + 1 for i, u in enumerate(got) if any(s in u for s in q["urls"])), None)
            for k in ks:
                hits[k] += bool(rank and rank <= k)
            rr += 1 / rank if rank else 0
        results[mode] = {**{f"hit@{k}": round(hits[k] / len(rows), 3) for k in ks}, "MRR": round(rr / len(rows), 3)}
    return len(rows), results


async def e2e_eval():
    import httpx
    from app import main

    tool_ok = ans_ok = safe_ok = n_safe = 0
    lat = []
    async with httpx.AsyncClient(timeout=120) as client:
        for q in QUESTIONS:
            t0 = time.time()
            history = [{"role": "user", "content": q["q"]}]
            blocked = main.guard(history, "ar" if main.reply_language(history) == "Arabic" else "en")  # same path as /api/chat
            if blocked:
                tools_used, answer, served = {"guardrail"}, blocked, "guardrail"
            else:
                plan, ok, planner = await main.plan_step(client, history)
                context, _, trace = main.act_step(plan)
                tools_used = {s["tool"] for s in trace}
                answer, served = "", ""
                system = main.answer_system(context, history)
                for cand in main.llm.candidates("answer"):
                    try:
                        answer = "".join([d async for d in main.llm.stream(client, cand, system, history)])
                    except Exception:
                        answer = ""
                    if answer:
                        served = f"{cand[0]}:{cand[1].split('/')[-1]}"
                        break
            t_ok = q.get("tool") in tools_used if q.get("tool") else True
            low = answer.lower()
            a_ok = all(any(alt.lower() in low for alt in group.split("|")) for group in q.get("expect", []))
            leaked = [f for f in q.get("forbid", []) if f.lower() in low]
            a_ok = a_ok and not leaked
            tool_ok += t_ok
            ans_ok += a_ok
            if q.get("safety"):
                n_safe += 1
                safe_ok += t_ok and a_ok
            lat.append(time.time() - t0)
            tag = " [safety]" if q.get("safety") else ""
            print(f"{'✓' if t_ok else '✗'}tool {'✓' if a_ok else '✗'}answer  {lat[-1]:5.1f}s  {q['q'][:60]:60s} "
                  f"{sorted(tools_used)} [{served}]{tag}" + (f"  FORBIDDEN: {leaked}" if leaked else ""))
            if not a_ok:
                print("      answer:", answer[:240].replace("\n", " "))
            await asyncio.sleep(0 if blocked else 2)  # stay under free-tier rate limits
    n = len(QUESTIONS)
    lat.sort()
    return {"tool_selection_acc": round(tool_ok / n, 3), "answer_keyword_acc": round(ans_ok / n, 3),
            "safety_pass": f"{safe_ok}/{n_safe}", "p50_latency_s": round(lat[n // 2], 1), "n": n}


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--e2e", action="store_true")
    a = ap.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")
    n, res = retrieval_eval(Index(ROOT / "data"))
    print(f"\nRetrieval ({n} questions with labelled sources)")
    for mode, m in res.items():
        print(f"  {mode:8s} " + "  ".join(f"{k}={v}" for k, v in m.items()))
    if a.e2e:
        print("\nEnd-to-end")
        print(asyncio.run(e2e_eval()))
