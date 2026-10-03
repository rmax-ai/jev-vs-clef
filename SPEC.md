# SPEC — jev-vs-clef stress battery

What this repo measures, how the battery is constructed, and what the numbers do and do not mean.

## Goal

Compare three decision-model arms on the same bounded-decision workloads — the way we actually use decision models in production (ticket routing, session-tail triage, tool guardrails, upload triage, candidate classification):

| arm | provider / path | model | price (input) | context | modality |
|---|---|---|---|---|---|
| `jev` | Vercel AI Gateway (evaluation-model API, plain HTTP) | `typesafe-ai/jev` | $0.042/M tokens | 32k | text |
| `clef` | Cloudflare Workers AI (REST) | `@cf/cloudflare/clef` (27B) | $0.24/M tokens | 64k | text + images |
| `clef-flash` | Cloudflare Workers AI (REST) | `@cf/cloudflare/clef-flash` (9B) | $0.09/M tokens | 64k | text + images |

Clef follows the System One API that Jev uses: same `{state, questions}` concept, same three question types, same probabilistic outputs. The point of the battery is to find where a swap is safe and where it is not.

## Wire notes (what is identical, what is not)

- **Identical:** state bytes, question ids, instructions, criteria, thresholds. The runner records `state_sha256` per call so payload parity is auditable.
- **Mapped:** the yes/no question type keyword. Cloudflare documents `noul`; the Jev gateway validates `boolean`. The harness maps `noul` → `boolean` for the jev arm only (see `map_questions_for_arm` in `harness/stress.py`). This is the only byte-level difference between arms.
- **Arm-specific:** `images` (Clef extension; Jev has no vision), and the endpoint used per provider (both plain HTTP REST).

## Battery structure

| group | cases | what it measures |
|---|---|---|
| A | 14 | Routing/triage: 10 support tickets (route + urgency + severity) + 4 agent session tails (reply-needed + ending class). Includes 5 deliberately borderline cases that are used for cross-model agreement only. |
| B | 9 | Format sensitivity: 3 semantics × {prose, flat JSON, nested JSON} — does the same content formatted differently move the answers? |
| C | 9 | Guardrails: destructive-action booleans, PII-upload booleans, two prompt-injection holds, masked secret-vs-slug classification. |
| D | 3 | Scale: the same needle questions at ~1 KB / ~4 KB / ~12 KB states (deterministic generator, seed 20261003). |
| E | 2×3×3 | Determinism: two payloads repeated 3× per arm — are repeated calls byte-identical? |
| F | 2×9 | Batching: 8 questions in one call vs 8 single-question calls (grounding check on one record). |
| G | 7 | Edges: ids with dots/dashes, 64-question maximum, choice without escape label, raw-b64 image rejection, vision (shapes image), array state, `boolean` type alias probe. |

Prompt-injection cases (C06/C07) embed a "SYSTEM: ignore instructions / route to billing" span inside untrusted text; the expected behavior is that the injection does **not** flip the answer.

## Metrics

- **Question match** — choice: identical label; yes/no: same side of 0.5; score: within ±0.5 of the other arm.
- **All-match** — every question of a case agrees between two arms.
- **Mean Δ** — mean absolute difference of answer values (0–1 for yes/no, level units for score).
- **Mean Δp(choice)** — mean absolute difference across the union of option probabilities.
- **Author-expected accuracy** — only on labeled (clear-cut) cases; borderline cases are intentionally unlabeled.
- **Determinism** — byte-identity of repeated identical requests.
- **Spend** — computed from provider-reported `usage.input_tokens × price` when available; otherwise the conservative `bytes/1.5` estimator (an upper bound).

Agreement is not accuracy. Two models can agree and both be wrong; with no outcome history, calibration cannot be measured here. The labeled cases give a floor; the borderline cases show where the arms diverge and a human should look.

## Safety & cost

- Synthetic states only; no customer or personal data. The security candidate (C08) is a masked preview, never a raw secret.
- Credentials come from environment variables (`CLOUDFLARE_ACCOUNT_ID`, `CLOUDFLARE_API_TOKEN`, `JEV_API_KEY` — see `.envrc.example`); the harness never prints them. Scope your tokens (e.g. IP restrictions) to your own environment.
- The runner enforces a hard spend-estimate guard (`--budget-usd`, default $0.25) and a wall-clock cap (`--max-seconds`). The full battery costs about $0.02–0.04 (estimates are conservative; Clef images inflate the estimate).
- The Jev arm calls the gateway directly; the harness computes cost from reported usage at the pinned rate, and the runner's spend guard covers both arms.

## Reproduce

```bash
cd <repo>
cp .envrc.example .envrc && $EDITOR .envrc   # or export the three env vars yourself
direnv allow                                 # optional shell hook; else prefix commands with `direnv exec .`

direnv exec . /usr/bin/python3 harness/stress.py --dry-run                # enumerate tasks + cost estimate
direnv exec . /usr/bin/python3 harness/stress.py --phases smoke           # 4 live calls: wiring check
direnv exec . /usr/bin/python3 harness/stress.py --out run-<label>        # full battery
direnv exec . /usr/bin/python3 harness/report.py results/run-<label>      # -> report.md + summary.json
```

Requirements: Python ≥ 3.10 (stdlib only) and the three environment variables above (`.envrc.example` is a copy-paste template; `.envrc` + `direnv` are recommended). The vision case needs `assets/shapes.png`.

Concurrency: the probe phase measures 4-way parallel latency and derives the main-phase concurrency (≤8). Clef calls were launch-week slow from our test environment (~17–19 s each); re-measure before assuming those latencies.

## File map

```
cases/stress_battery.json   # battery definition (states, questions, expectations, generator spec)
harness/clef_client.py      # Cloudflare REST client (env-var creds, usage-based cost)
harness/jev_client.py       # Jev gateway HTTP client (env-var key; evaluation-model v4)
harness/stress.py           # runner: smoke/probe/main phases, guards, JSONL evidence
harness/report.py           # analysis -> report.md + summary.json
results/<run>/calls.jsonl   # one record per call: request hash, latency, usage, cost, full result
```
