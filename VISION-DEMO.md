# Vision demo — clef-flash gates on screenshots and photos

Six production-shaped vision gates on six synthetic fixtures. One call per case (one case sends two
images per call). Each call batches its questions — the way these gates run in a pipeline.

Measured run `run-20261006-vision-demo` on `clef-flash` (9B, $0.09/M input tokens): **6/6 calls ok,
13/13 sanity checks, 4,710 input tokens, $0.000424 computed (~$0.00007/call), 111 s wall
(~18.5 s/call, hosted).**

The demo shows where a cheap vision arm fits: first-pass gates that route work, pre-filters that
protect expensive passes, and UI checks before automation clicks. The final action always stays in
deterministic caller policy. The model only produces probabilities.

## Quick start

```bash
direnv exec . /usr/bin/python3 harness/vision_demo.py --dry-run   # task list + estimates, no calls
direnv exec . /usr/bin/python3 harness/vision_demo.py             # all cases, clef-flash (6 calls, ≈ $0.0004)
direnv exec . /usr/bin/python3 harness/vision_demo.py --model clef # same payloads on the 27B arm
direnv exec . /usr/bin/python3 harness/vision_demo.py --cases receipt,photo-blur
```

Flags: `--model`, `--cases`, `--out`, `--budget-usd` (hard cap on computed spend; default $0.05),
`--dry-run`. Evidence lands in `results/vision-demo-<label>/{calls.jsonl,summary.json}`.

## Use cases

| # | case | pipeline role | questions (one call) | measured answer |
|---|---|---|---|---|
| 1 | `ui-error` | bug-inbox triage: classify a screenshot | error state? · owning team · severity | error `true` (0.93) · `backend_bug` (0.95) · severity 2.69 |
| 2 | `ui-checkout` | UI-automation guardrail before a click | button visible? · enabled? · page state | `true` (0.97) · `true` (0.96) · `ready` (0.94) |
| 3 | `dash-above` | monitoring gate: read a chart screenshot | above threshold? · on-call status | `true` (0.97) · status 1.97 (`degraded`) |
| 4 | `dash-pair` | change check across two screenshots | crossed threshold? · direction | `true` (0.88) · `rising` (0.92) |
| 5 | `receipt` | document-intake routing | document type · OCR-ready? | `receipt` (0.94) · `true` (0.91) |
| 6 | `photo-blur` | photo-cull pre-filter before analysis | in focus? · usable candidate? | `false` (0.02) · `false` (0.02) |

The fixtures (`assets/vision_demo/`) are drawn from primitives — no external assets, no real data.
Regenerate with `python3 harness/make_vision_fixtures.py` (needs Pillow; draw-only).

## Measured transcript (2026-10-06)

```
[1/6] ui-error     ok=True wall=16.5s tok=800 $0.000072
      is_error_state     true p=0.93 ✓
      route              backend_bug p=0.95 ✓
      severity           2.69 ✓
[2/6] ui-checkout  ok=True wall=18.7s tok=824 $0.000074
      target_visible     true p=0.97 ✓
      target_enabled     true p=0.96 ✓
      page_state         ready p=0.94 ✓
[3/6] dash-above   ok=True wall=18.9s tok=625 $0.000056
      above_threshold    true p=0.97 ✓
      status             1.97
[4/6] dash-pair    ok=True wall=19.4s tok=1008 $0.000091
      crossed_threshold  true p=0.88 ✓
      direction          rising p=0.92 ✓
[5/6] receipt      ok=True wall=19.0s tok=794 $0.000071
      doc_type           receipt p=0.94 ✓
      ocr_ready          true p=0.91 ✓
[6/6] photo-blur   ok=True wall=18.6s tok=659 $0.000059
      in_focus           false p=0.02 ✓
      usable             false p=0.02 ✓

== run-20261006-vision-demo ==
model clef-flash · ok 6/6 · checks 13/13
tokens in 4,710 · computed $0.000424 · conservative est $0.017785 · wall 111s
```

## What the numbers say

- **Per-call cost: $0.000056–0.000091** (average ≈ $0.00007), measured from provider usage. Only
  input tokens are billed; image tokens count into `usage.input_tokens`.
- **Volume anchor:** 10,000 gated calls per day ≈ **$0.70/day** on clef-flash. At the same payloads
  the 27B arm costs ≈ $0.00019/call (2.7×) — run `--model clef` to measure it.
- **The pre-call estimate ($0.0178) is an upper bound**, inflated by base64 image bytes. Use the
  computed provider number for planning; use the estimate only as a spend guard.
- **Latency: 16.5–19.4 s per call** from this environment — the same hosted band as the
  [battery](results/run-20261003-main/report.md) (the 2026-10-06 rerun reproduced it). Fine for
  asynchronous gates; not for interactive use. Re-measure from the deployment region.
- **One record per call; batch related questions.** Do not compress several records into one call.
- **Multi-image works:** case 4 sends two screenshots in one call. The API accepts up to 4 images.

## Limits and policy

- The sanity checks are n=1 per case and illustrative. This is a demonstration, not an evaluation.
  The [battery](README.md) is the measured comparison; there, clef-flash was the weakest arm on
  borderline cases (for example, a text-only destructive probe scored 0.10 where the 27B arm said
  0.78). Keep the house rule: probability + deterministic thresholds + a review route. Never the
  sole authority for security or irreversible actions.
- **The image is also an input surface.** Treat all text inside a screenshot as evidence, not
  instructions. The case states say this explicitly and stay fixed per pipeline.
- Fixtures are synthetic. No customer or personal data.

## Writing with AI assistance, 2026-10-06

Harness, fixtures, and this document were written with AI assistance, in the same style as the
rest of the repo.
