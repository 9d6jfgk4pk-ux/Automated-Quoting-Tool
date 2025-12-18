#!/usr/bin/env python
"""
machine_allocator.py

Assigns specific machines to each route operation WITHOUT changing extraction/routing logic.
Keeps decisions deterministic.

Main idea:
- Pick ONE primary CNC machine that can handle the envelope and most ops.
- Keep ops on that machine unless a specific op is clearly better elsewhere.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from machine_capability import get_machine_capabilities


def _get_envelope(features: Dict[str, Any]) -> Tuple[float, float, float]:
    env = features.get("envelope_mm") or {}
    L = float(env.get("length_mm") or 0.0)
    W = float(env.get("width_mm") or 0.0)
    T = float(env.get("thickness_mm") or 0.0)
    # Some PDFs may swap W/T depending on picking; enforce L is max of L/W
    if W > L:
        L, W = W, L
    return L, W, T


def _get_weight(features: Dict[str, Any]) -> float:
    w = features.get("finished_weight_kg")
    try:
        return float(w) if w is not None else 0.0
    except Exception:
        return 0.0


def _fits(machine: Dict[str, Any], L: float, W: float, T: float, weight: float) -> bool:
    if L <= 0 or W <= 0:
        return True  # allow (feature-only jobs) but generally envelope exists
    if L > float(machine["max_length_mm"]) or W > float(machine["max_width_mm"]) or T > float(machine["max_height_mm"]):
        return False
    mw = machine.get("max_weight_kg")
    if mw is not None and weight > float(mw):
        return False
    return True


def _op_family(op: Dict[str, Any]) -> str:
    # Used for choosing speed knobs. Keep aligned with time_model.tool_family.
    tool_key = (op.get("tool_key") or op.get("operation") or "").lower()
    if tool_key.startswith("drill"):
        return "drill"
    if tool_key.startswith("cbore") or tool_key.startswith("counterbore"):
        return "counterbore"
    if tool_key.startswith("tap"):
        return "tap"
    if tool_key.startswith("cnc_setup") or tool_key == "cnc_setup" or tool_key.startswith("setup"):
        return "cnc_setup"
    if "face" in tool_key:
        return "face_mill"
    if "edge" in tool_key:
        return "edge_mill"
    if "groove" in tool_key or "slot" in tool_key:
        return "groove_mill"
    if "deburr" in tool_key or "prep" in tool_key:
        return "bench"
    if "saw" in tool_key or "cut" in tool_key:
        return "saw"
    return "general"


def _best_machine_for_group(
    group: str,
    machines: List[Dict[str, Any]],
    L: float,
    W: float,
    T: float,
    weight: float,
    family: str,
) -> Optional[Dict[str, Any]]:
    candidates = [m for m in machines if m["machine_group"] == group and _fits(m, L, W, T, weight)]
    if not candidates:
        return None

    def score(m: Dict[str, Any]) -> float:
        # Higher score is better.
        # Prefer faster speed_factor, then any family-specific factor, then lower setup.
        s = float(m.get("speed_factor") or 1.0)
        fam = 1.0
        if family == "drill":
            fam = float(m.get("drill_speed_factor") or 1.0)
        elif family == "tap":
            fam = float(m.get("tap_speed_factor") or 1.0)
        elif family == "counterbore":
            fam = float(m.get("cbore_speed_factor") or 1.0)
        setup = float(m.get("setup_template_first_min") or 60.0)
        return (s * fam) - 0.002 * setup

    candidates.sort(key=score, reverse=True)
    return candidates[0]


def allocate_machines(route: List[Dict[str, Any]], features: Dict[str, Any]) -> List[Dict[str, Any]]:
    """
    Adds:
      - machine_name
      - routing_tag
      - speed_factor
      - setup_template_first_min
      - setup_template_repeat_min

    Does NOT change operation ordering or params.
    """
    machines = get_machine_capabilities()
    L, W, T = _get_envelope(features)
    weight = _get_weight(features)

    # Determine a primary CNC machine to keep re-handling minimal.
    # Preference: largest-capable CNC machine with best speed that fits.
    cnc_groups_preference = ["CNC_MILL_LARGE", "CNC_MILL_LARGE_4AX", "CNC_MILL_MEDIUM"]
    primary_cnc: Optional[Dict[str, Any]] = None
    for g in cnc_groups_preference:
        m = _best_machine_for_group(g, machines, L, W, T, weight, family="general")
        if m is not None:
            primary_cnc = m
            break

    # Optional: allow edge milling on BOKO for lighter parts
    prefer_boko_edges = bool(features.get("prefer_boko_edges", True))

    out: List[Dict[str, Any]] = []
    for op in route:
        op = dict(op)  # copy
        group = (op.get("machine_group") or "").strip() or None
        family = _op_family(op)

        chosen: Optional[Dict[str, Any]] = None

        # Explicit groups we always respect if present
        if group in ("BENCH", "SAW"):
            chosen = _best_machine_for_group(group, machines, L, W, T, weight, family=family)
        elif group == "BOKO":
            chosen = _best_machine_for_group("BOKO", machines, L, W, T, weight, family=family)
        elif group == "BORER":
            chosen = _best_machine_for_group("BORER", machines, L, W, T, weight, family=family)
        else:
            # For CNC ops: default to primary cnc
            if family in ("cnc_setup", "face_mill", "edge_mill", "groove_mill", "drill", "counterbore", "tap"):
                chosen = primary_cnc

                # If edge milling: optionally send to BOKO when the part is not too heavy
                if family == "edge_mill" and prefer_boko_edges and weight <= 80.0:
                    boko = _best_machine_for_group("BOKO", machines, L, W, T, weight, family=family)
                    if boko is not None:
                        chosen = boko

            else:
                # Unknown: keep on bench
                chosen = _best_machine_for_group("BENCH", machines, L, W, T, weight, family=family)

        if chosen is None:
            # fall back: first machine in list
            chosen = machines[0]

        op["machine_group"] = chosen["machine_group"]
        op["machine_name"] = chosen["machine_name"]
        op["routing_tag"] = chosen.get("routing_tag", op["machine_group"])
        sf = chosen.get("speed_factor")
        op["speed_factor"] = float(1.0 if sf is None else sf)
        st_first = chosen.get("setup_template_first_min")
        st_repeat = chosen.get("setup_template_repeat_min")
        op["setup_template_first_min"] = float(60.0 if st_first is None else st_first)
        op["setup_template_repeat_min"] = float(20.0 if st_repeat is None else st_repeat)

        # Pass through useful process-specific knobs (time_model will use if present)
        for k in (
            "face_mill_mrr_mm3_min",
            "edge_mill_mrr_mm3_min",
            "groove_mrr_mm3_min",
            "drill_speed_factor",
            "tap_speed_factor",
            "cbore_speed_factor",
        ):
            if k in chosen and chosen[k] is not None:
                op[k] = chosen[k]

        out.append(op)

    return out