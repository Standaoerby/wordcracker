"""Performance benchmark for v2 — runs the 40-example suite N times and
aggregates per-intent latency (p50/p95), per-tool runtime, cache hit
rate, and critic verification rate.

Use this after sprint cycles to spot regressions or to compare runs
with/without warm caches.

Run on the host:
    python3 ~/wordcracker/tests/v2/bench_v2.py --runs 2 --out bench_$(date +%s).md
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
import urllib.error
import urllib.request
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from tests.v2.run_functional_40 import QUESTIONS_40

BASE = "http://127.0.0.1:8890"


def ask(q: str) -> dict:
    req = urllib.request.Request(
        f"{BASE}/api/chat",
        data=json.dumps({"question": q}).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    t0 = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=180) as r:
            d = json.load(r)
    except (urllib.error.URLError, json.JSONDecodeError) as e:
        return {"_ok": False, "_error": str(e),
                "_wall": time.perf_counter() - t0}
    d["_ok"] = True
    d["_wall"] = time.perf_counter() - t0
    return d


def run_bench(runs: int) -> tuple[list[dict], dict]:
    per_intent_latency: dict[str, list[float]] = defaultdict(list)
    per_tool_runtime: dict[str, list[int]] = defaultdict(list)
    critic_clean = 0
    critic_flagged = 0
    cache_hits = 0
    cache_total = 0
    rows = []
    for run_i in range(runs):
        for i, q in enumerate(QUESTIONS_40, 1):
            d = ask(q)
            if not d.get("_ok"):
                rows.append({"run": run_i, "qid": i,
                             "intent": "ERROR", "wall": d["_wall"]})
                continue
            intent = d.get("intent", "?")
            per_intent_latency[intent].append(d["_wall"])
            for tc in d.get("tool_calls") or []:
                if "runtime_ms" in tc:
                    per_tool_runtime[tc["name"]].append(tc["runtime_ms"])
                cache_total += 1
                if tc.get("cache_hit"):
                    cache_hits += 1
            critic = d.get("critic") or {}
            if critic:
                if critic.get("issues_flagged"):
                    critic_flagged += 1
                else:
                    critic_clean += 1
            rows.append({"run": run_i, "qid": i, "intent": intent,
                         "wall": d["_wall"]})
            print(f"  R{run_i+1} Q{i:02d} {intent:20s} {d['_wall']:5.1f}s")
    summary = {
        "runs": runs,
        "questions": len(QUESTIONS_40),
        "total_calls": runs * len(QUESTIONS_40),
        "cache_hit_rate": (cache_hits / cache_total) if cache_total else 0,
        "cache_hits": cache_hits,
        "cache_calls": cache_total,
        "critic_clean": critic_clean,
        "critic_flagged": critic_flagged,
    }
    summary["intent_latency"] = {}
    for intent, vals in per_intent_latency.items():
        vals_sorted = sorted(vals)
        summary["intent_latency"][intent] = {
            "n": len(vals),
            "p50": vals_sorted[len(vals_sorted) // 2],
            "p95": vals_sorted[max(0, int(len(vals_sorted) * 0.95) - 1)],
            "max": vals_sorted[-1],
            "mean": statistics.mean(vals),
        }
    summary["tool_runtime"] = {}
    for tool, vals in per_tool_runtime.items():
        vals_sorted = sorted(vals)
        summary["tool_runtime"][tool] = {
            "n": len(vals),
            "p50_ms": vals_sorted[len(vals_sorted) // 2],
            "p95_ms": vals_sorted[max(0, int(len(vals_sorted) * 0.95) - 1)],
            "mean_ms": int(statistics.mean(vals)),
        }
    return rows, summary


def emit_report(rows, summary, out_path: Path):
    md = [f"# v2 performance benchmark — {datetime.now().isoformat(timespec='seconds')}",
          "",
          f"- runs: {summary['runs']}",
          f"- questions per run: {summary['questions']}",
          f"- total calls: {summary['total_calls']}",
          f"- cache hit rate: {summary['cache_hit_rate']:.0%} ({summary['cache_hits']}/{summary['cache_calls']})",
          f"- critic: {summary['critic_clean']} clean / {summary['critic_flagged']} flagged",
          "",
          "## Per-intent wall-clock latency",
          "",
          "| Intent | n | p50 (s) | p95 (s) | max (s) | mean (s) |",
          "|---|---:|---:|---:|---:|---:|"]
    for intent in sorted(summary["intent_latency"]):
        v = summary["intent_latency"][intent]
        md.append(f"| {intent} | {v['n']} | {v['p50']:.2f} | {v['p95']:.2f}"
                  f" | {v['max']:.2f} | {v['mean']:.2f} |")
    md.append("")
    md.append("## Per-tool internal runtime")
    md.append("")
    md.append("| Tool | n | p50 (ms) | p95 (ms) | mean (ms) |")
    md.append("|---|---:|---:|---:|---:|")
    for tool in sorted(summary["tool_runtime"]):
        v = summary["tool_runtime"][tool]
        md.append(f"| {tool} | {v['n']} | {v['p50_ms']} | {v['p95_ms']}"
                  f" | {v['mean_ms']} |")
    out_path.write_text("\n".join(md), encoding="utf-8")
    print(f"\nReport: {out_path}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", type=int, default=1,
                    help="number of full 40-q passes")
    ap.add_argument("--out", default=None)
    args = ap.parse_args()
    rows, summary = run_bench(args.runs)
    out = (Path(args.out) if args.out
           else Path(f"bench_v2_{datetime.now():%Y%m%d_%H%M%S}.md"))
    emit_report(rows, summary, out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
