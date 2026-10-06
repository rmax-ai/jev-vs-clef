# jev-vs-clef

A head-to-head stress comparison of two decision-model families on synthetic, production-shaped decisions:

- **Jev** — TypeSafe System One, called via the Vercel AI Gateway evaluation-model API (plain HTTP), `typesafe-ai/jev`.
- **Clef / Clef-flash** — Cloudflare Workers AI, `@cf/cloudflare/clef` (27B) and `@cf/cloudflare/clef-flash` (9B).

Cloudflare shipped Clef (Oct 1, 2026) as "fully Jev-API compatible" with vision input, a 64k context window, and Apache-2.0 open weights. This repo asks the operational question — *where is a swap safe, and what does it cost?* — on the bounded-decision workloads we actually run in production: ticket routing, agent session-tail triage, tool guardrails, upload triage, and candidate classification.

<!-- RESULTS:BEGIN -->
## Headline (two dated full runs: `run-20261003-main` launch window · `run-20261004-rerun`)

The identical 159-call battery was run twice from the same test environment: during Clef's launch week (2026-10-03) and again on 2026-10-06 (the rerun; directory label `run-20261004-rerun`). All metrics below are recomputed from each run's raw calls. Where a cell carries two values, the format is **launch · rerun**.

| metric | jev | clef (27B) | clef-flash (9B) |
|---|---:|---:|---:|
| latency p50 (test environment) | **0.3 s · 0.3 s** | 18.7 s · 18.6 s | 18.6 s · 18.5 s |
| avg cost / call (provider usage) | $0.000021 · $0.000021 | $0.000111 · $0.000113 | $0.000038 · $0.000039 |
| author-expected decision accuracy (n=24) | 92% · 83% | **96% · 96%** | 79% · 79% |
| question agreement vs jev (48 q, A+C) | — | **90% · 88%** | 85% · 83% |
| repeat determinism (×3) | labels stable, minor p-jitter | byte-identical | byte-identical |
| vision / 64-question / dotted ids / array state | — | ✔ | ✔ |

**Findings**

- **Hosted latency did not normalize.** The ~18–19 s clef response band measured during launch week reproduced in the full rerun three days later (p50 18.6 s / 18.5 s; 4-way parallel median 18.8 s vs sequential 18.6 s — 1.01×; launch week: sequential 18.8 s vs parallel 19.3 s — 1.03×). An operator probe on 2026-10-04 observed ~0.70 s / ~0.48 s medians; neither full run, nor a matched-shape 10-call-per-model probe at rerun time (medians ~18.7 s / ~18.7 s), reproduced sub-second medians. An intentionally invalid image request returned only after the same ~18.7 s delay — consistent with serving-path overhead rather than per-request compute, though the mechanism was not isolated. Treat hosted latency as time- and environment-dependent; re-measure from the intended region and concurrency profile before latency-sensitive use.
- **Clef 27B led the labeled subset in both runs** (96% in both runs vs Jev 92% → 83% and Clef-flash 79% on 24 author-expected question-level decisions — small-n; run-to-run movement of the labeled set is visible and expected at this size). Divergences clustered on the deliberately borderline tickets (mixed-topic routing, spam, security disclosure, feature request) — the cases a human should review anyway.
- **All three arms resisted both prompt-injection probes** (C06 urgency flip, C07 route flip): 6/6 held in both runs.
- **Format sensitivity is real for yes/no questions.** The same refund request scored "urgent" 0.90 (prose) / 0.86 (flat JSON) / 0.09–0.10 (nested JSON) on clef in both runs; the `rm -rf` destructive probe moved across arms on at least one format (clef 0.93 → 0.31–0.34 → 0.58). Choice and score answers were stable across formats. Keep the state format fixed per pipeline and re-validate when it changes.
- **Determinism differs.** Clef repeats are byte-identical (gate/cache friendly); Jev repeats drift numerically on probabilities (±0.01–0.05 across observed runs) with stable labels.
- **Clef adds vision** (3/3 on the synthetic shapes image, both sizes, both runs), 64-question requests, ids with dots/dashes, array states — and retains needles to ~13 KB states. The API does **not** enforce an escape label (a 2-option choice was accepted); an escape-label policy stays caller-side.
- **One wire gap in the "swap" story:** clef follows the System One API and expects `noul` — the native wire type for yes/no questions (TypeSafe's API and Cloudflare's docs agree). The Vercel AI Gateway evaluation API, the Jev path this harness uses, names the same type `boolean`; Vercel's docs bridge the two ("on the TypeSafe-compatible API, the equivalent question type is Noul"). Each surface rejects the other's keyword — re-verified live 2026-10-04. The harness maps this single field per arm.

Full tables: [launch run report](results/run-20261003-main/report.md) · [raw calls](results/run-20261003-main/calls.jsonl) · [rerun report](results/run-20261004-rerun/report.md) · [raw calls](results/run-20261004-rerun/calls.jsonl) · [delta table](results/run-20261004-rerun/delta-vs-20261003.md). Spend: **$0.0097 + $0.0099 computed / $0.017 + $0.017 conservative** across the two 159-call runs; no unexpected failures in either run (the only non-ok records are the three intentional rejection probes — raw-b64 image 422 ×2, `boolean` alias 400 ×1 per run).
<!-- RESULTS:END -->

## What was tested

159 live calls per run (two runs: `run-20261003-main`, 2026-10-03; `run-20261004-rerun`, executed 2026-10-06; identical battery, byte-identical battery hash recorded in each run's `meta.json`):

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

All states are synthetic; every arm receives byte-identical `state` + `questions` for a case (the single exception — the yes/no type keyword `noul` ↔ `boolean` — is mapped per arm and documented in [SPEC.md](SPEC.md)). Raw per-call evidence (request hashes, latencies, usage, full result bodies) lives in each run's `calls.jsonl`; the rerun's `provenance.json` records the harness hashes, repo SHA, and environment.

## Vision demo (clef-flash)

A companion demo runs clef-flash on six production-shaped vision gates — screenshot triage, a pre-click UI-automation guardrail, a dashboard alert gate, a two-screenshot change check, document-intake routing, and a photo-cull pre-filter. One call per case; each call batches its questions. All fixtures are synthetic and drawn from primitives.

Measured 2026-10-06 (`run-20261006-vision-demo`): 6/6 calls ok, 13/13 sanity checks, 4,710 input tokens, **$0.000424 computed (~$0.00007/call)** — see [VISION-DEMO.md](VISION-DEMO.md) for the transcript, cost model, and limits.

```bash
direnv exec . /usr/bin/python3 harness/vision_demo.py --dry-run   # list tasks + estimates, no calls
direnv exec . /usr/bin/python3 harness/vision_demo.py             # all cases, clef-flash (6 calls ≈ $0.0004)
direnv exec . /usr/bin/python3 harness/vision_demo.py --model clef # same payloads on the 27B arm
```

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

Requirements: Python ≥ 3.10 (stdlib only) and three environment variables — `CLOUDFLARE_ACCOUNT_ID`, `CLOUDFLARE_API_TOKEN` (Workers AI) and `JEV_API_KEY` (gateway). Copy `.envrc.example` → `.envrc` and fill it in, or export them yourself. `assets/shapes.png` is only needed for the vision case. The two committed runs used these commands as-is; the only harness difference between their commits is docstring/comment text (commit `5d31066`).

## Layout

```
cases/stress_battery.json  battery definition — states, questions, author-expected labels, generator specs
.envrc.example             required env vars template (copy to .envrc; .envrc is gitignored)
harness/clef_client.py     Cloudflare REST client (env-var creds; usage-based cost; retries on 429/5xx only)
harness/jev_client.py      Jev gateway HTTP client (env-var key; pinned evaluation-model v4 protocol)
harness/stress.py          runner — smoke / probe / main, spend + time guards, JSONL evidence
harness/report.py          analysis — agreement, stability, determinism, batching, edges → report.md
harness/vision_demo.py     clef-flash vision demo — 6 production-shaped gates + measured run
harness/make_vision_fixtures.py  regenerates assets/vision_demo/*.png (Pillow; draw-only, no external assets)
assets/vision_demo/        synthetic fixtures for the vision demo
results/                   committed runs: calls.jsonl + report.md + summary.json (+ provenance for the rerun)
SPEC.md                    test design, metrics definitions, wire notes, safety notes
VISION-DEMO.md             vision demo — use cases, measured transcript, cost model, limits
```

## Caveats — read before quoting numbers

- **Hosted latency is time-dependent; date it and re-measure.** Clef calls from our test environment took ~17–19 s each in *both* dated runs (launch week and the 2026-10-06 rerun), against Cloudflare's advertised 209/39 ms medians. A separate 2026-10-04 operator probe observed sub-second medians that neither the full runs nor a matched 10-call probe reproduced. The mechanism was not isolated; these figures describe specific measurement windows, not a stable platform property.
- **Agreement is not accuracy.** The battery measures cross-model agreement and stability; author labels exist only for clear-cut cases. Calibration requires outcome history, which synthetic data cannot provide.
- **Costs are provider-usage-based** (Clef reports `usage.input_tokens`; the gateway reports `inputTokens`/`outputTokens`, and the harness computes Jev cost at the pinned $0.042/M rate). Pre-call estimates in `calls.jsonl` use a conservative `bytes/1.5` heuristic and are upper bounds, especially for images.
- Synthetic data only; no customer, personal, or secret material. The "secret candidate" probe is a masked preview.

## Provenance

Built 2026-10-03; rerun and vision demo added 2026-10-06; harness and analysis written with AI assistance. Clef model documentation: <https://developers.cloudflare.com/workers-ai/models/clef/>. Jev: <https://www.typesafe.ai> (System One).
