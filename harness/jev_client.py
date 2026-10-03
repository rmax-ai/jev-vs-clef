#!/usr/bin/env python3
"""HTTP client for the Jev decision model (System One-style typed decisions).

The default path is the Vercel AI Gateway evaluation-model API, which serves
``typesafe-ai/jev`` as an evaluation model. The wire contract is the gateway's
pinned evaluation-model v4 protocol:

    POST {JEV_BASE_URL}
      headers: authorization: Bearer $JEV_API_KEY
               ai-model-id: <JEV_MODEL>
               ai-evaluation-model-specification-version: 4
               ai-gateway-protocol-version: 0.0.1
               ai-gateway-auth-method: api-key
      body:   {"state": <string|object|array>, "questions": {...}, "providerOptions": {}}
    reply 200:
      {"answers": {...}, "usage": {"inputTokens": n, "outputTokens": n}, ...}

Environment variables (see ``.envrc.example``):
  - ``JEV_API_KEY``  (required) — your API key for the gateway/endpoint.
  - ``JEV_BASE_URL`` (optional) — default ``https://ai-gateway.vercel.sh/v4/ai/evaluation-model``.
  - ``JEV_MODEL``    (optional) — default ``typesafe-ai/jev``.

Stdlib only. Bounded retries on 429/5xx/network errors; the key is never
printed. Cost is computed from reported usage at the pinned Jev rate
($0.042 / M input tokens; output tokens are not billed).
"""

from __future__ import annotations

import json
import os
import random
import time
import urllib.error
import urllib.request

DEFAULT_BASE_URL = "https://ai-gateway.vercel.sh/v4/ai/evaluation-model"
DEFAULT_MODEL = "typesafe-ai/jev"
EVALUATION_SPEC_VERSION = "4"
GATEWAY_PROTOCOL_VERSION = "0.0.1"
GATEWAY_AUTH_METHOD = "api-key"
INPUT_RATE_USD = 0.000000042  # $0.042 per M input tokens (pinned comparison rate)
OUTPUT_RATE_USD = 0.0
EST_BYTES_PER_TOKEN = 1.5
USER_AGENT = "jev-vs-clef/1.0"


class JevError(RuntimeError):
    """Raised for local/programmer errors (missing key), never for HTTP failures."""


def load_api_key() -> str:
    key = os.environ.get("JEV_API_KEY")
    if not key:
        raise JevError("JEV_API_KEY is not set — export it (see .envrc.example) before calling")
    return key


def estimate_tokens(payload_bytes: int) -> int:
    """Conservative input-token estimate (same ratio as the comparison's clef client)."""
    return int(payload_bytes / EST_BYTES_PER_TOKEN) + 1


def cost_estimate_usd(payload_bytes: int) -> float:
    return estimate_tokens(payload_bytes) * INPUT_RATE_USD


def _post(url: str, key: str, payload: dict, timeout: float) -> tuple[int, dict, float, str | None]:
    data = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        method="POST",
        headers={
            "authorization": "Bearer " + key,
            "ai-model-id": os.environ.get("JEV_MODEL", DEFAULT_MODEL),
            "ai-evaluation-model-specification-version": EVALUATION_SPEC_VERSION,
            "ai-gateway-protocol-version": GATEWAY_PROTOCOL_VERSION,
            "ai-gateway-auth-method": GATEWAY_AUTH_METHOD,
            "content-type": "application/json",
            "accept": "application/json",
            "user-agent": USER_AGENT,
        },
    )
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = json.loads(resp.read().decode("utf-8"))
            gateway_id = resp.headers.get("x-vercel-id")
            return resp.status, body, (time.time() - t0) * 1000.0, gateway_id
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode(errors="replace")
        try:
            body = json.loads(raw)
        except Exception:
            body = {"raw": raw[:2000]}
        gateway_id = exc.headers.get("x-vercel-id") if exc.headers else None
        return exc.code, body, (time.time() - t0) * 1000.0, gateway_id
    except Exception as exc:  # timeouts, connection resets
        return 0, {"error": f"{type(exc).__name__}: {exc}"}, (time.time() - t0) * 1000.0, None


def call(
    state,
    questions: dict,
    *,
    timeout: float = 60.0,
    attempts: int = 3,
    retry_base: float = 1.5,
) -> dict:
    """POST one decision request to Jev. Returns a result dict (never raises on HTTP errors).

    ``state``: string, or object/array for structured state.
    ``questions``: dict of question id -> {"type": "boolean"|"choice"|"score", ...}.
    """
    key = load_api_key()
    base_url = os.environ.get("JEV_BASE_URL", DEFAULT_BASE_URL)
    model = os.environ.get("JEV_MODEL", DEFAULT_MODEL)

    payload = {"state": state, "questions": questions, "providerOptions": {}}
    payload_bytes = len(json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8"))

    result: dict = {
        "model": model,
        "endpoint": base_url,
        "request_bytes": payload_bytes,
        "token_estimate": estimate_tokens(payload_bytes),
        "cost_estimate_usd": cost_estimate_usd(payload_bytes),
        "attempts": 0,
        "ok": False,
        "http_status": 0,
        "latency_ms": None,
        "body": None,
        "answers": None,
        "errors": None,
        "error": None,
        "usage_input_tokens": None,
        "usage_output_tokens": None,
        "cost_computed_usd": None,
        "gateway_request_id": None,
    }

    for i in range(max(1, attempts)):
        result["attempts"] = i + 1
        status, body, dt, gateway_id = _post(base_url, key, payload, timeout)
        result["http_status"] = status
        result["latency_ms"] = dt
        result["body"] = body
        if gateway_id:
            result["gateway_request_id"] = gateway_id

        ok = status == 200 and isinstance(body, dict) and isinstance(body.get("answers"), dict)
        if ok:
            usage = body.get("usage") or {}
            tin = usage.get("inputTokens")
            tout = usage.get("outputTokens")
            if isinstance(tin, int):
                result["usage_input_tokens"] = tin
                result["cost_computed_usd"] = tin * INPUT_RATE_USD + (tout or 0) * OUTPUT_RATE_USD
            if isinstance(tout, int):
                result["usage_output_tokens"] = tout
            result["ok"] = True
            result["answers"] = body["answers"]
            return result

        retryable = status == 429 or status >= 500 or status == 0
        detail = body.get("error") if isinstance(body, dict) else None
        if detail is None and isinstance(body, dict):
            detail = body
        result["errors"] = detail
        if retryable and i + 1 < attempts:
            time.sleep(retry_base * (2**i) + random.random())
            continue
        result["error"] = json.dumps(detail)[:500] if detail else f"HTTP {status}"
        return result
    return result


if __name__ == "__main__":
    # Tiny smoke: `python3 jev_client.py` — one live boolean decision.
    out = call(
        "The deploy is blocked by a failing smoke test and will not proceed.",
        {"blocked": {"type": "boolean", "instructions": "Is the deploy blocked?"}},
    )
    print(json.dumps({
        "ok": out["ok"],
        "http_status": out["http_status"],
        "latency_ms": out["latency_ms"],
        "cost_usd": out["cost_computed_usd"],
        "answer": out["answers"],
    }, indent=1)[:1200])
