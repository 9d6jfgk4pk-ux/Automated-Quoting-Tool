# run_quote.py
from __future__ import annotations

import json
import os
import sys

from feature_extractor import extract_features
from verified_dims_loader import has_verified_dims, apply_verified_dims_for_pdf
from route_builder import build_route
from machine_allocator import allocate_machines
from time_model import estimate_times
from exporters import export_quote_bundle


def main():
    if len(sys.argv) < 2:
        print("Usage: python run_quote.py <pdf_path> [--outdir <folder>] [--no-print]")
        raise SystemExit(2)

    pdf_path = sys.argv[1]
    if not os.path.exists(pdf_path):
        raise FileNotFoundError(pdf_path)

    outdir = "outputs"
    no_print = False
    if "--outdir" in sys.argv:
        i = sys.argv.index("--outdir")
        if i + 1 >= len(sys.argv):
            raise SystemExit("Missing value after --outdir")
        outdir = sys.argv[i + 1]
    if "--no-print" in sys.argv:
        no_print = True

    features = extract_features(pdf_path)

    if has_verified_dims(pdf_path):
        features = apply_verified_dims_for_pdf(features, pdf_path)
    else:
        features.setdefault("warnings", []).append("No .dims_verified.json found; envelope dims not applied. STOP.")
        if not no_print:
            print(json.dumps(features, indent=2))
        raise SystemExit(2)

    route = build_route(features)
    route = allocate_machines(route, features)
    route = estimate_times(route, features)

    total_min = sum(op.get("total_min", 0) for op in route)
    total_hr = total_min / 60.0

    result = {
        "drawing_no": features.get("drawing_no"),
        "title": features.get("title"),
        "material": features.get("material"),
        "finished_weight_kg": features.get("finished_weight_kg"),
        "dims_source": features.get("dims_source"),
        "envelope_mm": {
            "length_mm": features.get("length_mm"),
            "width_mm": features.get("width_mm"),
            "thickness_mm": features.get("thickness_mm"),
        },
        "verified_bands_mm": features.get("verified_bands_mm"),
        "notes": features.get("notes", []),
        "features": features.get("holes", {}),
        "route": route,
        "totals": {
            "total_min": round(total_min, 2),
            "total_hr": round(total_hr, 3),
        },
        "warnings": features.get("warnings", []),
    }

    paths = export_quote_bundle(result, outdir=outdir)
    result["export_paths"] = paths
    result["status"] = "OK"

    if not no_print:
        print(json.dumps(result, indent=2))
    else:
        print(json.dumps({"export_paths": paths}, indent=2))


if __name__ == "__main__":
    main()
