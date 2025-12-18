"""
routing_engine.py

Provides machine selection logic for a given operation based on:
- machine_capability.get_machine_capabilities() table
- operation_kind (face_mill, drill, tap, etc)
- likely_machine_type (VMC, borer, Boko, bench, etc)
"""

from __future__ import annotations

from typing import Any, Dict, Optional

import pandas as pd


LIKELY_TO_ROUTING_TAG = {
    "VMC": "VMC",
    "vmc": "VMC",
    "Boko": "Boko",
    "boko": "Boko",
    "borer": "borer",
    "horizontal_mill": "horizontal_mill",
    "manual_mill": "horizontal_mill",
    "manual_drill": "manual_drill",
    "drill": "manual_drill",
    "lathe": "lathe",
    "bench": "bench",
    "other": "VMC",
}


def machine_size_ok(
    row: pd.Series,
    length_mm: float,
    width_mm: float,
    thickness_mm: float,
    weight_kg: Optional[float],
) -> bool:
    if row["max_length_mm"] is not None and length_mm and length_mm > row["max_length_mm"]:
        return False
    if row["max_width_mm"] is not None and width_mm and width_mm > row["max_width_mm"]:
        return False
    if row["max_thickness_mm"] is not None and thickness_mm and thickness_mm > row["max_thickness_mm"]:
        return False
    if row["max_weight_kg"] is not None and weight_kg is not None and weight_kg > row["max_weight_kg"]:
        return False
    return True


def machine_score_for_operation(
    row: pd.Series,
    operation_kind: str,
    part_type: str,
    heavy_plate: bool,
    total_holes: int,
) -> float:
    """
    Simple, deterministic score for machine suitability.
    """
    score = 0.0

    score += float(row["speed_factor"])

    suitable_for = str(row["suitable_for_types"] or "")
    if part_type and part_type in suitable_for:
        score += 0.5

    group = str(row["group"] or "")

    if operation_kind in ("face_mill", "face", "dimensional_reduction"):
        score += float(row["can_plate"]) * 1.5
    elif operation_kind in ("drill", "drill_counterbore", "tap"):
        score += float(row["hole_capacity"]) * 1.5
        score += float(row["can_grid"]) * 1.0
    elif operation_kind in ("slot", "groove"):
        score += float(row["can_plate"]) * 1.0
        score += float(row["can_strip"]) * 0.5
    elif operation_kind == "bore":
        score += float(row["can_bore"]) * 2.0
    elif operation_kind == "bench":
        score += 1.0

    # penalise manual for dense holes
    if operation_kind in ("drill", "drill_counterbore", "tap") and total_holes > 20:
        if int(row["manual_only"]) == 1:
            score -= 2.0

    if heavy_plate:
        if "CNC_MILL_LARGE" in group:
            score += 1.0
        if "CNC_MILL_MEDIUM" in group:
            score -= 0.3
        if operation_kind in ("face_mill", "slot", "bore"):
            score += float(row["can_plate"]) * 0.5

    return score


def choose_machine_for_operation(
    machines: pd.DataFrame,
    likely_machine_type: str,
    operation_kind: str,
    part_type: str,
    heavy_plate: bool,
    length_mm: float,
    width_mm: float,
    thickness_mm: float,
    weight_kg: Optional[float],
    total_holes: int,
) -> Optional[pd.Series]:
    """
    Filter and score machines to choose the most appropriate one for a given op.
    """
    routing_tag = LIKELY_TO_ROUTING_TAG.get(likely_machine_type, "VMC")

    subset_base = machines[machines["routing_tag"].str.lower() == routing_tag.lower()].copy()
    if subset_base.empty:
        subset_base = machines.copy()

    subset = subset_base[
        subset_base.apply(lambda r: machine_size_ok(r, length_mm, width_mm, thickness_mm, weight_kg), axis=1)
    ]

    if subset.empty:
        subset = subset_base

    if subset.empty:
        subset = machines.copy()

    subset = subset.copy()
    subset["routing_score"] = subset.apply(
        lambda r: machine_score_for_operation(r, operation_kind, part_type, heavy_plate, total_holes),
        axis=1,
    )
    subset = subset.sort_values("routing_score", ascending=False)
    return subset.iloc[0] if not subset.empty else None
