from __future__ import annotations

import io
import json
import subprocess
import sys
import tempfile
import textwrap
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
    reservation_lines: int = 0,
) -> None:
    run_dir = root / out
    run_dir.mkdir(parents=True, exist_ok=True)
    metadata = {
        "run": out,
        "requests_issued": requests,
        "attempts_total": attempts,
        "spend_estimate_usd": spend,
        "supplementary": [],
        "reservation_lines": reservation_lines,
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

    def run_append(
        self,
        out: str,
        *extra: str,
        cases: str = "C06",
        append: bool = True,
    ) -> tuple[int, list, str]:
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
        ]
        if append:
            sys.argv.append("--append")
        sys.argv.extend(extra)
        gemini_eval.execute = fake_execute
        output = io.StringIO()
        try:
            with redirect_stdout(output):
                rc = gemini_eval.main()
        finally:
            sys.argv = old_argv
            gemini_eval.execute = old_execute
        return rc, issued, output.getvalue()

    def run_latency_sample(
        self,
        out: str,
        calls: int,
        *,
        max_requests: int = 150,
    ) -> tuple[int, list[int], str]:
        provider_calls: list[int] = []

        def fake_post(*_args) -> tuple[int, dict, float]:
            provider_calls.append(1)
            return 200, {
                "answers": {"route": "technical"},
                "usage": {"total_input_tokens": 261},
            }, 1.0

        old_argv = sys.argv
        sys.argv = [
            "gemini_eval.py",
            "--out", out,
            "--latency-sample", str(calls),
            "--append",
            "--max-requests", str(max_requests),
        ]
        output = io.StringIO()
        try:
            with (
                mock.patch.object(gemini_decisions_client, "load_api_key",
                                  return_value="offline-placeholder"),
                mock.patch.object(gemini_decisions_client, "_post", side_effect=fake_post),
                redirect_stdout(output),
            ):
                rc = gemini_eval.main()
        finally:
            sys.argv = old_argv
        return rc, provider_calls, output.getvalue()

    def run_boundary_only_append(self, out: str, boundary: str) -> tuple[int, str]:
        old_argv = sys.argv
        sys.argv = [
            "gemini_eval.py",
            "--out", out,
            "--phases", "",
            "--only-cases", "__none__",
            "--append",
            "--client-path-boundary-utc", boundary,
        ]
        output = io.StringIO()
        try:
            with redirect_stdout(output):
                rc = gemini_eval.main()
        finally:
            sys.argv = old_argv
        return rc, output.getvalue()

    def test_append_records_the_effective_limits(self) -> None:
        seed_ledger(
            self.results, "limits",
            requests=10, attempts=10, spend=0.01,
            external={"attempts": 5, "spend_usd": 0.001},
        )

        rc, issued, _ = self.run_append(
            "limits", "--max-requests", "40", "--budget-usd", "2.5")

        self.assertEqual(rc, 0)
        self.assertEqual(len(issued), 1)
        meta = json.loads((self.results / "limits" / "meta.json").read_text(encoding="utf-8"))
        self.assertEqual(meta["limits"]["max_requests"], 40)
        self.assertEqual(meta["limits"]["budget_usd"], 2.5)
        self.assertEqual(meta["external_probes"]["attempts"], 5)

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

    def test_append_charges_unreconciled_attempts_and_distinct_sequences(self) -> None:
        seed_ledger(
            self.results,
            "reconcile",
            requests=7,
            attempts=8,
            spend=0.1,
            reservation_lines=1,
        )
        journal_path = self.results / "reconcile" / "reservations.jsonl"
        journal_path.write_text(
            '{"seq": 8, "attempt_index": 1, "est_usd": 0.01}\n'
            '{"seq": 9, "attempt_index": 1, "est_usd": 0.02}\n'
            '{"seq": 9, "attempt_index": 2, "est_usd": 0.03}\n',
            encoding="utf-8",
        )

        ledger = gemini_eval._load_append_ledger(
            self.results / "reconcile" / "meta.json",
            self.results / "reconcile" / "calls.jsonl",
            journal_path,
        )

        self.assertEqual(ledger.journal_lines, 3)
        self.assertEqual(ledger.unreconciled_attempts, 2)
        self.assertEqual(ledger.unreconciled_requests, 1)
        self.assertAlmostEqual(ledger.unreconciled_spend_usd, 0.05)

    def test_reservation_journal_rejects_invalid_records(self) -> None:
        seed_ledger(self.results, "invalid-journal", requests=1, attempts=1, spend=0.01)
        run_dir = self.results / "invalid-journal"
        journal_path = run_dir / "reservations.jsonl"
        invalid_lines = (
            "not-json\n",
            "[]\n",
            '{"attempt_index": 1, "est_usd": 0.01}\n',
            '{"seq": true, "attempt_index": 1, "est_usd": 0.01}\n',
            '{"seq": 1, "attempt_index": 1, "est_usd": 0.01, "extra": "value"}\n',
            '{"seq": 1, "attempt_index": 1}\n',
            '{"seq": 1, "attempt_index": 1, "est_usd": true}\n',
            '{"seq": 1, "attempt_index": 1, "est_usd": "0.01"}\n',
            '{"seq": 1, "attempt_index": 1, "est_usd": null}\n',
            '{"seq": 1, "attempt_index": 1, "est_usd": -0.01}\n',
            '{"seq": 1, "attempt_index": 1, "est_usd": NaN}\n',
            '{"seq": 1, "attempt_index": 1, "est_usd": Infinity}\n',
            '{"seq": 1, "attempt_index": 1, "est_usd": 1e999}\n',
        )

        for line in invalid_lines:
            with self.subTest(line=line):
                journal_path.write_text(line, encoding="utf-8")
                with self.assertRaises(ValueError):
                    gemini_eval._load_append_ledger(
                        run_dir / "meta.json",
                        run_dir / "calls.jsonl",
                        journal_path,
                    )

    def test_retry_attempts_are_each_reserved(self) -> None:
        task = next(
            t for t in gemini_eval.build_main_tasks()
            if t["case_id"] == "C06" and t["arm"] == "gemini"
        )
        estimate = gemini_eval.estimate_task_usd(task, "gemini")
        responses = [
            (503, {"error": "temporary"}, 1.0),
            (200, {"answers": {}}, 1.0),
        ]
        provider_calls: list[int] = []

        def fake_post(*_args) -> tuple[int, dict, float]:
            provider_calls.append(1)
            return responses[len(provider_calls) - 1]

        old_argv = sys.argv
        sys.argv = [
            "gemini_eval.py",
            "--out", "retry-journal",
            "--phases", "main",
            "--only-cases", "C06",
            "--only-arms", "gemini",
            "--append",
            "--max-requests", "5",
        ]
        try:
            with (
                mock.patch.object(gemini_decisions_client, "load_api_key",
                                  return_value="offline-placeholder"),
                mock.patch.object(gemini_decisions_client, "_post", side_effect=fake_post),
                mock.patch.object(gemini_decisions_client.time, "sleep", return_value=None),
                redirect_stdout(io.StringIO()),
            ):
                rc = gemini_eval.main()
        finally:
            sys.argv = old_argv

        run_dir = self.results / "retry-journal"
        reservations = [
            json.loads(line)
            for line in (run_dir / "reservations.jsonl").read_text(encoding="utf-8").splitlines()
        ]
        call = json.loads((run_dir / "calls.jsonl").read_text(encoding="utf-8"))
        metadata = json.loads((run_dir / "meta.json").read_text(encoding="utf-8"))
        self.assertEqual(rc, 0)
        self.assertEqual(len(provider_calls), 2)
        self.assertEqual(
            [(record["seq"], record["attempt_index"]) for record in reservations],
            [(1, 1), (1, 2)],
        )
        for record in reservations:
            self.assertEqual(set(record), {"seq", "attempt_index", "est_usd"})
            self.assertAlmostEqual(record["est_usd"], estimate)
        self.assertEqual(call["attempts"], 2)
        self.assertEqual(metadata["requests_issued"], 1)
        self.assertEqual(metadata["attempts_total"], 2)
        self.assertEqual(metadata["reservation_lines"], 2)

    def test_subprocess_kill_preserves_reservation_for_append(self) -> None:
        seed_ledger(self.results, "killed", requests=149, attempts=149, spend=0.04)
        marker = self.results / "provider-called"
        child = textwrap.dedent(
            """
            import importlib.util, os, pathlib, sys
            repo = pathlib.Path(sys.argv[1])
            root = pathlib.Path(sys.argv[2])
            marker = pathlib.Path(sys.argv[3])
            spec = importlib.util.spec_from_file_location(
                "gemini_eval_kill", repo / "harness" / "gemini_eval.py")
            m = importlib.util.module_from_spec(spec)
            sys.modules["gemini_eval_kill"] = m
            spec.loader.exec_module(m)
            m.RESULTS = root
            m.gemini_client.load_api_key = lambda: "offline-placeholder"

            def fake_post(*_args):
                marker.write_text("provider call completed", encoding="utf-8")
                return 200, {"answers": {}}, 1.0

            m.gemini_client._post = fake_post
            execute = m.execute

            def kill_after_provider(task):
                execute(task)
                os._exit(9)

            m.execute = kill_after_provider
            sys.argv = ["gemini_eval.py", "--out", "killed", "--phases", "main",
                        "--only-cases", "C06", "--only-arms", "gemini", "--append"]
            m.main()
            """
        )
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                child,
                str(gemini_eval.REPO),
                str(self.results),
                str(marker),
            ],
            cwd=str(gemini_eval.REPO),
            capture_output=True,
            text=True,
            timeout=30,
        )

        journal_path = self.results / "killed" / "reservations.jsonl"
        raw_journal = journal_path.read_bytes()
        rows = raw_journal.decode("utf-8").splitlines()
        self.assertEqual(result.returncode, 9, result.stdout + result.stderr)
        self.assertEqual(marker.read_text(encoding="utf-8"), "provider call completed")
        self.assertTrue(raw_journal.endswith(b"\n"))
        self.assertEqual(len(rows), 1)
        reservation = json.loads(rows[0])
        estimate = gemini_eval.estimate_task_usd(
            next(t for t in gemini_eval.build_main_tasks()
                 if t["case_id"] == "C06" and t["arm"] == "gemini"),
            "gemini",
        )
        self.assertEqual(set(reservation), {"seq", "attempt_index", "est_usd"})
        self.assertEqual((reservation["seq"], reservation["attempt_index"]), (1, 1))
        self.assertAlmostEqual(reservation["est_usd"], estimate)

        rc, issued, output = self.run_append("killed")

        self.assertEqual(rc, 2)
        self.assertEqual(issued, [])
        self.assertIn("refusing:", output)

        rc, issued, _ = self.run_append("killed", "--max-requests", "151")

        metadata = json.loads((self.results / "killed" / "meta.json").read_text())
        records = [
            json.loads(line)
            for line in journal_path.read_text(encoding="utf-8").splitlines()
        ]
        self.assertEqual(rc, 0)
        self.assertEqual(len(issued), 1)
        self.assertEqual(
            [(record["seq"], record["attempt_index"]) for record in records],
            [(1, 1), (1, 2)],
        )
        self.assertEqual(metadata["requests_issued"], 150)
        self.assertEqual(metadata["attempts_total"], 151)
        self.assertEqual(metadata["reservation_lines"], 2)

    def test_killed_budget_reservation_blocks_restart(self) -> None:
        task = next(
            t for t in gemini_eval.build_main_tasks()
            if t["case_id"] == "C06" and t["arm"] == "gemini"
        )
        estimate = gemini_eval.estimate_task_usd(task, "gemini")
        self.assertGreater(estimate, 0)
        budget = round(estimate * 1.5, 12)
        seed_ledger(
            self.results, "budget-killed",
            requests=0, attempts=0, spend=0.0,
        )

        child = textwrap.dedent(
            """
            import importlib.util, os, pathlib, sys
            repo = pathlib.Path(sys.argv[1])
            root = pathlib.Path(sys.argv[2])
            spec = importlib.util.spec_from_file_location(
                "gemini_eval_budget_kill", repo / "harness" / "gemini_eval.py")
            m = importlib.util.module_from_spec(spec)
            sys.modules["gemini_eval_budget_kill"] = m
            spec.loader.exec_module(m)
            m.RESULTS = root

            def kill_after_reservation(task):
                m._ACTIVE_BEFORE_ATTEMPT()
                os._exit(9)

            m.execute = kill_after_reservation
            sys.argv = ["gemini_eval.py", "--out", "budget-killed", "--phases", "main",
                        "--only-cases", "C06", "--only-arms", "gemini", "--append",
                        "--budget-usd", sys.argv[3]]
            m.main()
            """
        )
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                child,
                str(gemini_eval.REPO),
                str(self.results),
                repr(budget),
            ],
            cwd=str(gemini_eval.REPO),
            capture_output=True,
            text=True,
            timeout=30,
        )
        journal_path = self.results / "budget-killed" / "reservations.jsonl"
        rows = [
            json.loads(line)
            for line in journal_path.read_text(encoding="utf-8").splitlines()
        ]

        self.assertEqual(result.returncode, 9, result.stdout + result.stderr)
        self.assertEqual(len(rows), 1)
        self.assertEqual(set(rows[0]), {"seq", "attempt_index", "est_usd"})
        self.assertAlmostEqual(rows[0]["est_usd"], estimate)

        rc, issued, output = self.run_append(
            "budget-killed", "--budget-usd", repr(budget)
        )

        self.assertEqual(rc, 0)
        self.assertEqual(issued, [])
        self.assertIn("budget_cap", output)

    def test_clean_run_append_does_not_double_charge_reconciled_spend(self) -> None:
        task = next(
            t for t in gemini_eval.build_main_tasks()
            if t["case_id"] == "C06" and t["arm"] == "gemini"
        )
        estimate = gemini_eval.estimate_task_usd(task, "gemini")
        wide_budget = round(estimate * 2.5, 12)

        first_rc, first_issued, _ = self.run_append(
            "reconciled-spend", "--budget-usd", "10.0", append=False
        )
        second_rc, second_issued, _ = self.run_append(
            "reconciled-spend", "--budget-usd", repr(wide_budget)
        )
        journal_path = self.results / "reconciled-spend" / "reservations.jsonl"
        rows = [
            json.loads(line)
            for line in journal_path.read_text(encoding="utf-8").splitlines()
        ]
        metadata = json.loads(
            (self.results / "reconciled-spend" / "meta.json").read_text(encoding="utf-8")
        )

        self.assertEqual(first_rc, 0)
        self.assertEqual(len(first_issued), 1)
        self.assertEqual(second_rc, 0)
        self.assertEqual(len(second_issued), 1)
        self.assertEqual(len(rows), 2)
        self.assertTrue(all(set(row) == {"seq", "attempt_index", "est_usd"} for row in rows))
        for row in rows:
            self.assertAlmostEqual(row["est_usd"], estimate)
        self.assertEqual(metadata["reservation_lines"], 2)
        self.assertEqual(metadata["attempts_total"], 2)

    def test_out_reuse_without_append_refuses_and_preserves_evidence(self) -> None:
        seed_ledger(self.results, "reuse", requests=5, attempts=5, spend=0.01)
        reuse_dir = self.results / "reuse"
        (reuse_dir / "calls.jsonl").write_text('{"seq": 1, "ok": true}\n', encoding="utf-8")
        (reuse_dir / "reservations.jsonl").write_text(
            '{"seq": 1, "attempt_index": 1, "est_usd": 0.01}\n',
            encoding="utf-8",
        )
        before = {path.name: path.read_bytes() for path in reuse_dir.iterdir() if path.is_file()}

        rc, issued, output = self.run_append("reuse", append=False)

        after = {path.name: path.read_bytes() for path in reuse_dir.iterdir() if path.is_file()}
        self.assertEqual(rc, 2)
        self.assertEqual(issued, [])
        self.assertEqual(len(output.strip().splitlines()), 1)
        self.assertEqual(before, after)

        orphan_dir = self.results / "orphan"
        orphan_dir.mkdir()
        (orphan_dir / "calls.jsonl").write_text("", encoding="utf-8")
        (orphan_dir / "reservations.jsonl").write_text(
            '{"seq": 1, "attempt_index": 1, "est_usd": 0.01}\n',
            encoding="utf-8",
        )
        orphan_before = {
            path.name: path.read_bytes()
            for path in orphan_dir.iterdir()
            if path.is_file()
        }

        orphan_rc, orphan_issued, orphan_output = self.run_append("orphan", append=False)

        orphan_after = {
            path.name: path.read_bytes()
            for path in orphan_dir.iterdir()
            if path.is_file()
        }
        self.assertEqual(orphan_rc, 2)
        self.assertEqual(orphan_issued, [])
        self.assertEqual(len(orphan_output.strip().splitlines()), 1)
        self.assertEqual(orphan_before, orphan_after)

    def test_latency_reservation_and_case_filter_refusal(self) -> None:
        conflict_rc, conflict_issued, conflict_output = self.run_append(
            "latency-conflict", "--latency-sample", "1", append=False)
        self.assertEqual(conflict_rc, 2)
        self.assertEqual(conflict_issued, [])
        self.assertEqual(len(conflict_output.strip().splitlines()), 1)
        self.assertFalse((self.results / "latency-conflict").exists())

        seed_ledger(self.results, "latency", requests=0, attempts=0, spend=0.0)
        rc, provider_calls, _ = self.run_latency_sample(
            "latency", 2, max_requests=2)
        run_dir = self.results / "latency"
        rows = [
            json.loads(line)
            for line in (run_dir / "calls.jsonl").read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        reservations = [
            json.loads(line)
            for line in (run_dir / "reservations.jsonl").read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        estimate = gemini_eval.estimate_task_usd(
            gemini_eval.build_latency_tasks(1)[0], "gemini")

        self.assertEqual(rc, 0)
        self.assertEqual(len(provider_calls), 2)
        self.assertEqual(len(rows), 2)
        self.assertTrue(all(row["phase"] == "latency" for row in rows))
        self.assertTrue(all(row["case_id"] == "LAT1" for row in rows))
        self.assertTrue(all(
            row["client_path"] == gemini_decisions_client.CLIENT_PATH_VERSION
            for row in rows
        ))
        self.assertEqual(len(reservations), 2)
        for reservation in reservations:
            self.assertEqual(set(reservation), {"seq", "attempt_index", "est_usd"})
            self.assertAlmostEqual(reservation["est_usd"], estimate)

    def test_latency_kill_and_restart_keep_reservation(self) -> None:
        seed_ledger(self.results, "latency-killed", requests=0, attempts=0, spend=0.0)
        marker = self.results / "latency-provider-called"
        child = textwrap.dedent(
            """
            import importlib.util, os, pathlib, sys
            repo = pathlib.Path(sys.argv[1])
            root = pathlib.Path(sys.argv[2])
            marker = pathlib.Path(sys.argv[3])
            spec = importlib.util.spec_from_file_location(
                "gemini_eval_latency_kill", repo / "harness" / "gemini_eval.py")
            m = importlib.util.module_from_spec(spec)
            sys.modules["gemini_eval_latency_kill"] = m
            spec.loader.exec_module(m)
            m.RESULTS = root
            m.gemini_client.load_api_key = lambda: "offline-placeholder"

            def fake_post(*_args):
                marker.write_text("provider call completed", encoding="utf-8")
                os._exit(9)

            m.gemini_client._post = fake_post
            sys.argv = ["gemini_eval.py", "--out", "latency-killed",
                        "--latency-sample", "2", "--append", "--max-requests", "2"]
            m.main()
            """
        )
        killed = subprocess.run(
            [
                sys.executable,
                "-c",
                child,
                str(gemini_eval.REPO),
                str(self.results),
                str(marker),
            ],
            cwd=str(gemini_eval.REPO),
            capture_output=True,
            text=True,
            timeout=30,
        )
        run_dir = self.results / "latency-killed"
        journal_path = run_dir / "reservations.jsonl"
        calls_path = run_dir / "calls.jsonl"
        before_journal = journal_path.read_bytes()
        before_calls = calls_path.read_bytes()
        journal_rows = [json.loads(line) for line in before_journal.decode().splitlines()]
        estimate = gemini_eval.estimate_task_usd(
            gemini_eval.build_latency_tasks(1)[0], "gemini")

        self.assertEqual(killed.returncode, 9, killed.stdout + killed.stderr)
        self.assertEqual(marker.read_text(encoding="utf-8"), "provider call completed")
        self.assertEqual(len(journal_rows), 1)
        self.assertAlmostEqual(journal_rows[0]["est_usd"], estimate)
        self.assertEqual(before_calls, b"")

        restart_rc, restart_calls, restart_output = self.run_latency_sample(
            "latency-killed", 2, max_requests=2)
        self.assertEqual(restart_rc, 2)
        self.assertEqual(restart_calls, [])
        self.assertEqual(len(restart_output.strip().splitlines()), 1)
        self.assertEqual(journal_path.read_bytes(), before_journal)
        self.assertEqual(calls_path.read_bytes(), before_calls)

    def test_latency_cohorts_split_at_client_path_boundary(self) -> None:
        run_dir = self.results / "cohort"
        run_dir.mkdir()
        boundary = "2026-11-12T12:08:00Z"
        (run_dir / "meta.json").write_text(json.dumps({
            "run": "cohort",
            "client_path": "ipv4-first-v1",
            "client_path_boundary_utc": boundary,
            "requests_issued": 4,
            "attempts_total": 4,
            "spend_estimate_usd": 0.0,
            "external_probes": {"attempts": 0, "spend_usd": 0.0},
            "supplementary": [],
        }), encoding="utf-8")

        def row(ts: str, case_id: str, latency_ms: float) -> str:
            return json.dumps({
                "seq": case_id,
                "ts": ts,
                "phase": "main",
                "case_id": case_id,
                "arm": "gemini",
                "model": "gemini",
                "ok": True,
                "http_status": 200,
                "latency_ms": latency_ms,
                "attempts": 1,
                "cost_estimate_usd": 0.0,
                "cost_computed_usd": 0.0,
                "answers": {},
                "n_questions": 1,
            }) + "\n"

        (run_dir / "calls.jsonl").write_text(
            row("2026-11-12T12:06:00Z", "C06", 200.0)
            + row("2026-11-12T12:07:00Z", "C07", 400.0)
            + row("2026-11-12T12:08:00Z", "A01", 30000.0)
            + row("2026-11-12T12:08:01Z", "A02", 250.0),
            encoding="utf-8",
        )
        (run_dir / "latency-crosscheck.txt").write_text(
            "post-fix client call recorded after the boundary\n",
            encoding="utf-8",
        )
        report = gemini_eval.REPO / "harness" / "gemini_report.py"
        proc = subprocess.run(
            [sys.executable, str(report), str(run_dir)],
            cwd=str(gemini_eval.REPO),
            capture_output=True,
            text=True,
            timeout=30,
        )
        summary = json.loads((run_dir / "summary.json").read_text(encoding="utf-8"))
        cohorts = summary["arms"]["gemini"]["latency_cohorts"]
        report_text = (run_dir / "report.md").read_text(encoding="utf-8")

        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(cohorts["pre_fix"], {
            "n": 2, "p50": 200.0, "p95": 400.0, "min": 200.0, "max": 400.0,
        })
        self.assertEqual(cohorts["post_fix"], {
            "n": 2, "p50": 250.0, "p95": 30000.0,
            "min": 250.0, "max": 30000.0,
        })
        self.assertIn(boundary, report_text)
        self.assertIn("latency-crosscheck.txt", report_text)
        self.assertIn("30000 ms", report_text)

    def test_client_path_boundary_is_immutable(self) -> None:
        seed_ledger(self.results, "boundary-only", requests=7, attempts=7, spend=0.1)
        boundary_only_dir = self.results / "boundary-only"
        original_meta = json.loads(
            (boundary_only_dir / "meta.json").read_text(encoding="utf-8"))
        original_calls = (boundary_only_dir / "calls.jsonl").read_bytes()
        rc, output = self.run_boundary_only_append(
            "boundary-only", "2026-11-12T12:08:00Z")
        updated_meta = json.loads(
            (boundary_only_dir / "meta.json").read_text(encoding="utf-8"))
        self.assertEqual(rc, 0)
        self.assertEqual(len(output.strip().splitlines()), 1)
        self.assertEqual(updated_meta["client_path"], "ipv4-first-v1")
        self.assertEqual(
            updated_meta["client_path_boundary_utc"], "2026-11-12T12:08:00Z")
        self.assertTrue(all(
            updated_meta[key] == value for key, value in original_meta.items()
        ))
        self.assertEqual(
            (boundary_only_dir / "calls.jsonl").read_bytes(), original_calls)
        self.assertFalse((boundary_only_dir / "reservations.jsonl").exists())

        seed_ledger(self.results, "boundary", requests=0, attempts=0, spend=0.0)
        first_rc, first_calls, _ = self.run_append(
            "boundary", "--client-path-boundary-utc", "2026-11-12T12:08:00Z")
        run_dir = self.results / "boundary"
        meta_path = run_dir / "meta.json"
        self.assertEqual(first_rc, 0)
        self.assertEqual(len(first_calls), 1)
        first_meta = json.loads(meta_path.read_text(encoding="utf-8"))
        self.assertEqual(
            first_meta["client_path_boundary_utc"], "2026-11-12T12:08:00Z")
        self.assertEqual(first_meta["client_path"], "ipv4-first-v1")
        before = {path.name: path.read_bytes() for path in run_dir.iterdir() if path.is_file()}

        refused_rc, refused_calls, refused_output = self.run_append(
            "boundary", "--client-path-boundary-utc", "2026-11-13T00:00:00Z")
        after = {path.name: path.read_bytes() for path in run_dir.iterdir() if path.is_file()}
        self.assertEqual(refused_rc, 2)
        self.assertEqual(refused_calls, [])
        self.assertEqual(len(refused_output.strip().splitlines()), 1)
        self.assertEqual(before, after)

        invalid_rc, invalid_calls, invalid_output = self.run_append(
            "boundary", "--client-path-boundary-utc", "2026-11-12T12:08:00+00:00")
        invalid_after = {
            path.name: path.read_bytes() for path in run_dir.iterdir() if path.is_file()
        }
        self.assertEqual(invalid_rc, 2)
        self.assertEqual(invalid_calls, [])
        self.assertEqual(len(invalid_output.strip().splitlines()), 1)
        self.assertEqual(before, invalid_after)


if __name__ == "__main__":
    unittest.main()
