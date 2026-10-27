"""Prueba de extremo a extremo de sync_drive.py contra un Drive simulado en memoria.

Uso:  python3 tests/test_sync.py <cuaderno.nsa> [otro.nsa]
"""
import hashlib
import itertools
import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import sync_drive  # noqa: E402

FOLDER = sync_drive.FOLDER


class Req:
    def __init__(self, fn):
        self.fn = fn

    def execute(self):
        return self.fn()


class FakeUpload:
    def __init__(self, path, mimetype=None, resumable=False):
        self.data = open(path, "rb").read()
        self.mimetype = mimetype


class FakeFiles:
    def __init__(self, store):
        self.st = store
        self.ids = itertools.count(1)

    def _match(self, f, q):
        if "trashed=false" in q and f.get("trashed"):
            return False
        for parent in re.findall(r"'([^']+)' in parents", q):
            if parent not in f.get("parents", []):
                return False
        for k, v in re.findall(r"appProperties has \{ key='([^']+)' and value='([^']+)' \}", q):
            if (f.get("appProperties") or {}).get(k) != v:
                return False
        m = re.search(r"\bname='([^']+)'", q)
        if m and f["name"] != m.group(1):
            return False
        return True

    def list(self, q, **kw):
        return Req(lambda: {"files": [self._meta(f) for f in self.st.values() if self._match(f, q)]})

    def _meta(self, f):
        return {k: v for k, v in f.items() if k != "data"}

    def create(self, body, media_body=None, fields=None):
        def go():
            fid = f"id{next(self.ids)}"
            f = dict(body, id=fid)
            f.setdefault("parents", ["root"])
            f.setdefault("mimeType", media_body.mimetype if media_body else FOLDER)
            if media_body:
                f["data"] = media_body.data
                f["upload_mime"] = media_body.mimetype
                f["md5Checksum"] = hashlib.md5(media_body.data).hexdigest()
            self.st[fid] = f
            return self._meta(f)
        return Req(go)

    def update(self, fileId, body=None, media_body=None):
        def go():
            f = self.st[fileId]
            f.update(body or {})
            if media_body:
                f["data"] = media_body.data
            return self._meta(f)
        return Req(go)

    def delete(self, fileId):
        return Req(lambda: self.st.pop(fileId))

    def get_media(self, fileId):
        return Req(lambda: self.st[fileId]["data"])


class FakeService:
    def __init__(self):
        self.store = {}
        self._files = FakeFiles(self.store)

    def files(self):
        return self._files

    def add(self, name, parent, data=None, folder=False):
        fid = f"src{len(self.store)}"
        f = {"id": fid, "name": name, "parents": [parent], "mimeType": FOLDER if folder else "application/x-zip"}
        if data is not None:
            f["data"] = data
            f["md5Checksum"] = hashlib.md5(data).hexdigest()
        self.store[fid] = f
        return fid


def tree(svc, root, ind=""):
    for f in sorted(svc.store.values(), key=lambda x: x["name"]):
        if root in f.get("parents", []):
            kind = "📁" if f["mimeType"] == FOLDER else f"({f['mimeType'].split('.')[-1]})"
            print(f"{ind}{kind} {f['name']}")
            if f["mimeType"] == FOLDER:
                tree(svc, f["id"], ind + "   ")


def main(paths):
    svc = FakeService()
    src = svc.add("Noteshelf 3 Android", "root", folder=True)
    uni = svc.add("Carpeta", src, folder=True)
    for p in paths:
        svc.add(os.path.basename(p), uni, open(p, "rb").read())
    drive = sync_drive.Drive(svc, FakeUpload)

    print("== primera corrida (busca la carpeta por nombre)")
    assert sync_drive.run(drive, "", dpi=40) == []
    out = [f for f in svc.store.values() if (f.get("appProperties") or {}).get("nsaSync") == "root"][0]
    tree(svc, out["id"])
    n_files = len(svc.store)

    print("== segunda corrida sin cambios")
    assert sync_drive.run(drive, src, dpi=40) == []
    assert len(svc.store) == n_files, "no debía crear archivos nuevos"

    print("== cuaderno modificado")
    nsa = next(f for f in svc.store.values() if f["name"].endswith(".nsa"))
    nsa["md5Checksum"] = "cambiado"
    assert sync_drive.run(drive, src, dpi=40) == []
    assert len(svc.store) == n_files, "debía reemplazar, no duplicar"

    idx = next(f for f in svc.store.values() if (f.get("appProperties") or {}).get("nsaSync") == "index")
    assert idx["mimeType"] == sync_drive.GDOC and idx["upload_mime"] == "text/html"
    print("== índice:")
    print(re.sub(r"<[^>]+>", "", idx["data"].decode())[:900])
    print("TODO OK")


if __name__ == "__main__":
    main(sys.argv[1:])
