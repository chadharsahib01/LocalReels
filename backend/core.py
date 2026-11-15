"""LocalReels v2 — standalone core backend. Purani library ke absolute paths bhi serve karta hai."""
from __future__ import annotations

import json
import mimetypes
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, Response, StreamingResponse

ROOT = Path(__file__).resolve().parent.parent
MEDIA = Path(os.environ.get("LOCALREELS_MEDIA_DIR") or (ROOT / "media")).resolve()
MEDIA.mkdir(parents=True, exist_ok=True)
DB_PATH = ROOT / "localreels.db"
VIDEO_EXTS = {".mp4", ".webm", ".mkv", ".mov", ".m4v", ".avi", ".flv", ".ts"}
AUTH_SECRET = os.environ.get("LOCALREELS_AUTH", "nayi_app_secret_123")
OLD_ROOT = Path(os.environ.get("LOCALREELS_OLD_ROOT") or r"C:\LocalReels")

app = FastAPI(title="LocalReels Core", version="2.1.0")


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@contextmanager
def db():
    con = sqlite3.connect(DB_PATH, timeout=30)
    con.row_factory = sqlite3.Row
    try:
        yield con
        con.commit()
    finally:
        con.close()


SCHEMA = """
CREATE TABLE IF NOT EXISTS videos(
  id TEXT PRIMARY KEY, title TEXT NOT NULL, grp TEXT DEFAULT 'Unsorted',
  tags TEXT DEFAULT '[]', height INTEGER DEFAULT 0, dur REAL DEFAULT 0,
  filename TEXT, created_at TEXT, source_url TEXT DEFAULT '');
CREATE TABLE IF NOT EXISTS downloads(
  id TEXT PRIMARY KEY, url TEXT, status TEXT DEFAULT 'queued', progress REAL DEFAULT 0,
  error TEXT DEFAULT '', video_id TEXT DEFAULT '', created_at TEXT);
"""


def init_db() -> None:
    with db() as con:
        con.executescript(SCHEMA)
        have = {r[1] for r in con.execute("PRAGMA table_info(videos)")}
        for col in ("path", "poster_path", "old_id"):
            if col not in have:
                con.execute(f"ALTER TABLE videos ADD COLUMN {col} TEXT DEFAULT ''")


def probe(path: Path) -> tuple[int, float]:
    exe = shutil.which("ffprobe")
    if not exe:
        return 0, 0.0
    try:
        out = subprocess.run(
            [exe, "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=height:format=duration", "-of", "json", str(path)],
            capture_output=True, text=True, timeout=60)
        data = json.loads(out.stdout or "{}")
        streams = data.get("streams") or [{}]
        return int(streams[0].get("height") or 0), round(float((data.get("format") or {}).get("duration") or 0), 2)
    except Exception:
        return 0, 0.0


def make_thumb(src: Path, out: Path) -> bool:
    exe = shutil.which("ffmpeg")
    if not exe or out.is_file():
        return out.is_file()
    for seek in ("2", "0"):
        try:
            subprocess.run([exe, "-y", "-v", "error", "-ss", seek, "-i", str(src),
                            "-vframes", "1", "-vf", "scale=540:-2", str(out)],
                           capture_output=True, timeout=120)
            if out.is_file():
                return True
        except Exception:
            continue
    return False


def pretty(stem: str) -> str:
    return re.sub(r"[_\-]+", " ", stem).strip() or stem


def row_to_video(r: sqlite3.Row) -> dict:
    return {"id": r["id"], "title": r["title"], "group": r["grp"] or "Unsorted",
            "tags": json.loads(r["tags"] or "[]"), "height": r["height"] or 0,
            "duration": r["dur"] or 0, "created_at": r["created_at"],
            "source_url": r["source_url"] or ""}


def get_video(vid: str):
    with db() as con:
        return con.execute("SELECT * FROM videos WHERE id=?", (vid,)).fetchone()


def video_file_for(r) -> Path | None:
    for key in ("path", "filename"):
        val = r[key] if key in r.keys() else ""
        if not val:
            continue
        p = Path(val)
        if p.is_absolute() and p.is_file():
            return p
        cand = MEDIA / val
        if cand.is_file():
            return cand
    for f in MEDIA.glob(f"{r['id']}.*"):
        if f.suffix.lower() in VIDEO_EXTS:
            return f
    return None


def poster_file_for(r) -> Path | None:
    val = r["poster_path"] if "poster_path" in r.keys() else ""
    if val:
        p = Path(val)
        if p.is_file():
            return p
        if (MEDIA / val).is_file():
            return MEDIA / val
    local = MEDIA / f"{r['id']}.jpg"
    if local.is_file():
        return local
    src = video_file_for(r)
    if src and make_thumb(src, local):
        with db() as con:
            con.execute("UPDATE videos SET poster_path=? WHERE id=?", (str(local), r["id"]))
        return local
    return None


def add_video(vid: str, title: str, path: Path, group: str = "Unsorted",
              tags: list | None = None, source_url: str = "") -> dict:
    height, dur = probe(path)
    poster = MEDIA / f"{vid}.jpg"
    make_thumb(path, poster)
    with db() as con:
        con.execute(
            "INSERT OR REPLACE INTO videos(id,title,grp,tags,height,dur,filename,created_at,source_url,path,poster_path,old_id) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (vid, title[:200], group or "Unsorted", json.dumps(tags or []), height, dur,
             path.name, now(), source_url, str(path.resolve()),
             str(poster) if poster.is_file() else "", vid))
        row = con.execute("SELECT * FROM videos WHERE id=?", (vid,)).fetchone()
    return row_to_video(row)


def scan_media() -> int:
    added = 0
    with db() as con:
        known = {r["filename"] for r in con.execute("SELECT filename FROM videos")}
        known_paths = {(r["path"] or "").lower() for r in con.execute("SELECT path FROM videos")}
        ids = {r["id"] for r in con.execute("SELECT id FROM videos")}
    for f in sorted(MEDIA.iterdir()):
        if not f.is_file() or f.suffix.lower() not in VIDEO_EXTS:
            continue
        if f.name in known or str(f.resolve()).lower() in known_paths:
            continue
        vid = f.stem if f.stem not in ids else uuid.uuid4().hex[:10]
        add_video(vid, pretty(f.stem), f, "Library", [])
        added += 1
    return added


# ---------------------------------------------------------------- range-capable file serving
def _iter_file(path: Path, start: int, length: int, chunk: int = 1024 * 1024) -> Iterator[bytes]:
    with path.open("rb") as fh:
        fh.seek(start)
        left = length
        while left > 0:
            data = fh.read(min(chunk, left))
            if not data:
                break
            left -= len(data)
            yield data


def serve(path: Path, request: Request) -> Response:
    try:
        size = path.stat().st_size
    except OSError:
        raise HTTPException(404, "file missing")
    ext = path.suffix.lower()
    mt = {".mp4": "video/mp4", ".m4v": "video/mp4", ".webm": "video/webm",
          ".mkv": "video/x-matroska", ".mov": "video/quicktime",
          ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
          ".webp": "image/webp", ".vtt": "text/vtt"}.get(ext) or mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    base = {"Accept-Ranges": "bytes", "Cache-Control": "public, max-age=3600"}
    if request.method == "HEAD":
        return Response(status_code=200, media_type=mt,
                        headers={**base, "Content-Length": str(size)})
    rng = request.headers.get("range", "")
    m = re.fullmatch(r"bytes=(\d*)-(\d*)", rng.strip()) if rng else None
    if m:
        a, b = m.group(1), m.group(2)
        if not a and b:
            length = min(int(b), size)
            start, end = max(0, size - length), size - 1
        else:
            start = int(a or 0)
            end = min(int(b), size - 1) if b else size - 1
        if start >= size or start > end:
            return Response(status_code=416, headers={"Content-Range": f"bytes */{size}"})
        length = end - start + 1
        return StreamingResponse(_iter_file(path, start, length), status_code=206, media_type=mt,
                                 headers={**base, "Content-Range": f"bytes {start}-{end}/{size}",
                                          "Content-Length": str(length)})
    return FileResponse(path, media_type=mt, headers=base)


@app.api_route("/media/{name:path}", methods=["GET", "HEAD"], include_in_schema=False)
async def media(name: str, request: Request):
    rel = Path(name)
    if rel.is_absolute() or ".." in rel.parts:
        raise HTTPException(404)
    stem, ext = rel.stem, rel.suffix.lower()
    row = get_video(stem)
    if row is not None:
        if ext in {".jpg", ".jpeg", ".png", ".webp"}:
            p = poster_file_for(row)
            if p:
                return serve(p, request)
        elif ext == ".vtt":
            for cand in (MEDIA / f"{stem}.vtt", Path(row["path"] or "").with_suffix(".vtt")):
                if cand.is_file():
                    return serve(cand, request)
        else:
            p = video_file_for(row)
            if p:
                return serve(p, request)
    direct = MEDIA / rel
    if direct.is_file():
        return serve(direct, request)
    parent = MEDIA / rel.parent
    if parent.is_dir():
        for f in parent.iterdir():
            if f.is_file() and f.stem == stem and (
                    (ext in VIDEO_EXTS and f.suffix.lower() in VIDEO_EXTS) or f.suffix.lower() == ext):
                return serve(f, request)
    raise HTTPException(404, "media not found")


# ---------------------------------------------------------------- videos API
@app.get("/api/videos")
def list_videos(skip: int = 0, limit: int = 12, search: str = "", group: str = ""):
    like = f"%{search.lower()}%"
    sql = "SELECT * FROM videos WHERE (LOWER(title) LIKE ? OR LOWER(grp) LIKE ? OR LOWER(tags) LIKE ?)"
    args: list = [like, like, like]
    if group and group != "all":
        sql += " AND grp=?"
        args.append(group)
    with db() as con:
        total = con.execute("SELECT COUNT(*) n FROM (" + sql + ")", args).fetchone()["n"]
        rows = con.execute(sql + " ORDER BY created_at DESC LIMIT ? OFFSET ?",
                           args + [max(1, min(limit, 5000)), max(0, skip)]).fetchall()
    return {"videos": [row_to_video(r) for r in rows], "total": total}


@app.get("/api/groups")
def groups():
    with db() as con:
        rows = con.execute("SELECT grp, COUNT(*) n FROM videos GROUP BY grp ORDER BY grp").fetchall()
    return {"groups": [{"name": r["grp"], "count": r["n"]} for r in rows]}


@app.post("/api/videos/bulk-delete")
def bulk_delete(ids: list):
    ids = [str(i) for i in ids]
    with db() as con:
        for vid in ids:
            r = con.execute("SELECT * FROM videos WHERE id=?", (vid,)).fetchone()
            if not r:
                continue
            for key in ("filename",):
                if r[key]:
                    (MEDIA / r[key]).unlink(missing_ok=True)
            for extra in (f"{vid}.jpg", f"{vid}.vtt"):
                (MEDIA / extra).unlink(missing_ok=True)
            con.execute("DELETE FROM videos WHERE id=?", (vid,))
    return {"status": "Success", "deleted": len(ids)}


@app.post("/api/videos/update-group")
def update_group(payload: dict):
    name = str(payload.get("new_group", "")).strip()[:60]
    ids = [str(i) for i in (payload.get("video_ids") or [])]
    if not name or not ids:
        raise HTTPException(400, "new_group + video_ids chahiye")
    with db() as con:
        con.executemany("UPDATE videos SET grp=? WHERE id=?", [(name, i) for i in ids])
    return {"status": "Success"}


@app.post("/api/videos/update-tags")
def update_tags(payload: dict):
    tags = [str(t).strip() for t in (payload.get("tags") or []) if str(t).strip()]
    ids = [str(i) for i in (payload.get("video_ids") or [])]
    if not tags or not ids:
        raise HTTPException(400, "tags + video_ids chahiye")
    with db() as con:
        for vid in ids:
            r = con.execute("SELECT tags FROM videos WHERE id=?", (vid,)).fetchone()
            if not r:
                continue
            merged = list(dict.fromkeys(json.loads(r["tags"] or "[]") + tags))[:20]
            con.execute("UPDATE videos SET tags=? WHERE id=?", (json.dumps(merged), vid))
    return {"status": "Success"}


@app.post("/api/videos/rename")
def rename(payload: dict):
    vid = str(payload.get("video_id", ""))
    title = str(payload.get("title", "")).strip()[:200]
    if not vid or not title:
        raise HTTPException(400, "video_id + title chahiye")
    with db() as con:
        con.execute("UPDATE videos SET title=? WHERE id=?", (title, vid))
    return {"status": "Success"}


@app.post("/api/scan")
def rescan():
    return {"status": "Success", "added": scan_media()}


@app.post("/api/import-old")
def import_old(payload: dict | None = None):
    payload = payload or {}
    root = Path(payload.get("old_root") or OLD_ROOT)
    if not root.is_dir():
        raise HTTPException(400, f"Folder nahi mila: {root}")
    from backend.import_old import run_import
    return {"status": "Success", **run_import(root, copy=bool(payload.get("copy")), verbose=False)}


# ---------------------------------------------------------------- download
def _set_dl(dl_id: str, **kw) -> None:
    if not kw:
        return
    sets = ", ".join(f"{k}=?" for k in kw)
    with db() as con:
        con.execute(f"UPDATE downloads SET {sets} WHERE id=?", (*kw.values(), dl_id))


def _run_download(dl_id: str, url: str, group: str, tags: list) -> None:
    vid = uuid.uuid4().hex[:10]
    try:
        import yt_dlp  # noqa: F401
    except Exception:
        _set_dl(dl_id, status="error", error="yt-dlp missing: pip install yt-dlp")
        return
    ffmpeg = shutil.which("ffmpeg")
    args = ["--no-playlist", "--restrict-filenames", "--newline", "--no-colors",
            "--write-info-json", "-f", "bv*+ba/b", "--merge-output-format", "mp4",
            "-o", str(MEDIA / f"{vid}.%(ext)s")]
    if ffmpeg:
        args += ["--ffmpeg-location", str(Path(ffmpeg).parent)]
    _set_dl(dl_id, status="downloading", progress=0)
    try:
        proc = subprocess.Popen([sys.executable, "-m", "yt_dlp"] + args + [url],
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True, encoding="utf-8", errors="ignore")
    except Exception as exc:
        _set_dl(dl_id, status="error", error=str(exc)[:300])
        return
    last = -1
    for line in proc.stdout or []:
        m = re.search(r"(\d+(?:\.\d+)?)%", line)
        if m:
            pct = int(float(m.group(1)))
            if pct != last:
                last = pct
                _set_dl(dl_id, progress=pct)
    proc.wait()
    if proc.returncode != 0:
        for f in MEDIA.glob(f"{vid}.*"):
            f.unlink(missing_ok=True)
        _set_dl(dl_id, status="error", error="yt-dlp fail (URL / network check karo)")
        return
    file = next((f for f in MEDIA.glob(f"{vid}.*") if f.suffix.lower() in VIDEO_EXTS), None)
    if file is None:
        _set_dl(dl_id, status="error", error="file nahi mili")
        return
    if file.suffix.lower() != ".mp4" and ffmpeg:
        tmp = MEDIA / f"{vid}_remux.mp4"
        try:
            subprocess.run([ffmpeg, "-y", "-v", "error", "-i", str(file), "-c", "copy", str(tmp)],
                           capture_output=True, timeout=900)
            if tmp.is_file() and tmp.stat().st_size > 0:
                file.unlink(missing_ok=True)
                tmp.rename(MEDIA / f"{vid}.mp4")
                file = MEDIA / f"{vid}.mp4"
            else:
                tmp.unlink(missing_ok=True)
        except Exception:
            tmp.unlink(missing_ok=True)
    title = pretty(vid)
    info = MEDIA / f"{vid}.info.json"
    if info.is_file():
        try:
            title = str(json.loads(info.read_text(encoding="utf-8", errors="ignore")).get("title") or title)[:200]
        except Exception:
            pass
        info.unlink(missing_ok=True)
    add_video(vid, title, file, group or "Downloads", tags or ["download"], url)
    _set_dl(dl_id, status="done", progress=100, video_id=vid)


@app.post("/api/download")
def download(payload: dict, request: Request):
    url = str(payload.get("url", "")).strip()
    if not re.match(r"^https?://", url, re.I):
        raise HTTPException(400, "Sahi http/https URL chahiye")
    sent = request.headers.get("X-LocalReels-Auth")
    if sent and sent != AUTH_SECRET:
        raise HTTPException(403, "Auth header galat hai")
    dl_id = uuid.uuid4().hex[:10]
    with db() as con:
        con.execute("INSERT INTO downloads(id,url,status,progress,created_at) VALUES(?,?,?,0,?)",
                    (dl_id, url, "queued", now()))
    threading.Thread(target=_run_download,
                     args=(dl_id, url, str(payload.get("group") or "Downloads"),
                           [str(t) for t in (payload.get("tags") or [])]),
                     daemon=True).start()
    return {"status": "Success", "download_id": dl_id}


@app.get("/api/downloads")
def downloads():
    with db() as con:
        rows = con.execute("SELECT * FROM downloads ORDER BY created_at DESC LIMIT 30").fetchall()
    return {"downloads": [dict(r) for r in rows]}


@app.get("/api/health")
def health():
    with db() as con:
        n = con.execute("SELECT COUNT(*) n FROM videos").fetchone()["n"]
        ext = con.execute("SELECT COUNT(*) n FROM videos WHERE path<>''").fetchone()["n"]
    yt = False
    try:
        import yt_dlp  # noqa: F401
        yt = True
    except Exception:
        pass
    return {"ok": True, "mode": "standalone-core", "videos": n, "external_paths": ext,
            "media_dir": str(MEDIA), "old_root": str(OLD_ROOT), "db": str(DB_PATH),
            "ffmpeg": bool(shutil.which("ffmpeg")), "yt_dlp": yt}


init_db()
_added = scan_media()
print(f"[LocalReels core] media={MEDIA} | { _added } naye import | db={DB_PATH.name}")