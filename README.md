# jev-vs-clef

A head-to-head stress comparison of two decision-model families on synthetic, production-shaped decisions:

- **Jev** — TypeSafe System One, called via the Vercel AI Gateway evaluation-model API (plain HTTP), `typesafe-ai/jev`.
- **Clef / Clef-flash** — Cloudflare Workers AI, `@cf/cloudflare/clef` (27B) and `@cf/cloudflare/clef-flash` (9B).

Cloudflare shipped Clef (Oct 1, 2026) as "fully Jev-API compatible" with vision input, a 64k context window, and Apache-2.0 open weights. This repo asks the operational question — *where is a swap safe, and what does it cost?* — on the bounded-decision workloads we actually run in production: ticket routing, agent session-tail triage, tool guardrails, upload triage, and candidate classification.

<!-- RESULTS:BEGIN -->
## Headline (run `run-20261003-main`, 2026-10-03 — launch window)

| metric | jev | clef (27B) | clef-flash (9B) |
|---|---:|---:|---:|
| latency p50 (test environment) | **0.3 s** | 18.7 s | 18.6 s |
| avg cost / call (provider usage) | **$0.000021** | $0.000111 | $0.000038 |
| labeled-case accuracy (n=24) | 92% | **96%** | 79% |
| question agreement vs jev (48 q, A+C) | — | **90%** (43/48) | 85% (41/48) |
| repeat determinism (×3) | labels stable, minor p-jitter | byte-identical | byte-identical |
| vision / 64-question / dotted ids / array state | — | ✔ | ✔ |

**Findings**

- **Clef 27B led the labeled subset** (96% vs 92% / 79% on 24 clear-cut questions — small-n) and matched Jev on 90% of questions across the routing + guardrail battery. Divergences clustered on the deliberately borderline tickets (mixed-topic routing, spam, security disclosure, feature request) — the cases a human should review anyway.
- **All three arms resisted both prompt-injection probes** (C06 urgency flip, C07 route flip): 6/6 held.
- **Format sensitivity is real for yes/no questions.** The same refund request scored "urgent" 0.90 (prose) / 0.86 (flat JSON) / 0.09 (nested JSON) on clef; the `rm -rf` destructive probe moved across arms on at least one format. Choice and score answers were stable across formats. Keep the state format fixed per pipeline and re-validate when it changes.
- **Determinism differs.** Clef repeats are byte-identical (gate/cache friendly); Jev repeats drift numerically on probabilities (±0.01–0.04 across observed runs) with stable labels.
- **Latency is queue-bound during launch week.** ~18–19 s per clef call from our test environment, identical under 4-way parallelism (1.05×) — scale-from-zero, not compute (advertised medians: 209 ms / 39 ms). Re-measure before latency-sensitive use.
- **Clef adds vision** (3/3 on the synthetic shapes image, both sizes), 64-question requests, ids with dots/dashes, array states — and retains needles to ~13 KB states. The API does **not** enforce an escape label (a 2-option choice was accepted); an escape-label policy stays caller-side.
- **One wire gap in the "swap" story:** clef follows the System One API and expects `noul` — the native wire type for yes/no questions (TypeSafe's API and Cloudflare's docs agree). The Vercel AI Gateway evaluation API, the Jev path this harness uses, names the same type `boolean`; Vercel's docs bridge the two ("on the TypeSafe-compatible API, the equivalent question type is Noul"). Each surface rejects the other's keyword — re-verified live 2026-10-04. The harness maps this single field per arm.

Full tables: [results/run-20261003-main/report.md](results/run-20261003-main/report.md) · raw evidence: [calls.jsonl](results/run-20261003-main/calls.jsonl) · spend: **$0.0097 computed / $0.017 conservative estimate** across 159 calls, no unexpected failures.
<!-- RESULTS:END -->

## What was tested

159 live calls (run `run-20261003-main`, 2026-10-03):

| group | calls | coverage |
|---|---:|---|
| A | 42 | 10 support tickets + 4 agent session tails × 3 arms |
| B | 27 | format sensitivity: prose vs flat JSON vs nested JSON × 3 semantics × 3 arms |
| C | 27 | guardrails: destructive booleans, PII booleans, prompt-injection holds, masked secret classification |
| D | 9 | scale: same needle questions at ~1 KB / ~4 KB / ~12 KB states |
| E | 18 | determinism: 2 payloads × 3 repeats × 3 arms |
| F | 18 | batching: 8 questions in one call vs 8 single calls |
| G | 8 | edges: weird ids, 64-question max, choice without escape, bad-image 422, vision, array state, `boolean` alias |
| smoke/probe | 10 | wiring gates + 4-way concurrency probe |

All states are synthetic; every arm receives byte-identical `state` + `questions` for a case (the single exception — the yes/no type keyword `noul` ↔ `boolean` — is mapped per arm and documented in [SPEC.md](SPEC.md)). Raw per-call evidence (request hashes, latencies, usage, full result bodies) lives in the run's `calls.jsonl`.

## Reproduce

```bash
cp .envrc.example .envrc          # add your keys
direnv allow                      # if you use the direnv shell hook
# ...or wrap each command with `direnv exec .` as shown here:

direnv exec . /usr/bin/python3 harness/stress.py --dry-run          # enumerate tasks + cost guard check
direnv exec . /usr/bin/python3 harness/stress.py --phases smoke     # 4 live calls (wiring check)
direnv exec . /usr/bin/python3 harness/stress.py --out run-<label>  # full battery (default budget $0.25)
direnv exec . /usr/bin/python3 harness/report.py results/run-<label>
```

Requirements: Python ≥ 3.10 (stdlib only) and three environment variables — `CLOUDFLARE_ACCOUNT_ID`, `CLOUDFLARE_API_TOKEN` (Workers AI) and `JEV_API_KEY` (gateway). Copy `.envrc.example` → `.envrc` and fill it in, or export them yourself. `assets/shapes.png` is only needed for the vision case.

## Layout

```
cases/stress_battery.json  battery definition — states, questions, author-expected labels, generator specs
.envrc.example             required env vars template (copy to .envrc; .envrc is gitignored)
harness/clef_client.py     Cloudflare REST client (env-var creds; usage-based cost; retries on 429/5xx only)
harness/jev_client.py      Jev gateway HTTP client (env-var key; pinned evaluation-model v4 protocol)
harness/stress.py          runner — smoke / probe / main, spend + time guards, JSONL evidence
harness/report.py          analysis — agreement, stability, determinism, batching, edges → report.md
results/                   committed run(s): calls.jsonl + report.md + summary.json
SPEC.md                    test design, metrics definitions, wire notes, safety notes
```

## Caveats — read before quoting numbers

- **Launch-window latency.** Clef calls from our test environment took ~17–19 s each (sequential *and* 4-way parallel), against Cloudflare's advertised 209/39 ms medians. This is consistent with scale-from-zero queueing during launch week, not model compute. Re-measure before any latency-sensitive use.
- **Agreement is not accuracy.** The battery measures cross-model agreement and stability; author labels exist only for clear-cut cases. Calibration requires outcome history, which synthetic data cannot provide.
- **Costs are provider-usage-based** (Clef reports `usage.input_tokens`; the gateway reports `inputTokens`/`outputTokens`, and the harness computes Jev cost at the pinned $0.042/M rate). Pre-call estimates in `calls.jsonl` use a conservative `bytes/1.5` heuristic and are upper bounds, especially for images.
- Synthetic data only; no customer, personal, or secret material. The "secret candidate" probe is a masked preview.

## Provenance

Built 2026-10-03; harness and analysis written with AI assistance. Clef model documentation: <https://developers.cloudflare.com/workers-ai/models/clef/>. Jev: <https://www.typesafe.ai> (System One).
