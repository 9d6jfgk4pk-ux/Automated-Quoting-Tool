#!/usr/bin/env python
"""
pick_dims_verified.py

Interactive, human-in-the-loop dimension picker for PDF technical drawings.

UI:
- Mouse wheel: vertical scroll
- Shift + wheel: horizontal scroll
- Middle mouse drag: pan
- Space + left-drag: pan (hand tool)
- Ctrl + wheel: zoom (keeps cursor position)

Extraction:
- User selects the dimension text for LENGTH, WIDTH, THICKNESS.
- Script extracts the text from the PDF using PyMuPDF word boxes
  (works with rotated pages by converting selection rectangles properly),
  parses tolerance bands, and writes a .dims_verified.json file.

Requirements:
- pip install pymupdf pillow
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import re
import sys
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

try:
    import fitz  # PyMuPDF
except Exception:
    fitz = None

try:
    from PIL import Image, ImageTk
except Exception:
    Image = None
    ImageTk = None

# Optional: OpenAI fallback (disabled by default)
try:
    from openai import OpenAI
except Exception:
    OpenAI = None


# ----------------------------
# Regex for meta extraction
# ----------------------------
DRAWING_NO_RX = re.compile(r"\bDRAWING\s*No\.\s*(\d{5})\b", re.IGNORECASE)
MATERIAL_RX = re.compile(r"\bMATERIAL\s*[:\-]\s*(.+?)\.", re.IGNORECASE)
WEIGHT_RX = re.compile(r"\bFINISHED\s+WEIGHT\s+([0-9]+(?:\.[0-9]+)?)\s*Kg\b", re.IGNORECASE)


# ----------------------------
# Helpers
# ----------------------------
def _now_str() -> str:
    return _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _eprint(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def _as_float(s: str) -> Optional[float]:
    try:
        return float(s)
    except Exception:
        return None


def clean_material(s: str) -> str:
    return s.strip().lstrip("-: ").strip()


def extract_meta_text(doc: "fitz.Document") -> Dict[str, Optional[object]]:
    full = []
    for i in range(doc.page_count):
        try:
            full.append(doc.load_page(i).get_text("text") or "")
        except Exception:
            continue
    t = "\n".join(full)

    meta: Dict[str, Optional[object]] = {
        "drawing_no": None,
        "material": None,
        "finished_weight_kg": None,
    }

    m = DRAWING_NO_RX.search(t)
    if m:
        meta["drawing_no"] = m.group(1)

    mm = MATERIAL_RX.search(t)
    if mm:
        meta["material"] = clean_material(mm.group(1))

    mw = WEIGHT_RX.search(t)
    if mw:
        meta["finished_weight_kg"] = _as_float(mw.group(1))

    return meta


# ----------------------------
# Band parsing
# ----------------------------
_FLOAT_RX = re.compile(r"(?<![A-Za-z0-9])(\d+(?:\.\d+)?)")
_FRAC_RX = re.compile(r"([+-])?\s*(\d+)\s*/\s*(\d+)")

@dataclass
class Band:
    raw: Optional[str]
    kind: str  # none|single|limits|plus_minus|unilateral|ambiguous
    nominal_mm: Optional[float]
    min_mm: Optional[float]
    max_mm: Optional[float]


def _normalize_raw(raw: str) -> str:
    return (
        raw.replace("\u2013", "-")
        .replace("\u2014", "-")
        .replace("\u2212", "-")
        .replace("—", "-")
        .replace("–", "-")
        .strip()
    )


def _parse_inch_fraction_to_mm(frac: float) -> float:
    return frac * 25.4


def parse_band_mm(raw: Optional[str]) -> Band:
    if raw is None:
        return Band(raw=None, kind="none", nominal_mm=None, min_mm=None, max_mm=None)

    s = _normalize_raw(raw)
    if not s:
        return Band(raw=raw, kind="none", nominal_mm=None, min_mm=None, max_mm=None)

    s_lower = s.lower()

    if "±" in s or "+/-" in s_lower or "+/ -" in s_lower:
        nums = [float(m.group(1)) for m in _FLOAT_RX.finditer(s)]
        if len(nums) >= 2:
            nominal = nums[0]
            tol = nums[1]
            return Band(raw=raw, kind="plus_minus", nominal_mm=nominal, min_mm=nominal - tol, max_mm=nominal + tol)
        return Band(raw=raw, kind="ambiguous", nominal_mm=None, min_mm=None, max_mm=None)

    frac_m = _FRAC_RX.search(s)
    if frac_m:
        sign = -1.0 if (frac_m.group(1) == "-") else 1.0
        num = int(frac_m.group(2))
        den = int(frac_m.group(3))
        if den != 0:
            frac = sign * (num / den)
            frac_mm = _parse_inch_fraction_to_mm(frac)
            nums = [float(m.group(1)) for m in _FLOAT_RX.finditer(s)]
            if nums:
                nominal = max(nums)
                if frac_mm < 0:
                    return Band(raw=raw, kind="unilateral", nominal_mm=nominal + frac_mm / 2.0, min_mm=nominal + frac_mm, max_mm=nominal)
                else:
                    return Band(raw=raw, kind="unilateral", nominal_mm=nominal + frac_mm / 2.0, min_mm=nominal, max_mm=nominal + frac_mm)

    nums = [float(m.group(1)) for m in _FLOAT_RX.finditer(s)]
    if not nums:
        return Band(raw=raw, kind="ambiguous", nominal_mm=None, min_mm=None, max_mm=None)

    if len(nums) == 1:
        v = nums[0]
        return Band(raw=raw, kind="single", nominal_mm=v, min_mm=v, max_mm=v)

    if len(nums) > 2:
        best_pair = None
        best_diff = None
        for i in range(len(nums)):
            for j in range(i + 1, len(nums)):
                d = abs(nums[i] - nums[j])
                if best_diff is None or d < best_diff:
                    best_diff = d
                    best_pair = (nums[i], nums[j])
        if best_pair is None:
            return Band(raw=raw, kind="ambiguous", nominal_mm=None, min_mm=None, max_mm=None)
        a, b = best_pair
        mn, mx = (a, b) if a <= b else (b, a)
        return Band(raw=raw, kind="limits", nominal_mm=(mn + mx) / 2.0, min_mm=mn, max_mm=mx)

    a, b = nums[0], nums[1]
    mn, mx = (a, b) if a <= b else (b, a)
    return Band(raw=raw, kind="limits", nominal_mm=(mn + mx) / 2.0, min_mm=mn, max_mm=mx)


# ----------------------------
# Word extraction from rectangle
# ----------------------------
def words_in_rect(
    words: List[Tuple[float, float, float, float, str, int, int, int]],
    rect: "fitz.Rect",
) -> List[Tuple[float, float, float, float, str]]:
    out = []
    for x0, y0, x1, y1, w, *_ in words:
        cx = (x0 + x1) / 2.0
        cy = (y0 + y1) / 2.0
        if rect.contains(fitz.Point(cx, cy)):
            out.append((x0, y0, x1, y1, w))
    return out


def _rect_px_to_page_rect_rotated(px_rect: Tuple[float, float, float, float], px_per_pt: float) -> "fitz.Rect":
    x0, y0, x1, y1 = px_rect
    x_min, x_max = (x0, x1) if x0 <= x1 else (x1, x0)
    y_min, y_max = (y0, y1) if y0 <= y1 else (y1, y0)
    return fitz.Rect(x_min / px_per_pt, y_min / px_per_pt, x_max / px_per_pt, y_max / px_per_pt)


def _rect_rotated_to_text_rect(page: "fitz.Page", rect_rot_pt: "fitz.Rect") -> "fitz.Rect":
    try:
        dm = page.derotation_matrix
        rect_text = rect_rot_pt * dm
        rect_text.normalize()
        return rect_text
    except Exception:
        rect_rot_pt.normalize()
        return rect_rot_pt


def join_words_as_text(word_boxes: List[Tuple[float, float, float, float, str]]) -> str:
    if not word_boxes:
        return ""
    word_boxes = sorted(word_boxes, key=lambda t: (t[1], t[0]))

    heights = [abs(y1 - y0) for (x0, y0, x1, y1, w) in word_boxes]
    med_h = sorted(heights)[len(heights) // 2] if heights else 8.0
    line_tol = max(2.0, med_h * 0.7)

    lines: List[List[str]] = []
    cur_y: Optional[float] = None
    for x0, y0, x1, y1, w in word_boxes:
        if cur_y is None or abs(y0 - cur_y) > line_tol:
            lines.append([w])
            cur_y = y0
        else:
            lines[-1].append(w)

    return "\n".join(" ".join(parts) for parts in lines).strip()


# ----------------------------
# Tkinter rectangle picker (pan/scroll/zoom)
# ----------------------------
class DimPickerUI:
    def __init__(self, root, pil_img_base: "Image.Image", dim_label: str):
        import tkinter as tk

        self.root = root
        self.root.title(f"Pick {dim_label.upper()} (drag box around dimension text)")

        self.dim_label = dim_label

        # Base preview image (at dpi_preview render)
        self.base_img = pil_img_base
        self.base_w, self.base_h = self.base_img.size

        # Zoom state
        self.zoom = 1.0
        self.zoom_min = 0.2
        self.zoom_max = 6.0

        frame = tk.Frame(root)
        frame.pack(fill="both", expand=True)

        self.canvas = tk.Canvas(frame, cursor="cross")
        self.hbar = tk.Scrollbar(frame, orient="horizontal", command=self.canvas.xview)
        self.vbar = tk.Scrollbar(frame, orient="vertical", command=self.canvas.yview)
        self.canvas.configure(xscrollcommand=self.hbar.set, yscrollcommand=self.vbar.set)

        self.canvas.grid(row=0, column=0, sticky="nsew")
        self.vbar.grid(row=0, column=1, sticky="ns")
        self.hbar.grid(row=1, column=0, sticky="ew")
        frame.grid_rowconfigure(0, weight=1)
        frame.grid_columnconfigure(0, weight=1)

        self.label_var = tk.StringVar()
        self.label = tk.Label(root, textvariable=self.label_var, anchor="w", justify="left")
        self.label.pack(fill="x")

        self._space_down = False
        self._update_label()

        # Canvas image
        self.photo = None
        self.img_id = None
        self._render_zoomed_image()

        # Selection state
        self.start = None
        self.rect_id = None
        self.last_rect = None  # in CANVAS coords (zoomed pixels)

        # Bindings
        self.canvas.bind("<ButtonPress-1>", self.on_press)
        self.canvas.bind("<B1-Motion>", self.on_drag)
        self.canvas.bind("<ButtonRelease-1>", self.on_release)

        # Escape cancels selection
        self.root.bind("<Escape>", lambda e: self._cancel())

        # Wheel scroll (Windows)
        self.canvas.bind_all("<MouseWheel>", self.on_mousewheel)
        self.canvas.bind_all("<Shift-MouseWheel>", self.on_shift_mousewheel)
        self.canvas.bind_all("<Control-MouseWheel>", self.on_ctrl_mousewheel)

        # Pan
        self.canvas.bind_all("<ButtonPress-2>", self.on_pan_start)
        self.canvas.bind_all("<B2-Motion>", self.on_pan_move)

        self.root.bind("<KeyPress-space>", self.on_space_down)
        self.root.bind("<KeyRelease-space>", self.on_space_up)

    def _update_label(self):
        self.label_var.set(
            f"Select {self.dim_label.upper()} dimension.\n"
            f"Mouse wheel: scroll | Shift+wheel: horizontal | Ctrl+wheel: zoom\n"
            f"Middle drag OR hold SPACE: pan | Drag box around dimension TEXT."
        )

    def _render_zoomed_image(self):
        import tkinter as tk
        # Resize image for current zoom
        zw = max(1, int(self.base_w * self.zoom))
        zh = max(1, int(self.base_h * self.zoom))
        img = self.base_img.resize((zw, zh), resample=Image.NEAREST)

        self.photo = ImageTk.PhotoImage(img)

        if self.img_id is None:
            self.img_id = self.canvas.create_image(0, 0, anchor="nw", image=self.photo)
        else:
            self.canvas.itemconfigure(self.img_id, image=self.photo)

        self.canvas.configure(scrollregion=(0, 0, zw, zh))

    # -------- scrolling / panning --------
    def on_mousewheel(self, event):
        # Ignore if ctrl held (zoom handled elsewhere)
        if (event.state & 0x0004) != 0:
            return
        direction = -1 if event.delta > 0 else 1
        self.canvas.yview_scroll(direction * 3, "units")

    def on_shift_mousewheel(self, event):
        direction = -1 if event.delta > 0 else 1
        self.canvas.xview_scroll(direction * 3, "units")

    def on_ctrl_mousewheel(self, event):
        # Zoom around cursor position
        if event.delta == 0:
            return

        old_zoom = self.zoom
        factor = 1.12 if event.delta > 0 else (1 / 1.12)
        new_zoom = max(self.zoom_min, min(self.zoom_max, self.zoom * factor))
        if abs(new_zoom - old_zoom) < 1e-9:
            return

        # Canvas coords of cursor BEFORE zoom
        cx = self.canvas.canvasx(event.x)
        cy = self.canvas.canvasy(event.y)

        # Relative position in image (0..1) BEFORE zoom
        # (use current scrollregion size)
        sr = self.canvas.cget("scrollregion").split()
        if len(sr) == 4:
            _, _, sw, sh = map(float, sr)
        else:
            sw, sh = self.base_w * old_zoom, self.base_h * old_zoom

        rx = cx / max(sw, 1.0)
        ry = cy / max(sh, 1.0)

        self.zoom = new_zoom
        self._render_zoomed_image()

        # New scrollregion size
        sr = self.canvas.cget("scrollregion").split()
        _, _, sw2, sh2 = map(float, sr)

        # Maintain cursor position by moving view so cursor stays at same relative point
        target_x = rx * sw2
        target_y = ry * sh2

        # Set view so target point appears under cursor
        vx = (target_x - event.x) / max(sw2, 1.0)
        vy = (target_y - event.y) / max(sh2, 1.0)
        self.canvas.xview_moveto(max(0.0, min(1.0, vx)))
        self.canvas.yview_moveto(max(0.0, min(1.0, vy)))

    def on_pan_start(self, event):
        self.canvas.scan_mark(event.x, event.y)

    def on_pan_move(self, event):
        self.canvas.scan_dragto(event.x, event.y, gain=1)

    def on_space_down(self, event):
        self._space_down = True
        self.canvas.configure(cursor="fleur")

    def on_space_up(self, event):
        self._space_down = False
        self.canvas.configure(cursor="cross")

    # -------- selection --------
    def on_press(self, event):
        if self._space_down:
            return
        x = self.canvas.canvasx(event.x)
        y = self.canvas.canvasy(event.y)
        self.start = (x, y)
        if self.rect_id is not None:
            self.canvas.delete(self.rect_id)
            self.rect_id = None

    def on_drag(self, event):
        if self._space_down:
            return
        if not self.start:
            return
        x0, y0 = self.start
        x1 = self.canvas.canvasx(event.x)
        y1 = self.canvas.canvasy(event.y)
        if self.rect_id is None:
            self.rect_id = self.canvas.create_rectangle(x0, y0, x1, y1, outline="red", width=2)
        else:
            self.canvas.coords(self.rect_id, x0, y0, x1, y1)

    def on_release(self, event):
        if self._space_down:
            return
        if not self.start:
            return
        x0, y0 = self.start
        x1 = self.canvas.canvasx(event.x)
        y1 = self.canvas.canvasy(event.y)
        self.start = None

        x_min, x_max = (x0, x1) if x0 <= x1 else (x1, x0)
        y_min, y_max = (y0, y1) if y0 <= y1 else (y1, y0)

        if abs(x_max - x_min) < 5 or abs(y_max - y_min) < 5:
            return

        self.last_rect = (x_min, y_min, x_max, y_max)
        self.root.quit()

    def _cancel(self):
        self.last_rect = None
        self.root.quit()

    def get_last_rect_base_pixels(self) -> Optional[Tuple[float, float, float, float]]:
        """
        Convert last_rect (zoomed canvas pixels) back to base image pixels.
        """
        if self.last_rect is None:
            return None
        x0, y0, x1, y1 = self.last_rect
        # Convert from zoomed display pixels to base image pixels
        return (x0 / self.zoom, y0 / self.zoom, x1 / self.zoom, y1 / self.zoom)


def pick_rectangles_with_zoom(pil_img_base: "Image.Image", dim_order: List[str]) -> Dict[str, Tuple[float, float, float, float]]:
    import tkinter as tk
    out: Dict[str, Tuple[float, float, float, float]] = {}

    for dim in dim_order:
        root = tk.Tk()
        ui = DimPickerUI(root, pil_img_base, dim)
        root.mainloop()
        root.destroy()

        rect_base = ui.get_last_rect_base_pixels()
        if rect_base is None:
            raise SystemExit("Cancelled.")
        out[dim] = rect_base
        _eprint(f"[pick] {dim}: px_base={rect_base}")

    return out


# ----------------------------
# Optional GPT vision fallback for non-text PDFs
# ----------------------------
VISION_MODEL = os.getenv("OPENAI_VISION_MODEL", "gpt-5.2")
VISION_REASONING = os.getenv("OPENAI_REASONING_EFFORT", "low")

VISION_TRANSCRIBE_PROMPT = """Return STRICT JSON only.

Task: Transcribe ONLY the dimension text shown in this image crop.
- Return the text exactly as seen (digits, decimal points, hyphens, slashes, brackets, parentheses).
- Preserve line breaks if values are stacked.
- Do not interpret, do not "fix" numbers, do not add units unless printed.

Schema:
{
  "raw": string
}
""".strip()


def _vision_transcribe_crop(png_bytes: bytes) -> Optional[str]:
    if OpenAI is None:
        return None
    import base64
    client = OpenAI()
    b64 = base64.b64encode(png_bytes).decode("utf-8")
    try:
        resp = client.responses.create(
            model=VISION_MODEL,
            reasoning={"effort": VISION_REASONING},
            input=[{
                "role": "user",
                "content": [
                    {"type": "input_text", "text": VISION_TRANSCRIBE_PROMPT},
                    {"type": "input_image", "image_url": f"data:image/png;base64,{b64}"},
                ],
            }],
            text={"format": {"type": "json_object"}},
        )
        out_text = getattr(resp, "output_text", None) or ""
        if not out_text:
            return None
        obj = json.loads(out_text)
        raw = obj.get("raw")
        if isinstance(raw, str) and raw.strip():
            return raw.strip()
        return None
    except Exception:
        return None


def _render_crop_png(page: "fitz.Page", rect_pt: "fitz.Rect", dpi: int = 450) -> bytes:
    mat = fitz.Matrix(dpi / 72.0, dpi / 72.0)
    pix = page.get_pixmap(matrix=mat, clip=rect_pt, alpha=False)
    return pix.tobytes("png")


# ----------------------------
# Main
# ----------------------------
def main() -> None:
    if fitz is None:
        raise SystemExit("PyMuPDF (fitz) is required. Install with: pip install pymupdf")
    if Image is None or ImageTk is None:
        raise SystemExit("Pillow is required. Install with: pip install pillow")

    ap = argparse.ArgumentParser()
    ap.add_argument("pdf_path", help="Path to the PDF drawing")
    ap.add_argument("--page", type=int, default=1, help="1-based page number (default 1)")
    ap.add_argument("--dpi-preview", type=int, default=150, help="Preview render DPI for picking (default 150)")
    ap.add_argument("--out", default=None, help="Output .dims_verified.json path (default: <pdf>.dims_verified.json)")
    ap.add_argument("--vision-fallback", action="store_true", help="If no selectable text in a picked region, try GPT vision on a high-res crop")
    args = ap.parse_args()

    pdf_path = args.pdf_path
    if not os.path.exists(pdf_path):
        raise SystemExit(f"PDF not found: {pdf_path}")

    _eprint(f"[open] {pdf_path}")
    doc = fitz.open(pdf_path)
    page_index = max(0, args.page - 1)
    if page_index >= doc.page_count:
        raise SystemExit(f"Page {args.page} out of range (pdf has {doc.page_count} pages)")

    page = doc.load_page(page_index)
    meta = extract_meta_text(doc)
    _eprint(f"[meta] {meta}")
    _eprint(f"[page] rotation={int(getattr(page, 'rotation', 0) or 0)} deg")

    dpi_preview = int(args.dpi_preview)
    scale = dpi_preview / 72.0  # pixels per PDF point for base preview
    _eprint(f"[render] preview @ {dpi_preview} DPI (scale_px_per_pt={scale:.4f})")
    pix = page.get_pixmap(matrix=fitz.Matrix(scale, scale), alpha=False)
    pil_img_base = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)

    dim_order = ["length", "width", "thickness"]
    picked_px_base = pick_rectangles_with_zoom(pil_img_base, dim_order)

    words = page.get_text("words")

    bands: Dict[str, Dict[str, Optional[object]]] = {}
    snippets: Dict[str, str] = {}
    picked_pt_rotated: Dict[str, List[float]] = {}
    picked_pt_textcoords: Dict[str, List[float]] = {}

    for dim in dim_order:
        rect_px = picked_px_base[dim]

        # px (base preview) -> pt in rotated page coords
        rect_rot_pt = _rect_px_to_page_rect_rotated(rect_px, px_per_pt=scale)

        # Pad slightly
        pad = max(2.0, 0.06 * min(rect_rot_pt.width, rect_rot_pt.height))
        rect_rot_pt = fitz.Rect(rect_rot_pt.x0 - pad, rect_rot_pt.y0 - pad, rect_rot_pt.x1 + pad, rect_rot_pt.y1 + pad)
        rect_rot_pt.normalize()

        rect_text_pt = _rect_rotated_to_text_rect(page, rect_rot_pt)

        picked_pt_rotated[dim] = [rect_rot_pt.x0, rect_rot_pt.y0, rect_rot_pt.x1, rect_rot_pt.y1]
        picked_pt_textcoords[dim] = [rect_text_pt.x0, rect_text_pt.y0, rect_text_pt.x1, rect_text_pt.y1]

        wboxes = words_in_rect(words, rect_text_pt)
        raw = join_words_as_text(wboxes)

        if (not raw) and args.vision_fallback:
            _eprint(f"[fallback] No selectable text found for {dim}. Trying vision crop...")
            crop_png = _render_crop_png(page, rect_rot_pt, dpi=450)
            raw = _vision_transcribe_crop(crop_png) or ""

        if raw:
            _eprint(f"[snip] {dim}: {raw!r}")

        snippets[dim] = raw
        band = parse_band_mm(raw if raw else None)
        bands[dim] = {
            "raw": band.raw,
            "kind": band.kind,
            "nominal_mm": band.nominal_mm,
            "min_mm": band.min_mm,
            "max_mm": band.max_mm,
        }

        _eprint(f"[band] {dim}: raw={repr(raw)} -> kind={band.kind} min={band.min_mm} nom={band.nominal_mm} max={band.max_mm}")

    missing = [d for d in dim_order if bands[d]["kind"] == "none" or bands[d]["max_mm"] is None]
    if missing:
        _eprint(f"[error] Missing/invalid selections for: {missing}")
        _eprint("Re-run and draw tighter rectangles around the DIMENSION TEXT (not the dimension line).")
        _eprint("If the PDF has no selectable text in dims, add --vision-fallback.")
        raise SystemExit(2)

    envelope = {
        "length_mm": float(bands["length"]["max_mm"]),
        "width_mm": float(bands["width"]["max_mm"]),
        "thickness_mm": float(bands["thickness"]["max_mm"]),
    }

    out_obj = {
        "status": "VERIFIED_BY_USER_SELECTION",
        "source_pdf": os.path.abspath(pdf_path),
        "page": int(args.page),
        "picked_at": _now_str(),
        "meta": meta,
        "bands_mm": bands,
        "envelope_mm": envelope,
        "snippets": snippets,
        "debug": {
            "dpi_preview": dpi_preview,
            "scale_px_per_pt": scale,
            "picked_boxes_px_base": {k: list(map(float, v)) for k, v in picked_px_base.items()},
            "page_rotation_deg": int(getattr(page, "rotation", 0) or 0),
            "picked_boxes_pt_rotated": picked_pt_rotated,
            "picked_boxes_pt_textcoords": picked_pt_textcoords,
        },
    }

    out_path = args.out or (os.path.splitext(pdf_path)[0] + ".dims_verified.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(out_obj, f, indent=2)

    _eprint(f"[write] {out_path}")
    print(json.dumps(out_obj, indent=2))


if __name__ == "__main__":
    main()
