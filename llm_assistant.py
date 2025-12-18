"""
llm_assistant.py

Thin wrappers around GPT-5.2 to augment deterministic feature extraction and
provide routing sanity feedback. All calls are optional and will fall back to
existing logic if the API is unavailable or a response cannot be parsed.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from llm_client import chat_completion, extract_json_blob, llm_available


LLM_SYSTEM_PROMPT = """
You are assisting a manufacturing estimator. Given raw PDF text from a technical
drawing, extract machining features in a concise JSON schema. Only respond with
JSON.

Schema:
{
  "notes": [string],
  "holes": {
    "drill_thru": [ {"count": int, "dia_mm": float} ],
    "counterbore": [ {"count": int, "pilot_dia_mm": float, "cbore_dia_mm": float, "cbore_depth_mm": float} ],
    "tapped": [ {"count": int, "thread": string, "depth_mm": float|null, "thru": bool|null} ]
  },
  "grooves": [ {"count": int|null, "width_mm": float, "depth_mm": float, "length_mm": float|null} ],
  "warnings": [string]
}

Rules: derive counts/diameters/depths from the text if stated. Keep units in mm
and integers for counts. If uncertain, include a warning. Avoid inventing data
that is not present.
"""


ROUTE_SANITY_SYSTEM = """
You review manufacturing routes. Given features and a proposed route table,
return a concise JSON with:
{
  "observations": [string],
  "missing_ops": [string],
  "timing_flags": [string]
}
Keep output brief and actionable; do not include code fences.
"""


def _clean_number(x: Any) -> Optional[float]:
    try:
        return float(x)
    except Exception:  # noqa: BLE001
        return None


def _parse_llm_features(raw: Dict[str, Any]) -> Dict[str, Any]:
    holes = raw.get("holes") or {}
    grooves = raw.get("grooves") or []
    notes = raw.get("notes") or []
    warnings = raw.get("warnings") or []

    def _norm_list(v):
        if isinstance(v, dict):
            return [v]
        return v if isinstance(v, list) else []

    drill = []
    for d in _norm_list(holes.get("drill_thru")):
        c = d.get("count")
        dia = _clean_number(d.get("dia_mm"))
        if c and dia:
            drill.append({"count": int(c), "dia_mm": float(dia)})

    cbore = []
    for c in _norm_list(holes.get("counterbore")):
        cnt = c.get("count")
        pilot = _clean_number(c.get("pilot_dia_mm"))
        cd = _clean_number(c.get("cbore_dia_mm"))
        depth = _clean_number(c.get("cbore_depth_mm"))
        if cnt and pilot and cd and depth:
            cbore.append({
                "count": int(cnt),
                "pilot_dia_mm": float(pilot),
                "cbore_dia_mm": float(cd),
                "cbore_depth_mm": float(depth),
            })

    tapped = []
    for t in _norm_list(holes.get("tapped")):
        cnt = t.get("count")
        thread = t.get("thread") or t.get("thread_raw")
        depth = _clean_number(t.get("depth_mm"))
        thru = t.get("thru") if isinstance(t.get("thru"), bool) else None
        if cnt and thread:
            tapped.append({
                "count": int(cnt),
                "thread": str(thread).strip(),
                "thread_raw": str(thread).strip(),
                "depth_mm": depth,
                "thru": thru,
            })

    g_list = []
    for g in _norm_list(grooves):
        width = _clean_number(g.get("width_mm"))
        depth = _clean_number(g.get("depth_mm"))
        length = _clean_number(g.get("length_mm"))
        count = g.get("count")
        if width and depth:
            g_list.append({
                "count": int(count) if isinstance(count, (int, float)) else None,
                "width_mm": float(width),
                "depth_mm": float(depth),
                "length_mm": float(length) if length else None,
            })

    return {
        "notes": notes if isinstance(notes, list) else [],
        "holes": {"drill_thru": drill, "counterbore": cbore, "tapped": tapped},
        "grooves": g_list,
        "warnings": warnings if isinstance(warnings, list) else [],
    }


def enrich_features_with_llm(pdf_text: str) -> Dict[str, Any]:
    if not llm_available():
        return {"ok": False, "error": "LLM not configured"}

    messages = [
        {"role": "system", "content": LLM_SYSTEM_PROMPT},
        {"role": "user", "content": pdf_text[:15000]},
    ]

    resp = chat_completion(messages)
    if not resp.get("ok"):
        return {"ok": False, "error": resp.get("error")}

    parsed = extract_json_blob(resp.get("content") or "")
    if not isinstance(parsed, dict):
        return {"ok": False, "error": "Could not parse JSON from LLM response"}

    features = _parse_llm_features(parsed)
    features["llm_raw"] = parsed
    return {"ok": True, "features": features}


def review_route_with_llm(features: Dict[str, Any], route: List[Dict[str, Any]]) -> Dict[str, Any]:
    if not llm_available():
        return {"ok": False, "error": "LLM not configured"}

    try:
        payload = {
            "features": features,
            "route": [
                {
                    "op_no": r.get("op_no"),
                    "operation": r.get("operation"),
                    "machine_group": r.get("machine_group"),
                    "setup_min": r.get("setup_min"),
                    "runtime_min": r.get("runtime_min"),
                }
                for r in route
            ],
        }
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}

    messages = [
        {"role": "system", "content": ROUTE_SANITY_SYSTEM},
        {"role": "user", "content": json.dumps(payload, indent=2)[:15000]},
    ]

    resp = chat_completion(messages)
    if not resp.get("ok"):
        return {"ok": False, "error": resp.get("error")}

    parsed = extract_json_blob(resp.get("content") or "")
    if not isinstance(parsed, dict):
        return {"ok": False, "error": "Could not parse JSON from LLM response"}

    return {
        "ok": True,
        "observations": parsed.get("observations") or [],
        "missing_ops": parsed.get("missing_ops") or [],
        "timing_flags": parsed.get("timing_flags") or [],
        "llm_raw": parsed,
    }
