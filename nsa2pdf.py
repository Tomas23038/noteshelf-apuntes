#!/usr/bin/env python3
"""
nsa2pdf — convierte cuadernos de Noteshelf 3 (.nsa, backup de Android) en
formatos legibles: un PDF aplanado (fondo + trazos a mano) y un resumen .md
con el texto disponible por página. Opcionalmente, PNG por página.

Formato .nsa (ingeniería inversa, Noteshelf 3 Android, document_Ver 11.x):
  <nombre>.ns3_a/
    Document.plist            páginas: uuid, associatedPDFFileName,
                              associatedPDFKitPageIndex (1-based), pdfKitPageRect
    Templates/*.ns_pdf        PDFs normales (fondo: hoja, plantilla o PDF importado)
    Annotations/<page_uuid>   SQLite, tabla `annotation`
        annotationType 0 = trazo: strokeColor (0xRRGGBB), strokeWidth, penType,
                           stroke_segments_v3 = N x 7 float32 LE
                           (x1, y1, x2, y2, tamaño, presión, flag)
        annotationType 2 = imagen  (Resources/<id>.*, boundingRect_*)
        annotationType 5 = figura  (shape_data JSON: controlPoints, ...)
        annotationType 7 = sticker (emojiName -> Resources/)
    RecognitionFiles/RecognitionInfo_<lang>.plist
        pageRecognitionInfo[uuid].recognisedText = OCR de la letra (si Noteshelf lo hizo)
    Resources/                imágenes insertadas

Uso:
  python3 -I nsa2pdf.py cuaderno.nsa [otro.nsa ...] -o salida/ [--png 110 [--solo-mano]]
"""
from __future__ import annotations

import argparse
import html
import json
import os
import plistlib
import re
import sqlite3
import struct
import sys
import tempfile
import zipfile
from collections import Counter, defaultdict
from datetime import datetime
from statistics import median

import pymupdf as fitz

INK, IMAGE, SHAPE, STICKER = 0, 2, 5, 7
IMG_EXT = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif", ".tif", ".tiff"}


# ---------------------------------------------------------------- helpers
def rect_size(s: str):
    nums = re.findall(r"[-+]?\d*\.?\d+", s or "")
    return (float(nums[2]), float(nums[3])) if len(nums) >= 4 else None


def rgb(c) -> tuple:
    c = int(c or 0) & 0xFFFFFF
    return ((c >> 16) & 255) / 255, ((c >> 8) & 255) / 255, (c & 255) / 255


def saturation(c) -> float:
    r, g, b = rgb(c)
    mx, mn = max(r, g, b), min(r, g, b)
    if mx == mn:
        return 0.0
    l = (mx + mn) / 2
    return (mx - mn) / (2 - mx - mn) if l > 0.5 else (mx - mn) / (mx + mn)


def segments(blob: bytes):
    if not blob:
        return []
    blob = blob[: len(blob) - len(blob) % 28]
    return list(struct.iter_unpack("<7f", blob))


def open_sqlite(data: bytes):
    tf = tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False)
    tf.write(data)
    tf.close()
    con = sqlite3.connect(tf.name)
    con.row_factory = sqlite3.Row
    return con, tf.name


def split_strokes(segs):
    """Une segmentos consecutivos en polilíneas; corta si hay un salto grande."""
    if not segs:
        return []
    lens = [((s[2] - s[0]) ** 2 + (s[3] - s[1]) ** 2) ** 0.5 for s in segs]
    med = median(lens) if lens else 0
    thr = max(10.0, med * 4)
    polys, cur, prev = [], [], None
    for s in segs:
        x1, y1, x2, y2, size = s[0], s[1], s[2], s[3], s[4]
        if prev and ((x1 - prev[0]) ** 2 + (y1 - prev[1]) ** 2) ** 0.5 > thr and len(cur) >= 2:
            polys.append(cur)
            cur = []
        if not cur:
            cur.append((x1, y1, size))
        cur.append((x2, y2, size))
        prev = (x2, y2)
    if len(cur) >= 2:
        polys.append(cur)
    return polys


# ---------------------------------------------------------------- core
class Notebook:
    def __init__(self, path: str):
        self.path = path
        self.zf = zipfile.ZipFile(path)
        names = self.zf.namelist()
        doc_member = next((n for n in names if n.endswith("Document.plist")), None)
        if not doc_member:
            raise RuntimeError("No es un .nsa de Noteshelf (falta Document.plist)")
        self.doc = plistlib.loads(self.zf.read(doc_member))
        self.pages = self.doc.get("pages", [])
        self.ann = {os.path.basename(n): n for n in names if "/Annotations/" in n and not n.endswith("/")}
        self.tpl = {os.path.basename(n): n for n in names if n.endswith(".ns_pdf") or n.lower().endswith(".pdf")}
        self.res = {}
        for n in names:
            if "/Resources/" in n and os.path.splitext(n)[1].lower() in IMG_EXT:
                b = os.path.splitext(os.path.basename(n))[0]
                self.res.setdefault(b, n)
                self.res.setdefault(b.lower(), n)
        self.ocr = {}
        for n in names:
            if "/RecognitionFiles/" in n and n.endswith(".plist"):
                try:
                    info = plistlib.loads(self.zf.read(n)).get("pageRecognitionInfo", {})
                    for uid, v in info.items():
                        t = (v or {}).get("recognisedText") or ""
                        if t.strip():
                            self.ocr[uid] = t.strip()
                except Exception:
                    pass
        self._tpl_cache = {}
        self.unknown_types = Counter()

    def template_page(self, out, p):
        name = os.path.basename(p.get("associatedPDFFileName") or "") or next(iter(self.doc.get("documents", {})), "")
        idx = int(p.get("associatedPDFKitPageIndex") or p.get("associatedPageIndex") or 1) - 1
        member = self.tpl.get(os.path.basename(name))
        if member:
            if member not in self._tpl_cache:
                data = self.zf.read(member)
                self._tpl_cache[member] = fitz.open(stream=data, filetype="pdf") if data[:4] == b"%PDF" else None
            src = self._tpl_cache[member]
            if src is not None and 0 <= idx < src.page_count:
                out.insert_pdf(src, from_page=idx, to_page=idx)
                return out[-1], src[idx].get_text().strip()
        w, h = rect_size(p.get("pdfKitPageRect")) or (1200, 1600)
        return out.new_page(width=w * 0.5, height=h * 0.5), ""

    def highlighter_pens(self):
        """penTypes que parecen resaltador: colores saturados/claros y trazo ancho."""
        widths, sats = defaultdict(list), defaultdict(list)
        for m in self.ann.values():
            con, tmp = open_sqlite(self.zf.read(m))
            try:
                for r in con.execute("SELECT penType, strokeWidth, strokeColor FROM annotation WHERE annotationType=0"):
                    pt = -1 if r["penType"] is None else int(r["penType"])
                    widths[pt].append(float(r["strokeWidth"] or 1))
                    sats[pt].append(saturation(r["strokeColor"]))
            except sqlite3.Error:
                pass
            finally:
                con.close()
                os.unlink(tmp)
        cands = [pt for pt in widths if len(widths[pt]) >= 5]
        if len(cands) < 2:
            return set()
        meds = {pt: median(widths[pt]) for pt in cands}
        thinnest = min(meds.values())
        return {pt for pt in cands if meds[pt] >= thinnest * 2.5 and sum(sats[pt]) / len(sats[pt]) > 0.35}

    def draw(self, page, member, sx, sy, hl_pens):
        con, tmp = open_sqlite(self.zf.read(member))
        n_ink = 0
        texts = []
        try:
            cols = {c[1] for c in con.execute("PRAGMA table_info(annotation)")}
            rows = list(con.execute("SELECT * FROM annotation"))
            H = page.rect.height

            # ¿eje Y invertido? se elige la variante con más puntos dentro de la página
            sample = []
            for r in rows:
                if r["annotationType"] == INK and "stroke_segments_v3" in cols:
                    sample += [(s[0], s[1]) for s in segments(r["stroke_segments_v3"])[::10]]
                if len(sample) > 400:
                    break
            def inside(inv):
                ok = sum(1 for x, y in sample if 0 <= x * sx <= page.rect.width and 0 <= (H - y * sy if inv else y * sy) <= H)
                return ok / len(sample) if sample else 0
            inv = inside(True) > inside(False) + 0.05
            Y = (lambda y: H - y * sy) if inv else (lambda y: y * sy)

            def bbox(r):
                if "boundingRect_w" not in cols or r["boundingRect_w"] is None:
                    return None
                x0, y0 = float(r["boundingRect_x"] or 0) * sx, float(r["boundingRect_y"] or 0) * sy
                w, h = float(r["boundingRect_w"]) * sx, float(r["boundingRect_h"]) * sy
                if inv:
                    y0 = H - y0 - h
                return fitz.Rect(x0, y0, x0 + w, y0 + h) if w > 0 and h > 0 else None

            for r in rows:
                t = r["annotationType"]
                if t == INK:
                    segs = segments(r["stroke_segments_v3"]) if "stroke_segments_v3" in cols else []
                    if not segs:
                        continue
                    n_ink += 1
                    base_w = float(r["strokeWidth"] or 1) * sx
                    pen = -1 if "penType" not in cols or r["penType"] is None else int(r["penType"])
                    opacity = 1.0
                    if pen in hl_pens:
                        opacity = 0.35
                    sizes = [s[4] for s in segs if s[4] > 0]
                    msz = median(sizes) if sizes else 0
                    if opacity < 1:
                        # resaltador: polilínea única de ancho constante (evita bandas al solaparse)
                        sh = page.new_shape()
                        for poly in split_strokes(segs):
                            sh.draw_polyline([(x * sx, Y(y)) for x, y, _ in poly])
                        sh.finish(width=base_w, color=rgb(r["strokeColor"]), stroke_opacity=opacity,
                                  lineCap=1, lineJoin=1, closePath=False)
                        sh.commit()
                        continue
                    # agrupa segmentos de ancho parecido para no generar un PDF gigante
                    buckets = defaultdict(list)
                    for poly in split_strokes(segs):
                        for (x1, y1, s1), (x2, y2, s2) in zip(poly, poly[1:]):
                            ratio = max(0.3, min(2.5, ((s1 + s2) / 2) / msz)) if msz else 1
                            w = round(base_w * ratio * 4) / 4 or 0.25
                            buckets[w].append(((x1 * sx, Y(y1)), (x2 * sx, Y(y2))))
                    for w, lines in buckets.items():
                        sh = page.new_shape()
                        for a, b in lines:
                            sh.draw_line(a, b)
                        sh.finish(width=w, color=rgb(r["strokeColor"]), stroke_opacity=opacity,
                                  lineCap=1, lineJoin=1)
                        sh.commit()
                elif t == SHAPE and "shape_data" in cols and r["shape_data"]:
                    try:
                        sd = json.loads(r["shape_data"])
                        pts = [(float(x) * sx, Y(float(y))) for x, y in sd.get("controlPoints") or []]
                    except Exception:
                        continue
                    if len(pts) < 2:
                        continue
                    sh = page.new_shape()
                    sh.draw_polyline(pts)
                    if int(sd.get("numberOfSides", 0) or 0) >= 3:
                        sh.draw_line(pts[-1], pts[0])
                    thick = float((sd.get("properties") or {}).get("strokeThickness", r["strokeWidth"] or 1))
                    sh.finish(width=thick * sx, color=rgb(r["strokeColor"]),
                              stroke_opacity=float(sd.get("strokeOpacity", 1)), lineCap=1, lineJoin=1)
                    sh.commit()
                elif t in (IMAGE, STICKER):
                    key = str(r["id"] if t == IMAGE else r["emojiName"] if "emojiName" in cols else "")
                    m = self.res.get(key) or self.res.get(os.path.splitext(key)[0]) or self.res.get(key.lower())
                    rect = bbox(r)
                    if m and rect:
                        try:
                            page.insert_image(rect, stream=self.zf.read(m))
                        except Exception:
                            pass
                else:
                    # tipos no verificados (p. ej. cuadros de texto): se rescata cualquier texto plano
                    self.unknown_types[t] += 1
                    for c in cols:
                        if "text" in c.lower():
                            v = r[c]
                            if isinstance(v, bytes):
                                try:
                                    v = v.decode("utf-8")
                                except UnicodeDecodeError:
                                    continue
                            if isinstance(v, str) and v.strip() and not v.lstrip().startswith(("{", "[", "bplist")):
                                texts.append(v.strip())
                                rect = bbox(r)
                                if rect:
                                    try:
                                        page.insert_textbox(rect, v.strip(), fontsize=max(6, rect.height / 6))
                                    except Exception:
                                        pass
                                break
        finally:
            con.close()
            os.unlink(tmp)
        return n_ink, texts

    def title(self) -> str:
        inner = next((n.split("/")[0] for n in self.zf.namelist() if n.split("/")[0].endswith(".ns3_a")), "")
        return re.sub(r"\.ns3_a$", "", inner).strip() or re.sub(r"\.nsa$", "", os.path.basename(self.path), flags=re.I).strip()

    def convert(self, outdir: str, png_dpi: int = 0, ink_only: bool = False, name: str | None = None):
        base = re.sub(r'[\\/:*?"<>|]', "_", name or self.title())
        base = re.sub(r"\s+", " ", base).strip()
        hl = self.highlighter_pens()
        out = fitz.open()
        summary = []
        for i, p in enumerate(self.pages, 1):
            page, pdf_text = self.template_page(out, p)
            dims = rect_size(p.get("pdfKitPageRect"))
            sx = page.rect.width / dims[0] if dims and dims[0] else 1
            sy = page.rect.height / dims[1] if dims and dims[1] else 1
            n_ink, texts = 0, []
            uid = p.get("uuid")
            if uid in self.ann:
                n_ink, texts = self.draw(page, self.ann[uid], sx, sy, hl)
            summary.append(dict(n=i, ink=n_ink, pdf_text=pdf_text, ocr=self.ocr.get(uid, ""),
                                boxes=texts, bookmark=p.get("bookmarkTitle") or "", tags=p.get("tags") or [],
                                date=page_date(p)))
        os.makedirs(outdir, exist_ok=True)
        pdf_path = os.path.join(outdir, base + ".pdf")
        out.save(pdf_path, garbage=3, deflate=True)

        pngs = []
        if png_dpi:
            pdir = os.path.join(outdir, base + "_paginas")
            os.makedirs(pdir, exist_ok=True)
            for i, pg in enumerate(out, 1):
                if ink_only and not summary[i - 1]["ink"]:
                    continue
                fp = os.path.join(pdir, f"p{i:03d}.png")
                pg.get_pixmap(dpi=png_dpi).save(fp)
                pngs.append(fp)
        out.close()

        md_path = os.path.join(outdir, base + ".md")
        with open(md_path, "w", encoding="utf-8") as f:
            f.write(summary_markdown(base, summary, self.unknown_types))
        with open(os.path.join(outdir, base + ".html"), "w", encoding="utf-8") as f:
            f.write(summary_html(base, summary, self.unknown_types))
        return pdf_path, md_path, pngs, summary


# ---------------------------------------------------------------- resúmenes
def page_date(p) -> str:
    """Fecha local (YYYY-MM-DD HH:MM) de la última edición de la página (timestamps Unix)."""
    ts = p.get("lastUpdated") or p.get("creationDate")
    if not ts:
        return ""
    try:
        from zoneinfo import ZoneInfo
        tz = ZoneInfo(os.environ.get("NSA_TZ") or "UTC")
        return datetime.fromtimestamp(float(ts), tz).strftime("%Y-%m-%d %H:%M")
    except Exception:
        return ""


def dates_overview(summary):
    """{fecha: [páginas]} usando solo el día de la última edición."""
    by_day = defaultdict(list)
    for s in summary:
        if s.get("date"):
            by_day[s["date"][:10]].append(s["n"])
    return dict(sorted(by_day.items(), reverse=True))


def _ranges(nums):
    nums, out, i = sorted(nums), [], 0
    while i < len(nums):
        j = i
        while j + 1 < len(nums) and nums[j + 1] == nums[j] + 1:
            j += 1
        out.append(str(nums[i]) if i == j else f"{nums[i]}-{nums[j]}")
        i = j + 1
    return ", ".join(out)


def summary_markdown(base, summary, unknown=None) -> str:
    ink = [s["n"] for s in summary if s["ink"]]
    md = [f"# {base}", "",
          f"- Páginas: {len(summary)}",
          f"- Páginas con escritura a mano: {len(ink)} ({_ranges(ink) or 'ninguna'})",
          f"- OCR de Noteshelf disponible en: {sum(1 for s in summary if s['ocr'])} páginas"]
    days = dates_overview(summary)
    if days:
        md.append("- Páginas editadas por día: " + "; ".join(f"{d}: {_ranges(p)}" for d, p in days.items()))
    if unknown:
        md.append(f"- Anotaciones de tipo no verificado: {dict(unknown)} (revisar visualmente)")
    md.append("")
    for s in summary:
        head = f"## Página {s['n']}" + (f" — {s['bookmark']}" if s["bookmark"] else "")
        md.append(head)
        meta = [f"editada {s['date']}"] if s.get("date") else []
        meta.append(f"trazos a mano: {s['ink']}")
        if s["tags"]:
            meta.append("etiquetas: " + ", ".join(s["tags"]))
        md.append("_" + " · ".join(meta) + "_")
        if s["pdf_text"]:
            md += ["", "**Texto impreso (fondo/PDF):**", "", s["pdf_text"]]
        if s["ocr"]:
            md += ["", "**Letra a mano (OCR de Noteshelf, puede tener errores):**", "", s["ocr"]]
        if s["boxes"]:
            md += ["", "**Cuadros de texto:**", ""] + s["boxes"]
        if s["ink"] and not s["ocr"]:
            md += ["", f"_Escritura a mano sin OCR: ver la imagen p{s['n']:03d}._"]
        md.append("")
    return "\n".join(md)


def summary_html(base, summary, unknown=None) -> str:
    """El mismo resumen en HTML simple, para importarlo como Google Doc."""
    out = ['<html><head><meta charset="utf-8"></head><body>']
    for line in summary_markdown(base, summary, unknown).split("\n"):
        t = html.escape(line)
        if line.startswith("# "):
            out.append(f"<h1>{t[2:]}</h1>")
        elif line.startswith("## "):
            out.append(f"<h2>{t[3:]}</h2>")
        elif line.startswith("- "):
            out.append(f"<p>• {t[2:]}</p>")
        elif line.startswith("**") and line.endswith("**"):
            out.append(f"<p><b>{t.strip('*')}</b></p>")
        elif line.startswith("_") and line.endswith("_") and len(line) > 1:
            out.append(f"<p><i>{t[1:-1]}</i></p>")
        elif line.strip():
            out.append(f"<p>{t}</p>")
    out.append("</body></html>")
    return "\n".join(out)


def main():
    ap = argparse.ArgumentParser(description="Noteshelf .nsa -> PDF + resumen .md")
    ap.add_argument("inputs", nargs="+")
    ap.add_argument("-o", "--outdir", default="nsa_out")
    ap.add_argument("--png", type=int, default=0, metavar="DPI", help="también exporta PNG por página")
    ap.add_argument("--solo-mano", action="store_true", help="con --png, solo páginas con escritura a mano")
    a = ap.parse_args()
    rc = 0
    for f in a.inputs:
        try:
            nb = Notebook(f)
            pdf, md, pngs, s = nb.convert(a.outdir, a.png, a.solo_mano)
            ink = [x["n"] for x in s if x["ink"]]
            print(f"OK  {os.path.basename(f)}: {len(s)} págs, a mano en {len(ink)} -> {pdf}")
            print(f"    resumen: {md}" + (f" | {len(pngs)} PNG" if pngs else ""))
        except Exception as e:
            rc = 1
            print(f"ERR {f}: {e}", file=sys.stderr)
    sys.exit(rc)


if __name__ == "__main__":
    main()
