#!/usr/bin/env python
"""
route_builder.py

Build a deterministic, shop-style manufacturing route from extracted features + verified envelope.
This file must NOT touch the dimension picker logic.

Key improvements vs earlier lightweight routing:
- Always includes face/edge milling when notes indicate "machine finish all over" (3.2 etc.)
- Adds an optional stock-prep saw cut step (can be toggled in features)
- De-duplicates pilot drilling for counterbored holes
"""

from __future__ import annotations

from typing import Any, Dict, List, Tuple


def _get_envelope(features: Dict[str, Any]) -> Tuple[float, float, float]:
    env = features.get("envelope_mm") or {}
    L = float(env.get("length_mm") or 0.0)
    W = float(env.get("width_mm") or 0.0)
    T = float(env.get("thickness_mm") or 0.0)
    if W > L:
        L, W = W, L
    return L, W, T


def _notes_lower(features: Dict[str, Any]) -> str:
    notes = features.get("notes") or []
    if isinstance(notes, str):
        notes = [notes]
    return " ".join(str(n) for n in notes).lower()


def _get_feature_groups(features: Dict[str, Any]) -> Dict[str, Any]:
    # Your extractor stores groups under `features`
    fg = features.get("features")
    if isinstance(fg, dict):
        return fg
    return {}


def _choose_base_cnc_group(features: Dict[str, Any]) -> str:
    L, W, T = _get_envelope(features)
    weight = float(features.get("finished_weight_kg") or 0.0)
    # conservative rule: bigger/heavier => large VMC
    if weight >= 80.0 or max(L, W, T) >= 800.0 or T >= 60.0:
        return "CNC_MILL_LARGE_4AX"
    return "CNC_MILL_MEDIUM"


def build_route(features: Dict[str, Any]) -> List[Dict[str, Any]]:
    """
    Returns list of operations with:
      op_no, operation, machine_group, description, params, tool_key
    Machine assignment and timing happen downstream.
    """
    drawing_no = str(features.get("drawing_no") or "").strip()
    title = str(features.get("title") or "").strip()
    L, W, T = _get_envelope(features)

    notes_l = _notes_lower(features)
    fg = _get_feature_groups(features)
    cnc_group = _choose_base_cnc_group(features)

    route: List[Dict[str, Any]] = []
    op_no = 10

    def add(operation: str, machine_group: str, description: str, params: Dict[str, Any], tool_key: str) -> None:
        nonlocal op_no, route
        route.append({
            "op_no": op_no,
            "operation": operation,
            "machine_group": machine_group,
            "description": description,
            "params": params,
            "tool_key": tool_key,
        })
        op_no += 10

    # Always: prep / review
    add(
        operation="prep",
        machine_group="BENCH",
        description=f"Review drawing, confirm datum/zero, prep workholding ({drawing_no} {title})".strip(),
        params={"drawing_no": drawing_no, "title": title},
        tool_key="prep",
    )

    # Optional: stock cut (bandsaw / flame-cut handling)
    include_stock_cut = bool(features.get("include_stock_cut", True))
    if include_stock_cut:
        add(
            operation="saw_cut",
            machine_group="SAW",
            description="Cut raw stock/blank to length (allowance assumed)",
            params={"length_mm": L, "width_mm": W, "thickness_mm": T},
            tool_key="saw_cut",
        )
    machine_finish_all_over = ("machine finish" in notes_l) or ("3.2" in notes_l) or ("finish all over" in notes_l)


    # If we have any CNC machining at all, add an explicit CNC setup/program op.
    has_cnc_work = machine_finish_all_over or bool(grooves) or bool(fg.get("drill_thru")) or bool(fg.get("counterbore")) or bool(fg.get("tapped"))
    if has_cnc_work:
        add(
            operation="cnc_setup",
            machine_group=cnc_group,
            description="CNC setup: load/clamp, indicate/probe, tooling, program prove-out (Qty 1)",
            params={"primary_machine_group": cnc_group},
            tool_key="cnc_setup",
        )

    # If drawing indicates machine finish all over, include face + edge work.
    if machine_finish_all_over:
        add(
            operation="face_mill",
            machine_group=cnc_group,
            description="Face/skim both main faces to achieve machine finish / control thickness",
            params={"length_mm": L, "width_mm": W, "faces": 2},
            tool_key="face_mill",
        )
        add(
            operation="edge_mill",
            machine_group=cnc_group,
            description="Edge mill perimeter to finished size (machine finish all over)",
            params={"length_mm": L, "width_mm": W, "thickness_mm": T, "edges": 4},
            tool_key="edge_mill",
        )

    # Grooves (if present)
    grooves = fg.get("grooves") or fg.get("groove") or fg.get("slots") or []
    if isinstance(grooves, dict):
        grooves = [grooves]
    if isinstance(grooves, list):
        for g in grooves:
            try:
                count = int(g.get("count") or 1)
            except Exception:
                count = 1
            width = float(g.get("width_mm") or g.get("groove_width_mm") or 0.0)
            depth = float(g.get("depth_mm") or g.get("groove_depth_mm") or 0.0)
            # Groove length often not explicit; assume runs most of length unless provided.
            glen = float(g.get("length_mm") or (0.9 * L if L else 0.0))
            if width and depth:
                add(
                    operation="groove_mill",
                    machine_group=cnc_group,
                    description=f"Mill {count}x grooves {width:g} wide x {depth:g} deep",
                    params={"count": count, "width_mm": width, "depth_mm": depth, "length_mm": glen},
                    tool_key="groove_mill",
                )

    # Holes: drill thru
    drill_thru = fg.get("drill_thru") or []
    if isinstance(drill_thru, dict):
        drill_thru = [drill_thru]

    # Counterbores
    counterbores = fg.get("counterbore") or []
    if isinstance(counterbores, dict):
        counterbores = [counterbores]

    # Build helper: map drill groups for de-duplication
    drill_index = {}
    for d in drill_thru:
        try:
            key = (int(d.get("count") or 0), float(d.get("dia_mm") or 0.0))
        except Exception:
            continue
        drill_index[key] = True

    # Add drilling ops
    for d in drill_thru:
        count = int(d.get("count") or 0)
        dia = float(d.get("dia_mm") or 0.0)
        if count <= 0 or dia <= 0:
            continue
        add(
            operation="drill",
            machine_group=cnc_group,
            description=f"Drill {count}x Ø{dia:g} THRU",
            params={"count": count, "dia_mm": dia, "thru": True, "depth_mm": T, "feature_group": "drill_thru"},
            tool_key=f"drill_{dia:g}",
        )

    # Add counterbore ops (+ pilot drill if missing)
    for c in counterbores:
        try:
            count = int(c.get("count") or 0)
        except Exception:
            count = 0
        pilot = float(c.get("pilot_dia_mm") or 0.0)
        cd = float(c.get("cbore_dia_mm") or 0.0)
        cdep = float(c.get("cbore_depth_mm") or 0.0)
        if count <= 0 or cd <= 0 or cdep <= 0:
            continue

        # pilot drill if we don't already have it
        if pilot > 0 and not drill_index.get((count, pilot), False):
            add(
                operation="drill",
                machine_group=cnc_group,
                description=f"Drill pilot {count}x Ø{pilot:g} THRU (for counterbore)",
                params={"count": count, "dia_mm": pilot, "thru": True, "depth_mm": T, "feature_group": "counterbore_pilot"},
                tool_key=f"drill_{pilot:g}",
            )

        add(
            operation="counterbore",
            machine_group=cnc_group,
            description=f"Counterbore {count}x Ø{cd:g} x {cdep:g} deep",
            params={"count": count, "pilot_dia_mm": pilot, "cbore_dia_mm": cd, "cbore_depth_mm": cdep, "feature_group": "counterbore"},
            tool_key=f"cbore_{cd:g}",
        )

    # Tapped holes
    tapped = fg.get("tapped") or []
    if isinstance(tapped, dict):
        tapped = [tapped]
    for t in tapped:
        try:
            count = int(t.get("count") or 0)
        except Exception:
            count = 0
        thread = str(t.get("thread") or t.get("thread_raw") or "").strip()
        if count <= 0 or not thread:
            continue
        thru = t.get("thru")
        depth_mm = t.get("depth_mm")
        add(
            operation="tap",
            machine_group=cnc_group,
            description=f"Tap {count}x {thread}" + (" THRU" if thru is True else (f" x {depth_mm:g} deep" if isinstance(depth_mm, (int, float)) else "")),
            params={
                "count": count,
                "thread_raw": thread,
                "thread": thread,
                "thru": thru,
                "depth_mm": float(depth_mm) if isinstance(depth_mm, (int, float)) else None,
                "feature_group": "tapped",
            },
            tool_key=f"tap_{thread.split()[0]}",
        )

    # Deburr/inspect at bench
    add(
        operation="deburr",
        machine_group="BENCH",
        description="Deburr, break edges, inspect",
        params={"length_mm": L, "width_mm": W, "thickness_mm": T},
        tool_key="deburr",
    )

    return route