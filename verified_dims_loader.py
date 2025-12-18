# verified_dims_loader.py
from __future__ import annotations

import json
import os
from typing import Any, Dict, Optional


def verified_dims_path_for_pdf(pdf_path: str) -> str:
    base, _ = os.path.splitext(pdf_path)
    return base + ".dims_verified.json"


def has_verified_dims(pdf_path: str) -> bool:
    return os.path.exists(verified_dims_path_for_pdf(pdf_path))


def load_verified_dims(pdf_path: str) -> Dict[str, Any]:
    path = verified_dims_path_for_pdf(pdf_path)
    if not os.path.exists(path):
        raise FileNotFoundError(f"No verified dims file found: {path}")
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def apply_verified_dims(features: Dict[str, Any], verified_obj: Dict[str, Any]) -> Dict[str, Any]:
    """
    Overwrites ONLY the dimensional envelope fields in the features dict.
    Keeps provenance so downstream routing/time trusts it.
    """
    env = (verified_obj.get("envelope_mm") or {})
    bands = (verified_obj.get("bands_mm") or {})

    # Hard override: these are now "ground truth"
    features["length_mm"] = env.get("length_mm")
    features["width_mm"] = env.get("width_mm")
    features["thickness_mm"] = env.get("thickness_mm")

    # Store explicit tolerance bands for audit / stock logic
    features["verified_bands_mm"] = bands

    # Provenance
    features["dims_source"] = verified_obj.get("status", "VERIFIED_BY_USER_SELECTION")
    features["verified_dims_file"] = verified_obj.get("source_pdf", None)

    return features


def apply_verified_dims_for_pdf(features: Dict[str, Any], pdf_path: str) -> Dict[str, Any]:
    """
    Convenience wrapper: if a verified dims JSON exists, apply it.
    """
    if not has_verified_dims(pdf_path):
        return features
    verified = load_verified_dims(pdf_path)
    return apply_verified_dims(features, verified)
