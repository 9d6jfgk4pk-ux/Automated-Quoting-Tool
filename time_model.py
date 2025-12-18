#!/usr/bin/env python
"""
time_model.py

Deterministic time estimation for a routed part.

Why your previous totals were too low:
- You were mostly counting "ideal spindle time" and small per-op setups.
- One-off quoting must include: CAM/programming effort, heavy-part handling, probing, tool changes,
  realistic feed/peck/cycle overhead, and multi-face work (flip).

This model stays modular:
- route_builder decides *what* ops exist
- machine_allocator decides *where* (which machine)
- time_model decides *how long* (objective, calibrated by constants)

You should tune ONLY the constants below (or in machine_capability.py),
not the formulas, once you start collecting real data.
"""

from __future__ import annotations

import math
import re
from typing import Any, Dict, List, Optional, Tuple


# ----------------------------
# Global calibration constants
# ----------------------------

# Stock/finish assumptions
DEFAULT_FINISH_ALLOWANCE_FACE_MM = 1.0        # per face skim allowance when "machine finish all over"
DEFAULT_FINISH_ALLOWANCE_EDGE_MM = 2.0        # total stock removed across width/length (approx)
DEFAULT_GROOVE_LENGTH_FRACTION = 0.90         # groove length assumed if not provided

# One-off overheads
BASE_CAM_MIN = 20.0                           # minimum CAM/programming effort per part
CAM_MIN_PER_OPERATION_TYPE = 6.0              # per distinct machining op family
CAM_MIN_PER_HOLE = 0.35                       # per hole (drill/cbore/tap) adds CAM time
CAM_MIN_HEAVY_PART_BONUS = 15.0               # extra thinking/handling for heavy parts

# Handling / flip
HEAVY_WEIGHT_KG = 80.0
FLIP_SETUP_LIGHT_MIN = 15.0
FLIP_SETUP_HEAVY_MIN = 45.0

# Toolchange/overheads (minutes)
TOOLCHANGE_MIN = 2.0
OP_START_OVERHEAD_MIN = 1.0                   # approach / safe start / confirm offsets

# Default material removal rates (mm^3/min) if machine does not override
DEFAULT_FACE_MRR = 15_000.0
DEFAULT_EDGE_MRR = 10_000.0
DEFAULT_GROOVE_MRR = 6_000.0

# Hole cycle-time knobs (minutes)
INDEX_MIN_PER_HOLE = 0.20                     # move/index between holes (incl. settle)
APPROACH_RETRACT_MIN = 0.30                   # per hole
CHIP_CLEAR_MIN_PER_10_HOLES = 1.0

# Drilling feed proxy (mm/min) baseline by diameter band (very conservative, includes pecking reality)
DRILL_FEED_MM_MIN_SMALL = 140.0   # ~<=10mm
DRILL_FEED_MM_MIN_MED = 170.0     # ~10-16mm
DRILL_FEED_MM_MIN_LARGE = 200.0   # >16mm

# Peck multiplier based on depth/(5*dia)
def _peck_factor(depth_mm: float, dia_mm: float) -> float:
    if depth_mm <= 0 or dia_mm <= 0:
        return 1.0
    ratio = depth_mm / (5.0 * dia_mm)
    if ratio <= 1.0:
        return 1.15
    # grows moderately with depth; clamp to avoid runaway
    return min(2.0, 1.15 + 0.55 * (ratio - 1.0))


# Counterbore time model (minutes per hole)
# Conservative because many shops interpolate pockets rather than use a dedicated counterbore tool.
def _cbore_min_per_hole(cbore_dia_mm: float, cbore_depth_mm: float) -> float:
    # baseline + depth + diameter effect
    return 2.5 + 0.05 * cbore_depth_mm + 0.02 * cbore_dia_mm


# Tapping combined time (drill+tap) minutes per hole by thread class (qty=1 quoting)
# This intentionally includes chip clearing, alignment caution, reversing, and verification.
TAP_MIN_PER_HOLE_BASE = {
    "M6": 2.0,
    "M8": 2.6,
    "M10": 3.5,
    "M12": 4.2,
    "M16": 5.5,
    "M20": 7.0,
    "M24": 9.0,
}


# Deburr/inspect model
DEBURR_BASE_MIN = 15.0
DEBURR_MIN_PER_FEATURE = 0.50                 # per hole/counterbore/tap feature edge
DEBURR_HEAVY_BONUS_MIN = 10.0


# ----------------------------
# Helpers
# ----------------------------

_THREAD_RX = re.compile(r"\bM(\d+)\b", re.IGNORECASE)


def tool_family(tool_key: str) -> str:
    k = (tool_key or "").lower()
    if k.startswith("drill"):
        return "drill"
    if k.startswith("cbore") or k.startswith("counterbore"):
        return "counterbore"
    if k.startswith("tap"):
        return "tap"
    if k.startswith("cnc_setup") or k == "cnc_setup":
        return "cnc_setup"
    if "face" in k:
        return "face_mill"
    if "edge" in k:
        return "edge_mill"
    if "groove" in k or "slot" in k:
        return "groove_mill"
    if "deburr" in k:
        return "deburr"
    if "prep" in k:
        return "prep"
    if "saw" in k or "cut" in k:
        return "saw_cut"
    return "general"


def _get_envelope(features: Dict[str, Any]) -> Tuple[float, float, float]:
    env = features.get("envelope_mm") or {}
    L = float(env.get("length_mm") or 0.0)
    W = float(env.get("width_mm") or 0.0)
    T = float(env.get("thickness_mm") or 0.0)
    if W > L:
        L, W = W, L
    return L, W, T


def _weight(features: Dict[str, Any]) -> float:
    try:
        return float(features.get("finished_weight_kg") or 0.0)
    except Exception:
        return 0.0


def _notes_lower(features: Dict[str, Any]) -> str:
    notes = features.get("notes") or []
    if isinstance(notes, str):
        notes = [notes]
    return " ".join(str(n) for n in notes).lower()


def _count_holes(features: Dict[str, Any]) -> int:
    fg = features.get("features") or {}
    total = 0
    for k in ("drill_thru", "counterbore", "tapped"):
        v = fg.get(k) or []
        if isinstance(v, dict):
            v = [v]
        if isinstance(v, list):
            for it in v:
                try:
                    total += int(it.get("count") or 0)
                except Exception:
                    pass
    return total


def _distinct_machining_families(route: List[Dict[str, Any]]) -> int:
    fams = set()
    for op in route:
        fams.add(tool_family(op.get("tool_key") or op.get("operation") or ""))
    # Do not count prep/deburr/saw as CAM complexity
    fams -= {"prep", "deburr", "saw_cut", "general"}
    return len(fams)


def estimate_cam_minutes(route: List[Dict[str, Any]], features: Dict[str, Any]) -> float:
    """
    CAM/programming overhead for Qty 1.
    This is added into the first CNC setup so the route table stays shop-focused.
    """
    holes = _count_holes(features)
    families = _distinct_machining_families(route)
    w = _weight(features)

    cam = BASE_CAM_MIN
    cam += families * CAM_MIN_PER_OPERATION_TYPE
    cam += holes * CAM_MIN_PER_HOLE
    if w >= HEAVY_WEIGHT_KG:
        cam += CAM_MIN_HEAVY_PART_BONUS

    # Clamp to sensible bounds
    return float(max(15.0, min(cam, 240.0)))   # 0.25 hr .. 4 hr


def _machine_rate(op: Dict[str, Any], key: str, default: float) -> float:
    v = op.get(key)
    try:
        return float(v) if v is not None else float(default)
    except Exception:
        return float(default)


def _machine_speed_factor(op: Dict[str, Any], family: str) -> float:
    # overall speed factor already applied; family-specific optional factors:
    sf = float(op.get("speed_factor") or 1.0)
    fam = 1.0
    if family == "drill":
        fam = float(op.get("drill_speed_factor") or 1.0)
    elif family == "tap":
        fam = float(op.get("tap_speed_factor") or 1.0)
    elif family == "counterbore":
        fam = float(op.get("cbore_speed_factor") or 1.0)
    return max(0.3, sf * fam)


def _drill_feed_mm_min(dia_mm: float) -> float:
    if dia_mm <= 10.0:
        return DRILL_FEED_MM_MIN_SMALL
    if dia_mm <= 16.0:
        return DRILL_FEED_MM_MIN_MED
    return DRILL_FEED_MM_MIN_LARGE


def _parse_thread_size(thread: str) -> Optional[str]:
    m = _THREAD_RX.search(thread or "")
    if not m:
        return None
    return f"M{int(m.group(1))}"


# ----------------------------
# Runtime estimators by op
# ----------------------------

def _runtime_prep_min(features: Dict[str, Any]) -> float:
    # review + workholding thinking, heavier parts take longer to handle
    w = _weight(features)
    base = 15.0
    if w >= HEAVY_WEIGHT_KG:
        base += 5.0
    return base


def _runtime_saw_cut_min(features: Dict[str, Any]) -> float:
    # Simple constant with mild scaling for thickness
    _, _, T = _get_envelope(features)
    base = 12.0
    if T >= 60.0:
        base += 3.0
    return base


def _runtime_face_mill_min(op: Dict[str, Any], features: Dict[str, Any]) -> float:
    L, W, _ = _get_envelope(features)
    faces = int((op.get("params") or {}).get("faces") or 1)
    # Heavy, thick parts often need more cleanup to achieve "machine finish all over".
    _, _, T = _get_envelope(features)
    w_part = _weight(features)
    allowance = DEFAULT_FINISH_ALLOWANCE_FACE_MM
    if w_part >= HEAVY_WEIGHT_KG or T >= 60.0:
        allowance = max(allowance, 3.0)

    area = max(0.0, L) * max(0.0, W)
    vol = area * allowance * max(1, faces)  # mm^3
    mrr = _machine_rate(op, "face_mill_mrr_mm3_min", DEFAULT_FACE_MRR)
    base = (vol / mrr) if mrr > 0 else 0.0
    overhead = 10.0 * max(1, faces) + OP_START_OVERHEAD_MIN
    return base + overhead


def _runtime_edge_mill_min(op: Dict[str, Any], features: Dict[str, Any]) -> float:
    L, W, T = _get_envelope(features)
    perim = 2.0 * (max(0.0, L) + max(0.0, W))
    allowance = DEFAULT_FINISH_ALLOWANCE_EDGE_MM
    # approximate volume removed on edges
    vol = perim * max(0.0, T) * allowance
    mrr = _machine_rate(op, "edge_mill_mrr_mm3_min", DEFAULT_EDGE_MRR)
    base = (vol / mrr) if mrr > 0 else 0.0
    overhead = 15.0 + OP_START_OVERHEAD_MIN
    return base + overhead


def _runtime_groove_mill_min(op: Dict[str, Any], features: Dict[str, Any]) -> float:
    L, _, _ = _get_envelope(features)
    p = op.get("params") or {}
    count = int(p.get("count") or 1)
    w = float(p.get("width_mm") or 0.0)
    d = float(p.get("depth_mm") or 0.0)
    glen = float(p.get("length_mm") or (DEFAULT_GROOVE_LENGTH_FRACTION * L if L else 0.0))

    vol = max(0.0, count) * max(0.0, w) * max(0.0, d) * max(0.0, glen)
    mrr = _machine_rate(op, "groove_mrr_mm3_min", DEFAULT_GROOVE_MRR)
    base = (vol / mrr) if mrr > 0 else 0.0
    overhead = 10.0 + OP_START_OVERHEAD_MIN
    return base + overhead


def _runtime_drill_min(op: Dict[str, Any], features: Dict[str, Any]) -> float:
    p = op.get("params") or {}
    count = int(p.get("count") or 0)
    dia = float(p.get("dia_mm") or 0.0)
    depth = float(p.get("depth_mm") or 0.0)

    if count <= 0 or dia <= 0 or depth <= 0:
        return 0.0

    feed = _drill_feed_mm_min(dia)
    cut_min = depth / feed if feed > 0 else 0.0
    pf = _peck_factor(depth, dia)
    per_hole = (APPROACH_RETRACT_MIN + cut_min) * pf + INDEX_MIN_PER_HOLE
    total = count * per_hole
    total += TOOLCHANGE_MIN + OP_START_OVERHEAD_MIN
    total += (count / 10.0) * CHIP_CLEAR_MIN_PER_10_HOLES
    return total


def _runtime_counterbore_min(op: Dict[str, Any], features: Dict[str, Any]) -> float:
    p = op.get("params") or {}
    count = int(p.get("count") or 0)
    cd = float(p.get("cbore_dia_mm") or 0.0)
    cdep = float(p.get("cbore_depth_mm") or 0.0)

    if count <= 0 or cd <= 0 or cdep <= 0:
        return 0.0

    per = _cbore_min_per_hole(cd, cdep) + INDEX_MIN_PER_HOLE
    total = count * per
    total += TOOLCHANGE_MIN + OP_START_OVERHEAD_MIN
    total += (count / 10.0) * CHIP_CLEAR_MIN_PER_10_HOLES
    return total


def _runtime_tap_min(op: Dict[str, Any], features: Dict[str, Any]) -> float:
    p = op.get("params") or {}
    count = int(p.get("count") or 0)
    thread = str(p.get("thread") or p.get("thread_raw") or "").strip()
    if count <= 0 or not thread:
        return 0.0

    tsize = _parse_thread_size(thread) or thread.split()[0].upper()
    base_per = TAP_MIN_PER_HOLE_BASE.get(tsize, 6.0)

    # Depth scaling
    L, W, T = _get_envelope(features)
    thru = p.get("thru")
    depth = p.get("depth_mm")
    if thru is True:
        eff_depth = T if T > 0 else 40.0
    else:
        eff_depth = float(depth) if isinstance(depth, (int, float)) and depth else 40.0

    # Scale linearly against 40mm reference
    per = base_per * (eff_depth / 40.0)
    per += 0.25  # extra check/clearance per hole
    total = count * (per + INDEX_MIN_PER_HOLE)
    total += TOOLCHANGE_MIN + OP_START_OVERHEAD_MIN
    return total


def _runtime_deburr_min(features: Dict[str, Any]) -> float:
    fg = features.get("features") or {}
    n = 0
    for k in ("drill_thru", "counterbore", "tapped"):
        v = fg.get(k) or []
        if isinstance(v, dict):
            v = [v]
        if isinstance(v, list):
            for it in v:
                try:
                    n += int(it.get("count") or 0)
                except Exception:
                    pass

    w = _weight(features)
    total = DEBURR_BASE_MIN + n * DEBURR_MIN_PER_FEATURE
    if w >= HEAVY_WEIGHT_KG:
        total += DEBURR_HEAVY_BONUS_MIN
    return total


# ----------------------------
# Main estimator
# ----------------------------

def estimate_times(route: List[Dict[str, Any]], features: Dict[str, Any]) -> List[Dict[str, Any]]:
    """
    Takes a route with machine assignments and appends:
      setup_min, runtime_min, total_min
      setup_hr, runtime_hr, total_hr
      time_inputs (debug)
    """
    if not isinstance(route, list):
        raise TypeError("route must be a list of op dicts")

    cam_min = estimate_cam_minutes(route, features)
    notes_l = _notes_lower(features)
    weight = _weight(features)

    # Determine first CNC op index (where to attach CAM time)
    first_cnc_idx: Optional[int] = None
    for i, op in enumerate(route):
        fam = tool_family(op.get("tool_key") or op.get("operation") or "")
        if fam in ("cnc_setup", "face_mill", "edge_mill", "groove_mill", "drill", "counterbore", "tap"):
            first_cnc_idx = i
            break

    out: List[Dict[str, Any]] = []
    prev_machine_name: Optional[str] = None

    for i, op0 in enumerate(route):
        op = dict(op0)
        fam = tool_family(op.get("tool_key") or op.get("operation") or "")
        machine_name = op.get("machine_name") or op.get("machine_group") or "UNKNOWN"

        # Setup time depends on whether we changed machines
        if machine_name != prev_machine_name:
            setup_min = float(op.get("setup_template_first_min") or 0.0)
        else:
            # Same machine, same setup: do not charge another full setup by default.
            setup_min = 0.0
            if bool(op.get("new_setup")):
                st_rep = op.get("setup_template_repeat_min")
                setup_min = float(0.0 if st_rep is None else st_rep)

        # Attach CAM/programming time to first CNC setup (qty=1)
        if first_cnc_idx is not None and i == first_cnc_idx:
            setup_min += cam_min

        # Flip handling for multi-face work (face milling faces>1)
        if fam == "face_mill":
            faces = int(((op.get("params") or {}).get("faces") or 1))
            if faces > 1:
                flip = FLIP_SETUP_HEAVY_MIN if weight >= HEAVY_WEIGHT_KG else FLIP_SETUP_LIGHT_MIN
                setup_min += (faces - 1) * flip

        # Runtime (pre machine scaling)
        if fam == "prep":
            runtime_min = _runtime_prep_min(features)
        elif fam == "saw_cut":
            runtime_min = _runtime_saw_cut_min(features)
        elif fam == "cnc_setup":
            runtime_min = 0.0
        elif fam == "face_mill":
            runtime_min = _runtime_face_mill_min(op, features)
        elif fam == "edge_mill":
            runtime_min = _runtime_edge_mill_min(op, features)
        elif fam == "groove_mill":
            runtime_min = _runtime_groove_mill_min(op, features)
        elif fam == "drill":
            runtime_min = _runtime_drill_min(op, features)
        elif fam == "counterbore":
            runtime_min = _runtime_counterbore_min(op, features)
        elif fam == "tap":
            runtime_min = _runtime_tap_min(op, features)
        elif fam == "deburr":
            runtime_min = _runtime_deburr_min(features)
        else:
            runtime_min = 0.0

        # Scale runtime by machine speed
        sf = _machine_speed_factor(op, fam if fam != "general" else "general")
        runtime_min = float(runtime_min) / float(sf if sf else 1.0)

        total_min = float(setup_min) + float(runtime_min)

        op["setup_min"] = round(float(setup_min), 2)
        op["runtime_min"] = round(float(runtime_min), 2)
        op["total_min"] = round(float(total_min), 2)

        op["setup_hr"] = round(op["setup_min"] / 60.0, 3)
        op["runtime_hr"] = round(op["runtime_min"] / 60.0, 3)
        op["total_hr"] = round(op["total_min"] / 60.0, 3)

        op["time_inputs"] = {
            "cam_min_attached_if_first_cnc": (i == first_cnc_idx),
            "cam_min": round(cam_min, 1),
            "machine_speed_factor_used": round(sf, 3),
            "family": fam,
        }

        out.append(op)
        prev_machine_name = machine_name

    return out


def route_totals(route: List[Dict[str, Any]]) -> Dict[str, float]:
    total_min = 0.0
    for op in route:
        try:
            total_min += float(op.get("total_min") or 0.0)
        except Exception:
            pass
    return {
        "total_min": round(total_min, 2),
        "total_hr": round(total_min / 60.0, 3),
    }