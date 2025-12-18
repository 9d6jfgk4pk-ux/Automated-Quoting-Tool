# app_streamlit.py
from __future__ import annotations

import os
import sys
import json
import subprocess
from pathlib import Path
from typing import Dict, Any, List, Optional

import streamlit as st

from feature_extractor import extract_features
from verified_dims_loader import (
    has_verified_dims,
    apply_verified_dims_for_pdf,
    verified_dims_path_for_pdf,
)
from route_builder import build_route
from machine_allocator import allocate_machines
from time_model import estimate_times
from exporters import export_quote_bundle

# -----------------------------
# Local folders
# -----------------------------
BASE_DIR = Path(__file__).resolve().parent
UPLOAD_DIR = BASE_DIR / "uploaded_pdfs"
OUTPUT_DIR = BASE_DIR / "outputs"
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

# -----------------------------
# Picker settings
# -----------------------------
# Ensure your working picker script is named like this:
PICKER_SCRIPT = BASE_DIR / "pick_dims_verified1.py"

st.set_page_config(page_title="CWEM Estimator", layout="wide")


# -----------------------------
# Helpers
# -----------------------------
def save_uploaded_file(uploaded, dest_path: Path) -> None:
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    with open(dest_path, "wb") as f:
        f.write(uploaded.getbuffer())


def list_uploaded_pdfs() -> List[Path]:
    return sorted(UPLOAD_DIR.glob("*.pdf"), key=lambda p: p.name.lower())


def _light(ok: bool, label: str) -> str:
    return f"{'🟢' if ok else '🔴'} {label}"


def file_download_button(label: str, path: str, mime: str) -> None:
    p = Path(path)
    if not p.exists():
        st.warning(f"Missing file: {p.name}")
        return
    st.download_button(label=label, data=p.read_bytes(), file_name=p.name, mime=mime)


def run_picker_for_pdf(pdf_path: str) -> Dict[str, Any]:
    """
    Launches the Tk picker as a blocking subprocess.
    Writes output to the exact path expected by verified_dims_loader.py.
    """
    if not PICKER_SCRIPT.exists():
        return {"ok": False, "error": f"Picker script not found: {PICKER_SCRIPT}"}

    out_path = Path(verified_dims_path_for_pdf(pdf_path))
    out_path.parent.mkdir(parents=True, exist_ok=True)

    cmd = [
        sys.executable,
        str(PICKER_SCRIPT),
        pdf_path,
        "--out",
        str(out_path),
        "--dpi-preview",
        "150",
    ]

    try:
        completed = subprocess.run(cmd, capture_output=True, text=True)
        return {
            "ok": completed.returncode == 0 and out_path.exists(),
            "returncode": completed.returncode,
            "stdout": completed.stdout[-4000:],
            "stderr": completed.stderr[-4000:],
            "out_path": str(out_path),
            "out_exists": out_path.exists(),
        }
    except Exception as e:
        return {"ok": False, "error": str(e), "out_path": str(out_path)}


def run_pipeline(pdf_path: str, outdir: str) -> Dict[str, Any]:
    """
    Runs the pipeline. Requires verified dims already present.
    Returns result dict incl. export_paths.
    """
    features = extract_features(pdf_path)

    if not has_verified_dims(pdf_path):
        features.setdefault("warnings", []).append("No verified dims JSON found. Run picker first.")
        return {"status": "BLOCKED", "reason": "missing_verified_dims", "features": features}

    features = apply_verified_dims_for_pdf(features, pdf_path)

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
    return result


def compute_status(pdf_path: str) -> Dict[str, bool]:
    p = Path(pdf_path)
    pdf_ok = p.exists()
    dims_ok = has_verified_dims(pdf_path)

    features_ok = False
    try:
        _ = extract_features(pdf_path)
        features_ok = True
    except Exception:
        features_ok = False

    # Exports: if quote JSON exists in outputs with prefix drawing_no, count as available
    drawing_no = p.stem
    exports_ok = any(OUTPUT_DIR.glob(f"{drawing_no}_*_quote.json"))
    route_ok = exports_ok

    return {
        "pdf_ok": pdf_ok,
        "dims_ok": dims_ok,
        "features_ok": features_ok,
        "route_ok": route_ok,
        "exports_ok": exports_ok,
    }


# -----------------------------
# UI
# -----------------------------
st.title("CWEM Estimator UI")
st.caption("One-click flow: Picker → Route/Time → XLSX export")

colA, colB = st.columns([1, 1], gap="large")

with colA:
    st.subheader("1) Upload PDFs")
    pdf_files = st.file_uploader(
        "Upload drawing PDFs",
        type=["pdf"],
        accept_multiple_files=True,
    )
    if pdf_files:
        for f in pdf_files:
            save_uploaded_file(f, UPLOAD_DIR / f.name)
        st.success(f"Saved {len(pdf_files)} PDF(s).")

with colB:
    st.subheader("2) Optional: Upload verified dims JSONs")
    st.write("Normally you will run the picker from this UI. Upload is optional.")
    dims_files = st.file_uploader(
        "Upload one or more .dims_verified.json files",
        type=["json"],
        accept_multiple_files=True,
        key="dimsjson",
    )
    if dims_files:
        saved = 0
        for f in dims_files:
            # expects filename like 26660.dims_verified.json
            stem = f.name.replace(".dims_verified.json", "")
            fake_pdf_path = str(UPLOAD_DIR / f"{stem}.pdf")
            dest_path = Path(verified_dims_path_for_pdf(fake_pdf_path))
            save_uploaded_file(f, dest_path)
            saved += 1
        st.success(f"Saved {saved} verified dims JSON(s).")

st.divider()
st.subheader("3) Drawing list")

pdfs = list_uploaded_pdfs()
if not pdfs:
    st.info("No PDFs uploaded yet.")
    st.stop()

rows = []
for p in pdfs:
    s = compute_status(str(p))
    rows.append({
        "PDF": p.name,
        "PDF ok": "✅" if s["pdf_ok"] else "❌",
        "Dims": "✅" if s["dims_ok"] else "❌",
        "Features": "✅" if s["features_ok"] else "❌",
        "Exports": "✅" if s["exports_ok"] else "❌",
    })
st.dataframe(rows, use_container_width=True, hide_index=True)

selected_name = st.selectbox("Select a PDF", [p.name for p in pdfs])
selected_pdf = str(UPLOAD_DIR / selected_name)

st.write("Verified dims expected at:", verified_dims_path_for_pdf(selected_pdf))

st.divider()
st.subheader("4) Status lights (selected)")

status = compute_status(selected_pdf)
st.markdown(
    "\n".join([
        _light(status["pdf_ok"], "PDF present"),
        _light(status["dims_ok"], "Verified dims present"),
        _light(status["features_ok"], "Features extractable"),
        _light(status["exports_ok"], "Exports available"),
    ])
)

st.divider()
st.subheader("5) One-click run")

st.write("This will:")
st.write("1) open the picker → 2) save verified dims → 3) run route+time → 4) export XLSX + CSV/JSON → 5) show results")

if st.button("Run picker → then route/time → export", type="primary"):
    # 1) picker
    with st.spinner("Picker running… complete the popup and close it…"):
        picker_res = run_picker_for_pdf(selected_pdf)
    st.session_state["picker_result"] = picker_res

    if not picker_res.get("ok"):
        st.session_state["last_result"] = None
        st.error("Picker failed or output missing.")
        st.code(picker_res.get("stderr") or "", language="text")
        st.code(picker_res.get("stdout") or "", language="text")
        st.stop()

    # 2) pipeline
    with st.spinner("Running route/time + exports…"):
        res = run_pipeline(selected_pdf, outdir=str(OUTPUT_DIR))
    st.session_state["last_result"] = res
    st.success("Complete.")
    st.rerun()

picker_res = st.session_state.get("picker_result")
if picker_res:
    st.subheader("Picker log")
    if picker_res.get("ok"):
        st.success(f"Picker OK. Saved: {picker_res.get('out_path')}")
    else:
        st.error("Picker failed.")
    if picker_res.get("stderr"):
        st.code(picker_res["stderr"], language="text")
    if picker_res.get("stdout"):
        st.code(picker_res["stdout"], language="text")

res = st.session_state.get("last_result")
if res:
    st.divider()
    st.subheader("6) Result")
    if res.get("status") != "OK":
        st.error(f"Blocked: {res.get('reason')}")
        st.json(res.get("features", {}))
    else:
        # Quick headline
        t = res.get("totals") or {}
        env = res.get("envelope_mm") or {}
        st.markdown(
            f"**Total time:** {t.get('total_hr')} hr ({t.get('total_min')} min)  \n"
            f"**Envelope:** {env.get('length_mm')} × {env.get('width_mm')} × {env.get('thickness_mm')} mm"
        )

        with st.expander("Full JSON result"):
            st.json(res)

        st.subheader("Downloads")
        paths = res.get("export_paths") or {}

        c1, c2, c3, c4, c5 = st.columns(5)
        with c1:
            if "route_times_xlsx" in paths:
                file_download_button(
                    "Route+Times XLSX",
                    paths["route_times_xlsx"],
                    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                )
        with c2:
            if "quote_json" in paths:
                file_download_button("Quote JSON", paths["quote_json"], "application/json")
        with c3:
            if "route_csv" in paths:
                file_download_button("Route CSV", paths["route_csv"], "text/csv")
        with c4:
            if "route_times_csv" in paths:
                file_download_button("Route+Times CSV", paths["route_times_csv"], "text/csv")
        with c5:
            if "summary_txt" in paths:
                file_download_button("Summary TXT", paths["summary_txt"], "text/plain")

st.caption(f"PDF store: {UPLOAD_DIR} | Exports: {OUTPUT_DIR} | Picker: {PICKER_SCRIPT}")
