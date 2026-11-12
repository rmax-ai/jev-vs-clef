# Gemini Decisions API — small evaluation (2026-11-12)

Bounded spot check of `gemini-decision-1` (Gemini Decisions API) against the two arms this repo already
measures, on the frozen battery in `cases/stress_battery.json`. Trigger:
`rmax-ai/delegation-queue#316`. Raw evidence: [`results/run-20261112-gemini-eval/`](results/run-20261112-gemini-eval/)
(`calls.jsonl`, `report.md`, `summary.json`, `meta.json`, `latency-crosscheck.txt`).

**Verdict: needs a larger eval — not adoptable for latency-sensitive gates yet, promising on quality,
cost, batching and multimodal input.** Three specific gaps are named at the end; each is a small
dollar-cost follow-up, not an architectural risk.

## What was verified before spending (official docs + live probes)

| item | evidence |
|---|---|
| Endpoint | `POST https://generativelanguage.googleapis.com/v1beta/decisions` — the path the `google-genai` 3.2.0 SDK itself calls (captured by pointing the SDK at a local listener), and HTTPS 200 live |
| Model | `GET /v1beta/models/gemini-decision-1` → `supportedGenerationMethods: ["createDecision"]`, `inputTokenLimit: 262144`, `outputTokenLimit: 1` |
| Auth | existing credential (`GEMINI_API_KEY` from the local password store) accepted on the first live call; no new billing or provisioning |
| Wire | `{"model", "input", "questions"}`; `noul` / `choice` / `score`; `x-goog-api-key` header; images as inline base64 content blocks |
| Pricing (docs) | $0.10 / M input tokens, output tokens always 0 and unbilled |
| Docs claims not taken on faith | 150–250 ms end-to-end, near-flat latency from 1→100 questions, "lowest calibration error", 5× cheaper than 3.5 Flash-Lite |

A wrong assumption here was caught cheaply: `:createDecision` is **not** a REST path (404); the SDK
posts to `/v1beta/decisions`.

## Method

Three arms, byte-identical state and questions, one mapped keyword (`noul` → `boolean` for the Vercel
evaluation API only). Sequential single calls from one host, no concurrency.

| group | cases | calls |
|---|---|---:|
| A routing + session tails | 14 (9 author-labeled, 5 deliberately borderline) | 42 |
| C guardrails | 9 (7 labeled, 2 prompt-injection holds) | 27 |
| B format sensitivity | 1 semantic × 3 state formats | 9 |
| E determinism | 2 payloads × 3 repeats | 18 |
| F batching | 8 questions in one call vs single-question calls | 19 |
| vision | 6 synthetic image gates (gemini, clef-flash; jev has no image input) | 12 |
| smoke / wiring | 1 text + 1 image case | 5 |

**Request accounting (cap: 150, retries included):** 132 planned + 6 supplementary (clef image rerun
after a data-URI bug) + 2 supplementary (gemini C06/C07 after a structured-state bug) + 2 latency
cross-checks + 8 endpoint/auth verification probes = **150**. Provider-side spend from reported usage:
**≈ $0.0040** (gemini $0.0017, clef-flash $0.0016, jev $0.0007), against a $1 cap. Wall clock: ~50 min.

Two harness defects consumed 19 rejected requests and 8 reruns. Both are fixed in this PR and both
failures remain visible in `calls.jsonl` next to their successful reruns — the evidence shows the
defect and the repair rather than a cleaned-up success.

## Results (recomputed from `calls.jsonl`)

### Health, latency, cost

| metric | gemini | jev | clef-flash |
|---|---:|---:|---:|
| requests ok / planned | 35/47 | 37/37 | 43/43 |
| p50 latency | 24,819 ms\* | 254 ms | 18,333 ms |
| p95 latency | 25,209 ms\* | 367 ms | 18,976 ms |
| computed spend | $0.001685 | $0.000695 | $0.001578 |
| cost / question | $0.000021 | $0.000009 | $0.000017 |
| provider errors / retries | 0 / 0 | 0 / 0 | 0 / 0 |

\* **Not provider latency.** Every Gemini call took 23.9–25.5 s regardless of payload (16–1,333 tokens,
1–8 questions). The identical request completes in **0.43 s via `curl`** and **0.26 s** through this
client after `prefer_ipv4()`. The cause is this host's DNS/route: `getaddrinfo` returns AAAA records
only and the IPv6 route blackholes, so `urllib` paid ~8 serial connect timeouts per request. Jev and
clef-flash were unaffected. **A p95 for Gemini was therefore not measured** — a 10-call sample is
~$0.0003 and was outside the request cap.

### Quality on author-labeled decisions

Same 9 cases / 24 question-level decisions, all three arms:

| arm | correct / 24 | accuracy | choice | noul | score | Brier (yes/no) |
|---|---:|---:|---:|---:|---:|---:|
| gemini | 20 | 83.3% | 9/9 | 7/9 | 4/6 | 0.158 |
| jev | 23 | 95.8% | 9/9 | 9/9 | 5/6 | 0.043 |
| clef-flash | 19 | 79.2% | 8/9 | 7/9 | 4/6 | 0.080 |

All four Gemini misses are one pattern: urgency/severity on the two tickets the author labeled
*not urgent* (A02, A03) — `urgent` 0.76 and 0.91 where the label is `false`, with severity rounded one
level high. Route (choice) is 9/9. clef-flash fails the same two tickets the same way (0.57, 0.62);
jev gets both right. That reads as a shared reading of "same-day attention" on soft-priority tickets,
not a Gemini-specific defect — and it is exactly the borderline a human gate exists for.

Confidence carries signal on both arms that report it: mean confidence on correct answers vs incorrect
— gemini 0.90 vs 0.64 (n=20/4), clef-flash 0.63 vs 0.22 (n=12/3; clef reports confidence on a
subset of answer types). Small n; not a calibration claim.

### Agreement with Gemini (labeled + borderline cases)

| arm | question match | all-question case match | mean abs Δp |
|---|---:|---:|---:|
| jev | 86.3% (51 q) | 72.2% (18 cases) | 0.094 |
| clef-flash | 87.7% (65 q) | 79.2% (24 cases) | 0.084 |

### Determinism (3 identical repeats)

| payload | gemini | jev | clef-flash |
|---|---|---|---|
| A01 (ticket) | labels stable, numeric spread 0.0 | stable, 0.0 | stable, 0.0 |
| C01 (destructive) | unmeasured (adapter defect) | **label changed**, spread 0.03 | stable, 0.0 |

Gemini was numerically identical across repeats on the case that was measured — gate/cache friendly,
like clef and unlike jev's ±0.01–0.05 drift.

### Batching (8 questions in one call vs 8 single-question calls)

| arm | agreement | latency ratio (batched / single) |
|---|---:|---:|
| gemini | 1.0 | 1.0 (uninformative — client-path floor) |
| jev | 1.0 | 1.46 |
| clef-flash | 1.0 | 0.92 |

Answer agreement is perfect for all three: batching does not change the decision. Gemini's advertised
"near-flat 1→100 questions" remains untested; the contaminated run cannot speak to it.

### Format sensitivity (one semantic: prose / flat JSON / nested JSON)

| arm | `route` | `urgent` | reading |
|---|---|---|---|
| gemini | billing (prose only) | 0.72 (prose only) | structured-state calls hit the adapter defect — unmeasured |
| jev | billing in all 3 | 0.56 / 0.48 / 0.28 | yes/no label flips at 0.5 — known format sensitivity |
| clef-flash | billing in all 3 | 0.60 / 0.53 / 0.22 | same, larger spread |

### Vision (six synthetic production-shaped gates)

Both vision-capable arms pass **13/13** sanity checks across 6 cases (error screenshot, checkout
pre-click, dashboard threshold, two-image change check, receipt intake, photo-cull). Jev is
unsupported, recorded as such rather than scored.

### Prompt injection

All three arms held both probes (urgency flip C06, route flip C07): `route=technical`,
`urgent≈0.95–0.99` in every arm. Gemini's 2/2 is measured after the adapter fix; the other 7 guardrail
cases are the named gap below.

## Limits

- n is small: 24 labeled question-level decisions common to all arms, 31 for the two arms without the
  adapter gap. This bounds precision, not recall, and cannot establish calibration.
- Agreement is not accuracy; author labels are a floor for clear-cut cases only.
- One host, one region, sequential calls. Hosted latency is time- and tier-dependent — this repo's clef
  numbers already showed an 18 s band that a lighter probe did not reproduce.
- Cost is computed from provider-reported tokens at list price; no invoice reconciliation.
- Images are synthetic; no customer, personal or secret material. The secret-candidate probe is masked.
- Prompt-injection resistance over two probes is a smoke test, not a security claim.

## Recommendation

**Needs a larger eval.** Do not put Gemini behind a latency-sensitive gate on this evidence, and do
not treat the advertised 150–250 ms as measured. Quality, cost/decision, determinism, batching
invariance and multimodal coverage are all competitive enough to justify the follow-up:

1. **Clean latency sample** — 10 calls, ~$0.0003, 5 s. Required before any latency claim; the fix is
   already in `harness/gemini_decisions_client.py`.
2. **Finish the guardrail group** — 8 calls, ~$0.0003 (C01–C05, C08, C09 + C01 repeats for the gemini arm).
3. Only then a 100+ case labeled set if a production swap is still on the table; calibration needs
   outcome history that synthetic data cannot provide.

## Reproduce

```bash
# one extra env var beyond the repo's three (never commit it):
export GEMINI_API_KEY           # value from the local password store

python3 harness/gemini_eval.py --dry-run                     # 132 tasks + guards, no calls
python3 harness/gemini_eval.py --out run-<label> --phases smoke
python3 harness/gemini_eval.py --out run-<label>               # request + spend caps enforced pre-call
python3 harness/gemini_report.py results/run-<label>           # -> report.md + summary.json
```

Supplementary reruns append to the same evidence file:

```bash
python3 harness/gemini_eval.py --out run-<label> --only-cases C01,C02,C03,C04,C05,C08,C09 \
  --only-arms gemini --append
```

Files: `harness/gemini_decisions_client.py` (stdlib REST adapter, usage-based cost, `prefer_ipv4`),
`harness/gemini_eval.py` (runner, request/spend/time guards), `harness/gemini_report.py` (analysis,
dedupes superseded attempts, recomputes every published number from `calls.jsonl`).

## Source pins

- Docs: <https://aistudio.google.com/docs/decisions-api> and <https://aistudio.google.com/docs/models/gemini-decision-1>
- Announcement: <https://blog.google/innovation-and-ai/technology/developers-tools/gemini-decisions-api/>
- SDK path confirmation: `google-genai` 3.2.0 (`client.decisions.create` → `POST /v1beta/decisions`)
- Battery hash: `fdcc8a762718848705a3db1b3130f941196fc981e8fdd74b3d52b575440cbe20`
