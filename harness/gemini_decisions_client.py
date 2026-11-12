#!/usr/bin/env python3
"""Minimal stdlib client for the Gemini Decisions API (``gemini-decision-1``).

Wire contract (verified live 2026-11-12 against the published docs and the
google-genai 3.2.0 SDK's own request path):

    POST https://generativelanguage.googleapis.com/v1beta/decisions
      header: x-goog-api-key: <key>
      body:   {"model": "gemini-decision-1",
               "input": <string | Content | Content[]>,
               "questions": {"<id>": {"type": "noul|choice|score",
                                      "instructions": "...",
                                      "criteria": {...} | [...]}}}
    reply 200:
      {"answers": {"<id>": {...}}, "usage": {"total_input_tokens": n,
                                             "total_output_tokens": 0, ...}}

Differences from the System One wire used by Jev and Clef:

- Yes/no questions use the native type name ``noul`` (no ``boolean`` mapping).
- ``input`` is a shared multimodal content block list; images are inline
  ``{"type": "image", "data": "<base64>", "mime_type": "image/png"}``.
- Answers carry ``confidence`` and, for score questions, a ``legend`` mapping
  level index -> level description.

Environment variable: ``GEMINI_API_KEY`` (never printed or logged).

Stdlib only. Bounded retries with backoff on 429/5xx/network errors. Cost is
computed from provider-reported usage at the published rate
($0.10 / M input tokens; output tokens are always 0 and unbilled).
"""

from __future__ import annotations

import base64
import json
import mimetypes
import os
import random
import socket
import time
import urllib.error
import urllib.request

BASE_URL = "https://generativelanguage.googleapis.com"
API_VERSION = "v1beta"
MODEL = "gemini-decision-1"
INPUT_RATE_USD_PER_MTOKEN = 0.10
# Conservative estimator shared with the other arms (canonical bytes / 1.5).
EST_BYTES_PER_TOKEN = 1.5
USER_AGENT = "jev-vs-clef/1.0"


class GeminiDecisionsError(RuntimeError):
    """Raised for local/programmer errors (missing key), never for HTTP failures."""


def load_api_key() -> str:
    key = os.environ.get("GEMINI_API_KEY")
    if not key:
        raise GeminiDecisionsError(
            "GEMINI_API_KEY is not set — export it (see .envrc.example) before calling"
        )
    return key


def prefer_ipv4() -> None:
    """Try IPv4 addresses first.

    On this box ``getaddrinfo`` returns AAAA records only, and the host's IPv6
    route blackholes; ``urllib`` walks the list serially, so every request paid
    ~8 connect timeouts (~25 s) before a working address. Measured 2026-11-12:
    identical payload 24.8 s via urllib vs 0.43 s via curl (curl sorts
    families itself). Set ``GEMINI_PREFER_IPV4=0`` to restore the default.
    """
    if os.environ.get("GEMINI_PREFER_IPV4", "1") != "1":
        return
    if getattr(socket, "_jev_vs_clef_ipv4_first", False):
        return
    original = socket.getaddrinfo

    def getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):  # noqa: A002
        infos = original(host, port, family, type, proto, flags)
        infos.sort(key=lambda info: 0 if info[0] == socket.AF_INET else 1)
        return infos

    socket.getaddrinfo = getaddrinfo
    socket._jev_vs_clef_ipv4_first = True


prefer_ipv4()


def estimate_tokens(payload_bytes: int) -> int:
    return int(payload_bytes / EST_BYTES_PER_TOKEN) + 1


def cost_estimate_usd(payload_bytes: int) -> float:
    return estimate_tokens(payload_bytes) * INPUT_RATE_USD_PER_MTOKEN / 1_000_000


def image_content(path: str, mime: str | None = None) -> dict:
    """Return an inline image content block for the Decisions ``input`` array."""
    if mime is None:
        mime = mimetypes.guess_type(path)[0] or "image/png"
    with open(path, "rb") as fh:
        raw = fh.read()
    return {"type": "image", "data": base64.b64encode(raw).decode("ascii"), "mime_type": mime}


def build_input(state, image_paths: list[str] | None = None) -> object:
    """Build the ``input`` value.

    A plain string is sent as-is (the documented shorthand). Anything else —
    structured state, or a state with images — becomes a content-block list;
    the API rejects a bare object at ``input[0]``.
    """
    text = state if isinstance(state, str) else json.dumps(
        state, separators=(",", ":"), sort_keys=True, ensure_ascii=False)
    if not image_paths:
        return text if isinstance(state, str) else [{"type": "text", "text": text}]
    blocks: list[dict] = [image_content(p) for p in image_paths]
    blocks.append({"type": "text", "text": text})
    return blocks


def _post(url: str, key: str, payload: dict, timeout: float) -> tuple[int, dict, float]:
    data = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        method="POST",
        headers={
            "x-goog-api-key": key,
            "content-type": "application/json",
            "user-agent": USER_AGENT,
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
    state,
    questions: dict,
    images: list[str] | None = None,
    *,
    api_key: str | None = None,
    timeout: float = 120.0,
    attempts: int = 3,
    retry_base: float = 1.5,
) -> dict:
    """POST one decision request. Returns a result dict (never raises on HTTP errors)."""
    if api_key is None:
        api_key = load_api_key()

    payload = {"model": MODEL, "input": build_input(state, images), "questions": questions}
    payload_bytes = len(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
    url = f"{BASE_URL}/{API_VERSION}/decisions"

    result: dict = {
        "model": MODEL,
        "endpoint": f"{BASE_URL}/{API_VERSION}/decisions",
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
        "cost_computed_usd": None,
    }

    for i in range(max(1, attempts)):
        result["attempts"] = i + 1
        status, body, dt = _post(url, api_key, payload, timeout)
        result["http_status"] = status
        result["latency_ms"] = dt
        result["body"] = body

        if status == 200 and isinstance(body, dict) and isinstance(body.get("answers"), dict):
            result["ok"] = True
            result["answers"] = body["answers"]
            usage = body.get("usage") if isinstance(body.get("usage"), dict) else {}
            tin = usage.get("total_input_tokens")
            if isinstance(tin, int):
                result["usage_input_tokens"] = tin
                result["cost_computed_usd"] = tin * INPUT_RATE_USD_PER_MTOKEN / 1_000_000
            return result

        retryable = status == 429 or status >= 500 or status == 0
        if retryable and i + 1 < attempts:
            time.sleep(retry_base * (2**i) + random.random())
            continue
        err = body.get("error") if isinstance(body, dict) else None
        result["errors"] = err
        result["error"] = json.dumps(err or body)[:500]
        return result

    return result


def summarize(result: dict) -> str:
    lat = f"{result['latency_ms']:.0f} ms" if result.get("latency_ms") is not None else "n/a"
    return (
        f"{result['model']}: ok={result['ok']} http={result['http_status']} "
        f"{lat} attempts={result['attempts']} est=${result['cost_estimate_usd']:.6f}"
    )


if __name__ == "__main__":
    # Tiny CLI: `python3 gemini_decisions_client.py` — one live smoke call.
    out = call(
        "The blue mug I ordered on Monday arrived cracked on Wednesday.",
        {
            "damaged": {"type": "noul", "instructions": "Did the item arrive damaged?"},
            "queue": {
                "type": "choice",
                "instructions": "Which queue should handle this?",
                "criteria": {
                    "billing": "payments and refunds",
                    "replacements": "damaged items",
                    "general": "product questions",
                },
            },
        },
    )
    print(summarize(out))
    print(json.dumps(out.get("answers"), indent=1, sort_keys=True))
