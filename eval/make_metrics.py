"""
Build app/static/metrics.json for the "How it works" page from real outputs (nothing typed by hand except the
browser-suite sizes, which are counted from the test files themselves).

  python eval/make_metrics.py
"""
import ast
import json
import re
import subprocess
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "app" / "static" / "metrics.json"


def eval_results():
    txt = (ROOT / "eval" / "results_vertex.txt").read_text(encoding="utf-8")
    retrieval = {}
    for mode in ("bm25", "dense", "hybrid"):
        m = re.search(rf"^\s*{mode}\s+(.*)$", txt, re.M)
        if m:
            retrieval[mode] = {k: float(v) for k, v in re.findall(r"([\w@]+)=([\d.]+)", m.group(1))}
    summary = ast.literal_eval(re.search(r"^\{.*\}$", txt, re.M).group(0))
    rows = [l for l in txt.splitlines() if re.match(r"^[✓✗]tool", l)]
    return retrieval, summary, len(rows)


def offline_tests():
    out = []
    for f in ("test_calculator", "test_compare", "test_guardrails", "test_stt_unit", "test_data_quality", "test_llm_unit"):
        r = subprocess.run([sys.executable, str(ROOT / "tests" / f"{f}.py")], capture_output=True, text=True,
                           encoding="utf-8", env={**__import__("os").environ, "PYTHONIOENCODING": "utf-8"})
        out.append({"name": f"tests/{f}.py", "passed": r.returncode == 0, "detail": (r.stdout.strip().splitlines() or [""])[-1]})
    return out


def browser_suites():
    """Real results saved by each browser suite at the end of its last run (tests/results/*.json)."""
    labels = {"ui_calculator": "Investment calculator", "ui_compare": "Comparison in chat",
              "ui_compare_builder": "Comparison builder", "ui_voice": "Voice (Chrome + fake microphone)"}
    out = []
    for f, label in labels.items():
        r = ROOT / "tests" / "results" / f"{f}.json"
        if r.exists():
            out.append({"name": f"tests/{f}.py", "label": label, **json.loads(r.read_text(encoding="utf-8"))})
    return out


def main():
    retrieval, e2e, n = eval_results()
    metrics = {"generated": date.today().isoformat(), "retrieval": retrieval, "e2e": {**e2e, "questions": n},
               "offline_tests": offline_tests(), "browser_suites": browser_suites()}
    OUT.write_text(json.dumps(metrics, indent=1, ensure_ascii=False), encoding="utf-8")
    print(json.dumps(metrics, indent=1, ensure_ascii=False))


if __name__ == "__main__":
    main()
