"""
llm_client.py

Lightweight helper for calling the OpenAI Chat Completions API (GPT-5.2 by default)
without adding external dependencies. This module keeps network usage optional and
returns structured results so callers can gracefully fall back to deterministic
logic when no API key is configured or a request fails.

Configuration (environment variables):
- OPENAI_API_KEY or LLM_API_KEY   : bearer token (required to send requests)
- OPENAI_API_BASE (optional)      : override base URL (default https://api.openai.com)
- OPENAI_MODEL (optional)         : override default model name (default gpt-5.2)
"""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional


DEFAULT_MODEL = os.getenv("OPENAI_MODEL", "gpt-5.2")
DEFAULT_BASE = os.getenv("OPENAI_API_BASE", "https://api.openai.com")


def llm_available() -> bool:
    return bool(os.getenv("OPENAI_API_KEY") or os.getenv("LLM_API_KEY"))


def _auth_header() -> str:
    key = os.getenv("OPENAI_API_KEY") or os.getenv("LLM_API_KEY")
    if not key:
        raise RuntimeError("Missing OPENAI_API_KEY or LLM_API_KEY")
    return f"Bearer {key}"


def chat_completion(
    messages: List[Dict[str, str]],
    model: str = DEFAULT_MODEL,
    temperature: float = 0.0,
    timeout: int = 30,
    extra_payload: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Minimal chat completion call.

    Returns a dict with keys: ok (bool), content (str|None), error (str|None), raw (Any).
    """

    url = DEFAULT_BASE.rstrip("/") + "/v1/chat/completions"
    payload: Dict[str, Any] = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
    }
    if extra_payload:
        payload.update(extra_payload)

    data = json.dumps(payload).encode("utf-8")

    try:
        req = urllib.request.Request(
            url,
            data=data,
            headers={
                "Content-Type": "application/json",
                "Authorization": _auth_header(),
            },
            method="POST",
        )
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "content": None, "error": str(exc), "raw": None}

    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = json.load(resp)
    except urllib.error.HTTPError as exc:
        try:
            detail = exc.read().decode("utf-8")
        except Exception:  # noqa: BLE001
            detail = str(exc)
        return {"ok": False, "content": None, "error": f"HTTP {exc.code}: {detail}", "raw": None}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "content": None, "error": str(exc), "raw": None}

    try:
        content = (raw.get("choices") or [{}])[0].get("message", {}).get("content")
    except Exception:  # noqa: BLE001
        content = None

    return {"ok": content is not None, "content": content, "error": None, "raw": raw}


def extract_json_blob(text: str) -> Optional[Dict[str, Any]]:
    """Extracts the first JSON object from a string (inside or outside fences)."""
    if not text:
        return None

    fence_match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    raw = fence_match.group(1) if fence_match else text

    # Find first JSON object in the text
    brace_start = raw.find("{")
    brace_end = raw.rfind("}")
    if brace_start == -1 or brace_end == -1 or brace_end <= brace_start:
        return None

    try:
        return json.loads(raw[brace_start : brace_end + 1])
    except Exception:  # noqa: BLE001
        return None
