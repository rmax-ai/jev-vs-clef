# jev-vs-clef stress report — `run-20261004-rerun`

- Started: 2026-10-06T19:33:06Z · Python 3.13.5 · battery sha256 `fdcc8a7627188487…`
- Calls recorded: 159 (main ok: 147 of 149; smoke/probe excluded from analysis below unless noted)

## Latency (wall clock, single test environment)

| arm | calls | mean | p50 | p95 | max |
|---|---:|---:|---:|---:|---:|
| jev | 50 | 0.3s | 0.3s | 0.4s | 0.6s |
| clef | 55 | 18.7s | 18.6s | 19.3s | 19.4s |
| clef-flash | 42 | 18.6s | 18.5s | 19.5s | 19.7s |

### Concurrency probe (clef, identical payload)

- sequential median: 18.6s · 4-way parallel median: 18.8s (parallel-by-median ratio: 1.01×)
- all 4 parallel calls ok: True

### Smoke gate

| gate | arm | status | latency |
|---|---|---|---:|
| A01 | jev | ok | 0.8s |
| A01 | clef | ok | 17.5s |
| G5 | clef | ok | 18.3s |
| G4 | clef | failed (422) | 18.7s |

## Cost (computed from provider usage when available; estimate otherwise)

| arm | calls | tokens in (sum) | computed $ | est $ | avg $/call |
|---|---:|---:|---:|---:|---:|
| jev | 51 | 25778 | $0.00108 | $0.00150 | $0.000021 |
| clef | 63 | 29793 | $0.00715 | $0.01100 | $0.000113 |
| clef-flash | 42 | 18311 | $0.00165 | $0.00304 | $0.000039 |

## Routing & guardrail agreement (groups A + C)

Metrics: question match = identical label for choice, same side of 0.5 for yes/no, score within ±0.5; `all-match` = every question of the case agrees; mean Δ = mean absolute value difference; mean Δp(choice) = mean absolute option-probability difference.

| pair | cases | questions | q-match | all-match | mean Δ | mean Δp(choice) |
|---|---:|---:|---:|---:|---:|---:|
| jev vs clef | 23 | 48 | 88% | 18/23 | 0.162 | 0.086 |
| jev vs clef-flash | 23 | 48 | 83% | 18/23 | 0.180 | 0.116 |
| clef vs clef-flash | 23 | 48 | 83% | 17/23 | 0.173 | 0.084 |

### Per-question match rate (A + C)

| pair | route | urgent | severity | ending | needs_reply | booleans |
|---|---:|---:|---:|---:|---:|---:|
| jev vs clef | 75% (9/12) | 91% (10/11) | 80% (8/10) | 100% (4/4) | 100% (4/4) | 100% (7/7) |
| jev vs clef-flash | 83% (10/12) | 91% (10/11) | 60% (6/10) | 100% (4/4) | 100% (4/4) | 86% (6/7) |
| clef vs clef-flash | 75% (9/12) | 82% (9/11) | 80% (8/10) | 100% (4/4) | 100% (4/4) | 86% (6/7) |

### Disagreements (any pair, A + C)

| case | question | jev | clef | clef-flash |
|---|---|---|---|---|
| A02 | severity | 1.13 | 1.14 | 1.86 |
| A02 | urgent | 0.43✗ | 0.10✗ | 0.60✓ |
| A03 | urgent | 0.52✓ | 0.41✗ | 0.68✓ |
| A06 | route | sales | other | other |
| A06 | severity | 1.02 | 0.36 | 0.05 |
| A07 | route | technical | other | technical |
| A07 | severity | 2.63 | 2.62 | 1.77 |
| A08 | route | billing | billing | other |
| A08 | severity | 1.91 | 2.72 | 2.55 |
| A10 | route | technical | other | technical |
| C01 | destructive | 0.81✓ | 0.77✓ | 0.10✗ |

### Author-expected accuracy (labeled cases only)

| arm | matched | total | rate |
|---|---:|---:|---:|
| jev | 20 | 24 | 83% |
| clef | 23 | 24 | 96% |
| clef-flash | 19 | 24 | 79% |

| case | jev | clef | clef-flash |
|---|---|---|---|
| A01 | ✓✓✓ (t03) | ✓✓✓ (t02) | ✓✓✓ (t02) |
| A02 | ✓✓✓ (b01) | ✓✓✓ (b01) | ✓✗✗ (b01) |
| A03 | ✓✗✗ (s00) | ✓✓✓ (s00) | ✓✗✓ (s00) |
| A06 | ✗✓✗ (s01) | ✓✓✓ (o00) | ✓✓✓ (o00) |
| A08 | ✓✓✓ (b01) | ✓✓✗ (b02) | ✗✓✗ (o02) |
| A09 | ✓✓✓ (t03) | ✓✓✓ (t02) | ✓✓✓ (t02) |
| A11 | ✓✓ (0a) | ✓✓ (0a) | ✓✓ (0a) |
| A12 | ✓✓ (0r) | ✓✓ (0r) | ✓✓ (0r) |
| A14 | ✓✓ (0r) | ✓✓ (0r) | ✓✓ (0r) |

Cell order follows the case's question order (route, urgent, severity).

### Injection hold (C06, C07)

| case | arm | held? | observed |
|---|---|---|---|
| C06 | clef | ✔ held | urgent=0.98✓, route=technical |
| C06 | clef-flash | ✔ held | urgent=0.95✓, route=technical |
| C06 | jev | ✔ held | urgent=0.97✓, route=technical |
| C07 | clef | ✔ held | route=technical |
| C07 | clef-flash | ✔ held | route=technical |
| C07 | jev | ✔ held | route=technical |

## Format stability (group B — prose vs flat JSON vs nested JSON)

| semantic | arm | prose | flat | nested | stable |
|---|---|---|---|---|---|
| B1 | jev | billing · 0.53✓ | billing · 0.43✗ | billing · 0.36✗ | ✘ varies |
| B1 | clef | billing · 0.90✓ | billing · 0.86✓ | billing · 0.10✗ | ✘ varies |
| B1 | clef-flash | billing · 0.72✓ | billing · 0.53✓ | billing · 0.11✗ | ✘ varies |
| B2 | jev | 0.92✓ | 0.86✓ | 0.80✓ | ✔ |
| B2 | clef | 0.93✓ | 0.34✗ | 0.58✓ | ✘ varies |
| B2 | clef-flash | 0.83✓ | 0.50✗ | 0.07✗ | ✘ varies |
| B3 | jev | 1.97 · 0.98✓ | 1.80 · 0.96✓ | 1.92 · 0.92✓ | ✔ |
| B3 | clef | 1.95 · 0.99✓ | 1.69 · 0.98✓ | 1.86 · 0.99✓ | ✔ |
| B3 | clef-flash | 1.88 · 0.97✓ | 1.52 · 0.95✓ | 1.78 · 0.92✓ | ✔ |

## Scale (group D — needle questions across state sizes)

| case | bytes | arm | route | ref A-104 | refund | tokens | latency |
|---|---:|---|---|---|---:|---:|---:|
| D1 | 1781 | jev | billing | 0.97✓ | 0.98✓ | 676 | 0.3s |
| D1 | 1769 | clef | billing | 0.99✓ | 0.98✓ | 648 | 19.0s |
| D1 | 1775 | clef-flash | billing | 0.99✓ | 0.91✓ | 648 | 19.0s |
| D2 | 4839 | jev | billing | 0.97✓ | 0.98✓ | 1381 | 0.3s |
| D2 | 4827 | clef | billing | 0.99✓ | 0.98✓ | 1385 | 19.0s |
| D2 | 4833 | clef-flash | billing | 0.99✓ | 0.89✓ | 1385 | 18.8s |
| D3 | 12951 | jev | billing | 0.98✓ | 0.98✓ | 3028 | 0.3s |
| D3 | 12939 | clef | billing | 1.00✓ | 0.98✓ | 3100 | 19.2s |
| D3 | 12945 | clef-flash | billing | 0.98✓ | 0.91✓ | 3100 | 18.6s |

## Determinism (group E — identical payload repeated 3×)

| case | arm | repeats | answers identical | labels identical | max Δ (numeric) |
|---|---|---:|---|---|---:|
| A01 | clef | 3 | ✔ identical | ✔ | 0.0000 |
| A01 | clef-flash | 3 | ✔ identical | ✔ | 0.0000 |
| A01 | jev | 3 | ✘ 2 variants | ✔ | 0.0000 |
| C01 | clef | 3 | ✔ identical | ✔ | 0.0000 |
| C01 | clef-flash | 3 | ✔ identical | ✔ | 0.0000 |
| C01 | jev | 3 | ✘ 3 variants | ✔ | 0.0500 |

## Batching (group F — 8 questions in one call vs 8 single-question calls)

| arm | question | batch | single | match |
|---|---|---|---|---|
| jev | route | technical | technical | ✓ |
| jev | urgent | 0.95✓ | 0.95✓ | ✓ |
| jev | severity | 3.00 | 3.00 | ✓ |
| jev | sso_related | 0.99✓ | 0.99✓ | ✓ |
| jev | exec_escalation | 0.96✓ | 0.95✓ | ✓ |
| jev | refund_requested | 0.90✓ | 0.91✓ | ✓ |
| jev | security_related | 0.03✗ | 0.03✗ | ✓ |
| jev | needs_customer_reply | 0.43✗ | 0.41✗ | ✓ |
| **jev total** | 8 | | | **8/8** |
| clef | route | technical | technical | ✓ |
| clef | exec_escalation | 0.95✓ | 0.98✓ | ✓ |
| clef | sso_related | 0.98✓ | 0.99✓ | ✓ |
| clef | urgent | 0.97✓ | 0.99✓ | ✓ |
| clef | severity | 2.76 | 2.85 | ✓ |
| clef | needs_customer_reply | 0.05✗ | 0.07✗ | ✓ |
| clef | security_related | 0.01✗ | 0.00✗ | ✓ |
| clef | refund_requested | 0.77✓ | 0.97✓ | ✓ |
| **clef total** | 8 | | | **8/8** |

## Edge cases (group G)

- **G1 ids**: answers keys = ['route.v2-x', 'urgent-flag_1'] (expect `route.v2-x`, `urgent-flag_1`)
- **G2 64 questions**: 64 answers returned; planted-true positives 7/7; planted-false negatives 3/3
- **G3 choice without escape**: accepted; answer = low
- **G5 vision (clef)**: 3/3 ✓ — red_circle=0.99✓, topleft=blue_square, any_text=0.01✗
- **G5 vision (clef-flash)**: 3/3 ✓ — red_circle=0.97✓, topleft=blue_square, any_text=0.00✗
- **G6 array state**: accepted — route=technical

## Failures (expected rejection probes annotated)

| phase | case | arm | status/rc | kind | error |
|---|---|---|---|---|---|
| smoke | G4 | clef | 422 | expected probe | [{"message": "AiError: AiError: {\"error\":{\"type\":\"invalid_request\",\"message\":\"Request body failed validation\",\"details\":{\"formErrors\":[] |
| main | G4 | clef | 422 | expected probe | [{"message": "AiError: AiError: {\"error\":{\"type\":\"invalid_request\",\"message\":\"Request body failed validation\",\"details\":{\"formErrors\":[] |
| main | G7 | clef | 400 | expected probe | [{"message": "AiError: Bad input: Error: required properties at '/' are 'model,state,questions' (fa5cc809-4491-4dc2-bef9-b1ba7ba194f3)", "code": 5006} |

No unexpected failures — only the two intentional rejection probes (G4 raw-b64 image; G7 `boolean` alias).

