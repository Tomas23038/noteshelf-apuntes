#!/usr/bin/env python3
"""
sync_drive.py — convierte los respaldos de Noteshelf (.nsa) que hay en Google Drive
en archivos legibles, dentro de una carpeta "Apuntes legibles" del mismo Drive.

Por cada cuaderno crea (o actualiza, si cambió):
  Apuntes legibles/<subcarpeta>/<cuaderno>/
      <cuaderno> — resumen        Google Doc: texto por página, OCR, fechas
      <cuaderno>.pdf              PDF aplanado (fondo + escritura a mano)
      <cuaderno> — p012.png       imagen de cada página con escritura a mano
y un Google Doc "Índice de apuntes" con todos los cuadernos y qué páginas se
editaron cada día.

Variables de entorno:
  GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET, GOOGLE_REFRESH_TOKEN   (obligatorias)
  SOURCE_FOLDER_ID     carpeta de respaldo de Noteshelf (si falta, busca "Noteshelf 3 Android")
  OUTPUT_FOLDER_NAME   nombre de la carpeta de salida (por defecto "Apuntes legibles")
  PNG_DPI              resolución de las imágenes (por defecto 110)
  FORCE                "1"/"true" para reconvertir todo
  NSA_TZ               zona horaria para las fechas (por defecto UTC)

Permisos OAuth usados: drive.readonly (leer los .nsa) + drive.file (crear y
modificar solo los archivos que crea este script).
"""
from __future__ import annotations

import html
import json
import os
import re
import sys
import tempfile
import traceback
from datetime import datetime

from nsa2pdf import Notebook, _ranges, dates_overview

FOLDER = "application/vnd.google-apps.folder"
GDOC = "application/vnd.google-apps.document"
SCOPES = ["https://www.googleapis.com/auth/drive.readonly",
          "https://www.googleapis.com/auth/drive.file"]
FIELDS = "id,name,mimeType,md5Checksum,modifiedTime,size,parents,appProperties"


def log(*a):
    print(*a, flush=True)


# ---------------------------------------------------------------- Drive
class Drive:
    def __init__(self, service, upload_cls):
        self.s = service
        self.Upload = upload_cls  # MediaFileUpload (o un doble en las pruebas)

    def query(self, q: str):
        out, token = [], None
        while True:
            r = self.s.files().list(q=q, fields=f"nextPageToken,files({FIELDS})", pageSize=1000,
                                    pageToken=token, spaces="drive").execute()
            out += r.get("files", [])
            token = r.get("nextPageToken")
            if not token:
                return out

    def children(self, folder_id):
        return self.query(f"'{folder_id}' in parents and trashed=false")

    def by_prop(self, key, value, parent=None):
        q = f"appProperties has {{ key='{key}' and value='{value}' }} and trashed=false"
        if parent:
            q += f" and '{parent}' in parents"
        return self.query(q)

    def walk_nsa(self, root_id, path=()):
        """Todos los .nsa bajo root_id, con la ruta de carpetas (ids, nombres)."""
        for f in self.children(root_id):
            if f["mimeType"] == FOLDER:
                yield from self.walk_nsa(f["id"], path + ((f["id"], f["name"]),))
            elif f["name"].lower().endswith(".nsa"):
                yield f, path

    def mkdir(self, name, parent, props):
        body = {"name": name, "mimeType": FOLDER, "appProperties": props}
        if parent:
            body["parents"] = [parent]
        return self.s.files().create(body=body, fields=FIELDS).execute()

    def ensure_dir(self, name, parent, props):
        key = "src" if "src" in props else "nsaSync"
        found = [f for f in self.by_prop(key, props[key], parent) if f["mimeType"] == FOLDER]
        if found:
            f = found[0]
            if f["name"] != name:
                self.s.files().update(fileId=f["id"], body={"name": name}).execute()
            return f["id"]
        return self.mkdir(name, parent, props)["id"]

    def upload(self, path, name, parent, mime, as_doc=False, props=None):
        body = {"name": name, "parents": [parent]}
        if as_doc:
            body["mimeType"] = GDOC
        if props:
            body["appProperties"] = props
        media = self.Upload(path, mimetype=mime, resumable=False)
        return self.s.files().create(body=body, media_body=media, fields="id").execute()["id"]

    def replace_doc(self, file_id, html_path):
        media = self.Upload(html_path, mimetype="text/html", resumable=False)
        self.s.files().update(fileId=file_id, media_body=media).execute()

    def delete(self, file_id):
        self.s.files().delete(fileId=file_id).execute()

    def download(self, file_id, dest):
        data = self.s.files().get_media(fileId=file_id).execute()
        with open(dest, "wb") as f:
            f.write(data)


# ---------------------------------------------------------------- estado
def load_state(drive, root):
    files = drive.by_prop("nsaSync", "state", root)
    if not files:
        return None, {}
    try:
        return files[0]["id"], json.loads(drive.s.files().get_media(fileId=files[0]["id"]).execute())
    except Exception:
        return files[0]["id"], {}


def save_state(drive, root, state_id, state, tmp):
    p = os.path.join(tmp, "_estado.json")
    with open(p, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=1)
    if state_id:
        drive.s.files().update(fileId=state_id,
                               media_body=drive.Upload(p, mimetype="application/json", resumable=False)).execute()
    else:
        drive.upload(p, "_estado.json", root, "application/json", props={"nsaSync": "state"})


# ---------------------------------------------------------------- índice
def index_html(state) -> str:
    items = [v for v in state.values() if not v.get("missing")]
    items.sort(key=lambda v: v.get("last_edit", ""), reverse=True)
    try:
        from zoneinfo import ZoneInfo
        now = datetime.now(ZoneInfo(os.environ.get("NSA_TZ") or "UTC")).strftime("%Y-%m-%d %H:%M")
    except Exception:
        now = datetime.now().strftime("%Y-%m-%d %H:%M")
    out = ['<html><head><meta charset="utf-8"></head><body>', "<h1>Índice de apuntes</h1>",
           f"<p><i>Actualizado {html.escape(now)}. Cuadernos ordenados por última edición. "
           "Cada cuaderno tiene su carpeta con el resumen (Google Doc), el PDF y las imágenes "
           "de las páginas escritas a mano.</i></p>"]
    for v in items:
        out.append(f"<h2>{html.escape(v['name'])}</h2>")
        out.append(f"<p>• Ubicación: {html.escape(v.get('path') or '(raíz)')}</p>")
        out.append(f"<p>• Última edición: {html.escape(v.get('last_edit') or '?')} · "
                   f"{v.get('pages', '?')} páginas · a mano: {html.escape(v.get('ink') or 'ninguna')}</p>")
        days = list((v.get("by_day") or {}).items())[:12]
        if days:
            out.append("<p>• Páginas editadas por día: " +
                       html.escape("; ".join(f"{d}: {p}" for d, p in days)) + "</p>")
        if v.get("folder"):
            out.append(f'<p>• Carpeta: <a href="https://drive.google.com/drive/folders/{v["folder"]}">abrir</a></p>')
    out.append("</body></html>")
    return "\n".join(out)


# ---------------------------------------------------------------- un cuaderno
def nice_name(fname: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"\.nsa$", "", fname, flags=re.I)).strip()


def process(drive, f, path, out_root, tmp, dpi, prev):
    name = nice_name(f["name"])
    work = tempfile.mkdtemp(dir=tmp)
    src = os.path.join(work, "in.nsa")
    drive.download(f["id"], src)
    outdir = os.path.join(work, "out")
    nb = Notebook(src)
    pdf, _md, pngs, summary = nb.convert(outdir, dpi, True, name=name)
    base = os.path.splitext(os.path.basename(pdf))[0]
    html_path = os.path.join(outdir, base + ".html")

    # carpetas espejo de la estructura original y carpeta del cuaderno
    parent = out_root
    for fid, fname in path:
        parent = drive.ensure_dir(fname, parent, {"nsaSync": "dir", "src": fid})
    folder = drive.ensure_dir(name, parent, {"nsaSync": "nb", "src": f["id"]})

    # se reemplaza todo el contenido generado del cuaderno
    for old in drive.children(folder):
        drive.delete(old["id"])
    drive.upload(html_path, f"{name} — resumen", folder, "text/html", as_doc=True)
    drive.upload(pdf, f"{name}.pdf", folder, "application/pdf")
    for p in pngs:
        n = re.search(r"p(\d+)\.png$", p).group(1)
        drive.upload(p, f"{name} — p{n}.png", folder, "image/png")

    ink = [s["n"] for s in summary if s["ink"]]
    days = dates_overview(summary)
    return {
        "name": name, "md5": f.get("md5Checksum"), "modified": f.get("modifiedTime"),
        "folder": folder, "path": "/".join(n for _, n in path),
        "pages": len(summary), "ink": _ranges(ink), "ocr_pages": sum(1 for s in summary if s["ocr"]),
        "last_edit": max((s["date"] for s in summary if s.get("date")), default=""),
        "by_day": {d: _ranges(p) for d, p in days.items()},
    }


# ---------------------------------------------------------------- main
def run(drive, source_id, out_name="Apuntes legibles", dpi=110, force=False):
    tmp = tempfile.mkdtemp()
    if not source_id:
        roots = [f for f in drive.query("name='Noteshelf 3 Android' and trashed=false") if f["mimeType"] == FOLDER]
        if not roots:
            raise SystemExit("No encontré la carpeta 'Noteshelf 3 Android'; define SOURCE_FOLDER_ID.")
        source_id = roots[0]["id"]
    out_root = drive.ensure_dir(out_name, None, {"nsaSync": "root"})
    state_id, state = load_state(drive, out_root)

    seen, done, failed = set(), 0, []
    for i, (f, path) in enumerate(drive.walk_nsa(source_id), 1):
        seen.add(f["id"])
        prev = state.get(f["id"], {})
        same = prev.get("md5") and prev.get("md5") == f.get("md5Checksum") and not prev.get("missing")
        if same and not force:
            continue
        # los registros no muestran nombres: en un repo público son visibles para cualquiera
        log(f"→ cuaderno #{i}: convirtiendo")
        try:
            state[f["id"]] = process(drive, f, path, out_root, tmp, dpi, prev)
            done += 1
            st = state[f["id"]]
            log(f"   ok: {st['pages']} págs, a mano: {st['ink'] or 'ninguna'}")
        except Exception as e:
            failed.append(f["id"])
            log(f"   ERROR en cuaderno #{i}: {type(e).__name__}")
            if os.environ.get("NSA_DEBUG"):
                traceback.print_exc()

    for k, v in state.items():
        v["missing"] = k not in seen  # cuadernos borrados en Noteshelf: salen del índice

    if done or not drive.by_prop("nsaSync", "index", out_root) or force:
        p = os.path.join(tmp, "indice.html")
        with open(p, "w", encoding="utf-8") as fh:
            fh.write(index_html(state))
        idx = drive.by_prop("nsaSync", "index", out_root)
        if idx:
            try:
                drive.replace_doc(idx[0]["id"], p)
            except Exception:
                drive.delete(idx[0]["id"])
                drive.upload(p, "Índice de apuntes", out_root, "text/html", as_doc=True, props={"nsaSync": "index"})
        else:
            drive.upload(p, "Índice de apuntes", out_root, "text/html", as_doc=True, props={"nsaSync": "index"})
    save_state(drive, out_root, state_id, state, tmp)

    log(f"Listo: {done} convertidos, {len(seen) - done - len(failed)} sin cambios, {len(failed)} con error.")
    return failed


def main():
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build
    from googleapiclient.http import MediaFileUpload

    missing = [k for k in ("GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET", "GOOGLE_REFRESH_TOKEN") if not os.environ.get(k)]
    if missing:
        raise SystemExit(f"Faltan secretos: {', '.join(missing)}")
    creds = Credentials(None, refresh_token=os.environ["GOOGLE_REFRESH_TOKEN"],
                        client_id=os.environ["GOOGLE_CLIENT_ID"], client_secret=os.environ["GOOGLE_CLIENT_SECRET"],
                        token_uri="https://oauth2.googleapis.com/token", scopes=SCOPES)
    service = build("drive", "v3", credentials=creds, cache_discovery=False)
    failed = run(Drive(service, MediaFileUpload),
                 os.environ.get("SOURCE_FOLDER_ID", "").strip(),
                 os.environ.get("OUTPUT_FOLDER_NAME", "").strip() or "Apuntes legibles",
                 int(os.environ.get("PNG_DPI", "110") or 110),
                 os.environ.get("FORCE", "").lower() in ("1", "true", "yes"))
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
