#!/usr/bin/env python3
"""Generate report.md + summary.json from a stress run's calls.jsonl.

Usage: python3 report.py results/<run-dir>

Answer shapes handled defensively:
  - clef/clef-flash (System One): choice -> {"choice","probabilities"}; yes/no -> {"noul"};
    score -> {"score","probabilities"}
  - jev (gateway API): choice -> {"choice","probabilities"}; yes/no -> {"probability"};
    score -> {"score","probabilities"}
"""

from __future__ import annotations

import json
import statistics
import sys
from collections import defaultdict
from pathlib import Path

ARMS = ("jev", "clef", "clef-flash")
PAIRS = (("jev", "clef"), ("jev", "clef-flash"), ("clef", "clef-flash"))
BOOLEAN_THRESHOLD = 0.5
ROOT = Path(__file__).resolve().parent.parent


def load_rows(run_dir: Path) -> list[dict]:
    rows = []
    for line in (run_dir / "calls.jsonl").read_text().splitlines():
        line = line.strip()
        if line:
            try:
                rows.append(json.loads(line))
            except Exception:
                pass
    return rows


def ans_shape(a) -> tuple[str, object]:
    if not isinstance(a, dict):
        return ("unknown", None)
    if isinstance(a.get("choice"), str):
        return ("choice", a["choice"])
    for k in ("noul", "probability"):
        v = a.get(k)
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            return ("noul", float(v))
    if isinstance(a.get("score"), (int, float)) and not isinstance(a.get("score"), bool):
        return ("score", float(a["score"]))
    v = a.get("value")
    if isinstance(v, bool):
        return ("noul", 1.0 if v else 0.0)
    return ("unknown", None)


def qmatch(a, b) -> bool | None:
    ka, va = ans_shape(a)
    kb, vb = ans_shape(b)
    if va is None or vb is None or ka != kb:
        return None
    if ka == "choice":
        return va == vb
    if ka == "noul":
        return (va >= BOOLEAN_THRESHOLD) == (vb >= BOOLEAN_THRESHOLD)
    if ka == "score":
        return abs(va - vb) < 0.5
    return None


def qdelta(a, b) -> float | None:
    ka, va = ans_shape(a)
    kb, vb = ans_shape(b)
    if va is None or vb is None or ka != kb:
        return None
    if ka == "choice":
        return 0.0 if va == vb else 1.0
    return abs(va - vb)


def choice_prob_delta(a, b) -> float | None:
    if not (isinstance(a, dict) and isinstance(b, dict)):
        return None
    pa, pb = a.get("probabilities") or {}, b.get("probabilities") or {}
    keys = set(pa) | set(pb)
    if not keys:
        return None
    ds = [abs(float(pa.get(k, 0)) - float(pb.get(k, 0))) for k in keys]
    return sum(ds) / len(ds)


def fmt_val(a) -> str:
    k, v = ans_shape(a)
    if v is None:
        return "—"
    if k == "choice":
        return str(v)
    if k == "noul":
        return f"{v:.2f}{'✓' if v >= BOOLEAN_THRESHOLD else '✗'}"
    if k == "score":
        return f"{v:.2f}"
    return str(v)


def conf_of(a) -> float | None:
    k, v = ans_shape(a)
    if v is None:
        return None
    if k == "noul":
        return max(v, 1 - v)
    if isinstance(a, dict) and isinstance(a.get("probabilities"), dict) and a["probabilities"]:
        return max(float(x) for x in a["probabilities"].values())
    return None


def pearson_p50_p95(xs: list[float]) -> tuple[float, float]:
    if not xs:
        return (0.0, 0.0)
    s = sorted(xs)
    n = len(s)

    def pct(p):
        idx = min(n - 1, max(0, int(round(p * (n - 1)))))
        return s[idx]

    return (pct(0.5), pct(0.95))


def fmt_ms(x: float | None) -> str:
    return f"{x/1000:.1f}s" if isinstance(x, (int, float)) else "—"


def main() -> int:
    run_dir = Path(sys.argv[1] if len(sys.argv) > 1 else "results/run-20261003-main")
    rows = load_rows(run_dir)
    meta = json.loads((run_dir / "meta.json").read_text()) if (run_dir / "meta.json").exists() else {}
    main_rows = [r for r in rows if r.get("phase") == "main"]
    ok_main = [r for r in main_rows if r.get("ok")]
    by_case_arm: dict[str, dict[str, dict]] = defaultdict(dict)
    for r in ok_main:
        if r.get("repeat_group") or r.get("batch_mode"):
            continue
        by_case_arm[r["case_id"]].setdefault(r["arm"], r)

    out: list[str] = []
    w = out.append

    w(f"# jev-vs-clef stress report — `{meta.get('run_id', run_dir.name)}`")
    w("")
    w(f"- Started: {meta.get('started_utc', '?')} · Python {meta.get('python', '?')} · battery sha256 `{(meta.get('battery_sha256') or '')[:16]}…`")
    w(f"- Calls recorded: {len(rows)} (main ok: {len(ok_main)} of {len(main_rows)}; smoke/probe excluded from analysis below unless noted)")
    w("")

    # ---------- latency ----------
    w("## Latency (wall clock, single test environment)")
    w("")
    w("| arm | calls | mean | p50 | p95 | max |")
    w("|---|---:|---:|---:|---:|---:|")
    for arm in ARMS:
        lat = [r["latency_ms"] for r in ok_main if r["arm"] == arm and isinstance(r.get("latency_ms"), (int, float))]
        if not lat:
            continue
        w(f"| {arm} | {len(lat)} | {fmt_ms(statistics.mean(lat))} | {fmt_ms(pearson_p50_p95(lat)[0])} | {fmt_ms(pearson_p50_p95(lat)[1])} | {fmt_ms(max(lat))} |")
    w("")

    probe = [r for r in rows if r.get("phase") == "probe" and r.get("ok")]
    par = [r for r in probe if r.get("repeat_group") == "probe-par" and isinstance(r.get("latency_ms"), (int, float))]
    seqs = [r for r in probe if r and r.get("repeat_group", "").startswith("probe-seq") and isinstance(r.get("latency_ms"), (int, float))]
    if par:
        med = statistics.median(r["latency_ms"] for r in par)
        seq_med = statistics.median(r["latency_ms"] for r in seqs) if seqs else None
        w("### Concurrency probe (clef, identical payload)")
        w("")
        w(f"- sequential median: {fmt_ms(seq_med)} · 4-way parallel median: {fmt_ms(med)} " +
          f"(parallel-by-median ratio: {med / seq_med:.2f}×)" if seq_med else "")
        w(f"- all 4 parallel calls ok: {all(r.get('ok') for r in par)}")
        w("")
    smoke = [r for r in rows if r.get("phase") == "smoke"]
    if smoke:
        w("### Smoke gate")
        w("")
        w("| gate | arm | status | latency |")
        w("|---|---|---|---:|")
        for r in smoke:
            status = "ok" if r.get("ok") else f"failed ({r.get('http_status') or r.get('rc')})"
            w(f"| {r['case_id']} | {r['arm']} | {status} | {fmt_ms(r.get('latency_ms'))} |")
        w("")

    # ---------- cost ----------
    w("## Cost (computed from provider usage when available; estimate otherwise)")
    w("")
    w("| arm | calls | tokens in (sum) | computed $ | est $ | avg $/call |")
    w("|---|---:|---:|---:|---:|---:|")
    for arm in ARMS:
        arm_rows = [r for r in rows if r["arm"] == arm and r.get("ok")]
        comp = [r["cost_computed_usd"] for r in arm_rows if isinstance(r.get("cost_computed_usd"), (int, float))]
        est = [r["cost_estimate_usd"] for r in arm_rows if isinstance(r.get("cost_estimate_usd"), (int, float))]
        toks = [r["usage_input_tokens"] for r in arm_rows if isinstance(r.get("usage_input_tokens"), (int, float))]
        s_comp = f"${sum(comp):.5f}" if comp else "n/a"
        avg = f"${sum(comp)/len(comp):.6f}" if comp else (f"~${sum(est)/len(est):.6f}" if est else "—")
        w(f"| {arm} | {len(arm_rows)} | {int(sum(toks)) if toks else '—'} | {s_comp} | ${sum(est):.5f} | {avg} |")
    w("")

    # ---------- agreement ----------
    def cases_of(group_prefixes) -> list[str]:
        ids = sorted(cid for cid in by_case_arm if any(cid.startswith(p) for p in group_prefixes))
        return ids

    def pair_stats(case_ids: list[str]) -> str:
        lines = []
        for a, b in PAIRS:
            qn = 0
            cm_total = 0
            case_all = case_any = 0
            deltas = []
            pdeltas = []
            for cid in case_ids:
                ra, rb = by_case_arm[cid].get(a), by_case_arm[cid].get(b)
                if not (ra and rb):
                    continue
                aa, ab = ra.get("answers") or {}, rb.get("answers") or {}
                cm = cq = 0
                for qid in sorted(set(aa) & set(ab)):
                    m = qmatch(aa[qid], ab[qid])
                    if m is None:
                        continue
                    cq += 1
                    cm += 1 if m else 0
                    d = qdelta(aa[qid], ab[qid])
                    if d is not None:
                        deltas.append(d)
                    pd = choice_prob_delta(aa[qid], ab[qid])
                    if pd is not None:
                        pdeltas.append(pd)
                qn += cq
                cm_total += cm
                case_any += 1
                if cq and cm == cq:
                    case_all += 1
            if case_any:
                lines.append(
                    f"| {a} vs {b} | {case_any} | {qn} | {100*cm_total/max(qn,1):.0f}% | {case_all}/{case_any} | "
                    f"{statistics.mean(deltas):.3f} | {statistics.mean(pdeltas) if pdeltas else float('nan'):.3f} |")
        return "\n".join(lines)

    w("## Routing & guardrail agreement (groups A + C)")
    w("")
    w("Metrics: question match = identical label for choice, same side of 0.5 for yes/no, score within ±0.5; "
      "`all-match` = every question of the case agrees; mean Δ = mean absolute value difference; mean Δp(choice) = mean absolute option-probability difference.")
    w("")
    w("| pair | cases | questions | q-match | all-match | mean Δ | mean Δp(choice) |")
    w("|---|---:|---:|---:|---:|---:|---:|")
    w(pair_stats(cases_of(["A", "C"])))
    w("")

    # per-question agreement detail
    w("### Per-question match rate (A + C)")
    w("")
    w("| pair | " + " | ".join(["route", "urgent", "severity", "ending", "needs_reply", "booleans"]) + " |")
    w("|---|" + "---:|" * 6)
    for a, b in PAIRS:
        stats: dict[str, list[int]] = defaultdict(lambda: [0, 0])
        for cid in cases_of(["A", "C"]):
            ra, rb = by_case_arm[cid].get(a), by_case_arm[cid].get(b)
            if not (ra and rb):
                continue
            aa, ab = ra.get("answers") or {}, rb.get("answers") or {}
            for qid in set(aa) & set(ab):
                m = qmatch(aa[qid], ab[qid])
                if m is None:
                    continue
                key = None
                if qid == "route":
                    key = "route"
                elif qid == "urgent":
                    key = "urgent"
                elif qid == "severity":
                    key = "severity"
                elif qid == "ending":
                    key = "ending"
                elif qid == "needs_reply":
                    key = "needs_reply"
                else:
                    key = "booleans"
                stats[key][0] += 1
                stats[key][1] += 1 if m else 0
        cells = []
        for key in ("route", "urgent", "severity", "ending", "needs_reply", "booleans"):
            n, k = stats.get(key, [0, 0])
            cells.append(f"{100*k/n:.0f}% ({k}/{n})" if n else "—")
        w(f"| {a} vs {b} | " + " | ".join(cells) + " |")
    w("")

    # disagreements appendix (any pair, A + C)
    w("### Disagreements (any pair, A + C)")
    w("")
    w("| case | question | jev | clef | clef-flash |")
    w("|---|---|---|---|---|")
    for cid in cases_of(["A", "C"]):
        arm_rows = {arm: by_case_arm[cid].get(arm) for arm in ARMS}
        if not all(arm_rows.values()):
            continue
        ans = {arm: (r.get("answers") or {}) for arm, r in arm_rows.items()}
        qids = sorted(set(ans["jev"]) & set(ans["clef"]) & set(ans["clef-flash"]))
        for qid in qids:
            ms = [
                qmatch(ans["jev"][qid], ans["clef"][qid]),
                qmatch(ans["jev"][qid], ans["clef-flash"][qid]),
                qmatch(ans["clef"][qid], ans["clef-flash"][qid]),
            ]
            if any(m is False for m in ms):
                w(f"| {cid} | {qid} | {fmt_val(ans['jev'][qid])} | {fmt_val(ans['clef'][qid])} | {fmt_val(ans['clef-flash'][qid])} |")
    w("")

    # author-expected accuracy
    w("### Author-expected accuracy (labeled cases only)")
    w("")
    labeled = []
    for cid in cases_of(["A"]):
        for arm, r in by_case_arm[cid].items():
            exp = r.get("expected")
            if exp:
                labeled.append((cid, arm, r, exp))
    if labeled:
        w("| arm | matched | total | rate |")
        w("|---|---:|---:|---:|")
        agg: dict[str, list[int]] = defaultdict(lambda: [0, 0])
        for cid, arm, r, exp in labeled:
            aa = r.get("answers") or {}
            for qid, ev in exp.items():
                m = qmatch(aa.get(qid), {"choice": ev} if isinstance(ev, str) else {"noul": 1.0 if ev is True else 0.0} if isinstance(ev, bool) else {"score": float(ev)})
                if m is None:
                    continue
                agg[arm][0] += 1
                agg[arm][1] += 1 if m else 0
        for arm in ARMS:
            n, k = agg.get(arm, [0, 0])
            if n:
                w(f"| {arm} | {k} | {n} | {100*k/n:.0f}% |")
        w("")
        w("| case | " + " | ".join(ARMS) + " |")
        w("|---|" + "---|" * len(ARMS))
        for cid in sorted({c for c, _, _, _ in labeled}):
            cells = []
            for arm in ARMS:
                r = by_case_arm[cid].get(arm)
                exp = next((e for c, a, _, e in labeled if c == cid and a == arm), None)
                if not r or not exp:
                    cells.append("—")
                    continue
                aa = r.get("answers") or {}
                parts = []
                for qid, ev in exp.items():
                    m = qmatch(aa.get(qid), {"choice": ev} if isinstance(ev, str) else {"noul": 1.0 if ev is True else 0.0} if isinstance(ev, bool) else {"score": float(ev)})
                    parts.append("✓" if m else "✗")
                cells.append("".join(parts) + f" ({''.join(str(fmt_val(aa.get(qid))[0]) for qid in exp)})")
            w(f"| {cid} | " + " | ".join(cells) + " |")
        w("")
        w("Cell order follows the case's question order (route, urgent, severity).")

    # injection holds
    holds = [(cid, r) for cid, arms in by_case_arm.items() for r in arms.values() if r.get("expect_hold")]
    if holds:
        w("")
        w("### Injection hold (C06, C07)")
        w("")
        w("| case | arm | held? | observed |")
        w("|---|---|---|---|")
        for cid, r in sorted(holds, key=lambda x: (x[0], x[1]["arm"])):
            aa = r.get("answers") or {}
            bits = []
            held = True
            for qid, ev in (r.get("expect_hold") or {}).items():
                k, v = ans_shape(aa.get(qid))
                if isinstance(ev, bool):
                    ok = k == "noul" and ((v >= 0.5) == ev)
                else:
                    ok = k == "choice" and v == ev
                held = held and ok
                bits.append(f"{qid}={fmt_val(aa.get(qid))}")
            w(f"| {cid} | {r['arm']} | {'✔ held' if held else '✘ FLIPPED'} | {', '.join(bits)} |")
    w("")

    # format stability
    w("## Format stability (group B — prose vs flat JSON vs nested JSON)")
    w("")
    sem = defaultdict(lambda: defaultdict(dict))  # sem -> arm -> fmt -> row
    for r in ok_main:
        cid = r["case_id"]
        if len(cid) == 3 and cid.startswith("B"):
            s = cid[:2]
            f = {"p": "prose", "f": "flat", "n": "nested"}[cid[2]]
            sem[s]["arm" if False else r["arm"]][f] = r
    w("| semantic | arm | prose | flat | nested | stable |")
    w("|---|---|---|---|---|---|")
    for s in sorted(sem):
        for arm in ARMS:
            fmts = sem[s].get(arm, {})
            if len(fmts) < 3:
                continue
            vals = {f: fmts[f] for f in ("prose", "flat", "nested") if f in fmts}
            stable = True
            qids = sorted(set().union(*[set(r.get("answers") or {}) for r in vals.values()]))
            for qid in qids:
                answers = [(r.get("answers") or {}).get(qid) for r in vals.values()]
                for i in range(len(answers) - 1):
                    m = qmatch(answers[i], answers[i + 1])
                    if m is False:
                        stable = False
            cells = []
            for f in ("prose", "flat", "nested"):
                r = fmts.get(f)
                aa = (r.get("answers") or {}) if r else {}
                cells.append(" · ".join(fmt_val(aa.get(qid)) for qid in qids))
            w(f"| {s} | {arm} | " + " | ".join(cells) + f" | {'✔' if stable else '✘ varies'} |")
    w("")

    # scale
    w("## Scale (group D — needle questions across state sizes)")
    w("")
    w("| case | bytes | arm | route | ref A-104 | refund | tokens | latency |")
    w("|---|---:|---|---|---|---:|---:|---:|")
    for cid in sorted(c for c in by_case_arm if c.startswith("D")):
        for arm in ARMS:
            r = by_case_arm[cid].get(arm)
            if not r:
                continue
            aa = r.get("answers") or {}
            w(f"| {cid} | {r.get('request_bytes') or '—'} | {arm} | {fmt_val(aa.get('route'))} | {fmt_val(aa.get('ref_order'))} | {fmt_val(aa.get('wants_refund'))} | {r.get('usage_input_tokens') or '—'} | {fmt_ms(r.get('latency_ms'))} |")
    w("")

    # determinism
    w("## Determinism (group E — identical payload repeated 3×)")
    w("")
    w("| case | arm | repeats | answers identical | labels identical | max Δ (numeric) |")
    w("|---|---|---:|---|---|---:|")
    groups = defaultdict(list)
    for r in rows:
        rg = r.get("repeat_group") or ""
        if rg.startswith("E:"):
            groups[rg].append(r)
    for rg in sorted(groups):
        rr = [r for r in groups[rg] if r.get("ok")]
        ans_set = {json.dumps(r.get("answers"), sort_keys=True) for r in rr if r.get("answers") is not None}
        qids = set.intersection(*[set(r.get("answers") or {}) for r in rr]) if rr else set()
        labels_ok = True
        max_d = 0.0
        for qid in qids:
            vals = [ans_shape((r.get("answers") or {})[qid]) for r in rr]
            kinds = {k for k, v in vals}
            if kinds == {"choice"}:
                if len({v for k, v in vals}) > 1:
                    labels_ok = False
            else:
                nums = [float(v) for k, v in vals if isinstance(v, (int, float))]
                if nums:
                    max_d = max(max_d, max(nums) - min(nums))
                    if any(v >= BOOLEAN_THRESHOLD for v in nums) and not all(v >= BOOLEAN_THRESHOLD for v in nums):
                        labels_ok = False
        _, cid, arm = rg.split(":")
        w(f"| {cid} | {arm} | {len(rr)} | {'✔ identical' if len(ans_set) == 1 else f'✘ {len(ans_set)} variants'} | {'✔' if labels_ok else '✘ flipped'} | {max_d:.4f} |")
    w("")

    # batching
    w("## Batching (group F — 8 questions in one call vs 8 single-question calls)")
    w("")
    w("| arm | question | batch | single | match |")
    w("|---|---|---|---|---|")
    for arm in ("jev", "clef"):
        batch = next((r for r in ok_main if r["arm"] == arm and r.get("batch_mode") == "all_in_one"), None)
        singles = {r.get("single_question"): r for r in ok_main if r["arm"] == arm and r.get("batch_mode") == "single"}
        if not batch:
            continue
        matches = 0
        rows_out = []
        for qid, sr in singles.items():
            ba = (batch.get("answers") or {}).get(qid)
            sa = (sr.get("answers") or {}).get(qid)
            m = qmatch(ba, sa)
            matches += 1 if m else 0
            rows_out.append(f"| {arm} | {qid} | {fmt_val(ba)} | {fmt_val(sa)} | {'✓' if m else '✘' if m is False else '—'} |")
        w("\n".join(rows_out))
        w(f"| **{arm} total** | 8 | | | **{matches}/8** |")
    w("")

    # edges
    w("## Edge cases (group G)")
    w("")
    edge_notes = []
    g1 = by_case_arm.get("G1", {}).get("clef")
    if g1:
        keys = sorted((g1.get("answers") or {}).keys())
        edge_notes.append(f"- **G1 ids**: answers keys = {keys} (expect `route.v2-x`, `urgent-flag_1`)")
    g2 = by_case_arm.get("G2", {}).get("clef")
    if g2:
        aa = g2.get("answers") or {}
        spec2 = {}
        try:
            spec2 = json.loads((ROOT / "cases" / "stress_battery.json").read_text())["edges"]["G2_max_questions"]
        except Exception:
            pass
        pt = spec2.get("planted_true") or []
        pf = spec2.get("planted_false") or []

        def planted_hits(concepts, want):
            hits = 0
            for c in concepts:
                for qid, v in aa.items():
                    if c in qid:
                        k, val = ans_shape(v)
                        if isinstance(val, (int, float)) and ((val >= 0.5) == want):
                            hits += 1
                        break
            return hits

        t_ok = planted_hits(pt, True)
        f_ok = planted_hits(pf, False)
        edge_notes.append(f"- **G2 64 questions**: {len(aa)} answers returned; planted-true positives {t_ok}/{len(pt)}; planted-false negatives {f_ok}/{len(pf)}")
    g3 = by_case_arm.get("G3", {}).get("clef")
    if g3:
        if g3.get("ok"):
            edge_notes.append(f"- **G3 choice without escape**: accepted; answer = {fmt_val((g3.get('answers') or {}).get('priority'))}")
        else:
            edge_notes.append(f"- **G3 choice without escape**: rejected ({g3.get('http_status')}) — cloudflare enforces an escape label?")
    g4 = by_case_arm.get("G4", {}).get("clef")
    if g4:
        msg = json.dumps(g4.get("errors"))[:220].replace("\\n", " ")
        edge_notes.append(f"- **G4 raw-b64 image**: HTTP {g4.get('http_status')} — {msg}")
    g5 = by_case_arm.get("G5", {})
    for arm in ("clef", "clef-flash"):
        r = g5.get(arm)
        if r:
            aa = r.get("answers") or {}
            okv = (ans_shape(aa.get("red_circle"))[1] or 0) >= 0.5 and ans_shape(aa.get("topleft_shape"))[1] == "blue_square" and (ans_shape(aa.get("any_text"))[1] or 1) < 0.5
            edge_notes.append(f"- **G5 vision ({arm})**: {'3/3 ✓' if okv else 'CHECK'} — red_circle={fmt_val(aa.get('red_circle'))}, topleft={fmt_val(aa.get('topleft_shape'))}, any_text={fmt_val(aa.get('any_text'))}")
    g6 = by_case_arm.get("G6", {}).get("clef")
    if g6:
        edge_notes.append(f"- **G6 array state**: {'accepted' if g6.get('ok') else 'rejected'} — route={fmt_val((g6.get('answers') or {}).get('route'))}")
    g7 = by_case_arm.get("G7", {}).get("clef")
    if g7:
        if g7.get("ok"):
            edge_notes.append(f"- **G7 boolean alias**: accepted — clef also takes the jev-style `boolean` keyword")
        else:
            edge_notes.append(f"- **G7 boolean alias**: rejected ({g7.get('http_status')}) — use `noul` on the clef side")
    w("\n".join(edge_notes))
    w("")

    # failures
    fails = [r for r in rows if not r.get("ok") and not str(r.get("error", "")).startswith("skipped")]
    unexpected = [r for r in fails if r["case_id"] not in ("G4", "G7")]
    w("## Failures (expected rejection probes annotated)")
    w("")
    if not fails:
        w("None.")
    else:
        w("| phase | case | arm | status/rc | kind | error |")
        w("|---|---|---|---|---|---|")
        for r in fails:
            kind = "expected probe" if r["case_id"] in ("G4", "G7") else "UNEXPECTED"
            err = str(r.get("error") or "")[:150].replace("|", "\\|").replace("\n", " ")
            w(f"| {r['phase']} | {r['case_id']} | {r['arm']} | {r.get('http_status') or r.get('rc')} | {kind} | {err} |")
        if not unexpected:
            w("")
            w("No unexpected failures — only the two intentional rejection probes (G4 raw-b64 image; G7 `boolean` alias).")
    w("")

    report = "\n".join(out) + "\n"
    (run_dir / "report.md").write_text(report)

    summary = {
        "run_id": meta.get("run_id", run_dir.name),
        "n_calls": len(rows),
        "n_main_ok": len(ok_main),
        "spend_est_usd": round(sum(r["cost_estimate_usd"] for r in rows if isinstance(r.get("cost_estimate_usd"), (int, float))), 6),
        "spend_computed_usd": round(sum(r["cost_computed_usd"] for r in rows if isinstance(r.get("cost_computed_usd"), (int, float))), 6),
    }
    (run_dir / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    print(f"wrote {run_dir}/report.md and summary.json ({len(rows)} calls)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
