from __future__ import annotations

import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

HARNESS = Path(__file__).resolve().parent
if str(HARNESS) not in sys.path:
    sys.path.insert(0, str(HARNESS))

import gemini_decisions_client
import gemini_eval


def fake_record(task: dict, attempts: int = 1) -> dict:
    return {
        "ts": "2026-01-01T00:00:00Z",
        "phase": task["phase"],
        "case_id": task["case_id"],
        "arm": task["arm"],
        "model": task["arm"],
        "ok": True,
        "http_status": 200,
        "latency_ms": 1.0,
        "attempts": attempts,
        "cost_estimate_usd": 0.0001,
        "cost_computed_usd": 0.0001,
        "answers": {},
    }


def seed_ledger(
    root: Path,
    out: str,
    *,
    requests: int,
    attempts: int,
    spend: float,
    external: dict | None = None,
) -> None:
    run_dir = root / out
    run_dir.mkdir(parents=True, exist_ok=True)
    metadata = {
        "run": out,
        "requests_issued": requests,
        "attempts_total": attempts,
        "spend_estimate_usd": spend,
        "supplementary": [],
    }
    if external is not None:
        metadata["external_probes"] = external
    (run_dir / "meta.json").write_text(json.dumps(metadata), encoding="utf-8")
    (run_dir / "calls.jsonl").write_text("", encoding="utf-8")


class CapsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.results = Path(self.tempdir.name)
        self.original_results = gemini_eval.RESULTS
        gemini_eval.RESULTS = self.results

    def tearDown(self) -> None:
        gemini_eval.RESULTS = self.original_results
        self.tempdir.cleanup()

    def run_append(self, out: str, *extra: str, cases: str = "C06") -> tuple[int, list, str]:
        issued: list[dict] = []

        def fake_execute(task: dict) -> dict:
            issued.append(task)
            return fake_record(task)

        old_argv, old_execute = sys.argv, gemini_eval.execute
        sys.argv = [
            "gemini_eval.py",
            "--out", out,
            "--phases", "main",
            "--only-cases", cases,
            "--only-arms", "gemini",
            "--append",
            *extra,
        ]
        gemini_eval.execute = fake_execute
        output = io.StringIO()
        try:
            with redirect_stdout(output):
                rc = gemini_eval.main()
        finally:
            sys.argv = old_argv
            gemini_eval.execute = old_execute
        return rc, issued, output.getvalue()

    def test_append_on_used_up_ledger_refuses_before_issuance(self) -> None:
        seed_ledger(self.results, "used", requests=150, attempts=150, spend=0.05)

        rc, issued, output = self.run_append("used")

        self.assertEqual(rc, 2)
        self.assertEqual(issued, [])
        self.assertEqual(len(output.strip().splitlines()), 1)
        self.assertIn("refusing:", output)

    def test_cumulative_overshoot_refuses_all_planned_tasks(self) -> None:
        seed_ledger(self.results, "overshoot", requests=149, attempts=149, spend=0.05)

        rc, issued, output = self.run_append("overshoot", cases="C06,C07")

        self.assertEqual(rc, 2)
        self.assertEqual(issued, [])
        self.assertIn("2 planned tasks", output)

    def test_retry_is_charged_before_second_provider_issuance(self) -> None:
        guards = gemini_eval.Guards(
            budget=1.0, max_requests=1, max_seconds=60.0)
        provider_calls: list[int] = []

        def fake_post(*_args) -> tuple[int, dict, float]:
            provider_calls.append(1)
            return 503, {"error": "temporary"}, 1.0

        def before_attempt() -> str | None:
            return guards.start_attempt(0.001)

        with (
            mock.patch.object(gemini_decisions_client, "_post", side_effect=fake_post),
            mock.patch.object(gemini_decisions_client.time, "sleep", return_value=None),
        ):
            result = gemini_decisions_client.call(
                "state",
                {"q": {"type": "noul", "instructions": "test"}},
                api_key="placeholder",
                attempts=3,
                retry_base=0,
                before_attempt=before_attempt,
            )

        self.assertEqual(len(provider_calls), 1)
        self.assertEqual(guards.attempts, 1)
        self.assertEqual(result["attempts"], 1)
        self.assertEqual(result["guard_stop_reason"], "request_cap")

    def test_external_probes_fold_into_cumulative_ledger(self) -> None:
        seed_ledger(
            self.results,
            "external",
            requests=5,
            attempts=6,
            spend=0.3,
            external={"attempts": 1, "spend_usd": 0.2},
        )
        estimate = gemini_eval.estimate_task_usd(
            next(t for t in gemini_eval.build_main_tasks()
                 if t["case_id"] == "C06" and t["arm"] == "gemini"),
            "gemini",
        )

        rc, issued, _ = self.run_append(
            "external", "--external-attempts", "2",
            "--external-spend-usd", "0.03",
        )

        metadata = json.loads((self.results / "external" / "meta.json").read_text())
        self.assertEqual(rc, 0)
        self.assertEqual(len(issued), 1)
        self.assertEqual(metadata["requests_issued"], 6)
        self.assertEqual(metadata["attempts_total"], 7)
        self.assertEqual(metadata["external_probes"], {"attempts": 3, "spend_usd": 0.23})
        self.assertEqual(metadata["spend_estimate_usd"], round(0.3 + estimate, 6))

    def test_note_is_recorded_verbatim(self) -> None:
        seed_ledger(self.results, "note", requests=1, attempts=1, spend=0.01)
        note = "gate: first pass with trailing spaces  "

        rc, issued, _ = self.run_append("note", "--note", note)

        metadata = json.loads((self.results / "note" / "meta.json").read_text())
        self.assertEqual(rc, 0)
        self.assertEqual(len(issued), 1)
        self.assertEqual(metadata["supplementary"][-1]["note"], note)

    def test_budget_cap_blocks_attempt_without_charging_it(self) -> None:
        guards = gemini_eval.Guards(
            budget=0.1,
            max_requests=150,
            max_seconds=60.0,
            requests=1,
            attempts=1,
            spent=0.1,
        )

        reason = guards.start_attempt(0.0001)

        self.assertEqual(reason, "budget_cap")
        self.assertEqual(guards.attempts, 1)
        self.assertEqual(guards.spent, 0.1)

    def test_append_ledger_rejects_boolean_counter(self) -> None:
        seed_ledger(self.results, "invalid", requests=1, attempts=1, spend=0.01)
        meta_path = self.results / "invalid" / "meta.json"
        metadata = json.loads(meta_path.read_text())
        metadata["attempts_total"] = True
        meta_path.write_text(json.dumps(metadata), encoding="utf-8")

        with self.assertRaises(ValueError):
            gemini_eval._load_append_ledger(
                meta_path, self.results / "invalid" / "calls.jsonl")


if __name__ == "__main__":
    unittest.main()
