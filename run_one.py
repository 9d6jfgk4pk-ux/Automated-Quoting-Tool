# run_one.py
from __future__ import annotations

import json
import sys

from feature_extractor import extract_features
from verified_dims_loader import apply_verified_dims_for_pdf, has_verified_dims


def main():
    if len(sys.argv) < 2:
        print("Usage: python run_one.py <pdf_path>")
        raise SystemExit(2)

    pdf_path = sys.argv[1]

    features = extract_features(pdf_path)

    if has_verified_dims(pdf_path):
        features = apply_verified_dims_for_pdf(features, pdf_path)
    else:
        features["warnings"].append("No .dims_verified.json found; envelope dims not applied.")

    print(json.dumps(features, indent=2))


if __name__ == "__main__":
    main()
