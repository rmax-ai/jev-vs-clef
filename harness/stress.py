#!/usr/bin/env python3
"""jev-vs-clef stress runner.

Runs the battery in ``cases/stress_battery.json`` against three arms:
  - ``jev``       — Jev via the Vercel AI Gateway evaluation-model API (plain HTTP)
  - ``clef``      — Cloudflare Workers AI ``@cf/cloudflare/clef`` (27B)
  - ``clef-flash``— Cloudflare Workers AI ``@cf/cloudflare/clef-flash`` (9B)

Phases:
  smoke — 4 live calls: jev text, clef text, clef vision (data URI), clef bad-image (expected 4xx)
  probe — sequential + 4-way parallel latency probe on clef (drives main concurrency)
  main  — full battery A–G (routing, formats, guardrails, scale, determinism, batching, edges)

Every call is appended to ``results/<run>/calls.jsonl``. The runner never prints
credentials and enforces a hard spend-estimate guard (``--budget-usd``).

Usage:
  python3 stress.py --dry-run                 # enumerate tasks + cost estimate, no network
  python3 stress.py --phases smoke            # validate wiring live
  python3 stress.py                           # smoke + probe + full battery
"""

from __future__ import annotations

import argparse
import base64
import concurrent.futures as cf
import hashlib
import json
import random
import statistics
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))

import clef_client  # noqa: E402
import jev_client  # noqa: E402

BATTERY = json.loads((ROOT / "cases" / "stress_battery.json").read_text())
ASSETS = ROOT / "assets"
CLEF_ARMS = ("clef", "clef-flash")
ALL_ARMS = ("jev", "clef", "clef-flash")


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def utc_compact() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def canonical_bytes(state) -> bytes:
    if isinstance(state, str):
        return state.encode("utf-8")
    return json.dumps(state, separators=(",", ":"), sort_keys=True).encode("utf-8")


def sha16(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()[:16]


def resolve_questions(case: dict) -> dict:
    if case.get("questions"):
        return case["questions"]
    ref = case.get("questions_ref")
    if not ref:
        raise ValueError(f"case {case['id']} has no questions")
    return BATTERY["question_sets"][ref]


def build_scale_states() -> list[dict]:
    """Deterministically materialize the D-group scale states (seed fixed in the cases file)."""
    spec = BATTERY["scale_generator"]
    rng = random.Random(20261003)
    out = []
    for idx, target in enumerate(spec["sizes_bytes"], start=1):
        parts = [spec["base"], *spec["decoy_facts"]]
        while len("\n".join(parts).encode("utf-8")) < target:
            parts.append(rng.choice(spec["noise_corpus"]))
        state = "\n".join(parts)
        out.append({"id": f"D{idx}", "label": f"scale ~{target}B (actual {len(state.encode())}B)", "state": state,
                    "label_note": spec["notes"]})
    return out


def task(phase: str, case: dict, arm: str, *, state=None, questions=None, images=None, repeat_group=None, extra=None) -> dict:
    t = {
        "phase": phase,
        "case_id": case["id"],
        "label": case.get("label"),
        "arm": arm,
        "state": state if state is not None else case["state"],
        "questions": questions if questions is not None else resolve_questions(case),
        "images": images,
        "repeat_group": repeat_group,
        "expected": case.get("author_expected"),
        "expect_hold": case.get("expect_hold"),
        "note": case.get("label_note"),
    }
    if extra:
        t.update(extra)
    return t


def build_smoke_tasks() -> list[dict]:
    by_id = {c["id"]: c for c in BATTERY["cases"]}
    a01 = by_id["A01"]
    g5 = BATTERY["edges"]["G5_vision"]
    g4 = BATTERY["edges"]["G4_bad_image_raw_b64"]
    b64 = base64.b64encode((ASSETS / "shapes.png").read_bytes()).decode("ascii")
    smoke = [
        task("smoke", a01, "jev"),
        task("smoke", a01, "clef"),
        task("smoke", {"id": "G5", "label": "vision shapes"}, "clef", state="Classify the attached test image.", questions=g5["questions"], images=[clef_client.image_to_data_uri(str(ASSETS / "shapes.png"))]),
        task("smoke", {"id": "G4", "label": "bad image raw b64"}, "clef", state="Classify the attached image.", questions={"ok": {"type": "noul", "instructions": "Is an image attached?"}}, images=[b64]),
    ]
    return smoke


def build_probe_tasks() -> list[dict]:
    probe_case = {"id": "P", "label": "probe", "state": "The nightly backup completed with 2 warnings; no data loss reported.",
                  "questions": {"concern": {"type": "noul", "instructions": "Does this need attention today?"}},
                  "author_expected": None, "expect_hold": None, "label_note": None}
    seq1 = task("probe", probe_case, "clef", extra={"repeat_group": "probe-seq-a"})
    par = [task("probe", probe_case, "clef", extra={"repeat_group": "probe-par"}) for _ in range(4)]
    seq2 = task("probe", probe_case, "clef", extra={"repeat_group": "probe-seq-b"})
    return [seq1] + par + [seq2]


def build_main_tasks() -> list[dict]:
    cases = {c["id"]: c for c in BATTERY["cases"]}
    tasks: list[dict] = []

    # A — routing + tail triage; B — format sensitivity; C — guardrails
    for cid, case in cases.items():
        if cid.startswith(("A", "B", "C")):
            for arm in ALL_ARMS:
                tasks.append(task("main", case, arm))

    # D — scale states
    for sc in build_scale_states():
        case = {"id": sc["id"], "label": sc["label"], "state": sc["state"],
                "questions_ref": BATTERY["scale_generator"]["needle_questions_ref"],
                "author_expected": {"route": "billing", "ref_order": True, "wants_refund": True},
                "label_note": sc["label_note"]}
        for arm in ALL_ARMS:
            tasks.append(task("main", case, arm))

    # E — determinism repeats
    det = BATTERY["determinism"]
    for ref in det["refs"]:
        case = cases[ref]
        for rep in range(1, det["repeats"] + 1):
            for arm in ALL_ARMS:
                tasks.append(task("main", case, arm, repeat_group=f"E:{ref}:{arm}"))

    # F — batching (1 call with 8 questions vs 8 calls with 1)
    batch = BATTERY["batch"]
    batch_case = {"id": "F", "label": "batching", "state": batch["state"]}
    for arm in ("jev", "clef"):
        tasks.append(task("main", batch_case, arm, questions=resolve_questions({"questions_ref": batch["questions_ref"]}),
                          extra={"batch_mode": "all_in_one"}))
        for qid in batch["single_question_ids"]:
            qs = {"batch_v1": BATTERY["question_sets"]["batch_v1"]}
            single = {qid: qs["batch_v1"][qid]}
            tasks.append(task("main", batch_case, arm, questions=single, extra={"batch_mode": "single", "single_question": qid}))

    # G — edges (clef-only unless noted)
    edges = BATTERY["edges"]
    g1 = edges["G1_weird_ids"]
    tasks.append(task("main", {"id": "G1", "label": "weird ids"}, "clef", state=g1["state"], questions=g1["questions"]))
    g2 = edges["G2_max_questions"]
    g2_questions = {}
    concepts = g2["planted_true"] + g2["planted_false"] + [f"filler_{i:02d}" for i in range(1, 64 - len(g2["planted_true"]) - len(g2["planted_false"]) + 1)]
    for i, concept in enumerate(concepts[:64]):
        g2_questions[f"q{i:02d}_{concept.replace('-', '_')[:20]}"] = {
            "type": "noul", "instructions": f"Does the state mention '{concept}'?"}
    tasks.append(task("main", {"id": "G2", "label": "64 questions"}, "clef", state=g2["state"], questions=g2_questions,
                      extra={"planted_true": g2["planted_true"], "planted_false": g2["planted_false"]}))
    g3 = edges["G3_choice_without_escape"]
    tasks.append(task("main", {"id": "G3", "label": "choice without escape"}, "clef", state=g3["state"], questions=g3["questions"]))
    raw_b64 = base64.b64encode((ASSETS / "shapes.png").read_bytes()).decode("ascii")
    tasks.append(task("main", {"id": "G4", "label": "bad image raw b64"}, "clef", state="Classify the attached image.",
                      questions={"ok": {"type": "noul", "instructions": "Is an image attached?"}}, images=[raw_b64]))
    g5 = edges["G5_vision"]
    for arm in ("clef", "clef-flash"):
        tasks.append(task("main", {"id": "G5", "label": "vision shapes"}, arm,
                          state="Classify the attached test image for a vision pipeline smoke test.",
                          questions=g5["questions"], images=[clef_client.image_to_data_uri(str(ASSETS / "shapes.png"))]))
    g6 = edges["G6_array_state"]
    tasks.append(task("main", {"id": "G6", "label": "array state"}, "clef", state=g6["state"],
                      questions=resolve_questions({"questions_ref": g6["questions_ref"]})))
    g7 = edges["G7_type_alias"]
    tasks.append(task("main", {"id": "G7", "label": "boolean alias probe"}, "clef", state=g7["state"], questions=g7["questions"]))
    return tasks


def estimate_task_usd(t: dict, arm: str) -> float:
    if arm == "jev":
        return 0.00006  # conservative upper bound for these sizes
    payload = {"model": arm, "state": t["state"], "questions": t["questions"]}
    if t.get("images"):
        payload["images"] = ["x"] * len(t["images"])  # approximate; images not costed precisely
    size = len(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
    return clef_client.cost_estimate_usd(arm, size)


class Guards:
    def __init__(self, budget: float, max_seconds: float):
        self.budget = budget
        self.max_seconds = max_seconds
        self.spent = 0.0
        self.t0 = time.time()
        self._lock = threading.Lock()

    def update(self, rec: dict) -> None:
        with self._lock:
            self.spent += rec.get("cost_estimate_usd") or 0.0

    def stop_reason(self) -> str | None:
        if self.spent > self.budget:
            return "budget_exceeded"
        if time.time() - self.t0 > self.max_seconds:
            return "time_exceeded"
        return None


class Recorder:
    def __init__(self, path: Path, run_id: str):
        self.path = path
        self.run_id = run_id
        self._lock = threading.Lock()
        self._fh = open(path, "a", encoding="utf-8")
        self.count = 0

    def write(self, rec: dict) -> None:
        with self._lock:
            self.count += 1
            rec["run_id"] = self.run_id
            rec["seq"] = self.count
            self._fh.write(json.dumps(rec, separators=(",", ":")) + "\n")
            self._fh.flush()

    def close(self) -> None:
        self._fh.close()


def map_questions_for_arm(questions: dict, arm: str) -> dict:
    """Keyword mapping only: System One 'noul' <-> gateway 'boolean'.

    Clef follows the System One wire format with type ``noul`` for yes/no
    questions; the Jev gateway validates the same semantics as ``boolean``.
    All other bytes are identical between arms.
    """
    if arm != "jev":
        return questions
    mapped = {}
    for qid, q in questions.items():
        q2 = dict(q)
        if q2.get("type") == "noul":
            q2["type"] = "boolean"
        mapped[qid] = q2
    return mapped


def execute(task: dict) -> dict:
    arm = task["arm"]
    t0 = time.time()
    questions = map_questions_for_arm(task["questions"], arm)
    state_b = canonical_bytes(task["state"])
    payload_b = state_b + json.dumps(questions, separators=(",", ":"), sort_keys=True).encode("utf-8")
    rec_base = {
        "ts": utc_now(),
        "phase": task["phase"],
        "case_id": task["case_id"],
        "label": task.get("label"),
        "arm": arm,
        "model": arm,
        "state_sha256": hashlib.sha256(state_b).hexdigest(),
        "request_sha256": hashlib.sha256(payload_b).hexdigest(),
        "repeat_group": task.get("repeat_group"),
        "batch_mode": task.get("batch_mode"),
        "single_question": task.get("single_question"),
        "expected": task.get("expected"),
        "expect_hold": task.get("expect_hold"),
        "note": task.get("note"),
    }
    try:
        if arm == "jev":
            out = jev_client.call(task["state"], questions)
        else:
            out = clef_client.call(arm, task["state"], questions, images=task.get("images"))
        rec_base.update({
            "ok": out.get("ok"),
            "http_status": out.get("http_status"),
            "rc": out.get("rc"),
            "latency_ms": out.get("latency_ms"),
            "attempts": out.get("attempts"),
            "request_bytes": out.get("request_bytes"),
            "token_estimate": out.get("token_estimate"),
            "cost_estimate_usd": out.get("cost_estimate_usd"),
            "cost_computed_usd": out.get("cost_computed_usd"),
            "usage_input_tokens": out.get("usage_input_tokens"),
            "error": out.get("error"),
            "answers": out.get("answers"),
            "errors": out.get("errors"),
            "body": out.get("body"),
        })
    except Exception as exc:  # harness-level failure — record, never abort the pool
        rec_base.update({"ok": False, "error": f"harness_exception: {type(exc).__name__}: {exc}", "latency_ms": (time.time() - t0) * 1000.0})
    return rec_base


def progress_line(rec: dict, total: int) -> str:
    lat = f"{rec['latency_ms']:.0f}ms" if rec.get("latency_ms") is not None else "n/a"
    cost = rec.get("cost_estimate_usd")
    cost_s = f"${cost:.6f}" if isinstance(cost, (int, float)) else "n/a"
    flag = "ok" if rec.get("ok") else f"FAIL({rec.get('http_status') or rec.get('rc') or '?'})"
    return f"[{rec['seq']:>3}/{total}] {rec['phase']:<5} {rec['arm']:<10} {rec['case_id']:<3} {flag:<12} {lat:>8} {cost_s}"


def run_pool(tasks: list[dict], max_workers: int, rec: Recorder, guards: Guards, total: int) -> None:
    pending = list(tasks)
    inflight: set = set()
    skipped_reason = None
    with cf.ThreadPoolExecutor(max_workers=max_workers) as ex:
        while pending or inflight:
            while pending and len(inflight) < max_workers:
                reason = guards.stop_reason()
                if reason:
                    skipped_reason = reason
                    break
                inflight.add(ex.submit(execute, pending.pop(0)))
            if not inflight:
                break
            done, inflight = cf.wait(inflight, return_when=cf.FIRST_COMPLETED)
            for fut in done:
                r = fut.result()
                rec.write(r)
                guards.update(r)
                print(progress_line(r, total), flush=True)
    for t in pending:
        r = {
            "ts": utc_now(), "phase": t["phase"], "case_id": t["case_id"], "arm": t["arm"],
            "label": t.get("label"), "ok": False, "error": f"skipped:{skipped_reason or 'guard'}", "latency_ms": None,
            "repeat_group": t.get("repeat_group"), "batch_mode": t.get("batch_mode"),
            "single_question": t.get("single_question"),
        }
        rec.write(r)
        print(progress_line(r, total), flush=True)


PHASE_ORDER = ("smoke", "probe", "main")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--phases", default="smoke,probe,main", help="comma list of smoke,probe,main")
    ap.add_argument("--out", default=None, help="results dir (default results/run-<utc>)")
    ap.add_argument("--concurrency", type=int, default=None, help="clef pool size for main (default: probe decides; fallback 6)")
    ap.add_argument("--budget-usd", type=float, default=0.25)
    ap.add_argument("--max-seconds", type=float, default=1800)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    phases = [p.strip() for p in args.phases.split(",") if p.strip()]

    smoke = build_smoke_tasks() if "smoke" in phases else []
    probe = build_probe_tasks() if "probe" in phases else []
    main_tasks = build_main_tasks() if "main" in phases else []
    all_tasks = smoke + probe + main_tasks

    if args.dry_run:
        est = sum(estimate_task_usd(t, t["arm"]) for t in all_tasks)
        by_phase: dict[str, int] = {}
        by_arm: dict[str, int] = {}
        for t in all_tasks:
            by_phase[t["phase"]] = by_phase.get(t["phase"], 0) + 1
            by_arm[t["arm"]] = by_arm.get(t["arm"], 0) + 1
        print(f"DRY RUN — {len(all_tasks)} tasks; est cost ${est:.4f}")
        print(f"  phases: {by_phase}")
        print(f"  arms:   {by_arm}")
        if all_tasks:
            sizes = [len(canonical_bytes(t['state'])) for t in all_tasks]
            print(f"  state bytes: min={min(sizes)} p50={int(statistics.median(sizes))} max={max(sizes)}")
        return 0

    run_id = args.out or f"run-{utc_compact()}"
    out_dir = ROOT / "results" / run_id
    out_dir.mkdir(parents=True, exist_ok=True)
    meta = {
        "run_id": run_id,
        "started_utc": utc_now(),
        "phases": phases,
        "tasks_total": len(all_tasks),
        "battery_sha256": hashlib.sha256((ROOT / "cases" / "stress_battery.json").read_bytes()).hexdigest(),
        "python": sys.version.split()[0],
        "budget_usd": args.budget_usd,
    }
    (out_dir / "meta.json").write_text(json.dumps(meta, indent=2) + "\n")

    rec = Recorder(out_dir / "calls.jsonl", run_id)
    guards = Guards(args.budget_usd, args.max_seconds)

    if smoke:
        print(f"=== smoke ({len(smoke)} calls) ===", flush=True)
        run_pool(smoke, 1, rec, guards, len(all_tasks))

    concurrency = args.concurrency
    if probe:
        print("=== probe (sequential + 4-way parallel) ===", flush=True)
        run_pool([probe[0]], 1, rec, guards, len(all_tasks))
        run_pool(probe[1:5], 4, rec, guards, len(all_tasks))
        run_pool([probe[5]], 1, rec, guards, len(all_tasks))
        # derive concurrency from the parallel batch
        par = [r for r in read_results(out_dir / "calls.jsonl") if r.get("repeat_group") == "probe-par" and r.get("latency_ms")]
        if par and all(r.get("ok") for r in par):
            med = statistics.median(r["latency_ms"] for r in par)
            concurrency = concurrency or (8 if med <= 25000 else 6 if med <= 45000 else 4)
            print(f"probe: parallel median {med:.0f}ms -> concurrency {concurrency}", flush=True)
        else:
            concurrency = concurrency or 4
            print("probe: parallel batch unhealthy -> concurrency 4", flush=True)
    concurrency = concurrency or 6

    if main_tasks:
        print(f"=== main ({len(main_tasks)} calls, concurrency {concurrency}) ===", flush=True)
        clef_tasks = [t for t in main_tasks if t["arm"] in CLEF_ARMS]
        jev_tasks = [t for t in main_tasks if t["arm"] == "jev"]
        run_pool(clef_tasks, concurrency, rec, guards, len(all_tasks))
        run_pool(jev_tasks, 3, rec, guards, len(all_tasks))

    rec.close()
    rows = read_results(out_dir / "calls.jsonl")
    ok = sum(1 for r in rows if r.get("ok"))
    spent = sum(r.get("cost_estimate_usd") or 0.0 for r in rows if isinstance(r.get("cost_estimate_usd"), (int, float)))
    print(f"\nDONE — {len(rows)} recorded, {ok} ok, est spend ${spent:.4f}, out={out_dir}")
    return 0


def read_results(path: Path) -> list[dict]:
    rows = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if line:
            try:
                rows.append(json.loads(line))
            except Exception:
                pass
    return rows


if __name__ == "__main__":
    sys.exit(main())
