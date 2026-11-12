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
import math
import os
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
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
_ACTIVE_BEFORE_ATTEMPT: Callable[[], str | None] | None = None


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


@dataclass(frozen=True)
class Reservation:
    seq: int
    attempt_index: int


@dataclass(frozen=True)
class LedgerSeed:
    metadata: dict | None
    requests_issued: int
    attempts_total: int
    spend_estimate_usd: float
    external_attempts: int
    external_spend_usd: float
    journal_lines: int
    unreconciled_attempts: int
    unreconciled_requests: int
    reservations: tuple[Reservation, ...]


def _ledger_int(source: dict, key: str) -> int:
    value = source.get(key, 0)
    if type(value) is not int or value < 0:
        raise ValueError(f"append ledger field {key} must be a non-negative integer")
    return value


def _ledger_spend(source: dict, key: str) -> float:
    value = source.get(key, 0.0)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"append ledger field {key} must be a non-negative number")
    amount = float(value)
    if not math.isfinite(amount) or amount < 0:
        raise ValueError(f"append ledger field {key} must be a finite non-negative number")
    return amount


def _load_reservations(path: Path) -> list[Reservation]:
    """Parse the persisted JSONL boundary into strictly typed reservations."""
    if not path.exists():
        return []

    reservations: list[Reservation] = []
    try:
        with path.open(encoding="utf-8") as fh:
            for line_number, line in enumerate(fh, start=1):
                if not line.strip():
                    raise ValueError(f"reservation journal line {line_number} is empty")
                try:
                    raw = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise ValueError(
                        f"reservation journal line {line_number} is not valid JSON"
                    ) from exc
                if not isinstance(raw, dict) or set(raw) != {"seq", "attempt_index"}:
                    raise ValueError(
                        f"reservation journal line {line_number} must contain seq and attempt_index"
                    )
                seq = raw["seq"]
                attempt_index = raw["attempt_index"]
                if type(seq) is not int or seq < 1:
                    raise ValueError(
                        f"reservation journal line {line_number} seq must be a positive integer"
                    )
                if type(attempt_index) is not int or attempt_index < 1:
                    raise ValueError(
                        f"reservation journal line {line_number} attempt_index must be a positive integer"
                    )
                reservations.append(Reservation(seq, attempt_index))
    except UnicodeDecodeError as exc:
        raise ValueError("reservation journal must be UTF-8") from exc
    return reservations


def _append_reservation(path: Path, seq: int, attempt_index: int) -> None:
    """Durably append one reservation without replacing existing evidence."""
    line = json.dumps(
        {"seq": seq, "attempt_index": attempt_index},
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8") + b"\n"
    with path.open("a+b") as fh:
        fh.seek(0, os.SEEK_END)
        if fh.tell():
            fh.seek(-1, os.SEEK_END)
            if fh.read(1) != b"\n":
                fh.seek(0, os.SEEK_END)
                fh.write(b"\n")
        fh.seek(0, os.SEEK_END)
        if fh.write(line) != len(line):
            raise OSError("short write to reservation journal")
        fh.flush()
        os.fsync(fh.fileno())


def _load_append_ledger(
    meta_path: Path,
    calls_path: Path,
    reservations_path: Path | None = None,
) -> LedgerSeed:
    if reservations_path is None:
        reservations_path = meta_path.with_name("reservations.jsonl")
    reservations = _load_reservations(reservations_path)
    journal_lines = len(reservations)

    if not meta_path.exists():
        if calls_path.exists() and calls_path.stat().st_size:
            raise ValueError("append metadata is missing for an existing calls ledger")
        return LedgerSeed(
            None, 0, 0, 0.0, 0, 0.0,
            journal_lines, journal_lines, len({item.seq for item in reservations}),
            tuple(reservations),
        )

    metadata = json.loads(meta_path.read_text(encoding="utf-8"))
    if not isinstance(metadata, dict):
        raise ValueError("append metadata must be a JSON object")
    external = metadata.get("external_probes", {})
    if not isinstance(external, dict):
        raise ValueError("append ledger field external_probes must be an object")
    supplementary = metadata.get("supplementary", [])
    if not isinstance(supplementary, list):
        raise ValueError("append ledger field supplementary must be a list")
    reconciled_lines = _ledger_int(metadata, "reservation_lines")
    unreconciled = reservations[reconciled_lines:] if journal_lines > reconciled_lines else []
    return LedgerSeed(
        metadata=metadata,
        requests_issued=_ledger_int(metadata, "requests_issued"),
        attempts_total=_ledger_int(metadata, "attempts_total"),
        spend_estimate_usd=_ledger_spend(metadata, "spend_estimate_usd"),
        external_attempts=_ledger_int(external, "attempts"),
        external_spend_usd=_ledger_spend(external, "spend_usd"),
        journal_lines=journal_lines,
        unreconciled_attempts=max(0, journal_lines - reconciled_lines),
        unreconciled_requests=len({item.seq for item in unreconciled}),
        reservations=tuple(reservations),
    )


class Guards:
    def __init__(
        self,
        budget: float,
        max_requests: int,
        max_seconds: float,
        *,
        requests: int = 0,
        attempts: int = 0,
        spent: float = 0.0,
        external_attempts: int = 0,
    ):
        self.budget = budget
        self.max_requests = max_requests
        self.max_seconds = max_seconds
        self.spent = spent
        self.requests = requests
        self.attempts = attempts
        self.external_attempts = external_attempts
        self.t0 = time.time()

    def check(self, est_usd: float) -> str | None:
        if max(self.requests, self.attempts) + self.external_attempts >= self.max_requests:
            return "request_cap"
        if self.spent + est_usd > self.budget:
            return "budget_cap"
        if time.time() - self.t0 > self.max_seconds:
            return "time_cap"
        return None

    def start_attempt(
        self,
        est_usd: float,
        *,
        before_issue: Callable[[], None] | None = None,
    ) -> str | None:
        reason = self.check(est_usd)
        if reason:
            return reason
        if before_issue is not None:
            before_issue()
        self.attempts += 1
        self.spent += est_usd
        return None

    def record_request(self) -> None:
        self.requests += 1


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
        "attempts": 0,
    }
    try:
        if arm == "gemini":
            out = gemini_client.call(
                t["state"], questions, images=t.get("images"),
                before_attempt=_ACTIVE_BEFORE_ATTEMPT)
        elif arm == "jev":
            out = jev_client.call(
                t["state"], questions, before_attempt=_ACTIVE_BEFORE_ATTEMPT)
        else:
            # Clef wants data URIs, not filesystem paths; Gemini reads bytes itself.
            imgs = t.get("images")
            if imgs:
                imgs = [p if p.startswith("data:") else clef_client.image_to_data_uri(p) for p in imgs]
            out = clef_client.call(
                arm, t["state"], questions, images=imgs,
                before_attempt=_ACTIVE_BEFORE_ATTEMPT)
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
            "guard_stop_reason": out.get("guard_stop_reason"),
            "answers": out.get("answers"),
            "errors": out.get("errors"),
            "body": out.get("body"),
        })
    except Exception as exc:  # harness failure: record, never abort the run
        rec.update({"ok": False, "error": f"harness_exception: {type(exc).__name__}: {exc}",
                    "latency_ms": (time.time() - t0) * 1000.0})
    return rec


def main() -> int:
    global _ACTIVE_BEFORE_ATTEMPT
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=f"run-{utc_compact()}-gemini-eval")
    ap.add_argument("--phases", default="smoke,main")
    ap.add_argument("--budget-usd", type=float, default=BUDGET_DEFAULT)
    ap.add_argument("--max-requests", type=int, default=REQUESTS_DEFAULT)
    ap.add_argument("--max-seconds", type=float, default=6600.0)
    ap.add_argument("--only-cases", default="", help="comma-separated case ids (supplementary reruns)")
    ap.add_argument("--only-arms", default="", help="comma-separated arms (supplementary reruns)")
    ap.add_argument("--append", action="store_true", help="append to an existing run's calls.jsonl")
    ap.add_argument("--note", default="", help="verbatim note for this supplementary append")
    ap.add_argument("--external-attempts", type=int, default=0)
    ap.add_argument("--external-spend-usd", type=float, default=0.0)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    if args.external_attempts < 0:
        ap.error("--external-attempts must be non-negative")
    if not math.isfinite(args.external_spend_usd) or args.external_spend_usd < 0:
        ap.error("--external-spend-usd must be finite and non-negative")

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
    plan = (f"planned requests={len(tasks)} by_arm={by_arm} est_cost=${total_est:.4f} "
            f"caps: attempts<={args.max_requests} budget<=${args.budget_usd:.2f}")
    if args.dry_run:
        print(plan)
        for t, est in zip(tasks, estimates):
            print(f"  {t['phase']:<5} {t['arm']:<10} {t['case_id']:<8} q={len(t['questions']):<2} "
                  f"img={len(t.get('images') or [])} est=${est:.6f}")
        return 0

    out_dir = RESULTS / args.out
    calls_path = out_dir / "calls.jsonl"
    meta_path = out_dir / "meta.json"
    reservations_path = out_dir / "reservations.jsonl"
    if not args.append:
        try:
            has_calls = calls_path.exists() and calls_path.stat().st_size > 0
            has_reservations = (
                reservations_path.exists() and reservations_path.stat().st_size > 0
            )
            if meta_path.exists() or has_calls or has_reservations:
                print("refusing: --out already contains run evidence; use --append")
                return 2
        except OSError as exc:
            print(f"refusing: could not inspect existing output ({exc})")
            return 2
    try:
        ledger = (
            _load_append_ledger(meta_path, calls_path, reservations_path)
            if args.append else LedgerSeed(None, 0, 0, 0.0, 0, 0.0, 0, 0, 0, ())
        )
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"refusing: could not load append ledger ({exc})")
        return 2

    external_attempts = ledger.external_attempts + args.external_attempts
    external_spend = ledger.external_spend_usd + args.external_spend_usd
    prior_spend = ledger.spend_estimate_usd + ledger.external_spend_usd + args.external_spend_usd
    guards = Guards(
        args.budget_usd,
        args.max_requests,
        args.max_seconds,
        requests=ledger.requests_issued + ledger.unreconciled_requests,
        attempts=ledger.attempts_total + ledger.unreconciled_attempts,
        spent=prior_spend,
        external_attempts=external_attempts,
    )
    prior_attempts = max(guards.requests, guards.attempts) + guards.external_attempts
    minimum_total = prior_attempts + len(tasks)
    if minimum_total > args.max_requests:
        print(f"refusing: {prior_attempts} prior attempts + {len(tasks)} planned tasks "
              f"exceed --max-requests {args.max_requests}")
        return 2

    print(plan)
    out_dir.mkdir(parents=True, exist_ok=True)
    reservation_line_count = ledger.journal_lines
    accounted_request_sequences = {item.seq for item in ledger.reservations}
    reservation_attempt_indices: dict[int, int] = {}
    for reservation in ledger.reservations:
        reservation_attempt_indices[reservation.seq] = max(
            reservation_attempt_indices.get(reservation.seq, 0),
            reservation.attempt_index,
        )

    def persist_reservation(seq: int, attempt_index: int) -> None:
        nonlocal reservation_line_count
        _append_reservation(reservations_path, seq, attempt_index)
        reservation_line_count += 1
        reservation_attempt_indices[seq] = max(
            reservation_attempt_indices.get(seq, 0),
            attempt_index,
        )

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

    total = len(tasks)
    seq_offset = 0
    if args.append and calls_path.exists():
        with calls_path.open(encoding="utf-8") as existing:
            seq_offset = sum(1 for line in existing if line.strip())
    starting_requests = guards.requests
    with calls_path.open("a" if args.append else "w", encoding="utf-8") as fh:
        for seq, (t, est) in enumerate(zip(tasks, estimates), start=1 + seq_offset):
            reason = guards.check(est)
            if reason:
                print(f"STOP at {seq}/{total}: {reason} (spent=${guards.spent:.4f}, "
                      f"requests={guards.requests}, attempts={guards.attempts})")
                break
            attempts_before = guards.attempts
            stop = {"reason": None}
            attempt_index = reservation_attempt_indices.get(seq, 0)

            def before_attempt() -> str | None:
                nonlocal attempt_index
                next_attempt_index = attempt_index + 1
                try:
                    why = guards.start_attempt(
                        est,
                        before_issue=lambda: persist_reservation(seq, next_attempt_index),
                    )
                except OSError:
                    stop["reason"] = "reservation_journal"
                    raise
                if why:
                    stop["reason"] = why
                else:
                    attempt_index = next_attempt_index
                return why

            _ACTIVE_BEFORE_ATTEMPT = before_attempt
            try:
                rec = execute(t)
            finally:
                _ACTIVE_BEFORE_ATTEMPT = None

            reported_attempts = rec.get("attempts", 0)
            if type(reported_attempts) is not int or reported_attempts < 0:
                reported_attempts = 0
            charged_attempts = guards.attempts - attempts_before
            for _ in range(max(0, reported_attempts - charged_attempts)):
                next_attempt_index = attempt_index + 1
                try:
                    why = guards.start_attempt(
                        est,
                        before_issue=lambda: persist_reservation(seq, next_attempt_index),
                    )
                except OSError:
                    stop["reason"] = stop["reason"] or "reservation_journal"
                    break
                if why:
                    stop["reason"] = stop["reason"] or why
                    break
                attempt_index = next_attempt_index
            charged_attempts = guards.attempts - attempts_before
            rec["attempts"] = charged_attempts
            if stop["reason"]:
                rec["guard_stop_reason"] = stop["reason"]
            if charged_attempts and seq not in accounted_request_sequences:
                guards.record_request()
                accounted_request_sequences.add(seq)

            if charged_attempts or (rec.get("error") and not stop["reason"]):
                rec["seq"] = seq
                fh.write(json.dumps(rec, sort_keys=True) + "\n")
                fh.flush()
                lat = f"{rec['latency_ms']:.0f}ms" if rec.get("latency_ms") is not None else "n/a"
                flag = "ok" if rec.get("ok") else f"FAIL({rec.get('http_status') or '?'})"
                print(f"[{seq:>3}/{total}] {rec['phase']:<5} {rec['arm']:<10} {rec['case_id']:<8} "
                      f"{flag:<10} {lat:>9} ${(rec.get('cost_computed_usd') or 0):.6f}")
            if stop["reason"]:
                print(f"STOP at {seq}/{total}: {stop['reason']} (spent=${guards.spent:.4f}, "
                      f"requests={guards.requests}, attempts={guards.attempts})")
                break

    meta["finished_utc"] = utc_now()
    meta["requests_issued"] = guards.requests
    meta["attempts_total"] = guards.attempts
    meta["spend_estimate_usd"] = round(guards.spent - external_spend, 6)
    meta["external_probes"] = {
        "attempts": external_attempts,
        "spend_usd": round(external_spend, 6),
    }
    if args.append and ledger.metadata is not None:
        merged = dict(ledger.metadata)
        merged["finished_utc"] = meta["finished_utc"]
        merged["requests_issued"] = guards.requests
        merged["attempts_total"] = guards.attempts
        merged["reservation_lines"] = reservation_line_count
        merged["spend_estimate_usd"] = round(guards.spent - external_spend, 6)
        merged["external_probes"] = meta["external_probes"]
        merged.setdefault("supplementary", []).append({
            "finished_utc": meta["finished_utc"],
            "only_cases": args.only_cases,
            "only_arms": args.only_arms,
            "requests": guards.requests - starting_requests,
            "note": args.note,
        })
        meta = merged
    elif args.append:
        meta["supplementary"] = [{
            "finished_utc": meta["finished_utc"],
            "only_cases": args.only_cases,
            "only_arms": args.only_arms,
            "requests": guards.requests - starting_requests,
            "note": args.note,
        }]
    meta["reservation_lines"] = reservation_line_count
    meta_path.write_text(json.dumps(meta, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"wrote {calls_path} and {meta_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
