#!/usr/bin/env python3
"""Supplementary latency recheck for run-20261004-rerun (dq#145 evidence).

Purpose: replicate the *shape* of the operator's 2026-10-04 manual direct
probes (10 sequential successful direct API calls per Clef model) at rerun
time, as cross-check evidence for the full-battery latency measurement.

Method: strictly sequential (no concurrency), one fixed payload per model,
same endpoints/credentials as the committed harness. Writes its result JSON
next to this script. Not part of the scored battery; supplementary only.
"""
from __future__ import annotations

import json
import statistics
import sys
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent.parent / "harness"))

import clef_client  # noqa: E402

STATE = "The nightly backup completed with 2 warnings; no data loss reported."
QUESTIONS = {"concern": {"type": "noul", "instructions": "Does this need attention today?"}}
N_PER_MODEL = 10
MODELS = ("clef", "clef-flash")


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def main() -> int:
    out = {
        "method": ("10 sequential direct calls per model, one fixed payload; "
                   "replicates the shape of the operator's 2026-10-04 manual probes; "
                   "no concurrency; supplementary cross-check only"),
        "started_utc": utc_now(),
        "calls": [],
    }
    per = {}
    for model in MODELS:
        print(f"=== {model}: {N_PER_MODEL} sequential calls ===", flush=True)
        for i in range(1, N_PER_MODEL + 1):
            res = clef_client.call(model, STATE, QUESTIONS)
            rec = {
                "ts": utc_now(),
                "model": model,
                "n": i,
                "ok": res.get("ok"),
                "http_status": res.get("http_status"),
                "latency_ms": res.get("latency_ms"),
                "usage_input_tokens": res.get("usage_input_tokens"),
                "cost_computed_usd": res.get("cost_computed_usd"),
                "error": (str(res.get("error"))[:200] if res.get("error") else None),
            }
            out["calls"].append(rec)
            print(f"[{i:2}/{N_PER_MODEL}] {model} ok={rec['ok']} http={rec['http_status']} "
                  f"lat={rec['latency_ms']}ms", flush=True)
        ok_lats = [c["latency_ms"] for c in out["calls"]
                   if c["model"] == model and c["ok"] and isinstance(c["latency_ms"], (int, float))]
        per[model] = {
            "n_calls": N_PER_MODEL,
            "n_ok": sum(1 for c in out["calls"] if c["model"] == model and c["ok"]),
            "latencies_ms": ok_lats,
            "median_ms": statistics.median(ok_lats) if ok_lats else None,
            "min_ms": min(ok_lats) if ok_lats else None,
            "max_ms": max(ok_lats) if ok_lats else None,
        }
    out["ended_utc"] = utc_now()
    out["per_model"] = per
    dest = HERE / "supplementary-latency-recheck.json"
    dest.write_text(json.dumps(out, indent=2) + "\n")
    print("SUMMARY:", json.dumps({m: {k: per[m][k] for k in ("n_ok", "median_ms", "min_ms", "max_ms")}
                                  for m in per}))
    print("wrote", dest)
    return 0


if __name__ == "__main__":
    sys.exit(main())
