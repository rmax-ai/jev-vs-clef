#!/usr/bin/env python3
"""Analyse a gemini_eval run -> report.md + summary.json.

Reads ``results/<run>/calls.jsonl`` and recomputes every number from the raw
records. No network calls; deterministic.

    python3 harness/gemini_report.py results/run-20261112-gemini-eval
"""

from __future__ import annotations

import json
import math
import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from vision_demo import CASES as VISION_CASES  # noqa: E402

REPO = Path(__file__).resolve().parent.parent
BATTERY = json.loads((REPO / "cases" / "stress_battery.json").read_text())
CASES = {c["id"]: c for c in BATTERY["cases"]}
ARMS = ("gemini", "jev", "clef-flash")
VISION = {c["id"]: c for c in VISION_CASES}


def load_calls(run_dir: Path) -> list[dict]:
    rows = []
    with (run_dir / "calls.jsonl").open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def dedupe(rows: list[dict]) -> list[dict]:
    """Keep one row per planned task, latest wins.

    Supplementary reruns append to the same JSONL (an adapter fix, a case group
    re-run after a request-shape defect). Without this, a failed first attempt
    and its successful retry would be counted as two tasks.
    """
    keyed: dict[tuple, dict] = {}
    for r in rows:
        key = (r.get("phase"), r.get("case_id"), r.get("arm"), r.get("repeat_group"),
               r.get("batch_mode"), r.get("single_question"))
        keyed[key] = r
    return list(keyed.values())


def qtypes(case_id: str) -> dict[str, str]:
    if case_id in VISION:
        return {k: v["type"] for k, v in VISION[case_id]["questions"].items()}
    if case_id == "F":
        return {k: v["type"] for k, v in BATTERY["question_sets"]["batch_v1"].items()}
    case = CASES.get(case_id)
    if not case:
        return {}
    qs = case.get("questions") or BATTERY["question_sets"].get(case.get("questions_ref"), {})
    return {k: v["type"] for k, v in qs.items()}


def value(qtype: str, ans: dict | None) -> float | str | None:
    if not isinstance(ans, dict):
        return None
    t = "noul" if qtype in ("noul", "boolean") else qtype
    if t == "noul":
        v = ans.get("noul", ans.get("probability"))
        return float(v) if isinstance(v, (int, float)) else None
    if t == "choice":
        return ans.get("choice")
    if t == "score":
        v = ans.get("score")
        return float(v) if isinstance(v, (int, float)) else None
    return None


def pvector(qtype: str, ans: dict | None) -> dict:
    if not isinstance(ans, dict):
        return {}
    t = "noul" if qtype in ("noul", "boolean") else qtype
    if t == "noul":
        p = value("noul", ans)
        return {"true": p, "false": None if p is None else 1.0 - p}
    probs = ans.get("probabilities")
    return {str(k): float(v) for k, v in probs.items()} if isinstance(probs, dict) else {}


def match(qtype: str, a, b) -> bool | None:
    if a is None or b is None:
        return None
    t = "noul" if qtype in ("noul", "boolean") else qtype
    if t == "noul":
        return (a >= 0.5) == (b >= 0.5)
    if t == "choice":
        return a == b
    if t == "score":
        return abs(float(a) - float(b)) <= 0.5
    return None


def correct(qtype: str, answer, expected) -> bool | None:
    if answer is None or expected is None:
        return None
    t = "noul" if qtype in ("noul", "boolean") else qtype
    if t == "noul":
        return bool(answer >= 0.5) == bool(expected)
    if t == "choice":
        return answer == expected
    if t == "score":
        return int(round(float(answer))) == int(expected)
    return None


def pct(values: list[float], q: float) -> float | None:
    if not values:
        return None
    xs = sorted(values)
    idx = max(0, min(len(xs) - 1, math.ceil(q / 100.0 * len(xs)) - 1))
    return xs[idx]


def main() -> int:
    run_dir = Path(sys.argv[1] if len(sys.argv) > 1 else "results/run-20261112-gemini-eval")
    calls = dedupe(load_calls(run_dir))
    main_calls = [c for c in calls if c.get("phase") == "main"]

    summary: dict = {
        "run": run_dir.name,
        "requests_total": len(calls),
        "requests_main": len(main_calls),
        "arms": {},
        "agreement": {},
        "determinism": {},
        "batching": {},
        "format_sensitivity": {},
        "vision": {},
        "injection": {},
        "limits": [],
    }

    # --- per-arm health / latency / cost -----------------------------------
    for arm in ARMS:
        rows = [c for c in main_calls if c["arm"] == arm]
        ok = [c for c in rows if c.get("ok")]
        lat = [c["latency_ms"] for c in ok if isinstance(c.get("latency_ms"), (int, float))]
        cost = sum(c.get("cost_computed_usd") or 0.0 for c in ok)
        est = sum(c.get("cost_estimate_usd") or 0.0 for c in rows)
        usage = sum(c.get("usage_input_tokens") or 0 for c in ok)
        errors = Counter(
            f"http_{c.get('http_status')}" if c.get("http_status") else str(c.get("error"))[:60]
            for c in rows if not c.get("ok")
        )
        qcount = sum(c.get("n_questions") or 0 for c in ok)
        summary["arms"][arm] = {
            "requests": len(rows),
            "ok": len(ok),
            "errors": dict(errors),
            "latency_ms": {"p50": pct(lat, 50), "p95": pct(lat, 95), "min": min(lat) if lat else None,
                           "max": max(lat) if lat else None, "n": len(lat)},
            "input_tokens": usage,
            "cost_computed_usd": round(cost, 6),
            "cost_estimate_usd": round(est, 6),
            "cost_per_call_usd": round(cost / len(ok), 8) if ok else None,
            "cost_per_question_usd": round(cost / qcount, 8) if qcount else None,
        }

    # --- quality on labeled question-level decisions -----------------------
    labeled = defaultdict(lambda: {"n": 0, "correct": 0, "brier": [], "by_type": Counter(),
                                  "correct_by_type": Counter()})
    for c in main_calls:
        if not c.get("ok") or not c.get("expected") or c.get("repeat_group"):
            continue
        qt = qtypes(c["case_id"])
        arm = c["arm"]
        for qid, exp in c["expected"].items():
            if qid not in qt:
                continue
            ans = value(qt[qid], (c.get("answers") or {}).get(qid))
            verdict = correct(qt[qid], ans, exp)
            if verdict is None:
                continue
            labeled[arm]["n"] += 1
            labeled[arm]["by_type"][qt[qid]] += 1
            if verdict:
                labeled[arm]["correct"] += 1
                labeled[arm]["correct_by_type"][qt[qid]] += 1
            if qt[qid] in ("noul", "boolean") and isinstance(ans, (int, float)):
                labeled[arm]["brier"].append((float(ans) - (1.0 if exp else 0.0)) ** 2)
    for arm, d in labeled.items():
        summary["arms"][arm]["labeled"] = {
            "n": d["n"],
            "correct": d["correct"],
            "accuracy": round(d["correct"] / d["n"], 4) if d["n"] else None,
            "by_type": {k: f"{d['correct_by_type'][k]}/{v}" for k, v in d["by_type"].items()},
            "brier": round(sum(d["brier"]) / len(d["brier"]), 4) if d["brier"] else None,
        }

    # --- accuracy on the subset every arm actually measured ---------------
    # Adapters can drop a group for one arm (e.g. an initial request-shape defect).
    # Comparing pooled n across arms would then compare different case sets.
    by_case_arm = {}
    for c in main_calls:
        if c.get("ok") and c.get("expected") and not c.get("repeat_group"):
            by_case_arm[(c["case_id"], c["arm"])] = c
    labeled_cases = {cid for cid, _ in by_case_arm}
    common = sorted(cid for cid in labeled_cases
                    if all((cid, arm) in by_case_arm for arm in ARMS))
    common_quality = {}
    for arm in ARMS:
        n = correct_n = 0
        for cid in common:
            c = by_case_arm[(cid, arm)]
            qt = qtypes(cid)
            for qid, exp in c["expected"].items():
                if qid not in qt:
                    continue
                verdict = correct(qt[qid], value(qt[qid], (c.get("answers") or {}).get(qid)), exp)
                if verdict is None:
                    continue
                n += 1
                correct_n += int(verdict)
        common_quality[arm] = {"cases": len(common), "n": n, "correct": correct_n,
                               "accuracy": round(correct_n / n, 4) if n else None}
    summary["quality_common_subset"] = {"cases": common, "arms": common_quality}

    # --- agreement vs gemini ----------------------------------------------
    index = {(c["case_id"], c["arm"]): c for c in main_calls if c.get("batch_mode") in (None, "all_in_one")}
    for arm in ("jev", "clef-flash"):
        pairs = 0
        agree = 0
        case_all = 0
        case_all_agree = 0
        deltas = []
        for (case_id, a), base in index.items():
            if a != "gemini":
                continue
            other = index.get((case_id, arm))
            if not other or not base.get("ok") or not other.get("ok"):
                continue
            qt = qtypes(case_id)
            all_ok = True
            any_q = False
            for qid, t in qt.items():
                va = value(t, (base.get("answers") or {}).get(qid))
                vb = value(t, (other.get("answers") or {}).get(qid))
                m = match(t, va, vb)
                if m is None:
                    all_ok = False
                    continue
                any_q = True
                pairs += 1
                agree += int(m)
                all_ok &= m
                pa = pvector(t, (base.get("answers") or {}).get(qid))
                pb = pvector(t, (other.get("answers") or {}).get(qid))
                for k in set(pa) & set(pb):
                    if isinstance(pa[k], (int, float)) and isinstance(pb[k], (int, float)):
                        deltas.append(abs(pa[k] - pb[k]))
            if any_q:
                case_all += 1
                case_all_agree += int(all_ok)
        summary["agreement"][arm] = {
            "questions": pairs,
            "question_match_rate": round(agree / pairs, 4) if pairs else None,
            "cases": case_all,
            "all_question_match_rate": round(case_all_agree / case_all, 4) if case_all else None,
            "mean_abs_probability_delta": round(sum(deltas) / len(deltas), 4) if deltas else None,
        }

    # --- determinism -------------------------------------------------------
    groups = defaultdict(list)
    for c in main_calls:
        if c.get("repeat_group"):
            # runner records E:<ref>:<arm>:<rep> — group across repeats.
            key = c["repeat_group"]
            parts = key.split(":")
            if len(parts) == 4 and parts[0] == "E":
                key = ":".join(parts[:3])
            groups[key].append(c)
    det = {}
    for key, rows in sorted(groups.items()):
        arm = rows[0]["arm"]
        ok_rows = [r for r in rows if r.get("ok")]
        if len(ok_rows) < len(rows):
            det[key] = {"arm": arm, "repeats": len(rows), "ok": len(ok_rows),
                        "label_stable": None, "max_numeric_spread": None,
                        "note": "unmeasured — at least one repeat was rejected"}
            continue
        labels = []
        spreads = []
        for c in ok_rows:
            qt = qtypes(c["case_id"])
            labels.append(tuple(
                (qid, value(t, (c.get("answers") or {}).get(qid)))
                for qid, t in sorted(qt.items())
            ))
            for qid, t in qt.items():
                vals = [value(t, (r.get("answers") or {}).get(qid)) for r in rows]
                nums = [v for v in vals if isinstance(v, (int, float))]
                if len(nums) > 1:
                    spreads.append(max(nums) - min(nums))
        det[key] = {
            "arm": arm,
            "repeats": len(rows),
            "label_stable": len(set(labels)) == 1,
            "max_numeric_spread": round(max(spreads), 4) if spreads else None,
        }
    summary["determinism"] = det

    # --- batching ----------------------------------------------------------
    batched = {c["arm"]: c for c in main_calls if c.get("batch_mode") == "all_in_one" and c.get("ok")}
    single = defaultdict(dict)
    for c in main_calls:
        if c.get("batch_mode") == "single" and c.get("ok"):
            single[c["arm"]][c.get("single_question")] = c
    for arm, b in batched.items():
        qt = qtypes("F")
        rows = single.get(arm, {})
        agree = 0
        n = 0
        for qid, s in rows.items():
            va = value(qt[qid], (b.get("answers") or {}).get(qid))
            vb = value(qt[qid], (s.get("answers") or {}).get(qid))
            m = match(qt[qid], va, vb)
            if m is not None:
                n += 1
                agree += int(m)
        single_lat = [s["latency_ms"] for s in rows.values() if isinstance(s.get("latency_ms"), (int, float))]
        summary["batching"][arm] = {
            "batched_questions": b.get("n_questions"),
            "batched_latency_ms": round(b["latency_ms"]) if b.get("latency_ms") else None,
            "single_calls": n,
            "single_p50_latency_ms": (round(pct(single_lat, 50)) if pct(single_lat, 50) else None),
            "agreement_batched_vs_single": round(agree / n, 4) if n else None,
            "latency_ratio_batched_over_single": (
                round((b["latency_ms"] / pct(single_lat, 50)), 2)
                if b.get("latency_ms") and pct(single_lat, 50) else None
            ),
        }

    # --- format sensitivity (one semantic, three state formats) ------------
    fmt_groups = {"B1p": "prose", "B1f": "flat_json", "B1n": "nested_json"}
    fmt = {}
    for arm in ARMS:
        per_q = defaultdict(dict)
        for cid, label in fmt_groups.items():
            row = next((c for c in main_calls if c["case_id"] == cid and c["arm"] == arm and c.get("ok")), None)
            if not row:
                continue
            for qid, t in qtypes(cid).items():
                per_q[qid][label] = (value(t, (row.get("answers") or {}).get(qid)),
                                     pvector(t, (row.get("answers") or {}).get(qid)))
        entry = {}
        for qid, by_fmt in per_q.items():
            vals = [(lbl, v[0]) for lbl, v in by_fmt.items() if v[0] is not None]
            spreads = []
            for a in range(len(vals)):
                for b in range(a + 1, len(vals)):
                    if isinstance(vals[a][1], (int, float)) and isinstance(vals[b][1], (int, float)):
                        spreads.append(abs(vals[a][1] - vals[b][1]))
            labels = [v[1] for v in vals]
            complete = len(by_fmt) == len(fmt_groups)
            entry[qid] = {"values": {k: v[0] for k, v in by_fmt.items()},
                          "max_numeric_spread": round(max(spreads), 4) if spreads else None,
                          "label_stable": (len(set(map(str, labels))) == 1) if complete else None,
                          "measured_formats": sorted(by_fmt)}
        fmt[arm] = entry
    summary["format_sensitivity"] = fmt

    # --- vision ------------------------------------------------------------
    for arm in ("gemini", "clef-flash"):
        rows = [c for c in main_calls if c["arm"] == arm and c["case_id"] in VISION and c.get("ok")]
        checks = 0
        passed = 0
        detail = {}
        for c in rows:
            case = VISION[c["case_id"]]
            per = {}
            for qid, expected in case["check"].items():
                a = value(case["questions"][qid]["type"], (c.get("answers") or {}).get(qid))
                if a is None:
                    okq = None
                elif isinstance(expected, list):
                    okq = expected[0] <= float(a) <= expected[1]
                elif isinstance(expected, bool):
                    okq = bool(a >= 0.5) == expected
                else:
                    okq = a == expected
                per[qid] = {"expected": expected, "answer": a, "pass": okq}
                if okq is not None:
                    checks += 1
                    passed += int(okq)
            detail[c["case_id"]] = per
        summary["vision"][arm] = {"cases": len(rows), "checks": checks, "passed": passed,
                                  "detail": detail}

    # --- prompt-injection holds -------------------------------------------
    for cid in ("C06", "C07"):
        case = CASES[cid]
        for arm in ARMS:
            row = next((c for c in main_calls if c["case_id"] == cid and c["arm"] == arm and c.get("ok")), None)
            if not row:
                summary["injection"][f"{cid}:{arm}"] = {"held": None}
                continue
            held = True
            observed = {}
            for qid, exp in case["expect_hold"].items():
                v = value(qtypes(cid)[qid], (row.get("answers") or {}).get(qid))
                observed[qid] = v
                held &= bool(match(qtypes(cid)[qid], v, exp))
            summary["injection"][f"{cid}:{arm}"] = {"held": held, "observed": observed}

    (run_dir / "summary.json").write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n",
                                          encoding="utf-8")

    # --- markdown ----------------------------------------------------------
    L: list[str] = []
    L.append(f"# Gemini Decisions API — small evaluation (`{run_dir.name}`)\n")
    L.append(f"Raw records: `{run_dir.name}/calls.jsonl` ({len(load_calls(run_dir))} rows, "
             f"{len(calls)} planned tasks after superseding adapter-defect retries). "
             "Every metric below is recomputed from those records.\n")
    L.append("## Arms\n")
    L.append("| arm | model | wire | price / M input tokens |")
    L.append("|---|---|---|---:|")
    L.append("| gemini | `gemini-decision-1` | Decisions API `v1beta/decisions` | $0.10 |")
    L.append("| jev | `typesafe-ai/jev` | Vercel AI Gateway evaluation-model v4 | $0.042 |")
    L.append("| clef-flash | `@cf/cloudflare/clef-flash` | Cloudflare Workers AI | $0.09 |")

    L.append("\n## Health, latency, cost (main phase)\n")
    L.append("| metric | gemini | jev | clef-flash |")
    L.append("|---|---:|---:|---:|")
    a = summary["arms"]
    L.append("| requests ok/total | " + " | ".join(f"{a[x]['ok']}/{a[x]['requests']}" for x in ARMS) + " |")
    L.append("| p50 latency | " + " | ".join(
        f"{a[x]['latency_ms']['p50']:.0f} ms" if a[x]['latency_ms']['p50'] else "n/a" for x in ARMS) + " |")
    L.append("| p95 latency | " + " | ".join(
        f"{a[x]['latency_ms']['p95']:.0f} ms" if a[x]['latency_ms']['p95'] else "n/a" for x in ARMS) + " |")
    L.append("| max latency | " + " | ".join(
        f"{a[x]['latency_ms']['max']:.0f} ms" if a[x]['latency_ms']['max'] else "n/a" for x in ARMS) + " |")
    L.append("| computed spend | " + " | ".join(f"${a[x]['cost_computed_usd']:.6f}" for x in ARMS) + " |")
    L.append("| cost / call | " + " | ".join(
        f"${a[x]['cost_per_call_usd']:.6f}" if a[x]['cost_per_call_usd'] is not None else "n/a" for x in ARMS) + " |")
    L.append("| cost / question | " + " | ".join(
        f"${a[x]['cost_per_question_usd']:.6f}" if a[x]['cost_per_question_usd'] is not None else "n/a" for x in ARMS) + " |")
    L.append("| errors | " + " | ".join(str(a[x]['errors'] or "none") for x in ARMS) + " |")
    if a["gemini"]["errors"]:
        L.append("\nThe Gemini `400`s are one adapter defect, not model refusals: structured (`dict`) "
                 "state was sent as a bare object where the API requires a content block. It was fixed "
                 "in `harness/gemini_decisions_client.py` after the request cap was consumed, so "
                 "**C01–C05, C08, C09 and the C01 repeats are not measured for the Gemini arm**; "
                 "C06/C07 (the two prompt-injection probes) were re-run after the fix and are included. "
                 "Completing the gap is 8 calls ≈ $0.0003: "
                 "`python3 harness/gemini_eval.py --out run-<label> --only-cases C01,C02,C03,C04,C05,C08,C09 --only-arms gemini --append`.")

    crosscheck = run_dir / "latency-crosscheck.txt"
    if crosscheck.exists():
        L.append("\n### Gemini latency is a client-path artifact in this run — read before quoting\n")
        L.append("Every Gemini call in `calls.jsonl` took ~23.9–25.5 s regardless of payload size "
                 "(16–1,333 input tokens, 1–8 questions). The identical request through `curl` and "
                 "through the same client after the fix returns in well under a second, so the run's "
                 "Gemini p50/p95 measure this host's address selection (IPv6 AAAA records with a "
                 "blackholed route; `urllib` walks the list serially), not model latency. "
                 "The fix ships in `harness/gemini_decisions_client.py` (`prefer_ipv4`). "
                 "A clean Gemini latency sample is a 10-call, ~$0.0003 follow-up — it was not run "
                 "inside the 150-request cap.\n")
        L.append("```")
        L.append(crosscheck.read_text(encoding="utf-8").strip())
        L.append("```")
        L.append("\n`jev` and `clef-flash` latencies are unaffected (same client stack, but their "
                 "endpoints resolve to reachable addresses from this host).")

    L.append("\n## Quality on author-labeled decisions\n")
    L.append("| arm | correct / n | accuracy | by type | Brier (yes/no) |")
    L.append("|---|---:|---:|---|---:|")
    for x in ARMS:
        lab = a[x].get("labeled") or {}
        L.append(f"| {x} | {lab.get('correct')}/{lab.get('n')} | "
                 f"{lab.get('accuracy')} | {lab.get('by_type')} | {lab.get('brier')} |")

    cs = summary.get("quality_common_subset") or {}
    if cs.get("arms"):
        L.append(f"\nSame three arms on the identical case set they all measured "
                 f"({len(cs['cases'])} cases: {', '.join(cs['cases'])}):\n")
        L.append("| arm | correct / n | accuracy |")
        L.append("|---|---:|---:|")
        for x in ARMS:
            q = cs["arms"][x]
            L.append(f"| {x} | {q['correct']}/{q['n']} | {q['accuracy']} |")

    L.append("\n## Agreement with Gemini\n")
    L.append("| arm | question match | all-question case match | mean abs Δp |")
    L.append("|---|---:|---:|---:|")
    for x in ("jev", "clef-flash"):
        g = summary["agreement"].get(x, {})
        L.append(f"| {x} | {g.get('question_match_rate')} ({g.get('questions')} q) | "
                 f"{g.get('all_question_match_rate')} ({g.get('cases')} cases) | {g.get('mean_abs_probability_delta')} |")

    L.append("\n## Determinism (3 repeats)\n")
    L.append("| group | arm | label stable | max numeric spread |")
    L.append("|---|---|---|---:|")
    for key, d in summary["determinism"].items():
        L.append(f"| {key} | {d['arm']} | {d['label_stable']} | {d['max_numeric_spread']} |")

    L.append("\n## Batching (8 questions in one call vs single-question calls)\n")
    L.append("| arm | batched q | batched latency | single p50 | ratio | agreement |")
    L.append("|---|---:|---:|---:|---:|---:|")
    for x in ARMS:
        b = summary["batching"].get(x, {})
        L.append(f"| {x} | {b.get('batched_questions')} | {b.get('batched_latency_ms')} ms | "
                 f"{b.get('single_p50_latency_ms')} ms | {b.get('latency_ratio_batched_over_single')} | "
                 f"{b.get('agreement_batched_vs_single')} |")

    L.append("\n## Format sensitivity (one semantic: prose / flat JSON / nested JSON)\n")
    L.append("| arm | question | values | formats measured | max numeric spread | label stable |")
    L.append("|---|---|---|---|---:|---|")
    for x in ARMS:
        for qid, d in summary["format_sensitivity"].get(x, {}).items():
            L.append(f"| {x} | {qid} | {d['values']} | {d.get('measured_formats')} | "
                     f"{d['max_numeric_spread']} | {d['label_stable']} |")

    L.append("\n## Vision (synthetic image gates)\n")
    L.append("| arm | cases | sanity checks passed |")
    L.append("|---|---:|---:|")
    for x in ("gemini", "clef-flash"):
        v = summary["vision"].get(x, {})
        L.append(f"| {x} | {v.get('cases')} | {v.get('passed')}/{v.get('checks')} |")
    L.append("\njev: **unsupported** — the Jev gateway evaluation model accepts no image input; "
             "not measured rather than scored as a failure.")

    L.append("\n## Prompt-injection holds\n")
    L.append("| case:arm | held | observed |")
    L.append("|---|---|---|")
    for key, d in summary["injection"].items():
        L.append(f"| {key} | {d.get('held')} | {d.get('observed')} |")

    L.append("\n## Reading these numbers\n")
    L.append("- Agreement is not accuracy. Only the author-labeled subset bounds accuracy, and n is small "
             "(see the quality table); borderline cases exist to show divergence, not to score arms.")
    L.append("- **Do not quote the Gemini latency columns as provider latency** — they measure this host's "
             "IPv6 address-selection defect (see the cross-check above). `jev` and `clef-flash` latencies "
             "are real single-call, sequential measurements from one host and do not establish a deployment SLO.")
    L.append("- Cost is computed from provider-reported input tokens where the provider reports them; "
             "verify against billing before budgeting at scale. Gemini's per-call cost is higher here mostly "
             "because six of its calls carry images (~1.1–1.3k input tokens each).")
    L.append("- The Gemini `400`s and the `clef-flash` `422`s are harness defects, not model behaviour: the "
             "first run had a structured-state bug and a data-URI bug. Both are fixed in the harness; the raw "
             "failures stay in `calls.jsonl` and the successful reruns are appended, so the evidence shows "
             "both the defect and the repair.")

    (run_dir / "report.md").write_text("\n".join(L) + "\n", encoding="utf-8")
    print(f"wrote {run_dir/'report.md'} and {run_dir/'summary.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
