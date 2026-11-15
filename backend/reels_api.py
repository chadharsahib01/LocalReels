"""LocalReels v2 — additive API. Aapke existing tables ko chhuta nahi, apni reels.db use karta hai."""
from __future__ import annotations

import json
import shutil
import sqlite3
import subprocess
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse

ROOT = Path(__file__).resolve().parent.parent
MEDIA = ROOT / "media"
DB_PATH = ROOT / "reels.db"
VIDEO_EXTS = {".mp4", ".webm", ".m4v", ".mov", ".mkv"}

router = APIRouter()


def configure(root: Path, media_dir: Path) -> None:
    global ROOT, MEDIA, DB_PATH
    ROOT, MEDIA = Path(root), Path(media_dir)
    DB_PATH = ROOT / "reels.db"
    MEDIA.mkdir(parents=True, exist_ok=True)


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@contextmanager
def db():
    con = sqlite3.connect(DB_PATH, timeout=20)
    con.row_factory = sqlite3.Row
    try:
        yield con
        con.commit()
    finally:
        con.close()


SCHEMA = """
CREATE TABLE IF NOT EXISTS engagement(
  video_id TEXT PRIMARY KEY, liked INTEGER DEFAULT 0, saved INTEGER DEFAULT 0,
  views INTEGER DEFAULT 0, watch REAL DEFAULT 0, pos REAL DEFAULT 0,
  dur REAL DEFAULT 0, reaction TEXT DEFAULT '', updated_at TEXT);
CREATE TABLE IF NOT EXISTS comments(
  id TEXT PRIMARY KEY, video_id TEXT, author TEXT, body TEXT,
  likes INTEGER DEFAULT 0, liked INTEGER DEFAULT 0, created_at TEXT);
CREATE INDEX IF NOT EXISTS idx_comments_video ON comments(video_id);
CREATE TABLE IF NOT EXISTS reactions(video_id TEXT, emoji TEXT, count INTEGER DEFAULT 0,
  PRIMARY KEY(video_id, video_id, emoji));
CREATE TABLE IF NOT EXISTS playlists(id TEXT PRIMARY KEY, name TEXT, position INTEGER DEFAULT 0, created_at TEXT);
CREATE TABLE IF NOT EXISTS playlist_items(playlist_id TEXT, video_id TEXT, position INTEGER DEFAULT 0,
  PRIMARY KEY(playlist_id, video_id));
CREATE TABLE IF NOT EXISTS saved_views(id TEXT PRIMARY KEY, name TEXT, search TEXT, grp TEXT, sort TEXT, created_at TEXT);
CREATE TABLE IF NOT EXISTS overrides(video_id TEXT PRIMARY KEY, title TEXT, updated_at TEXT);
CREATE TABLE IF NOT EXISTS uploads(id TEXT PRIMARY KEY, title TEXT, grp TEXT, tags TEXT,
  height INTEGER, dur REAL, ext TEXT, created_at TEXT);
CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT);
"""


def init_db() -> None:
    with db() as con:
        con.executescript(SCHEMA)


# ---------------------------------------------------------------- UI files
def _ui(name: str, mime: str):
    path = ROOT / name
    if not path.is_file():
        raise HTTPException(404, f"{name} missing")
    return FileResponse(path, media_type=mime, headers={"Cache-Control": "no-cache"})


@router.get("/", include_in_schema=False)
def ui_index():
    return _ui("index.html", "text/html; charset=utf-8")


@router.get("/index.html", include_in_schema=False)
def ui_index_alias():
    return _ui("index.html", "text/html; charset=utf-8")


@router.get("/app.js", include_in_schema=False)
def ui_js():
    return _ui("app.js", "application/javascript; charset=utf-8")


@router.get("/styles.css", include_in_schema=False)
def ui_css():
    return _ui("styles.css", "text/css; charset=utf-8")


@router.get("/manifest.webmanifest", include_in_schema=False)
def ui_manifest():
    return _ui("manifest.webmanifest", "application/manifest+json")


@router.get("/sw.js", include_in_schema=False)
def ui_sw():
    return _ui("sw.js", "application/javascript; charset=utf-8")


@router.get("/icon.svg", include_in_schema=False)
def ui_icon():
    return _ui("icon.svg", "image/svg+xml")


# ---------------------------------------------------------------- helpers
def eng_row(r: sqlite3.Row) -> dict:
    return {"liked": r["liked"], "saved": r["saved"], "views": r["views"],
            "watch": r["watch"], "position": r["pos"], "duration": r["dur"],
            "reaction": r["reaction"] or ""}


def upload_row(r: sqlite3.Row) -> dict:
    vid = r["id"]
    return {"id": vid, "title": r["title"], "group": r["grp"] or "Uploads",
            "tags": json.loads(r["tags"] or "[]"), "height": r["height"] or 0,
            "duration": r["dur"] or 0, "src": f"/reels-media/{vid}{r['ext']}",
            "poster": f"/reels-media/{vid}.jpg", "uploaded": True,
            "added": r["created_at"], "likes": 0}


def probe(path: Path) -> tuple[int, float]:
    exe = shutil.which("ffprobe")
    if not exe:
        return 0, 0.0
    try:
        out = subprocess.run(
            [exe, "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=height:format=duration", "-of", "json", str(path)],
            capture_output=True, text=True, timeout=45)
        data = json.loads(out.stdout or "{}")
        streams = data.get("streams") or [{}]
        height = int(streams[0].get("height") or 0)
        dur = float((data.get("format") or {}).get("duration") or 0)
        return height, round(dur, 2)
    except Exception:
        return 0, 0.0


def make_thumb(path: Path, out: Path) -> bool:
    exe = shutil.which("ffmpeg")
    if not exe:
        return False
    try:
        subprocess.run([exe, "-y", "-v", "error", "-ss", "2", "-i", str(path),
                        "-vframes", "1", "-vf", "scale=540:-2", str(out)],
                       capture_output=True, timeout=90)
        return out.is_file()
    except Exception:
        return False


# ---------------------------------------------------------------- state
@router.get("/api/reels/state")
def get_state():
    with db() as con:
        engagement = {r["video_id"]: eng_row(r) for r in con.execute("SELECT * FROM engagement")}
        counts = {r["video_id"]: r["n"] for r in
                  con.execute("SELECT video_id, COUNT(*) n FROM comments GROUP BY video_id")}
        reactions: dict[str, dict[str, int]] = {}
        for r in con.execute("SELECT * FROM reactions"):
            reactions.setdefault(r["video_id"], {})[r["emoji"]] = r["count"]
        playlists = []
        for p in con.execute("SELECT * FROM playlists ORDER BY position, created_at"):
            items = [i["video_id"] for i in con.execute(
                "SELECT video_id FROM playlist_items WHERE playlist_id=? ORDER BY position", (p["id"],))]
            playlists.append({"id": p["id"], "name": p["name"], "items": items})
        saved = [{"id": r["id"], "name": r["name"], "search": r["search"],
                  "group": r["grp"], "sort": r["sort"]}
                 for r in con.execute("SELECT * FROM saved_views ORDER BY created_at DESC")]
        uploads = [upload_row(r) for r in con.execute("SELECT * FROM uploads ORDER BY created_at DESC")]
        overrides = {r["video_id"]: r["title"] for r in con.execute("SELECT * FROM overrides")}
        settings = {r["key"]: json.loads(r["value"]) for r in con.execute("SELECT * FROM settings")}
    return {"ok": True, "engagement": engagement, "comment_counts": counts, "reactions": reactions,
            "playlists": playlists, "saved_views": saved, "uploads": uploads,
            "overrides": overrides, "settings": settings}


@router.post("/api/reels/like")
def set_like(payload: dict):
    vid, val = str(payload.get("video_id", "")), 1 if payload.get("value") else 0
    if not vid:
        raise HTTPException(400, "video_id required")
    with db() as con:
        con.execute("INSERT INTO engagement(video_id, liked, updated_at) VALUES(?,?,?) "
                    "ON CONFLICT(video_id) DO UPDATE SET liked=excluded.liked, updated_at=excluded.updated_at",
                    (vid, val, now()))
    return {"ok": True, "liked": val}


@router.post("/api/reels/save")
def set_save(payload: dict):
    vid, val = str(payload.get("video_id", "")), 1 if payload.get("value") else 0
    if not vid:
        raise HTTPException(400, "video_id required")
    with db() as con:
        con.execute("INSERT INTO engagement(video_id, saved, updated_at) VALUES(?,?,?) "
                    "ON CONFLICT(video_id) DO UPDATE SET saved=excluded.saved, updated_at=excluded.updated_at",
                    (vid, val, now()))
    return {"ok": True, "saved": val}


@router.post("/api/reels/view")
def track_view(payload: dict):
    vid = str(payload.get("video_id", ""))
    if not vid:
        raise HTTPException(400, "video_id required")
    pos = float(payload.get("position") or 0)
    dur = float(payload.get("duration") or 0)
    delta = max(0.0, min(float(payload.get("delta") or 0), 30.0))
    first = bool(payload.get("first"))
    with db() as con:
        con.execute("INSERT INTO engagement(video_id, views, watch, pos, dur, updated_at) VALUES(?,?,?,?,?,?) "
                    "ON CONFLICT(video_id) DO UPDATE SET views=engagement.views+excluded.views, "
                    "watch=engagement.watch+excluded.watch, pos=excluded.pos, "
                    "dur=CASE WHEN excluded.dur>0 THEN excluded.dur ELSE engagement.dur END, "
                    "updated_at=excluded.updated_at",
                    (vid, 1 if first else 0, delta, pos, dur, now()))
        row = con.execute("SELECT * FROM engagement WHERE video_id=?", (vid,)).fetchone()
    return {"ok": True, "engagement": eng_row(row)}


@router.post("/api/reels/react")
def react(payload: dict):
    vid, emoji = str(payload.get("video_id", "")), str(payload.get("emoji", ""))[:8]
    if not vid or not emoji:
        raise HTTPException(400, "video_id + emoji required")
    with db() as con:
        con.execute("INSERT INTO reactions(video_id, emoji, count) VALUES(?,?,1) "
                    "ON CONFLICT(video_id, emoji) DO UPDATE SET count=count+1", (vid, emoji))
        con.execute("INSERT INTO engagement(video_id, reaction, updated_at) VALUES(?,?,?) "
                    "ON CONFLICT(video_id) DO UPDATE SET reaction=excluded.reaction", (vid, emoji, now()))
        rows = con.execute("SELECT emoji, count FROM reactions WHERE video_id=?", (vid,)).fetchall()
    return {"ok": True, "reactions": {r["emoji"]: r["count"] for r in rows}}


# ---------------------------------------------------------------- comments
@router.get("/api/reels/comments")
def list_comments(video_id: str):
    with db() as con:
        rows = con.execute("SELECT * FROM comments WHERE video_id=? ORDER BY created_at DESC", (video_id,)).fetchall()
    return {"ok": True, "comments": [dict(r) for r in rows]}


@router.post("/api/reels/comments")
def add_comment(payload: dict):
    vid = str(payload.get("video_id", ""))
    body = str(payload.get("body", "")).strip()[:400]
    if not vid or not body:
        raise HTTPException(400, "video_id + body required")
    cid = uuid.uuid4().hex[:10]
    author = str(payload.get("author") or "You")[:40]
    with db() as con:
        con.execute("INSERT INTO comments(id, video_id, author, body, likes, liked, created_at) VALUES(?,?,?,?,0,0,?)",
                    (cid, vid, author, body, now()))
        row = con.execute("SELECT * FROM comments WHERE id=?", (cid,)).fetchone()
    return {"ok": True, "comment": dict(row)}


@router.post("/api/reels/comments/like")
def like_comment(payload: dict):
    cid, val = str(payload.get("comment_id", "")), bool(payload.get("value"))
    with db() as con:
        row = con.execute("SELECT * FROM comments WHERE id=?", (cid,)).fetchone()
        if not row:
            raise HTTPException(404, "comment not found")
        new_likes = max(0, row["likes"] + (1 if val else -1))
        con.execute("UPDATE comments SET likes=?, liked=? WHERE id=?", (new_likes, 1 if val else 0, cid))
    return {"ok": True, "likes": new_likes, "liked": 1 if val else 0}


@router.delete("/api/reels/comments/{cid}")
def del_comment(cid: str):
    with db() as con:
        con.execute("DELETE FROM comments WHERE id=?", (cid,))
    return {"ok": True}


# ---------------------------------------------------------------- playlists
@router.post("/api/reels/playlists")
def create_playlist(payload: dict):
    name = str(payload.get("name", "")).strip()[:60]
    if not name:
        raise HTTPException(400, "name required")
    pid = uuid.uuid4().hex[:10]
    with db() as con:
        pos = con.execute("SELECT COALESCE(MAX(position),0)+1 p FROM playlists").fetchone()["p"]
        con.execute("INSERT INTO playlists(id,name,position,created_at) VALUES(?,?,?,?)", (pid, name, pos, now()))
    return {"ok": True, "playlist": {"id": pid, "name": name, "items": []}}


@router.post("/api/reels/playlists/rename")
def rename_playlist(payload: dict):
    pid, name = str(payload.get("id", "")), str(payload.get("name", "")).strip()[:60]
    with db() as con:
        con.execute("UPDATE playlists SET name=? WHERE id=?", (name, pid))
    return {"ok": True}


@router.delete("/api/reels/playlists/{pid}")
def delete_playlist(pid: str):
    with db() as con:
        con.execute("DELETE FROM playlist_items WHERE playlist_id=?", (pid,))
        con.execute("DELETE FROM playlists WHERE id=?", (pid,))
    return {"ok": True}


@router.post("/api/reels/playlists/items")
def playlist_add(payload: dict):
    pid = str(payload.get("playlist_id", ""))
    ids = [str(v) for v in (payload.get("video_ids") or [])]
    with db() as con:
        pos = con.execute("SELECT COALESCE(MAX(position),0) p FROM playlist_items WHERE playlist_id=?", (pid,)).fetchone()["p"]
        for vid in ids:
            pos += 1
            con.execute("INSERT OR IGNORE INTO playlist_items(playlist_id,video_id,position) VALUES(?,?,?)", (pid, vid, pos))
    return {"ok": True}


@router.post("/api/reels/playlists/remove")
def playlist_remove(payload: dict):
    with db() as con:
        con.execute("DELETE FROM playlist_items WHERE playlist_id=? AND video_id=?",
                    (str(payload.get("playlist_id", "")), str(payload.get("video_id", ""))))
    return {"ok": True}


@router.post("/api/reels/playlists/reorder")
def playlist_reorder(payload: dict):
    pid = str(payload.get("playlist_id", ""))
    ids = [str(v) for v in (payload.get("video_ids") or [])]
    with db() as con:
        for i, vid in enumerate(ids):
            con.execute("UPDATE playlist_items SET position=? WHERE playlist_id=? AND video_id=?", (i, pid, vid))
    return {"ok": True}


# ---------------------------------------------------------------- saved views / rename / settings
@router.post("/api/reels/views")
def save_view(payload: dict):
    name = str(payload.get("name", "")).strip()[:50]
    if not name:
        raise HTTPException(400, "name required")
    vid = uuid.uuid4().hex[:10]
    with db() as con:
        con.execute("INSERT INTO saved_views(id,name,search,grp,sort,created_at) VALUES(?,?,?,?,?,?)",
                    (vid, name, str(payload.get("search", "")), str(payload.get("group", "all")),
                     str(payload.get("sort", "new")), now()))
    return {"ok": True, "view": {"id": vid, "name": name, "search": payload.get("search", ""),
                                 "group": payload.get("group", "all"), "sort": payload.get("sort", "new")}}


@router.delete("/api/reels/views/{vid}")
def delete_view(vid: str):
    with db() as con:
        con.execute("DELETE FROM saved_views WHERE id=?", (vid,))
    return {"ok": True}


@router.post("/api/reels/rename")
def rename_video(payload: dict):
    vid, title = str(payload.get("video_id", "")), str(payload.get("title", "")).strip()[:200]
    if not vid or not title:
        raise HTTPException(400, "video_id + title required")
    with db() as con:
        con.execute("INSERT INTO overrides(video_id,title,updated_at) VALUES(?,?,?) "
                    "ON CONFLICT(video_id) DO UPDATE SET title=excluded.title", (vid, title, now()))
    return {"ok": True}


@router.post("/api/reels/settings")
def put_setting(payload: dict):
    key = str(payload.get("key", ""))[:40]
    with db() as con:
        con.execute("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (key, json.dumps(payload.get("value"))))
    return {"ok": True}


@router.post("/api/reels/history/clear")
def clear_history():
    with db() as con:
        con.execute("UPDATE engagement SET pos=0, watch=0, views=0")
    return {"ok": True}


# ---------------------------------------------------------------- uploads
@router.post("/api/reels/upload")
async def upload(file: UploadFile = File(...), title: str = Form(""), group: str = Form("Uploads"), tags: str = Form("")):
    ext = Path(file.filename or "video.mp4").suffix.lower()
    if ext not in VIDEO_EXTS:
        raise HTTPException(400, f"Unsupported format {ext}. MP4/WebM/MOV/MKV allowed.")
    vid = "up_" + uuid.uuid4().hex[:8]
    dest = MEDIA / f"{vid}{ext}"
    size = 0
    with dest.open("wb") as out:
        while True:
            chunk = await file.read(1024 * 1024)
            if not chunk:
                break
            size += len(chunk)
            if size > 3 * 1024 * 1024 * 1024:
                out.close()
                dest.unlink(missing_ok=True)
                raise HTTPException(413, "File 3GB se badi hai")
            out.write(chunk)
    await file.close()

    height, dur = probe(dest)
    make_thumb(dest, MEDIA / f"{vid}.jpg")
    tag_list = [t.strip() for t in tags.split(",") if t.strip()][:12]
    clean_title = (title or Path(file.filename or vid).stem)[:200]
    with db() as con:
        con.execute("INSERT INTO uploads(id,title,grp,tags,height,dur,ext,created_at) VALUES(?,?,?,?,?,?,?,?)",
                    (vid, clean_title, group or "Uploads", json.dumps(tag_list), height, dur, ext, now()))
        row = con.execute("SELECT * FROM uploads WHERE id=?", (vid,)).fetchone()
    return {"ok": True, "video": upload_row(row)}


@router.delete("/api/reels/uploads/{vid}")
def delete_upload(vid: str):
    with db() as con:
        row = con.execute("SELECT * FROM uploads WHERE id=?", (vid,)).fetchone()
        if row:
            (MEDIA / f"{vid}{row['ext']}").unlink(missing_ok=True)
            (MEDIA / f"{vid}.jpg").unlink(missing_ok=True)
            con.execute("DELETE FROM uploads WHERE id=?", (vid,))
            con.execute("DELETE FROM engagement WHERE video_id=?", (vid,))
            con.execute("DELETE FROM comments WHERE video_id=?", (vid,))
    return {"ok": True}


# ---------------------------------------------------------------- stats / export
@router.get("/api/reels/stats")
def stats():
    files = [p for p in MEDIA.rglob("*") if p.suffix.lower() in VIDEO_EXTS]
    disk = sum(p.stat().st_size for p in files if p.is_file())
    with db() as con:
        agg = con.execute("SELECT COALESCE(SUM(views),0) v, COALESCE(SUM(watch),0) w, "
                          "COALESCE(SUM(liked),0) l, COALESCE(SUM(saved),0) s FROM engagement").fetchone()
        comments = con.execute("SELECT COUNT(*) n FROM comments").fetchone()["n"]
        top = [dict(r) for r in con.execute(
            "SELECT video_id, watch, views FROM engagement ORDER BY watch DESC LIMIT 8")]
    return {"ok": True, "files": len(files), "disk_bytes": disk, "views": agg["v"],
            "watch_seconds": agg["w"], "liked": agg["l"], "saved": agg["s"],
            "comments": comments, "top": top,
            "ffmpeg": bool(shutil.which("ffmpeg")), "media_dir": str(MEDIA)}


@router.get("/api/reels/export")
def export():
    state = get_state()
    with db() as con:
        state["comments"] = [dict(r) for r in con.execute("SELECT * FROM comments")]
    state["exported_at"] = now()
    return JSONResponse(state, headers={"Content-Disposition": 'attachment; filename="localreels-backup.json"'})