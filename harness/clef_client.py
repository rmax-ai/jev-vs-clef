#!/usr/bin/env python3
"""Minimal stdlib client for Cloudflare Clef decision models.

Models: ``@cf/cloudflare/clef`` (27B) and ``@cf/cloudflare/clef-flash`` (9B).
Wire format: System One-compatible ``{model, state, questions[, images]}``.
REST answers arrive under ``result``.

Credentials come from the environment — ``CLOUDFLARE_ACCOUNT_ID`` and
``CLOUDFLARE_API_TOKEN`` (see ``.envrc.example``). Values are sent only in
the request header and are never printed or logged.

Stdlib only (Python >= 3.10). No retries on 4xx (fix the request instead);
bounded retries with backoff on 429/5xx/timeouts.
"""

from __future__ import annotations

import base64
import json
import mimetypes
import os
import random
import time
import urllib.error
import urllib.request

BASE = "https://api.cloudflare.com/client/v4"
MODELS = ("clef", "clef-flash")
# Cloudflare Workers AI unit pricing (input tokens; output is not billed).
PRICE_PER_MTOKEN_USD = {"clef": 0.24, "clef-flash": 0.09}
# Conservative token estimator shared across arms: canonical bytes / ratio.
EST_BYTES_PER_TOKEN = 1.5


class ClefError(RuntimeError):
    pass


def load_credentials() -> tuple[str, str]:
    """Return ``(account_id, token)`` from the environment (clear error if absent)."""
    account = os.environ.get("CLOUDFLARE_ACCOUNT_ID")
    token = os.environ.get("CLOUDFLARE_API_TOKEN")
    missing = [
        name
        for name, value in (("CLOUDFLARE_ACCOUNT_ID", account), ("CLOUDFLARE_API_TOKEN", token))
        if not value
    ]
    if missing:
        raise ClefError(
            "missing environment variable(s): "
            + ", ".join(missing)
            + " — set them (see .envrc.example) before calling"
        )
    return account, token


def estimate_tokens(payload_bytes: int) -> int:
    """Conservative input-token estimate (same ratio as the jev CLI estimator)."""
    return int(payload_bytes / EST_BYTES_PER_TOKEN) + 1


def cost_estimate_usd(model: str, payload_bytes: int) -> float:
    if model not in PRICE_PER_MTOKEN_USD:
        raise ClefError(f"unknown model {model!r}")
    return estimate_tokens(payload_bytes) * PRICE_PER_MTOKEN_USD[model] / 1_000_000


def image_to_data_uri(path: str, mime: str | None = None) -> str:
    """Read an image file and return a data-URI string for the ``images`` array."""
    if mime is None:
        mime = mimetypes.guess_type(path)[0] or "image/png"
    with open(path, "rb") as fh:
        raw = fh.read()
    return f"data:{mime};base64," + base64.b64encode(raw).decode("ascii")


def _post(url: str, token: str, payload: dict, timeout: float) -> tuple[int, dict, float]:
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        method="POST",
        headers={
            "Authorization": "Bearer " + token,
            "Content-Type": "application/json",
        },
    )
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = json.loads(resp.read().decode("utf-8"))
            return resp.status, body, (time.time() - t0) * 1000.0
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode(errors="replace")
        try:
            body = json.loads(raw)
        except Exception:
            body = {"raw": raw[:2000]}
        return exc.code, body, (time.time() - t0) * 1000.0
    except Exception as exc:  # timeouts, connection resets
        return 0, {"error": f"{type(exc).__name__}: {exc}"}, (time.time() - t0) * 1000.0


def call(
    model: str,
    state,
    questions: dict,
    images: list[str] | None = None,
    *,
    account: str | None = None,
    token: str | None = None,
    timeout: float = 240.0,
    attempts: int = 3,
    retry_base: float = 1.5,
) -> dict:
    """POST one decision request to Clef. Returns a result dict (never raises on HTTP errors).

    ``images``: optional list of data-URI strings (max 4, PNG/JPEG/WebP; the API
    rejects raw base64 without the ``data:`` prefix).
    """
    if model not in MODELS:
        raise ClefError(f"model must be one of {MODELS}, got {model!r}")
    if account is None or token is None:
        account, token = load_credentials()

    payload: dict = {"model": model, "state": state, "questions": questions}
    if images:
        payload["images"] = images
    payload_bytes = len(json.dumps(payload, separators=(",", ":")).encode("utf-8"))

    url = f"{BASE}/accounts/{account}/ai/run/@cf/cloudflare/{model}"
    result: dict = {
        "model": model,
        "endpoint": url,
        "request_bytes": payload_bytes,
        "token_estimate": estimate_tokens(payload_bytes),
        "cost_estimate_usd": cost_estimate_usd(model, payload_bytes),
        "attempts": 0,
        "ok": False,
        "http_status": 0,
        "latency_ms": None,
        "body": None,
        "answers": None,
        "errors": None,
        "error": None,
        "usage_input_tokens": None,
        "cost_computed_usd": None,
    }

    for i in range(max(1, attempts)):
        result["attempts"] = i + 1
        status, body, dt = _post(url, token, payload, timeout)
        result["http_status"] = status
        result["latency_ms"] = dt
        result["body"] = body

        retryable = status == 429 or status >= 500 or status == 0
        if status == 200 and isinstance(body, dict) and body.get("success", True):
            res = body.get("result") or {}
            answers = res.get("answers") if isinstance(res, dict) else None
            if answers is None and isinstance(body.get("answers"), dict):
                answers = body["answers"]
            result["ok"] = True
            result["answers"] = answers
            result["errors"] = body.get("errors")
            usage = res.get("usage") if isinstance(res, dict) else None
            if isinstance(usage, dict):
                tin = usage.get("input_tokens")
                if isinstance(tin, int):
                    result["usage_input_tokens"] = tin
                    result["cost_computed_usd"] = tin * PRICE_PER_MTOKEN_USD[model] / 1_000_000
            return result

        # Record server-side error detail, then decide whether to retry.
        result["errors"] = (body or {}).get("errors") if isinstance(body, dict) else None
        if retryable and i + 1 < attempts:
            time.sleep(retry_base * (2**i) + random.random())
            continue
        result["error"] = (
            json.dumps(result["errors"])[:500] if result["errors"] else json.dumps(body)[:500]
        )
        return result

    return result


def summarize(result: dict) -> str:
    lat = f"{result['latency_ms']:.0f} ms" if result.get("latency_ms") is not None else "n/a"
    return (
        f"{result['model']}: ok={result['ok']} http={result['http_status']} "
        f"{lat} attempts={result['attempts']} est=${result['cost_estimate_usd']:.6f}"
    )


if __name__ == "__main__":
    # Tiny CLI: `python3 clef_client.py <model> [state-file]` — one smoke call.
    import sys

    model = sys.argv[1] if len(sys.argv) > 1 else "clef"
    state = (
        open(sys.argv[2]).read()
        if len(sys.argv) > 2
        else "Checkout is failing for every customer and orders are blocked."
    )
    q = {
        "urgent": {"type": "noul", "instructions": "Is this urgent?"},
        "team": {
            "type": "choice",
            "instructions": "Which team should handle this?",
            "criteria": {
                "billing": "payments and invoices",
                "technical": "bugs and outages",
                "other": "none of the above",
            },
        },
    }
    out = call(model, state, q)
    print(summarize(out))
    print(json.dumps(out.get("answers"), indent=1)[:1200])
