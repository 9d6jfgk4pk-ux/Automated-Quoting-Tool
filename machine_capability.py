#!/usr/bin/env python
"""
machine_capability.py

Single source of truth for your shop's machine list + capability + default timing knobs.

Design goals
- Deterministic (no AI)
- Editable without touching the rest of the codebase
- Gives machine_allocator + time_model enough information to:
  - decide which machine(s) can physically take the part
  - apply sensible base setup times (first setup vs repeat)
  - apply speed/rate differences between machines

IMPORTANT
- This file is safe to edit as your shop learns.
- Keep units in mm, kg, minutes.
"""

from __future__ import annotations

from dataclasses import dataclass, asdict
from typing import Any, Dict, List, Optional


@dataclass(frozen=True)
class Machine:
    # Identity
    machine_name: str
    machine_group: str          # e.g. BENCH, SAW, CNC_MILL_LARGE_4AX, BOKO, BORER
    routing_tag: str            # short human tag used in route tables (e.g. VMC, BOKO, bench)

    # Capacity (rough; used for filtering)
    max_length_mm: float
    max_width_mm: float
    max_height_mm: float
    max_weight_kg: Optional[float] = None

    # Setup templates (minutes) for the *machine* (not per operation)
    # NOTE: setup time for additional faces/flip is handled in time_model.
    setup_template_first_min: float = 60.0
    setup_template_repeat_min: float = 20.0

    # Overall speed factor (1.0 = baseline). Runtime is divided by this.
    speed_factor: float = 1.0

    # Process-specific rates/knobs (optional overrides)
    # If None/missing, time_model will use conservative defaults.
    face_mill_mrr_mm3_min: Optional[float] = None
    edge_mill_mrr_mm3_min: Optional[float] = None
    groove_mrr_mm3_min: Optional[float] = None

    # Drill feed proxy (higher = faster). Used as a multiplier vs defaults.
    drill_speed_factor: Optional[float] = None
    tap_speed_factor: Optional[float] = None
    cbore_speed_factor: Optional[float] = None


def get_machine_capabilities() -> List[Dict[str, Any]]:
    """
    Returns a list of dict records (stable for JSON / Streamlit display).
    """
    machines: List[Machine] = [
        # Bench / fitting
        Machine(
            machine_name="Bench / Fitting Area",
            machine_group="BENCH",
            routing_tag="bench",
            max_length_mm=10_000,
            max_width_mm=10_000,
            max_height_mm=10_000,
            max_weight_kg=None,
            setup_template_first_min=0.0,
            setup_template_repeat_min=0.0,
            speed_factor=1.0,
        ),

        # Bandsaw / stock prep
        Machine(
            machine_name="Bandsaw",
            machine_group="SAW",
            routing_tag="SAW",
            max_length_mm=6_000,
            max_width_mm=1_000,
            max_height_mm=500,
            max_weight_kg=2_000,
            setup_template_first_min=0.0,
            setup_template_repeat_min=0.0,
            speed_factor=1.0,
        ),

        # Medium VMC
        Machine(
            machine_name="VM1000 Microcut",
            machine_group="CNC_MILL_MEDIUM",
            routing_tag="VMC",
            max_length_mm=1_000,
            max_width_mm=500,
            max_height_mm=500,
            max_weight_kg=500,
            setup_template_first_min=60.0,      # baseline from your earlier prompt
            setup_template_repeat_min=20.0,
            speed_factor=0.90,
            face_mill_mrr_mm3_min=12_000,
            edge_mill_mrr_mm3_min=9_000,
            groove_mrr_mm3_min=5_000,
            drill_speed_factor=0.90,
            tap_speed_factor=0.90,
            cbore_speed_factor=0.90,
        ),

        # Large VMC (Leadwell 1300 class)
        Machine(
            machine_name="Leadwell MCV 1300P",
            machine_group="CNC_MILL_LARGE_4AX",
            routing_tag="VMC",
            max_length_mm=1_300,
            max_width_mm=700,
            max_height_mm=650,
            max_weight_kg=1_500,
            setup_template_first_min=90.0,      # baseline from your earlier prompt
            setup_template_repeat_min=30.0,
            speed_factor=1.00,
            face_mill_mrr_mm3_min=15_000,
            edge_mill_mrr_mm3_min=10_000,
            groove_mrr_mm3_min=6_000,
            drill_speed_factor=1.00,
            tap_speed_factor=1.00,
            cbore_speed_factor=1.00,
        ),

        # Very large VMC (Leadwell 1500 class) – keep if you have it
        Machine(
            machine_name="Leadwell MCV 1500i",
            machine_group="CNC_MILL_LARGE",
            routing_tag="VMC",
            max_length_mm=1_500,
            max_width_mm=800,
            max_height_mm=700,
            max_weight_kg=2_000,
            setup_template_first_min=100.0,
            setup_template_repeat_min=35.0,
            speed_factor=1.05,
            face_mill_mrr_mm3_min=16_000,
            edge_mill_mrr_mm3_min=11_000,
            groove_mrr_mm3_min=6_500,
            drill_speed_factor=1.05,
            tap_speed_factor=1.05,
            cbore_speed_factor=1.05,
        ),

        # BOKO (used for perimeter work / ergonomic edge work)
        Machine(
            machine_name="BOKO F4",
            machine_group="BOKO",
            routing_tag="BOKO",
            max_length_mm=2_000,
            max_width_mm=1_000,
            max_height_mm=1_000,
            max_weight_kg=2_000,
            setup_template_first_min=45.0,
            setup_template_repeat_min=15.0,
            speed_factor=1.10,
            face_mill_mrr_mm3_min=14_000,
            edge_mill_mrr_mm3_min=16_000,
            groove_mrr_mm3_min=7_000,
            drill_speed_factor=0.85,
            tap_speed_factor=0.85,
            cbore_speed_factor=0.85,
        ),

        # Horizontal borer (optional)
        Machine(
            machine_name="Horizontal Borer",
            machine_group="BORER",
            routing_tag="BORER",
            max_length_mm=3_000,
            max_width_mm=1_500,
            max_height_mm=1_500,
            max_weight_kg=5_000,
            setup_template_first_min=120.0,
            setup_template_repeat_min=40.0,
            speed_factor=0.85,
        ),
    ]

    return [asdict(m) for m in machines]


def get_machine_by_name(name: str) -> Optional[Dict[str, Any]]:
    for m in get_machine_capabilities():
        if m["machine_name"] == name:
            return m
    return None
