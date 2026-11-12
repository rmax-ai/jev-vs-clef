#!/usr/bin/env python3
"""Small Gemini Decisions API evaluation against the existing Jev/Clef battery.

Scope (deliberately bounded — see GEMINI-DECISIONS.md):

- A (routing/tails) + C (guardrails): every case in the frozen battery, 3 arms.
- B: one semantic in 3 formats (prose / flat JSON / nested JSON), 3 arms.
- E: two payloads x 3 repeats, 3 arms — determinism.
- F: 8 questions in one call vs single-question calls, 3 arms.
- Vision: 6 synthetic image cases on the two vision-capable arms.

Arms: ``gemini`` (gemini-decision-1), ``jev`` (typesafe-ai/jev via the gateway),
``clef-flash`` (@cf/cloudflare/clef-flash). Every arm receives byte-identical
state + questions; the only per-arm wire mapping is the yes/no type keyword
(``noul`` on Gemini/Clef, ``boolean`` on the Vercel evaluation API).

Guards: hard request cap (default 150, includes retries) and spend cap
(default USD 1.00, conservative per-call estimates) — both enforced before a
call is issued.

Usage:

    export GEMINI_API_KEY JEV_API_KEY CLOUDFLARE_ACCOUNT_ID CLOUDFLARE_API_TOKEN   # already set in the env
    python3 harness/gemini_eval.py --dry-run
    python3 harness/gemini_eval.py --out run-20261112-gemini-eval --phases smoke
    python3 harness/gemini_eval.py --out run-20261112-gemini-eval
    python3 harness/gemini_report.py results/run-20261112-gemini-eval
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import clef_client  # noqa: E402
import gemini_decisions_client as gemini_client  # noqa: E402
import jev_client  # noqa: E402
from vision_demo import CASES as VISION_CASES  # noqa: E402

REPO = Path(__file__).resolve().parent.parent
BATTERY = json.loads((REPO / "cases" / "stress_battery.json").read_text())
ASSETS = REPO / "assets"
RESULTS = REPO / "results"

ALL_ARMS = ("gemini", "jev", "clef-flash")
VISION_ARMS = ("gemini", "clef-flash")
REQUESTS_DEFAULT = 150
BUDGET_DEFAULT = 1.00


def utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def utc_compact() -> str:
    return time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())


def canonical_bytes(state) -> bytes:
    if isinstance(state, str):
        return state.encode("utf-8")
    return json.dumps(state, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def sha16(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()[:16]


def resolve_questions(case: dict) -> dict:
    if "questions" in case:
        return case["questions"]
    return BATTERY["question_sets"][case["questions_ref"]]


def map_questions_for_arm(questions: dict, arm: str) -> dict:
    """Only the yes/no keyword differs: ``noul`` (Gemini/Clef) <-> ``boolean`` (gateway)."""
    if arm != "jev":
        return questions
    mapped = {}
    for qid, q in questions.items():
        q2 = dict(q)
        if q2.get("type") == "noul":
            q2["type"] = "boolean"
        mapped[qid] = q2
    return mapped


def task(phase: str, case: dict, arm: str, *, state=None, questions=None,
         images=None, repeat_group=None, extra=None) -> dict:
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


def vision_case(name: str) -> dict:
    for case in VISION_CASES:
        if case["id"] == name:
            return case
    raise KeyError(name)


def build_smoke_tasks() -> list[dict]:
    by_id = {c["id"]: c for c in BATTERY["cases"]}
    a01 = by_id["A01"]
    vis = vision_case("ui-error")
    return [
        task("smoke", a01, "gemini"),
        task("smoke", a01, "jev"),
        task("smoke", a01, "clef-flash"),
        task("smoke", vis, "gemini", state=vis["state"], questions=vis["questions"],
             images=[str(ASSETS / "vision_demo" / vis["images"][0])]),
        task("smoke", vis, "clef-flash", state=vis["state"], questions=vis["questions"],
             images=[str(ASSETS / "vision_demo" / vis["images"][0])]),
    ]


def build_main_tasks() -> list[dict]:
    cases = {c["id"]: c for c in BATTERY["cases"]}
    tasks: list[dict] = []

    # A + C — every routing/tail/guardrail case, byte-identical per arm.
    for cid, case in cases.items():
        if cid.startswith(("A", "C")):
            for arm in ALL_ARMS:
                tasks.append(task("main", case, arm))

    # B — one semantic in three state formats.
    for cid in ("B1p", "B1f", "B1n"):
        for arm in ALL_ARMS:
            tasks.append(task("main", cases[cid], arm))

    # E — repeats for determinism (labels + numeric drift).
    det = BATTERY["determinism"]
    for ref in det["refs"]:
        for rep in range(1, det["repeats"] + 1):
            for arm in ALL_ARMS:
                tasks.append(task("main", cases[ref], arm, repeat_group=f"E:{ref}:{arm}:{rep}"))

    # F — batching: one 8-question call per arm, plus single-question calls.
    batch = BATTERY["batch"]
    batch_case = {"id": "F", "label": "batching", "state": batch["state"]}
    batch_questions = BATTERY["question_sets"][batch["questions_ref"]]
    singles_per_arm = {"gemini": 8, "jev": 4, "clef-flash": 4}
    for arm in ALL_ARMS:
        tasks.append(task("main", batch_case, arm, questions=batch_questions,
                          extra={"batch_mode": "all_in_one"}))
        for qid in batch["single_question_ids"][: singles_per_arm[arm]]:
            tasks.append(task("main", batch_case, arm, questions={qid: batch_questions[qid]},
                              extra={"batch_mode": "single", "single_question": qid}))

    # Vision — synthetic image gates (Jev has no image input; arm marked unsupported).
    for case in VISION_CASES:
        images = [str(ASSETS / "vision_demo" / name) for name in case["images"]]
        for arm in VISION_ARMS:
            tasks.append(task("main", case, arm, state=case["state"], questions=case["questions"],
                              images=images, extra={"use_case": case["use_case"]}))
    return tasks


def estimate_task_usd(t: dict, arm: str) -> float:
    payload = {"model": arm, "state": t["state"], "questions": t["questions"]}
    if t.get("images"):
        payload["images"] = ["data:image/png;base64,AAAA"] * len(t["images"])
    size = len(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
    if arm == "gemini":
        return gemini_client.cost_estimate_usd(size)
    if arm == "jev":
        return max(0.00006, jev_client.cost_estimate_usd(size))
    return clef_client.cost_estimate_usd(arm, size)


class Guards:
    def __init__(self, budget: float, max_requests: int, max_seconds: float):
        self.budget = budget
        self.max_requests = max_requests
        self.max_seconds = max_seconds
        self.spent = 0.0
        self.requests = 0
        self.attempts = 0
        self.t0 = time.time()

    def check(self, est_usd: float) -> str | None:
        if self.requests >= self.max_requests:
            return "request_cap"
        if self.spent + est_usd > self.budget:
            return "budget_cap"
        if time.time() - self.t0 > self.max_seconds:
            return "time_cap"
        return None

    def record(self, est_usd: float, attempts: int) -> None:
        self.spent += est_usd
        self.requests += 1
        self.attempts += max(1, attempts)


def execute(t: dict) -> dict:
    arm = t["arm"]
    t0 = time.time()
    questions = map_questions_for_arm(t["questions"], arm)
    state_b = canonical_bytes(t["state"])
    payload_b = state_b + json.dumps(questions, separators=(",", ":"), sort_keys=True).encode("utf-8")
    rec = {
        "ts": utc_now(),
        "phase": t["phase"],
        "case_id": t["case_id"],
        "label": t.get("label"),
        "arm": arm,
        "model": arm,
        "state_sha256": hashlib.sha256(state_b).hexdigest(),
        "request_sha256": hashlib.sha256(payload_b).hexdigest(),
        "repeat_group": t.get("repeat_group"),
        "batch_mode": t.get("batch_mode"),
        "single_question": t.get("single_question"),
        "expected": t.get("expected"),
        "expect_hold": t.get("expect_hold"),
        "note": t.get("note"),
        "use_case": t.get("use_case"),
        "n_questions": len(questions),
        "image_count": len(t.get("images") or []),
    }
    try:
        if arm == "gemini":
            out = gemini_client.call(t["state"], questions, images=t.get("images"))
        elif arm == "jev":
            out = jev_client.call(t["state"], questions)
        else:
            # Clef wants data URIs, not filesystem paths; Gemini reads bytes itself.
            imgs = t.get("images")
            if imgs:
                imgs = [p if p.startswith("data:") else clef_client.image_to_data_uri(p) for p in imgs]
            out = clef_client.call(arm, t["state"], questions, images=imgs)
        rec.update({
            "ok": out.get("ok"),
            "http_status": out.get("http_status"),
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
    except Exception as exc:  # harness failure: record, never abort the run
        rec.update({"ok": False, "error": f"harness_exception: {type(exc).__name__}: {exc}",
                    "latency_ms": (time.time() - t0) * 1000.0})
    return rec


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=f"run-{utc_compact()}-gemini-eval")
    ap.add_argument("--phases", default="smoke,main")
    ap.add_argument("--budget-usd", type=float, default=BUDGET_DEFAULT)
    ap.add_argument("--max-requests", type=int, default=REQUESTS_DEFAULT)
    ap.add_argument("--max-seconds", type=float, default=6600.0)
    ap.add_argument("--only-cases", default="", help="comma-separated case ids (supplementary reruns)")
    ap.add_argument("--only-arms", default="", help="comma-separated arms (supplementary reruns)")
    ap.add_argument("--append", action="store_true", help="append to an existing run's calls.jsonl")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    phases = [p.strip() for p in args.phases.split(",") if p.strip()]
    tasks: list[dict] = []
    if "smoke" in phases:
        tasks += build_smoke_tasks()
    if "main" in phases:
        tasks += build_main_tasks()

    if args.only_cases:
        keep = {c.strip() for c in args.only_cases.split(",") if c.strip()}
        tasks = [t for t in tasks if t["case_id"] in keep]
    if args.only_arms:
        keep = {a.strip() for a in args.only_arms.split(",") if a.strip()}
        tasks = [t for t in tasks if t["arm"] in keep]

    estimates = [estimate_task_usd(t, t["arm"]) for t in tasks]
    total_est = sum(estimates)
    by_arm: dict[str, int] = {}
    for t in tasks:
        by_arm[t["arm"]] = by_arm.get(t["arm"], 0) + 1
    print(f"planned requests={len(tasks)} by_arm={by_arm} est_cost=${total_est:.4f} "
          f"caps: requests<={args.max_requests} budget<=${args.budget_usd:.2f}")
    if args.dry_run:
        for t, est in zip(tasks, estimates):
            print(f"  {t['phase']:<5} {t['arm']:<10} {t['case_id']:<8} q={len(t['questions']):<2} "
                  f"img={len(t.get('images') or [])} est=${est:.6f}")
        return 0

    if args.max_requests < len(tasks):
        print(f"refusing: {len(tasks)} planned requests exceed --max-requests {args.max_requests}")
        return 2

    out_dir = RESULTS / args.out
    out_dir.mkdir(parents=True, exist_ok=True)
    calls_path = out_dir / "calls.jsonl"
    meta_path = out_dir / "meta.json"
    meta = {
        "run": args.out,
        "created_utc": utc_now(),
        "model": gemini_client.MODEL,
        "api_version": gemini_client.API_VERSION,
        "arms": list(ALL_ARMS),
        "phases": phases,
        "planned_requests": len(tasks),
        "by_arm": by_arm,
        "limits": {"max_requests": args.max_requests, "budget_usd": args.budget_usd,
                   "max_seconds": args.max_seconds},
        "pricing_usd_per_mtok_input": {
            "gemini": gemini_client.INPUT_RATE_USD_PER_MTOKEN,
            "jev": jev_client.INPUT_RATE_USD * 1_000_000,
            "clef-flash": clef_client.PRICE_PER_MTOKEN_USD["clef-flash"],
        },
        "battery_sha256": hashlib.sha256((REPO / "cases" / "stress_battery.json").read_bytes()).hexdigest(),
    }

    guards = Guards(args.budget_usd, args.max_requests, args.max_seconds)
    total = len(tasks)
    seq_offset = 0
    if args.append and calls_path.exists():
        with calls_path.open(encoding="utf-8") as existing:
            seq_offset = sum(1 for line in existing if line.strip())
    with calls_path.open("a" if args.append else "w", encoding="utf-8") as fh:
        for seq, (t, est) in enumerate(zip(tasks, estimates), start=1 + seq_offset):
            reason = guards.check(est)
            if reason:
                print(f"STOP at {seq}/{total}: {reason} (spent=${guards.spent:.4f}, "
                      f"requests={guards.requests}, attempts={guards.attempts})")
                break
            rec = execute(t)
            rec["seq"] = seq
            guards.record(rec.get("cost_estimate_usd") or est, rec.get("attempts") or 1)
            fh.write(json.dumps(rec, sort_keys=True) + "\n")
            fh.flush()
            lat = f"{rec['latency_ms']:.0f}ms" if rec.get("latency_ms") is not None else "n/a"
            flag = "ok" if rec.get("ok") else f"FAIL({rec.get('http_status') or '?'})"
            print(f"[{seq:>3}/{total}] {rec['phase']:<5} {rec['arm']:<10} {rec['case_id']:<8} "
                  f"{flag:<10} {lat:>9} ${(rec.get('cost_computed_usd') or 0):.6f}")

    meta["finished_utc"] = utc_now()
    meta["requests_issued"] = guards.requests
    meta["attempts_total"] = guards.attempts
    meta["spend_estimate_usd"] = round(guards.spent, 6)
    if args.append and meta_path.exists():
        previous = json.loads(meta_path.read_text(encoding="utf-8"))
        merged = dict(previous)
        merged["finished_utc"] = meta["finished_utc"]
        merged["requests_issued"] = int(previous.get("requests_issued", 0)) + guards.requests
        merged["attempts_total"] = int(previous.get("attempts_total", 0)) + guards.attempts
        merged["spend_estimate_usd"] = round(
            float(previous.get("spend_estimate_usd", 0.0)) + guards.spent, 6)
        merged.setdefault("supplementary", []).append({
            "finished_utc": meta["finished_utc"],
            "only_cases": args.only_cases,
            "only_arms": args.only_arms,
            "requests": guards.requests,
            "note": "rerun after a harness adapter fix (clef image bytes)",
        })
        meta = merged
    meta_path.write_text(json.dumps(meta, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"wrote {calls_path} and {meta_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
