"""
feature_extractor_v2.py

Deterministic PDF-text feature extraction.

Key fixes vs v1:
- Robust parsing for drill + counterbore callouts even when the PDF text extractor interleaves
  unrelated notes between "THRO" and "C/BORE" (common in engineering drawings).
- Robust parsing for groove callouts where "WIDE" and "BY ... DEEP" can be separated/interleaved.
- Better handling of tapped callouts like "M20 x 40 DEEP" (depth=40mm).
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Tuple

import pdfplumber


def _f(x: str) -> Optional[float]:
    try:
        return float(x)
    except Exception:
        return None


def _norm_text(s: str) -> str:
    # Diameter symbols
    s = s.replace("⌀", "Ø").replace("∅", "Ø").replace("ø", "Ø")
    # Hyphens
    s = s.replace("\u2011", "-").replace("\u2013", "-").replace("\u2014", "-")
    # Collapse spaces
    s = re.sub(r"[ \t]+", " ", s)
    return s


def _extract_pdf_text(pdf_path: str) -> str:
    chunks: List[str] = []
    with pdfplumber.open(pdf_path) as pdf:
        for p in pdf.pages:
            t = p.extract_text() or ""
            if t.strip():
                chunks.append(t)
    return _norm_text("\n".join(chunks))


def _merge_by_key(items: List[Dict[str, Any]], key_fields: Tuple[str, ...], sum_field: str = "count") -> List[Dict[str, Any]]:
    merged: Dict[Tuple[Any, ...], Dict[str, Any]] = {}
    for it in items:
        k = tuple(it.get(f) for f in key_fields)
        if k not in merged:
            merged[k] = dict(it)
        else:
            merged[k][sum_field] = float(merged[k].get(sum_field, 0) or 0) + float(it.get(sum_field, 0) or 0)
    return list(merged.values())


# -------------------------
# Regex patterns (tuned for messy extraction ordering)
# -------------------------

RE_DRAWING_NO = re.compile(r"(?i)\bDRAWING\s*No\.?\s*([0-9]{4,})\b")
RE_TITLE = re.compile(r"(?i)\bTITLE\b\s*([A-Z0-9 \-/]+)")
RE_MATERIAL = re.compile(r"(?i)\bMATERIAL\b\s*([A-Z0-9 \-/]+)")
RE_WEIGHT = re.compile(r"(?i)\bFINISHED\s+WEIGHT\b\s*([0-9]+(?:\.[0-9]+)?)\s*Kg\b")

# Drill + counterbore: allow up to 120 chars (incl newlines) between THRO and C/BORE
RE_DRILL_AND_CBORE = re.compile(
    r"(?is)\b(\d+)\s*[- ]\s*HOLES?\s+DRILL\s*Ø?\s*([0-9]+(?:\.[0-9]+)?)\s*THRO\.?"
    r"[\s\S]{0,120}?C/?BORE\s*Ø?\s*([0-9]+(?:\.[0-9]+)?)\s*x\s*([0-9]+(?:\.[0-9]+)?)\s*DEEP"
)

RE_DRILL_THRU = re.compile(r"(?is)\b(\d+)\s*[- ]\s*HOLES?\s+DRILL\s*Ø?\s*([0-9]+(?:\.[0-9]+)?)\s*THRO\b")

RE_TAPPED = re.compile(
    r"(?is)\b(\d+)\s*[- ]\s*HOLES?\s+TAPPED\s+(M\d+(?:\s*x\s*[0-9]+(?:\.[0-9]+)?)?)\s*(THRO\.?|DEEP\.?)?\b"
)

# Grooves: allow interleaving text between WIDE and BY ... DEEP
RE_GROOVE = re.compile(
    r"(?is)\b(?:(\d+)\s*[- ]\s*)?GROOVES?\s*([0-9]+(?:\.[0-9]+)?)\s*WIDE"
    r"[\s\S]{0,120}?BY\s*([0-9]+(?:\.[0-9]+)?)\s*DEEP\b"
)

RE_MACHINE_FINISH = re.compile(r"(?i)\bmachine\s+finish\b.*\ball\s+over\b")


def extract_features(pdf_path: str) -> Dict[str, Any]:
    text = _extract_pdf_text(pdf_path)
    warnings: List[str] = []

    drawing_no = None
    m = RE_DRAWING_NO.search(text)
    if m:
        drawing_no = m.group(1).strip()

    title = None
    m = RE_TITLE.search(text)
    if m:
        title = m.group(1).strip()

    material = None
    m = RE_MATERIAL.search(text)
    if m:
        material = m.group(1).strip()

    finished_weight_kg = None
    m = RE_WEIGHT.search(text)
    if m:
        finished_weight_kg = _f(m.group(1))

    notes: List[str] = []
    if RE_MACHINE_FINISH.search(text):
        notes.append("machine finish all over")

    drill_thru: List[Dict[str, Any]] = []
    tapped: List[Dict[str, Any]] = []
    cbore: List[Dict[str, Any]] = []
    grooves: List[Dict[str, Any]] = []

    # 1) Drill + counterbore combined
    cbore_spans: List[Tuple[int, int]] = []
    for m in RE_DRILL_AND_CBORE.finditer(text):
        cbore_spans.append(m.span())
        count = int(m.group(1))
        hole_d = _f(m.group(2))
        cb_d = _f(m.group(3))
        cb_depth = _f(m.group(4))
        if hole_d is None or cb_d is None or cb_depth is None:
            continue

        drill_thru.append({"count": count, "dia_mm": float(hole_d)})
        cbore.append({"count": count, "pilot_dia_mm": float(hole_d), "cbore_dia_mm": float(cb_d), "cbore_depth_mm": float(cb_depth)})

    def _overlaps(span: Tuple[int, int], spans: List[Tuple[int, int]]) -> bool:
        a0, a1 = span
        for b0, b1 in spans:
            if a0 < b1 and b0 < a1:
                return True
        return False

    # 2) Standalone drill thru (skip if overlapped by a combined match)
    for m in RE_DRILL_THRU.finditer(text):
        if _overlaps(m.span(), cbore_spans):
            continue
        count = int(m.group(1))
        d = _f(m.group(2))
        if d is None:
            continue
        drill_thru.append({"count": count, "dia_mm": float(d)})

    # 3) Tapped holes
    for m in RE_TAPPED.finditer(text):
        count = int(m.group(1))
        thread_raw = (m.group(2) or "").strip().upper()
        mode = (m.group(3) or "").strip().upper()

        thru: Optional[bool]
        depth_mm: Optional[float]

        if "THRO" in mode:
            thru = True
            depth_mm = None
        elif "DEEP" in mode:
            thru = False
            # Interpret "M20 x 40 DEEP" as depth=40
            dm = re.search(r"\bM\d+\s*x\s*([0-9]+(?:\.[0-9]+)?)\b", thread_raw, flags=re.I)
            depth_mm = _f(dm.group(1)) if dm else None
        else:
            thru = None
            depth_mm = None

        base_thread = re.search(r"\b(M\d+)\b", thread_raw, flags=re.I)
        thread = base_thread.group(1).upper() if base_thread else thread_raw

        tapped.append({"count": count, "thread": thread, "thread_raw": thread_raw, "depth_mm": depth_mm, "thru": thru})

    # 4) Grooves
    for m in RE_GROOVE.finditer(text):
        count_s = m.group(1)
        count = int(count_s) if count_s else None
        width = _f(m.group(2))
        depth = _f(m.group(3))
        if width is None or depth is None:
            continue
        grooves.append({"count": count, "width_mm": float(width), "depth_mm": float(depth)})

    # Merge obvious duplicates
    drill_thru = _merge_by_key(drill_thru, ("dia_mm",))
    tapped = _merge_by_key(tapped, ("thread", "depth_mm", "thru"), sum_field="count")
    cbore = _merge_by_key(cbore, ("pilot_dia_mm", "cbore_dia_mm", "cbore_depth_mm"), sum_field="count")

    # Warnings / sanity checks
    for c in cbore:
        pilot = float(c.get("pilot_dia_mm") or 0)
        cnt = int(c.get("count") or 0)
        if pilot <= 0 or cnt <= 0:
            continue
        match = any(int(d.get("count") or 0) == cnt and abs(float(d.get("dia_mm") or 0) - pilot) < 1e-6 for d in drill_thru)
        if not match:
            warnings.append(f"Counterbore {cnt}x pilot Ø{pilot:g} has no matching drill-through group; check extraction.")

    if grooves and all(g.get("count") is None for g in grooves):
        warnings.append("Grooves detected but count not stated; default assumption will be required for routing/time.")

    return {
        "drawing_no": drawing_no,
        "title": title,
        "material": material,
        "finished_weight_kg": finished_weight_kg,
        "notes": notes,
        "holes": {"drill_thru": drill_thru, "tapped": tapped, "counterbore": cbore},
        "grooves": grooves,
        "warnings": warnings,
        "raw_text": text,
    }
