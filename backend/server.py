"""LocalReels v9.0.1 — standalone backend.
Purane DB schema ko safe migrate karta hai (columns pehle, indexes baad mein).
SQL search+pagination (FTS5), multi-worker thumbnails, ETag media,
self-hosted fonts, unified sources, backup/restore."""
from __future__ import annotations

import json
import mimetypes
import os
import queue
import re
import shutil
import socket
import sqlite3
import subprocess
import sys
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

import httpx
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse

ROOT = Path(__file__).resolve().parent.parent
PORT = int(os.environ.get("LOCALREELS_PORT") or 8000)
MEDIA = Path(os.environ.get("LOCALREELS_MEDIA_DIR") or (ROOT / "media")).resolve()
MEDIA.mkdir(parents=True, exist_ok=True)
FONTS = ROOT / "fonts"
FONTS.mkdir(parents=True, exist_ok=True)
DB = ROOT / "localreels.db"
AUTH = os.environ.get("LOCALREELS_AUTH", "nayi_app_secret_123")
THUMB_WORKERS = max(1, int(os.environ.get("LOCALREELS_THUMB_WORKERS") or 3))
VEXT = {".mp4", ".webm", ".mkv", ".mov", ".m4v", ".avi", ".flv", ".ts"}
FFPROBE, FFMPEG = shutil.which("ffprobe"), shutil.which("ffmpeg")

app = FastAPI(title="LocalReels", version="9.0.1")
print(f"[LocalReels] boot media={MEDIA}", flush=True)


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def pretty(s):
    return re.sub(r"[_\-]+", " ", str(s or "")).strip() or str(s or "")


def cx(ro=True):
    c = sqlite3.connect(f"file:{DB.as_posix()}?mode=ro" if ro else str(DB), uri=ro, timeout=30)
    c.row_factory = sqlite3.Row
    if not ro:
        try:
            c.execute("PRAGMA journal_mode=WAL")
        except Exception:
            pass
    return c


# ---------------------------------------------------------------- schema (tables only)
TABLES = """
CREATE TABLE IF NOT EXISTS videos(
 id TEXT PRIMARY KEY, title TEXT, grp TEXT DEFAULT 'Unsorted', tags TEXT DEFAULT '[]',
 width INTEGER DEFAULT 0, height INTEGER DEFAULT 0, dur REAL DEFAULT 0,
 filename TEXT, created_at TEXT, source_url TEXT DEFAULT '', path TEXT DEFAULT '',
 poster_path TEXT DEFAULT '', bytes INTEGER DEFAULT 0,
 deleted INTEGER DEFAULT 0, deleted_at TEXT DEFAULT '', source_id TEXT DEFAULT '');
CREATE TABLE IF NOT EXISTS jobs(
 id TEXT PRIMARY KEY, kind TEXT, input TEXT, status TEXT DEFAULT 'queued',
 progress REAL DEFAULT 0, error TEXT DEFAULT '', video_id TEXT DEFAULT '',
 quality TEXT DEFAULT 'best', created_at TEXT);
CREATE TABLE IF NOT EXISTS sources(
 id TEXT PRIMARY KEY, kind TEXT, name TEXT, location TEXT, created_at TEXT, last_scan TEXT DEFAULT '');
CREATE TABLE IF NOT EXISTS engagement(
 video_id TEXT PRIMARY KEY, liked INTEGER DEFAULT 0, saved INTEGER DEFAULT 0,
 views INTEGER DEFAULT 0, watch REAL DEFAULT 0, pos REAL DEFAULT 0,
 reaction TEXT DEFAULT '', speed REAL DEFAULT 0, updated_at TEXT);
CREATE TABLE IF NOT EXISTS comments(
 id TEXT PRIMARY KEY, video_id TEXT, author TEXT, body TEXT,
 likes INTEGER DEFAULT 0, liked INTEGER DEFAULT 0, created_at TEXT);
CREATE TABLE IF NOT EXISTS reactions(
 video_id TEXT, emoji TEXT, count INTEGER DEFAULT 0, PRIMARY KEY(video_id, emoji));
CREATE TABLE IF NOT EXISTS playlists(
 id TEXT PRIMARY KEY, name TEXT, position INTEGER DEFAULT 0, created_at TEXT);
CREATE TABLE IF NOT EXISTS playlist_items(
 playlist_id TEXT, video_id TEXT, position INTEGER DEFAULT 0, PRIMARY KEY(playlist_id, video_id));
CREATE TABLE IF NOT EXISTS saved_views(
 id TEXT PRIMARY KEY, name TEXT, search TEXT, grp TEXT, sort TEXT, aspect TEXT, created_at TEXT);
CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);
"""

VIDEO_COLS = {
    "title": "TEXT", "grp": "TEXT DEFAULT 'Unsorted'", "tags": "TEXT DEFAULT '[]'",
    "width": "INTEGER DEFAULT 0", "height": "INTEGER DEFAULT 0", "dur": "REAL DEFAULT 0",
    "filename": "TEXT", "created_at": "TEXT", "source_url": "TEXT DEFAULT ''",
    "path": "TEXT DEFAULT ''", "poster_path": "TEXT DEFAULT ''", "bytes": "INTEGER DEFAULT 0",
    "deleted": "INTEGER DEFAULT 0", "deleted_at": "TEXT DEFAULT ''", "source_id": "TEXT DEFAULT ''",
}
ENG_COLS = {
    "liked": "INTEGER DEFAULT 0", "saved": "INTEGER DEFAULT 0", "views": "INTEGER DEFAULT 0",
    "watch": "REAL DEFAULT 0", "pos": "REAL DEFAULT 0", "reaction": "TEXT DEFAULT ''",
    "speed": "REAL DEFAULT 0", "updated_at": "TEXT",
}
JOB_COLS = {
    "kind": "TEXT", "input": "TEXT", "status": "TEXT DEFAULT 'queued'",
    "progress": "REAL DEFAULT 0", "error": "TEXT DEFAULT ''", "video_id": "TEXT DEFAULT ''",
    "quality": "TEXT DEFAULT 'best'", "created_at": "TEXT",
}
SRC_COLS = {
    "kind": "TEXT", "name": "TEXT", "location": "TEXT",
    "created_at": "TEXT", "last_scan": "TEXT DEFAULT ''",
}
CM_COLS = {
    "video_id": "TEXT", "author": "TEXT", "body": "TEXT",
    "likes": "INTEGER DEFAULT 0", "liked": "INTEGER DEFAULT 0", "created_at": "TEXT",
}
SV_COLS = {
    "name": "TEXT", "search": "TEXT", "grp": "TEXT",
    "sort": "TEXT", "aspect": "TEXT", "created_at": "TEXT",
}

INDEXES = [
    "CREATE INDEX IF NOT EXISTS ix_v_live ON videos(deleted, created_at DESC)",
    "CREATE INDEX IF NOT EXISTS ix_v_grp ON videos(grp)",
    "CREATE INDEX IF NOT EXISTS ix_v_title ON videos(title)",
    "CREATE INDEX IF NOT EXISTS ix_v_dur ON videos(dur)",
    "CREATE INDEX IF NOT EXISTS ix_v_h ON videos(height)",
    "CREATE INDEX IF NOT EXISTS ix_v_bytes ON videos(bytes)",
    "CREATE INDEX IF NOT EXISTS ix_j_created ON jobs(created_at DESC)",
    "CREATE INDEX IF NOT EXISTS ix_cm ON comments(video_id, created_at DESC)",
]

FTS = False


def ensure_cols(c, table, cols):
    try:
        have = {r[1] for r in c.execute(f"PRAGMA table_info({table})")}
    except Exception as e:
        print(f"[LocalReels] pragma {table}:", e, flush=True)
        return
    if not have:
        return
    for name, typ in cols.items():
        if name not in have:
            try:
                c.execute(f"ALTER TABLE {table} ADD COLUMN {name} {typ}")
                print(f"[LocalReels] migrate: {table}.{name} added", flush=True)
            except Exception as e:
                print(f"[LocalReels] migrate {table}.{name} skip:", e, flush=True)


def init_db():
    global FTS
    c = cx(False)
    try:
        try:
            c.executescript(TABLES)
        except Exception as e:
            print("[LocalReels] tables:", e, flush=True)
        ensure_cols(c, "videos", VIDEO_COLS)
        ensure_cols(c, "engagement", ENG_COLS)
        ensure_cols(c, "jobs", JOB_COLS)
        ensure_cols(c, "sources", SRC_COLS)
        ensure_cols(c, "comments", CM_COLS)
        ensure_cols(c, "saved_views", SV_COLS)
        try:
            c.execute("UPDATE videos SET deleted=0 WHERE deleted IS NULL")
            c.execute("UPDATE videos SET width=0 WHERE width IS NULL")
            c.execute("UPDATE videos SET height=0 WHERE height IS NULL")
            c.execute("UPDATE videos SET bytes=0 WHERE bytes IS NULL")
            c.execute("UPDATE videos SET tags='[]' WHERE tags IS NULL OR tags=''")
            c.execute("UPDATE videos SET grp='Unsorted' WHERE grp IS NULL OR grp=''")
        except Exception as e:
            print("[LocalReels] normalize:", e, flush=True)
        c.commit()
        for sql in INDEXES:
            try:
                c.execute(sql)
            except Exception as e:
                print("[LocalReels] index skip:", e, flush=True)
        c.commit()
        try:
            c.execute("CREATE VIRTUAL TABLE IF NOT EXISTS videos_fts USING fts5(title, grp, tags, content='videos', content_rowid='rowid')")
            c.executescript("""
CREATE TRIGGER IF NOT EXISTS v_ai AFTER INSERT ON videos BEGIN
 INSERT INTO videos_fts(rowid,title,grp,tags) VALUES(new.rowid,new.title,new.grp,new.tags); END;
CREATE TRIGGER IF NOT EXISTS v_ad AFTER DELETE ON videos BEGIN
 INSERT INTO videos_fts(videos_fts,rowid,title,grp,tags) VALUES('delete',old.rowid,old.title,old.grp,old.tags); END;
CREATE TRIGGER IF NOT EXISTS v_au AFTER UPDATE ON videos BEGIN
 INSERT INTO videos_fts(videos_fts,rowid,title,grp,tags) VALUES('delete',old.rowid,old.title,old.grp,old.tags);
 INSERT INTO videos_fts(rowid,title,grp,tags) VALUES(new.rowid,new.title,new.grp,new.tags); END;
""")
            c.execute("INSERT INTO videos_fts(videos_fts) VALUES('rebuild')")
            c.commit()
            FTS = True
        except Exception as e:
            print("[LocalReels] FTS5 nahi bana, LIKE fallback:", e, flush=True)
            FTS = False
    finally:
        c.close()


init_db()


def log_meta(k, v):
    try:
        c = cx(False)
        c.execute("INSERT OR REPLACE INTO meta(key,value) VALUES(?,?)", (k, str(v)))
        c.commit()
        c.close()
    except Exception:
        pass


def get_meta(k, d=""):
    try:
        c = cx()
        r = c.execute("SELECT value FROM meta WHERE key=?", (k,)).fetchone()
        c.close()
        return r["value"] if r else d
    except Exception:
        return d


def rowdict(r):
    try:
        tags = json.loads(r["tags"] or "[]")
    except Exception:
        tags = []
    return {"id": str(r["id"]), "title": r["title"] or "Untitled", "group": r["grp"] or "Unsorted",
            "tags": tags, "width": r["width"] or 0, "height": r["height"] or 0,
            "duration": r["dur"] or 0, "bytes": r["bytes"] or 0,
            "created_at": r["created_at"], "source_url": r["source_url"] or ""}


def one(vid):
    try:
        c = cx()
        r = c.execute("SELECT * FROM videos WHERE id=?", (str(vid),)).fetchone()
        c.close()
        return dict(r) if r else None
    except Exception:
        return None


def vpath(r):
    p = Path(r.get("path") or "")
    if p.is_absolute() and p.is_file():
        return p
    if r.get("filename"):
        c = MEDIA / r["filename"]
        if c.is_file():
            return c
    try:
        for f in MEDIA.glob(f"{r['id']}.*"):
            if f.suffix.lower() in VEXT:
                return f
    except Exception:
        pass
    return None


def ppath(r):
    v = r.get("poster_path") or ""
    if v:
        p = Path(v)
        if p.is_file():
            return p
        if (MEDIA / v).is_file():
            return MEDIA / v
    l = MEDIA / f"{r['id']}.jpg"
    return l if l.is_file() else None


# ---------------------------------------------------------------- media: ETag + Range
def _iter(p, start, ln, ch=1 << 20):
    with p.open("rb") as f:
        f.seek(start)
        left = ln
        while left > 0:
            b = f.read(min(ch, left))
            if not b:
                break
            left -= len(b)
            yield b


def serve(p: Path, rq: Request):
    try:
        stt = p.stat()
    except OSError:
        raise HTTPException(404)
    size = stt.st_size
    tag = f'W/"{stt.st_mtime_ns:x}-{size:x}"'
    ext = p.suffix.lower()
    mt = {".mp4": "video/mp4", ".m4v": "video/mp4", ".webm": "video/webm",
          ".mkv": "video/x-matroska", ".mov": "video/quicktime", ".jpg": "image/jpeg",
          ".jpeg": "image/jpeg", ".png": "image/png", ".webp": "image/webp",
          ".vtt": "text/vtt"}.get(ext) or mimetypes.guess_type(p.name)[0] or "application/octet-stream"
    h = {"Accept-Ranges": "bytes", "ETag": tag, "Cache-Control": "public, max-age=31536000, immutable"}
    if rq.headers.get("if-none-match") == tag:
        return Response(304, headers=h)
    if rq.method == "HEAD":
        return Response(200, media_type=mt, headers={**h, "Content-Length": str(size)})
    m = re.fullmatch(r"bytes=(\d*)-(\d*)", (rq.headers.get("range") or "").strip())
    if m:
        a, b = m.group(1), m.group(2)
        if not a and b:
            ln = min(int(b), size)
            s0, e0 = max(0, size - ln), size - 1
        else:
            s0 = int(a or 0)
            e0 = min(int(b), size - 1) if b else size - 1
        if s0 >= size or s0 > e0:
            return Response(416, headers={"Content-Range": f"bytes */{size}"})
        ln = e0 - s0 + 1
        return StreamingResponse(_iter(p, s0, ln), 206, media_type=mt,
                                 headers={**h, "Content-Range": f"bytes {s0}-{e0}/{size}",
                                          "Content-Length": str(ln)})
    return FileResponse(p, media_type=mt, headers=h)


PLACEHOLDER = (
    '<svg xmlns="http://www.w3.org/2000/svg" width="640" height="360">'
    '<defs><linearGradient id="g" x1="0" y1="0" x2="1" y2="1">'
    '<stop offset="0" stop-color="#151C2F"/><stop offset="1" stop-color="#0A0D15"/></linearGradient></defs>'
    '<rect width="640" height="360" fill="url(#g)"/>'
    '<g opacity=".3"><rect x="272" y="140" width="96" height="80" rx="12" fill="none" stroke="#2CE5C8" stroke-width="6"/>'
    '<path d="M304 162v36l30-18z" fill="#2CE5C8"/></g></svg>')

_TQ: queue.Queue = queue.Queue()
_TSEEN: set = set()
_TLOCK = threading.Lock()


def _thumb_worker():
    while True:
        vid, src, out = _TQ.get()
        try:
            if FFMPEG and not out.is_file():
                for ss in ("2", "0.5", "0"):
                    subprocess.run([FFMPEG, "-y", "-v", "error", "-ss", ss, "-i", str(src),
                                    "-vframes", "1", "-vf", "scale=480:-2", str(out)],
                                   capture_output=True, timeout=90)
                    if out.is_file():
                        break
                if out.is_file():
                    c = cx(False)
                    c.execute("UPDATE videos SET poster_path=? WHERE id=?", (str(out), vid))
                    c.commit()
                    c.close()
        except Exception:
            pass
        finally:
            with _TLOCK:
                _TSEEN.discard(vid)
            _TQ.task_done()


for _ in range(THUMB_WORKERS):
    threading.Thread(target=_thumb_worker, daemon=True).start()
print(f"[LocalReels] thumbnail workers={THUMB_WORKERS}", flush=True)


def enqueue_thumb(vid, src):
    with _TLOCK:
        if vid in _TSEEN:
            return
        _TSEEN.add(vid)
    _TQ.put((vid, src, MEDIA / f"{vid}.jpg"))


@app.api_route("/media/{name:path}", methods=["GET", "HEAD"], include_in_schema=False)
async def media(name: str, rq: Request):
    rel = Path(name)
    if rel.is_absolute() or ".." in rel.parts:
        raise HTTPException(404)
    stem, ext = rel.stem, rel.suffix.lower()
    r = one(stem)
    if r:
        if ext in {".jpg", ".jpeg", ".png", ".webp"}:
            p = ppath(r)
            if p:
                return serve(p, rq)
            src = vpath(r)
            if src:
                enqueue_thumb(stem, src)
            return Response(PLACEHOLDER, media_type="image/svg+xml", headers={"Cache-Control": "no-store"})
        if ext == ".vtt":
            for c in (MEDIA / f"{stem}.vtt", Path(r.get("path") or "").with_suffix(".vtt")):
                if c.is_file():
                    return serve(c, rq)
            raise HTTPException(404)
        p = vpath(r)
        if p:
            return serve(p, rq)
    d = MEDIA / rel
    if d.is_file():
        return serve(d, rq)
    par = MEDIA / rel.parent
    if par.is_dir():
        for f in par.iterdir():
            if f.is_file() and f.stem == stem and ((ext in VEXT and f.suffix.lower() in VEXT) or f.suffix.lower() == ext):
                return serve(f, rq)
    raise HTTPException(404)


@app.post("/api/caption/{vid}")
async def caption(vid: str, file: UploadFile = File(...)):
    raw = (await file.read()).decode("utf-8", errors="ignore")
    await file.close()
    if not raw.strip().upper().startswith("WEBVTT"):
        out = ["WEBVTT", ""]
        for blk in re.split(r"\n\s*\n", raw.strip()):
            ls = [l for l in blk.splitlines() if l.strip()]
            if len(ls) > 1 and "-->" in ls[1]:
                out += [ls[1].replace(",", "."), *ls[2:], ""]
            elif ls and "-->" in ls[0]:
                out += [ls[0].replace(",", "."), *ls[1:], ""]
        raw = "\n".join(out)
    (MEDIA / f"{vid}.vtt").write_text(raw, encoding="utf-8")
    return {"ok": True}


# ---------------------------------------------------------------- SQL search + pagination
SORTS = {"new": "created_at DESC", "old": "created_at ASC", "title": "title COLLATE NOCASE ASC",
         "duration": "dur DESC", "quality": "height DESC", "size": "bytes DESC"}


def query_videos(skip=0, limit=24, search="", group="", aspect="all", sort="new", deleted=0):
    where = ["deleted=?"]
    args = [1 if deleted else 0]
    if group and group != "all":
        where.append("grp=?")
        args.append(group)
    if aspect == "landscape":
        where.append("width>0 AND height>0 AND width*1.0/height>1.06")
    elif aspect == "portrait":
        where.append("width>0 AND height>0 AND width*1.0/height<0.94")
    elif aspect == "square":
        where.append("width>0 AND height>0 AND width*1.0/height BETWEEN 0.94 AND 1.06")
    elif aspect == "unknown":
        where.append("(width=0 OR height=0 OR width IS NULL OR height IS NULL)")
    s = (search or "").strip()
    used_fts = False
    if s:
        if FTS:
            terms = re.findall(r"[\w]+", s, re.UNICODE)
            if terms:
                where.append("rowid IN (SELECT rowid FROM videos_fts WHERE videos_fts MATCH ?)")
                args.append(" ".join(t + "*" for t in terms))
                used_fts = True
        if not used_fts:
            where.append("(LOWER(title) LIKE ? OR LOWER(grp) LIKE ? OR LOWER(tags) LIKE ?)")
            like = "%" + s.lower() + "%"
            args += [like, like, like]
    w = " AND ".join(where)
    order = SORTS.get(sort, SORTS["new"])
    lim = max(1, min(int(limit), 500))
    off = max(0, int(skip))
    c = cx()
    try:
        try:
            total = c.execute(f"SELECT COUNT(*) n FROM videos WHERE {w}", args).fetchone()["n"]
            rows = c.execute(f"SELECT * FROM videos WHERE {w} ORDER BY {order} LIMIT ? OFFSET ?",
                             args + [lim, off]).fetchall()
        except sqlite3.OperationalError:
            if not used_fts:
                raise
            where2 = [x for x in where if "videos_fts" not in x]
            args2 = args[:-1]
            where2.append("(LOWER(title) LIKE ? OR LOWER(grp) LIKE ? OR LOWER(tags) LIKE ?)")
            like = "%" + s.lower() + "%"
            args2 += [like, like, like]
            w2 = " AND ".join(where2)
            total = c.execute(f"SELECT COUNT(*) n FROM videos WHERE {w2}", args2).fetchone()["n"]
            rows = c.execute(f"SELECT * FROM videos WHERE {w2} ORDER BY {order} LIMIT ? OFFSET ?",
                             args2 + [lim, off]).fetchall()
        return [rowdict(r) for r in rows], total
    finally:
        c.close()


@app.get("/api/videos")
def api_videos(skip: int = 0, limit: int = 24, search: str = "", group: str = "",
               aspect: str = "all", sort: str = "new"):
    vids, total = query_videos(skip, limit, search, group, aspect, sort, 0)
    return {"ok": True, "videos": vids, "total": total, "skip": skip, "limit": limit, "fts": FTS}


@app.get("/api/trash")
def api_trash_list(skip: int = 0, limit: int = 60):
    vids, total = query_videos(skip, limit, "", "", "all", "new", 1)
    return {"ok": True, "videos": vids, "total": total}


@app.get("/api/groups")
def api_groups():
    c = cx()
    rs = c.execute("SELECT grp name, COUNT(*) n FROM videos WHERE deleted=0 GROUP BY grp ORDER BY grp").fetchall()
    c.close()
    return {"ok": True, "groups": [dict(r) for r in rs]}


@app.get("/api/tags")
def api_tags():
    c = cx()
    rs = c.execute("SELECT tags FROM videos WHERE deleted=0").fetchall()
    c.close()
    g = {}
    for r in rs:
        try:
            for t in json.loads(r["tags"] or "[]"):
                g[t] = g.get(t, 0) + 1
        except Exception:
            pass
    return {"ok": True, "tags": [{"name": k, "count": v} for k, v in sorted(g.items(), key=lambda x: -x[1])]}


# ---------------------------------------------------------------- state
def state():
    c = cx()
    try:
        eng = {r["video_id"]: {"liked": r["liked"], "saved": r["saved"], "views": r["views"],
                               "watch": r["watch"], "position": r["pos"],
                               "reaction": r["reaction"] or "", "speed": r["speed"] or 0}
               for r in c.execute("SELECT * FROM engagement")}
        cnt = {r["video_id"]: r["n"] for r in c.execute("SELECT video_id,COUNT(*) n FROM comments GROUP BY video_id")}
        rx = {}
        for r in c.execute("SELECT * FROM reactions"):
            rx.setdefault(r["video_id"], {})[r["emoji"]] = r["count"]
        pls = []
        for p in c.execute("SELECT * FROM playlists ORDER BY position,created_at"):
            items = [i["video_id"] for i in c.execute(
                "SELECT video_id FROM playlist_items WHERE playlist_id=? ORDER BY position", (p["id"],))]
            pls.append({"id": p["id"], "name": p["name"], "items": items})
        sv = [{"id": r["id"], "name": r["name"], "search": r["search"], "group": r["grp"],
               "sort": r["sort"], "aspect": r["aspect"]}
              for r in c.execute("SELECT * FROM saved_views ORDER BY created_at DESC")]
        st = {}
        for r in c.execute("SELECT * FROM settings"):
            try:
                st[r["key"]] = json.loads(r["value"])
            except Exception:
                pass
        src = [dict(r) for r in c.execute("SELECT * FROM sources ORDER BY created_at DESC")]
    finally:
        c.close()
    return {"engagement": eng, "comment_counts": cnt, "reactions": rx,
            "playlists": pls, "saved_views": sv, "settings": st, "sources": src}


@app.get("/api/state")
@app.get("/api/reels/state")
def api_state():
    return {"ok": True, **state()}


def scan_state():
    c = cx()
    total = c.execute("SELECT COUNT(*) n FROM videos WHERE deleted=0").fetchone()["n"]
    done = c.execute("SELECT COUNT(*) n FROM videos WHERE deleted=0 AND width>0").fetchone()["n"]
    c.close()
    running = get_meta("scan_running") == "1"
    return {"running": running, "done": done, "total": total,
            "complete": total > 0 and done >= total, "ffmpeg": bool(FFPROBE)}


def probe(p: Path):
    if not FFPROBE:
        return None
    try:
        o = subprocess.run([FFPROBE, "-v", "error", "-select_streams", "v:0",
                            "-show_entries", "stream=width,height:format=duration:stream_side_data=rotation:stream_tags=rotate",
                            "-of", "json", str(p)], capture_output=True, text=True, timeout=25)
        d = json.loads(o.stdout or "{}")
        s = (d.get("streams") or [{}])[0]
        w, h = int(s.get("width") or 0), int(s.get("height") or 0)
        du = float((d.get("format") or {}).get("duration") or 0)
        rot = 0
        for sd in s.get("side_data_list") or []:
            if "rotation" in sd:
                rot = int(sd["rotation"])
        if not rot:
            rot = int((s.get("tags") or {}).get("rotate") or 0)
        if abs(rot) % 180 == 90:
            w, h = h, w
        return (w, h, round(du, 2)) if w and h else None
    except Exception:
        return None


def _scan_worker(ids):
    buf = []

    def work(vid):
        time.sleep(0.01)
        r = one(vid)
        p = vpath(r) if r else None
        return vid, (probe(p) if p else None), (p.stat().st_size if p else 0)

    try:
        with ThreadPoolExecutor(max_workers=THUMB_WORKERS) as pool:
            for vid, res, sz in pool.map(work, ids):
                buf.append(((res[0], res[1], res[2], sz, vid) if res else (0, 0, 0, sz, vid)))
                if len(buf) >= 40:
                    c = cx(False)
                    c.executemany("UPDATE videos SET width=?,height=?,dur=CASE WHEN ?>0 THEN ? ELSE dur END,bytes=? WHERE id=?",
                                  [(w, h, d, d, s, i) for w, h, d, s, i in buf])
                    c.commit()
                    c.close()
                    buf.clear()
    finally:
        if buf:
            try:
                c = cx(False)
                c.executemany("UPDATE videos SET width=?,height=?,dur=CASE WHEN ?>0 THEN ? ELSE dur END,bytes=? WHERE id=?",
                              [(w, h, d, d, s, i) for w, h, d, s, i in buf])
                c.commit()
                c.close()
            except Exception:
                pass
        log_meta("scan_running", "0")
        print("[LocalReels] ratio scan DONE (permanent)", flush=True)


@app.post("/api/aspects/scan")
def api_scan(payload: dict | None = None):
    payload = payload or {}
    st = scan_state()
    if st["running"]:
        return {"ok": True, "already": True, "scan": st}
    if not FFPROBE:
        return {"ok": False, "error": "ffprobe nahi mila — ffmpeg install karo", "scan": st}
    if st["complete"] and not payload.get("force"):
        return {"ok": True, "already_complete": True, "scan": st}
    c = cx()
    rows = c.execute("SELECT id FROM videos WHERE deleted=0" +
                     ("" if payload.get("force") else " AND (width=0 OR width IS NULL)")).fetchall()
    c.close()
    ids = [r["id"] for r in rows]
    if not ids:
        return {"ok": True, "already_complete": True, "scan": scan_state()}
    log_meta("scan_running", "1")
    threading.Thread(target=_scan_worker, args=(ids,), daemon=True).start()
    return {"ok": True, "scan": scan_state()}


@app.get("/api/aspects/summary")
def api_scan_summary():
    return {"ok": True, "scan": scan_state()}


@app.get("/api/aspects")
def api_aspects():
    c = cx()
    rs = c.execute("SELECT id,width,height FROM videos WHERE deleted=0 AND width>0").fetchall()
    c.close()
    return {"ok": True, "aspects": {str(r["id"]): [r["width"], r["height"]] for r in rs},
            "scan": scan_state(), "ffmpeg": bool(FFPROBE)}


@app.post("/api/aspects")
def api_aspects_put(payload: dict):
    vals = []
    for vid, wh in (payload.get("aspects") or {}).items():
        try:
            w, h = int(wh[0]), int(wh[1])
            if w > 0 and h > 0:
                vals.append((w, h, str(vid)))
        except Exception:
            continue
    if vals:
        c = cx(False)
        c.executemany("UPDATE videos SET width=?,height=? WHERE id=?", vals)
        c.commit()
        c.close()
    return {"ok": True, "count": len(vals)}


# ---------------------------------------------------------------- mutations
@app.post("/api/videos/rename")
@app.post("/api/reels/rename")
def api_rename(p: dict):
    vid, t = str(p.get("video_id", "")), str(p.get("title", "")).strip()[:200]
    if not vid or not t:
        raise HTTPException(400, "video_id + title chahiye")
    c = cx(False)
    c.execute("UPDATE videos SET title=? WHERE id=?", (t, vid))
    c.commit()
    c.close()
    return {"ok": True}


@app.post("/api/videos/update-group")
def api_group(p: dict):
    name = str(p.get("new_group", "")).strip()[:60]
    ids = [str(i) for i in (p.get("video_ids") or [])]
    if not name or not ids:
        raise HTTPException(400, "new_group + video_ids chahiye")
    c = cx(False)
    c.executemany("UPDATE videos SET grp=? WHERE id=?", [(name, i) for i in ids])
    c.commit()
    c.close()
    return {"ok": True}


@app.post("/api/videos/update-tags")
def api_tags_add(p: dict):
    tags = [str(t).strip() for t in (p.get("tags") or []) if str(t).strip()]
    ids = [str(i) for i in (p.get("video_ids") or [])]
    if not tags or not ids:
        raise HTTPException(400, "tags + video_ids chahiye")
    c = cx(False)
    for vid in ids:
        r = c.execute("SELECT tags FROM videos WHERE id=?", (vid,)).fetchone()
        if not r:
            continue
        try:
            cur = json.loads(r["tags"] or "[]")
        except Exception:
            cur = []
        c.execute("UPDATE videos SET tags=? WHERE id=?", (json.dumps(list(dict.fromkeys(cur + tags))[:24]), vid))
    c.commit()
    c.close()
    return {"ok": True}


@app.post("/api/videos/bulk-rename")
def api_bulk_rename(p: dict):
    ids = [str(i) for i in (p.get("ids") or [])]
    pat = str(p.get("pattern", "")).strip()
    if not ids or not pat:
        raise HTTPException(400, "ids + pattern chahiye")
    c = cx(False)
    out = []
    for n, vid in enumerate(ids, 1):
        r = c.execute("SELECT * FROM videos WHERE id=?", (vid,)).fetchone()
        if not r:
            continue
        t = (pat.replace("{n}", str(n).zfill(3)).replace("{title}", r["title"] or "")
             .replace("{group}", r["grp"] or "").replace("{date}", (r["created_at"] or "")[:10])
             .replace("{id}", vid))[:200]
        c.execute("UPDATE videos SET title=? WHERE id=?", (t, vid))
        out.append({"id": vid, "title": t})
    c.commit()
    c.close()
    return {"ok": True, "renamed": out}


@app.post("/api/videos/trash")
@app.post("/api/videos/bulk-delete")
def api_trash(p):
    ids = [str(i) for i in (p if isinstance(p, list) else (p.get("ids") or []))]
    if not ids:
        return {"ok": True, "count": 0}
    c = cx(False)
    c.executemany("UPDATE videos SET deleted=1,deleted_at=? WHERE id=?", [(now(), i) for i in ids])
    c.commit()
    c.close()
    return {"ok": True, "status": "Success", "count": len(ids)}


@app.post("/api/videos/restore")
def api_restore(p: dict):
    ids = [str(i) for i in (p.get("ids") or [])]
    c = cx(False)
    c.executemany("UPDATE videos SET deleted=0,deleted_at='' WHERE id=?", [(i,) for i in ids])
    c.commit()
    c.close()
    return {"ok": True, "count": len(ids)}


@app.post("/api/videos/purge")
def api_purge(p: dict):
    ids = [str(i) for i in (p.get("ids") or [])]
    freed = 0
    c = cx(False)
    try:
        for vid in ids:
            r = c.execute("SELECT * FROM videos WHERE id=?", (vid,)).fetchone()
            if r:
                rd = dict(r)
                for q in (vpath(rd), ppath(rd), MEDIA / f"{vid}.vtt"):
                    try:
                        if q and q.is_file():
                            freed += q.stat().st_size
                            q.unlink()
                    except Exception:
                        pass
            for t, col in (("videos", "id"), ("engagement", "video_id"), ("comments", "video_id"),
                           ("playlist_items", "video_id"), ("reactions", "video_id")):
                c.execute(f"DELETE FROM {t} WHERE {col}=?", (vid,))
        c.commit()
    finally:
        c.close()
    return {"ok": True, "count": len(ids), "freed": freed}


# ---------------------------------------------------------------- engagement
def _eng(vid, field, val):
    c = cx(False)
    c.execute(f"INSERT INTO engagement(video_id,{field},updated_at) VALUES(?,?,?) "
              f"ON CONFLICT(video_id) DO UPDATE SET {field}=excluded.{field},updated_at=excluded.updated_at",
              (vid, val, now()))
    c.commit()
    c.close()


@app.post("/api/like")
@app.post("/api/reels/like")
def api_like(p: dict):
    vid = str(p.get("video_id", ""))
    if not vid:
        raise HTTPException(400, "video_id")
    v = 1 if p.get("value") else 0
    _eng(vid, "liked", v)
    return {"ok": True, "liked": v}


@app.post("/api/save")
@app.post("/api/reels/save")
def api_save(p: dict):
    vid = str(p.get("video_id", ""))
    if not vid:
        raise HTTPException(400, "video_id")
    v = 1 if p.get("value") else 0
    _eng(vid, "saved", v)
    return {"ok": True, "saved": v}


@app.post("/api/speed")
def api_speed(p: dict):
    vid = str(p.get("video_id", ""))
    if vid:
        _eng(vid, "speed", float(p.get("value") or 0))
    return {"ok": True}


@app.post("/api/view")
@app.post("/api/reels/view")
def api_view(p: dict):
    vid = str(p.get("video_id", ""))
    if not vid:
        raise HTTPException(400, "video_id")
    pos = float(p.get("position") or 0)
    du = float(p.get("duration") or 0)
    delta = max(0.0, min(float(p.get("delta") or 0), 30.0))
    first = 1 if p.get("first") else 0
    c = cx(False)
    c.execute("INSERT INTO engagement(video_id,views,watch,pos,updated_at) VALUES(?,?,?,?,?) "
              "ON CONFLICT(video_id) DO UPDATE SET views=engagement.views+excluded.views,"
              "watch=engagement.watch+excluded.watch,pos=excluded.pos,updated_at=excluded.updated_at",
              (vid, first, delta, pos, now()))
    if du > 0:
        c.execute("UPDATE videos SET dur=? WHERE id=? AND (dur IS NULL OR dur=0)", (du, vid))
    c.commit()
    c.close()
    return {"ok": True}


@app.post("/api/react")
@app.post("/api/reels/react")
def api_react(p: dict):
    vid, em = str(p.get("video_id", "")), str(p.get("emoji", ""))[:8]
    if not vid or not em:
        raise HTTPException(400, "video_id + emoji")
    c = cx(False)
    c.execute("INSERT INTO reactions(video_id,emoji,count) VALUES(?,?,1) "
              "ON CONFLICT(video_id,emoji) DO UPDATE SET count=count+1", (vid, em))
    c.execute("INSERT INTO engagement(video_id,reaction,updated_at) VALUES(?,?,?) "
              "ON CONFLICT(video_id) DO UPDATE SET reaction=excluded.reaction", (vid, em, now()))
    rs = {r["emoji"]: r["count"] for r in c.execute("SELECT emoji,count FROM reactions WHERE video_id=?", (vid,))}
    c.commit()
    c.close()
    return {"ok": True, "reactions": rs}


@app.get("/api/comments")
@app.get("/api/reels/comments")
def api_cm(video_id: str):
    c = cx()
    rs = [dict(r) for r in c.execute("SELECT * FROM comments WHERE video_id=? ORDER BY created_at DESC LIMIT 300", (video_id,))]
    c.close()
    return {"ok": True, "comments": rs}


@app.post("/api/comments")
@app.post("/api/reels/comments")
def api_cm_add(p: dict):
    vid, body = str(p.get("video_id", "")), str(p.get("body", "")).strip()[:400]
    if not vid or not body:
        raise HTTPException(400, "video_id + body")
    cid = uuid.uuid4().hex[:10]
    c = cx(False)
    c.execute("INSERT INTO comments(id,video_id,author,body,likes,liked,created_at) VALUES(?,?,?,?,0,0,?)",
              (cid, vid, str(p.get("author") or "You")[:40], body, now()))
    r = dict(c.execute("SELECT * FROM comments WHERE id=?", (cid,)).fetchone())
    c.commit()
    c.close()
    return {"ok": True, "comment": r}


@app.post("/api/comments/like")
@app.post("/api/reels/comments/like")
def api_cm_like(p: dict):
    cid, val = str(p.get("comment_id", "")), bool(p.get("value"))
    c = cx(False)
    r = c.execute("SELECT * FROM comments WHERE id=?", (cid,)).fetchone()
    if not r:
        c.close()
        raise HTTPException(404)
    n = max(0, r["likes"] + (1 if val else -1))
    c.execute("UPDATE comments SET likes=?,liked=? WHERE id=?", (n, 1 if val else 0, cid))
    c.commit()
    c.close()
    return {"ok": True, "likes": n}


@app.delete("/api/comments/{cid}")
@app.delete("/api/reels/comments/{cid}")
def api_cm_del(cid: str):
    c = cx(False)
    c.execute("DELETE FROM comments WHERE id=?", (cid,))
    c.commit()
    c.close()
    return {"ok": True}


# ---------------------------------------------------------------- playlists / views / settings
@app.post("/api/playlists")
@app.post("/api/reels/playlists")
def api_pl_new(p: dict):
    name = str(p.get("name", "")).strip()[:60]
    if not name:
        raise HTTPException(400, "name")
    pid = uuid.uuid4().hex[:10]
    c = cx(False)
    pos = c.execute("SELECT COALESCE(MAX(position),0)+1 p FROM playlists").fetchone()["p"]
    c.execute("INSERT INTO playlists(id,name,position,created_at) VALUES(?,?,?,?)", (pid, name, pos, now()))
    c.commit()
    c.close()
    return {"ok": True, "playlist": {"id": pid, "name": name, "items": []}}


@app.post("/api/playlists/rename")
@app.post("/api/reels/playlists/rename")
def api_pl_ren(p: dict):
    c = cx(False)
    c.execute("UPDATE playlists SET name=? WHERE id=?", (str(p.get("name", ""))[:60], str(p.get("id", ""))))
    c.commit()
    c.close()
    return {"ok": True}


@app.delete("/api/playlists/{pid}")
@app.delete("/api/reels/playlists/{pid}")
def api_pl_del(pid: str):
    c = cx(False)
    c.execute("DELETE FROM playlist_items WHERE playlist_id=?", (pid,))
    c.execute("DELETE FROM playlists WHERE id=?", (pid,))
    c.commit()
    c.close()
    return {"ok": True}


@app.post("/api/playlists/items")
@app.post("/api/reels/playlists/items")
def api_pl_add(p: dict):
    pid = str(p.get("playlist_id", ""))
    c = cx(False)
    pos = c.execute("SELECT COALESCE(MAX(position),0) p FROM playlist_items WHERE playlist_id=?", (pid,)).fetchone()["p"]
    for vid in [str(v) for v in (p.get("video_ids") or [])]:
        pos += 1
        c.execute("INSERT OR IGNORE INTO playlist_items(playlist_id,video_id,position) VALUES(?,?,?)", (pid, vid, pos))
    c.commit()
    c.close()
    return {"ok": True}


@app.post("/api/playlists/remove")
@app.post("/api/reels/playlists/remove")
def api_pl_rm(p: dict):
    c = cx(False)
    c.execute("DELETE FROM playlist_items WHERE playlist_id=? AND video_id=?",
              (str(p.get("playlist_id", "")), str(p.get("video_id", ""))))
    c.commit()
    c.close()
    return {"ok": True}


@app.post("/api/playlists/reorder")
@app.post("/api/reels/playlists/reorder")
def api_pl_reorder(p: dict):
    pid = str(p.get("playlist_id", ""))
    c = cx(False)
    for i, vid in enumerate([str(v) for v in (p.get("video_ids") or [])]):
        c.execute("UPDATE playlist_items SET position=? WHERE playlist_id=? AND video_id=?", (i, pid, vid))
    c.commit()
    c.close()
    return {"ok": True}


@app.post("/api/views")
@app.post("/api/reels/views")
def api_view_save(p: dict):
    name = str(p.get("name", "")).strip()[:50]
    if not name:
        raise HTTPException(400, "name")
    vid = uuid.uuid4().hex[:10]
    c = cx(False)
    c.execute("INSERT INTO saved_views(id,name,search,grp,sort,aspect,created_at) VALUES(?,?,?,?,?,?,?)",
              (vid, name, str(p.get("search", "")), str(p.get("group", "all")),
               str(p.get("sort", "new")), str(p.get("aspect", "all")), now()))
    c.commit()
    c.close()
    return {"ok": True, "view": {"id": vid, "name": name, "search": p.get("search", ""),
                                 "group": p.get("group", "all"), "sort": p.get("sort", "new"),
                                 "aspect": p.get("aspect", "all")}}


@app.delete("/api/views/{vid}")
@app.delete("/api/reels/views/{vid}")
def api_view_del(vid: str):
    c = cx(False)
    c.execute("DELETE FROM saved_views WHERE id=?", (vid,))
    c.commit()
    c.close()
    return {"ok": True}


@app.post("/api/settings")
@app.post("/api/reels/settings")
def api_set(p: dict):
    c = cx(False)
    c.execute("INSERT OR REPLACE INTO settings(key,value) VALUES(?,?)",
              (str(p.get("key", ""))[:40], json.dumps(p.get("value"))))
    c.commit()
    c.close()
    return {"ok": True}


@app.post("/api/history/clear")
@app.post("/api/reels/history/clear")
def api_hist():
    c = cx(False)
    c.execute("UPDATE engagement SET pos=0,watch=0,views=0")
    c.commit()
    c.close()
    return {"ok": True}


# ---------------------------------------------------------------- stats / storage / backup
@app.get("/api/stats")
@app.get("/api/reels/stats")
def api_stats():
    c = cx()
    v = c.execute("SELECT COUNT(*) n, COALESCE(SUM(bytes),0) b, COALESCE(SUM(dur),0) d FROM videos WHERE deleted=0").fetchone()
    land = c.execute("SELECT COUNT(*) n FROM videos WHERE deleted=0 AND width>0 AND height>0 AND width*1.0/height>1.06").fetchone()["n"]
    port = c.execute("SELECT COUNT(*) n FROM videos WHERE deleted=0 AND width>0 AND height>0 AND width*1.0/height<0.94").fetchone()["n"]
    sq = c.execute("SELECT COUNT(*) n FROM videos WHERE deleted=0 AND width>0 AND height>0 AND width*1.0/height BETWEEN 0.94 AND 1.06").fetchone()["n"]
    unk = c.execute("SELECT COUNT(*) n FROM videos WHERE deleted=0 AND (width=0 OR height=0 OR width IS NULL)").fetchone()["n"]
    a = c.execute("SELECT COALESCE(SUM(views),0) v,COALESCE(SUM(watch),0) w,COALESCE(SUM(liked),0) l,COALESCE(SUM(saved),0) s FROM engagement").fetchone()
    cm = c.execute("SELECT COUNT(*) n FROM comments").fetchone()["n"]
    pl = c.execute("SELECT COUNT(*) n FROM playlists").fetchone()["n"]
    tr = c.execute("SELECT COUNT(*) n, COALESCE(SUM(bytes),0) b FROM videos WHERE deleted=1").fetchone()
    grp = [dict(r) for r in c.execute("SELECT grp name, COUNT(*) n, COALESCE(SUM(dur),0) d FROM videos WHERE deleted=0 GROUP BY grp ORDER BY n DESC LIMIT 10")]
    top = [dict(r) for r in c.execute(
        "SELECT v.id,v.title,e.watch,e.views FROM engagement e JOIN videos v ON v.id=e.video_id "
        "WHERE v.deleted=0 AND e.watch>0 ORDER BY e.watch DESC LIMIT 8")]
    c.close()
    return {"ok": True, "videos": v["n"], "disk_bytes": v["b"], "total_duration": v["d"],
            "landscape": land, "portrait": port, "square": sq, "unknown": unk,
            "views": a["v"], "watch_seconds": a["w"], "liked": a["l"], "saved": a["s"],
            "comments": cm, "playlists": pl, "trash_count": tr["n"], "trash_bytes": tr["b"],
            "groups": grp, "top": top, "media_dir": str(MEDIA), "ffmpeg": bool(FFMPEG), "fts": FTS}


@app.get("/api/storage")
def api_storage():
    c = cx()
    rs = [dict(r) for r in c.execute("SELECT id,title,grp,bytes,dur,path,filename FROM videos WHERE deleted=0 ORDER BY bytes DESC LIMIT 60")]
    c.close()
    items, buckets = [], {}
    for r in rs:
        p = vpath(r)
        sz = r["bytes"] or (p.stat().st_size if p else 0)
        it = {"id": r["id"], "title": r["title"], "group": r["grp"], "bytes": sz,
              "duration": r["dur"] or 0, "missing": p is None}
        items.append(it)
        if sz > 0:
            buckets.setdefault(sz, []).append(it)
    return {"ok": True, "largest": items, "duplicates": [v for v in buckets.values() if len(v) > 1][:20],
            "missing": [i for i in items if i["missing"]], "total_bytes": sum(i["bytes"] for i in items)}


@app.get("/api/export")
@app.get("/api/reels/export")
def api_export():
    s = state()
    c = cx()
    s["videos"] = [rowdict(r) for r in c.execute("SELECT * FROM videos")]
    s["comments"] = [dict(r) for r in c.execute("SELECT * FROM comments")]
    c.close()
    s["version"] = "9.0.1"
    s["exported_at"] = now()
    return JSONResponse(s, headers={"Content-Disposition": 'attachment; filename="localreels-backup.json"'})


@app.post("/api/restore")
def api_restore_backup(p: dict):
    if not isinstance(p, dict):
        raise HTTPException(400, "JSON backup chahiye")
    c = cx(False)
    n = {"videos": 0, "eng": 0, "cm": 0, "pl": 0}
    try:
        for v in (p.get("videos") or []):
            vid = str(v.get("id") or "")
            if not vid:
                continue
            ex = c.execute("SELECT path,poster_path,bytes,width,height FROM videos WHERE id=?", (vid,)).fetchone()
            c.execute("INSERT INTO videos(id,title,grp,tags,width,height,dur,filename,created_at,source_url,path,poster_path,bytes,deleted)"
                      " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,0) "
                      "ON CONFLICT(id) DO UPDATE SET title=excluded.title,grp=excluded.grp,tags=excluded.tags,"
                      "width=CASE WHEN videos.width>0 THEN videos.width ELSE excluded.width END,"
                      "height=CASE WHEN videos.height>0 THEN videos.height ELSE excluded.height END,"
                      "dur=CASE WHEN videos.dur>0 THEN videos.dur ELSE excluded.dur END,deleted=0",
                      (vid, v.get("title") or "Untitled", v.get("group") or "Imported",
                       json.dumps(v.get("tags") or []), v.get("width") or 0, v.get("height") or 0,
                       v.get("duration") or 0, "", v.get("created_at") or now(), v.get("source_url") or "",
                       ex["path"] if ex else "", ex["poster_path"] if ex else "", ex["bytes"] if ex else 0))
            n["videos"] += 1
        eng = p.get("engagement") or p.get("eng") or {}
        for vid, e in eng.items():
            c.execute("INSERT OR REPLACE INTO engagement(video_id,liked,saved,views,watch,pos,reaction,speed,updated_at)"
                      " VALUES(?,?,?,?,?,?,?,?,?)",
                      (str(vid), int(e.get("liked") or 0), int(e.get("saved") or 0), int(e.get("views") or 0),
                       float(e.get("watch") or 0), float(e.get("position") or 0),
                       e.get("reaction") or "", float(e.get("speed") or 0), now()))
            n["eng"] += 1
        for cm in (p.get("comments") or []):
            c.execute("INSERT OR REPLACE INTO comments(id,video_id,author,body,likes,liked,created_at) VALUES(?,?,?,?,?,?,?)",
                      (str(cm.get("id") or uuid.uuid4().hex[:10]), str(cm.get("video_id") or ""),
                       cm.get("author") or "You", cm.get("body") or "", int(cm.get("likes") or 0),
                       int(cm.get("liked") or 0), cm.get("created_at") or now()))
            n["cm"] += 1
        for pl in (p.get("playlists") or []):
            pid = str(pl.get("id") or uuid.uuid4().hex[:10])
            c.execute("INSERT OR REPLACE INTO playlists(id,name,position,created_at) VALUES(?,?,?,?)",
                      (pid, pl.get("name") or "Playlist", 0, now()))
            for i, vid in enumerate(pl.get("items") or []):
                c.execute("INSERT OR REPLACE INTO playlist_items(playlist_id,video_id,position) VALUES(?,?,?)", (pid, str(vid), i))
            n["pl"] += 1
        for k, v in (p.get("settings") or {}).items():
            c.execute("INSERT OR REPLACE INTO settings(key,value) VALUES(?,?)", (str(k)[:40], json.dumps(v)))
        c.commit()
    finally:
        c.close()
    return {"ok": True, "restored": n}


# ---------------------------------------------------------------- unified sources + jobs
@app.get("/api/sources")
def api_sources():
    c = cx()
    rs = [dict(r) for r in c.execute("SELECT * FROM sources ORDER BY created_at DESC")]
    c.close()
    return {"ok": True, "sources": rs}


@app.post("/api/sources")
def api_source_add(p: dict):
    kind = str(p.get("kind") or "folder")
    loc = str(p.get("location") or "").strip()
    if not loc:
        raise HTTPException(400, "location chahiye")
    if kind == "folder" and not Path(loc).is_dir():
        raise HTTPException(400, "Folder nahi mila: " + loc)
    sid = uuid.uuid4().hex[:10]
    c = cx(False)
    c.execute("INSERT INTO sources(id,kind,name,location,created_at) VALUES(?,?,?,?,?)",
              (sid, kind, str(p.get("name") or Path(loc).name or loc)[:60], loc, now()))
    c.commit()
    c.close()
    if kind == "folder":
        threading.Thread(target=scan_folder, args=(sid, loc), daemon=True).start()
    return {"ok": True, "id": sid}


@app.delete("/api/sources/{sid}")
def api_source_del(sid: str):
    c = cx(False)
    c.execute("DELETE FROM sources WHERE id=?", (sid,))
    c.commit()
    c.close()
    return {"ok": True}


def scan_folder(sid, loc):
    try:
        base = Path(loc)
        c = cx(False)
        known = {(r["path"] or "").lower() for r in c.execute("SELECT path FROM videos")}
        names = {(r["filename"] or "").lower() for r in c.execute("SELECT filename FROM videos")}
        ids = {r["id"] for r in c.execute("SELECT id FROM videos")}
        new = []
        for f in sorted(base.rglob("*")):
            if not f.is_file() or f.suffix.lower() not in VEXT:
                continue
            rp = str(f.resolve())
            if rp.lower() in known or f.name.lower() in names:
                continue
            vid = f.stem if f.stem not in ids else uuid.uuid4().hex[:10]
            ids.add(vid)
            new.append((vid, pretty(f.stem)[:200], base.name or "Library", "[]", 0, 0, 0.0,
                        f.name, now(), "", rp, "", f.stat().st_size, sid))
        if new:
            c.executemany("INSERT OR REPLACE INTO videos(id,title,grp,tags,width,height,dur,filename,"
                          "created_at,source_url,path,poster_path,bytes,source_id,deleted) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,0)", new)
        c.execute("UPDATE sources SET last_scan=? WHERE id=?", (now(), sid))
        c.commit()
        c.close()
        print(f"[LocalReels] folder scan {loc}: {len(new)} nayi", flush=True)
        if new:
            threading.Thread(target=_scan_worker, args=([r[0] for r in new],), daemon=True).start()
    except Exception as e:
        print("[LocalReels] scan folder:", e, flush=True)


@app.post("/api/sources/scan")
def api_sources_scan(p: dict | None = None):
    p = p or {}
    c = cx()
    rs = [dict(r) for r in c.execute("SELECT * FROM sources WHERE kind='folder'")]
    c.close()
    if p.get("id"):
        rs = [r for r in rs if r["id"] == p["id"]]
    for r in rs:
        threading.Thread(target=scan_folder, args=(r["id"], r["location"]), daemon=True).start()
    return {"ok": True, "queued": len(rs)}


_JOBS: dict = {}
_JLOCK = threading.Lock()


def _job_set(jid, **kw):
    if not kw:
        return
    c = cx(False)
    c.execute(f"UPDATE jobs SET {','.join(k+'=?' for k in kw)} WHERE id=?", (*kw.values(), jid))
    c.commit()
    c.close()


def _add_video(vid, title, path: Path, group, tags, url=""):
    w, h, du = probe(path) or (0, 0, 0.0)
    c = cx(False)
    c.execute("INSERT OR REPLACE INTO videos(id,title,grp,tags,width,height,dur,filename,created_at,"
              "source_url,path,poster_path,bytes,deleted) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,0)",
              (vid, str(title)[:200], group or "Downloads", json.dumps(tags or []), w, h, du,
               path.name, now(), url, str(path.resolve()), "", path.stat().st_size))
    r = dict(c.execute("SELECT * FROM videos WHERE id=?", (vid,)).fetchone())
    c.commit()
    c.close()
    enqueue_thumb(vid, path)
    return rowdict(r)


def _direct_download(jid, url, group, tags):
    vid = uuid.uuid4().hex[:10]
    try:
        with httpx.stream("GET", url, follow_redirects=True, timeout=60) as r:
            r.raise_for_status()
            ext = Path(url.split("?")[0]).suffix.lower()
            if ext not in VEXT:
                ct = r.headers.get("content-type", "")
                ext = ".webm" if "webm" in ct else ".mp4"
            dest = MEDIA / f"{vid}{ext}"
            total = int(r.headers.get("content-length") or 0)
            got, last = 0, -1
            with dest.open("wb") as f:
                for chunk in r.iter_bytes(1 << 20):
                    f.write(chunk)
                    got += len(chunk)
                    if total:
                        pct = int(got / total * 100)
                        if pct != last:
                            last = pct
                            _job_set(jid, progress=pct)
        title = pretty(Path(url.split("?")[0]).stem) or "Direct download"
        v = _add_video(vid, title, dest, group, tags, url)
        _job_set(jid, status="done", progress=100, video_id=vid)
        return v
    except Exception as e:
        _job_set(jid, status="error", error=str(e)[:200])
        return None


def _ytdlp(jid, url, group, tags, quality):
    vid = uuid.uuid4().hex[:10]
    try:
        import yt_dlp  # noqa
    except Exception:
        _job_set(jid, status="error", error="yt-dlp install nahi: pip install yt-dlp")
        return None
    fmt = {"best": "bv*+ba/b", "1080": "bv*[height<=1080]+ba/b[height<=1080]",
           "720": "bv*[height<=720]+ba/b[height<=720]", "480": "bv*[height<=480]+ba/b[height<=480]",
           "audio": "ba/b"}.get(quality, "bv*+ba/b")
    args = ["--no-playlist", "--restrict-filenames", "--newline", "--no-colors", "--write-info-json",
            "-f", fmt, "--merge-output-format", "mp4", "-o", str(MEDIA / f"{vid}.%(ext)s")]
    if quality != "audio":
        args += ["--write-subs", "--sub-langs", "en.*,hi.*", "--convert-subs", "srt"]
    if FFMPEG:
        args += ["--ffmpeg-location", str(Path(FFMPEG).parent)]
    _job_set(jid, status="downloading", progress=0)
    try:
        pr = subprocess.Popen([sys.executable, "-m", "yt_dlp"] + args + [url],
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                              text=True, encoding="utf-8", errors="ignore")
    except Exception as e:
        _job_set(jid, status="error", error=str(e)[:200])
        return None
    with _JLOCK:
        _JOBS[jid] = pr
    last = -1
    for line in pr.stdout or []:
        m = re.search(r"(\d+(?:\.\d+)?)%", line)
        if m:
            pct = int(float(m.group(1)))
            if pct != last:
                last = pct
                _job_set(jid, progress=pct)
    pr.wait()
    with _JLOCK:
        _JOBS.pop(jid, None)
    c = cx()
    st = c.execute("SELECT status FROM jobs WHERE id=?", (jid,)).fetchone()
    c.close()
    if st and st["status"] == "cancelled":
        for f in MEDIA.glob(f"{vid}.*"):
            try:
                f.unlink()
            except Exception:
                pass
        return None
    if pr.returncode != 0:
        for f in MEDIA.glob(f"{vid}.*"):
            try:
                f.unlink()
            except Exception:
                pass
        _job_set(jid, status="error", error="yt-dlp fail — URL ya network check karo")
        return None
    f = next((x for x in MEDIA.glob(f"{vid}.*") if x.suffix.lower() in VEXT), None)
    if not f:
        _job_set(jid, status="error", error="file nahi mili")
        return None
    title = pretty(vid)
    info = MEDIA / f"{vid}.info.json"
    if info.is_file():
        try:
            title = str(json.loads(info.read_text(encoding="utf-8", errors="ignore")).get("title") or title)[:200]
        except Exception:
            pass
        try:
            info.unlink()
        except Exception:
            pass
    for srt in MEDIA.glob(f"{vid}*.srt"):
        try:
            txt = srt.read_text(encoding="utf-8", errors="ignore")
            out = ["WEBVTT", ""]
            for blk in re.split(r"\n\s*\n", txt.strip()):
                ls = [l for l in blk.splitlines() if l.strip()]
                if len(ls) > 1 and "-->" in ls[1]:
                    out += [ls[1].replace(",", "."), *ls[2:], ""]
            (MEDIA / f"{vid}.vtt").write_text("\n".join(out), encoding="utf-8")
            srt.unlink()
            break
        except Exception:
            pass
    v = _add_video(vid, title, f, group, tags, url)
    _job_set(jid, status="done", progress=100, video_id=vid)
    return v


def _run_job(jid, inp, group, tags, quality):
    p = Path(inp)
    if p.exists():
        if p.is_dir():
            sid = uuid.uuid4().hex[:10]
            c = cx(False)
            c.execute("INSERT INTO sources(id,kind,name,location,created_at) VALUES(?,?,?,?,?)",
                      (sid, "folder", p.name, str(p), now()))
            c.commit()
            c.close()
            scan_folder(sid, str(p))
            _job_set(jid, status="done", progress=100)
            return
        if p.suffix.lower() in VEXT:
            vid = p.stem if not one(p.stem) else uuid.uuid4().hex[:10]
            v = _add_video(vid, pretty(p.stem), p, group or p.parent.name, tags)
            _job_set(jid, status="done", progress=100, video_id=v["id"])
            return
    if not re.match(r"^https?://", inp, re.I):
        _job_set(jid, status="error", error="Na local path, na valid URL")
        return
    clean = inp.split("?")[0].lower()
    direct = any(clean.endswith(e) for e in VEXT)
    if not direct:
        try:
            h = httpx.head(inp, follow_redirects=True, timeout=20)
            direct = "video" in (h.headers.get("content-type") or "")
        except Exception:
            direct = False
    if direct:
        _direct_download(jid, inp, group, tags)
    else:
        _ytdlp(jid, inp, group, tags, quality)


@app.post("/api/ingest")
def api_ingest(p: dict, rq: Request):
    inp = str(p.get("input") or p.get("url") or "").strip()
    if not inp:
        raise HTTPException(400, "input chahiye (folder path ya URL)")
    sent = rq.headers.get("X-LocalReels-Auth")
    if sent and sent != AUTH:
        raise HTTPException(403, "auth galat")
    jid = uuid.uuid4().hex[:10]
    c = cx(False)
    c.execute("INSERT INTO jobs(id,kind,input,status,progress,created_at,quality) VALUES(?,?,?,?,0,?,?)",
              (jid, "ingest", inp, "queued", now(), str(p.get("quality") or "best")))
    c.commit()
    c.close()
    threading.Thread(target=_run_job, args=(jid, inp, str(p.get("group") or "Downloads"),
                                            [str(t) for t in (p.get("tags") or [])],
                                            str(p.get("quality") or "best")), daemon=True).start()
    return {"ok": True, "job_id": jid, "id": jid}


@app.post("/api/download")
def api_download(p: dict, rq: Request):
    p = dict(p or {})
    p["input"] = p.get("url") or p.get("input")
    return api_ingest(p, rq)


@app.post("/api/import/urls")
def api_import_urls(p: dict, rq: Request):
    urls = [u.strip() for u in (p.get("urls") or []) if u.strip()]
    ids = []
    for u in urls[:80]:
        r = api_ingest({"input": u, "quality": p.get("quality") or "best",
                        "group": p.get("group") or "Downloads"}, rq)
        ids.append(r["job_id"])
    return {"ok": True, "queued": len(ids), "ids": ids}


@app.post("/api/jobs/cancel")
def api_job_cancel(p: dict):
    jid = str(p.get("id") or "")
    with _JLOCK:
        pr = _JOBS.get(jid)
    if pr:
        try:
            pr.terminate()
        except Exception:
            pass
    _job_set(jid, status="cancelled", error="User ne cancel kiya")
    return {"ok": True}


@app.post("/api/jobs/retry")
def api_job_retry(p: dict, rq: Request):
    jid = str(p.get("id") or "")
    c = cx()
    r = c.execute("SELECT * FROM jobs WHERE id=?", (jid,)).fetchone()
    c.close()
    if not r:
        raise HTTPException(404)
    return api_ingest({"input": r["input"], "quality": r["quality"]}, rq)


@app.get("/api/jobs")
def api_jobs():
    c = cx()
    rs = [dict(r) for r in c.execute("SELECT * FROM jobs ORDER BY created_at DESC LIMIT 40")]
    c.close()
    return {"ok": True, "jobs": rs, "downloads": rs}


@app.get("/api/downloads")
def api_downloads():
    return api_jobs()


@app.post("/api/upload")
@app.post("/api/reels/upload")
async def api_upload(file: UploadFile = File(...), title: str = Form(""),
                     group: str = Form("Uploads"), tags: str = Form("")):
    ext = Path(file.filename or "v.mp4").suffix.lower()
    if ext not in VEXT:
        raise HTTPException(400, f"{ext} support nahi")
    vid = "up_" + uuid.uuid4().hex[:8]
    dest = MEDIA / f"{vid}{ext}"
    size = 0
    with dest.open("wb") as out:
        while True:
            ch = await file.read(1 << 20)
            if not ch:
                break
            size += len(ch)
            if size > 4 * 1024 ** 3:
                out.close()
                dest.unlink(missing_ok=True)
                raise HTTPException(413, "4GB se badi file")
            out.write(ch)
    await file.close()
    v = _add_video(vid, title or Path(file.filename or vid).stem, dest, group,
                   [t.strip() for t in tags.split(",") if t.strip()])
    return {"ok": True, "video": v}


# ---------------------------------------------------------------- self-hosted fonts
FONT_CSS_URL = ("https://fonts.googleapis.com/css2?"
                "family=Sora:wght@600;700;800&family=Inter:wght@400;500;600;700&display=swap")
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
FALLBACK_CSS = ("/* Local fallback */\n"
                ":root{--font-display:ui-sans-serif,system-ui,'Segoe UI',Roboto,Arial,sans-serif;"
                "--font-body:ui-sans-serif,system-ui,'Segoe UI',Roboto,Arial,sans-serif}\n")


def fetch_fonts():
    css_path = FONTS / "fonts.css"
    if css_path.is_file() and any(FONTS.glob("*.woff2")):
        return
    try:
        with httpx.Client(headers={"User-Agent": UA}, timeout=30, follow_redirects=True) as cl:
            css = cl.get(FONT_CSS_URL).text
            urls = sorted(set(re.findall(r"url\((https://[^)]+\.woff2)\)", css)))
            if not urls:
                raise RuntimeError("woff2 nahi mila")
            for i, u in enumerate(urls):
                name = f"f{i}.woff2"
                fp = FONTS / name
                if not fp.is_file():
                    fp.write_bytes(cl.get(u).content)
                css = css.replace(u, f"/fonts/{name}")
            css = (":root{--font-display:'Sora',ui-sans-serif,system-ui,sans-serif;"
                   "--font-body:'Inter',ui-sans-serif,system-ui,sans-serif}\n") + css
            css_path.write_text(css, encoding="utf-8")
        print(f"[LocalReels] fonts self-hosted ({len(urls)} files)", flush=True)
    except Exception as e:
        if not css_path.is_file():
            css_path.write_text(FALLBACK_CSS, encoding="utf-8")
        print("[LocalReels] fonts offline — system fallback:", e, flush=True)


@app.get("/fonts/{name:path}", include_in_schema=False)
def fonts(name: str):
    rel = Path(name)
    if rel.is_absolute() or ".." in rel.parts:
        raise HTTPException(404)
    p = FONTS / rel
    if not p.is_file():
        if name == "fonts.css":
            return Response(FALLBACK_CSS, media_type="text/css")
        raise HTTPException(404)
    mt = "text/css" if p.suffix == ".css" else "font/woff2"
    return FileResponse(p, media_type=mt, headers={"Cache-Control": "public, max-age=31536000, immutable"})


# ---------------------------------------------------------------- lan / bootstrap / health
def _lan():
    ips = {"127.0.0.1"}
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ips.add(s.getsockname()[0])
        s.close()
    except Exception:
        pass
    try:
        for ip in socket.gethostbyname_ex(socket.gethostname())[2]:
            if not ip.startswith("127."):
                ips.add(ip)
    except Exception:
        pass
    return {"ok": True, "port": PORT,
            "urls": [f"http://{i}:{PORT}" for i in sorted(ips, key=lambda x: x.startswith("127."))]}


@app.get("/api/lan")
def api_lan():
    return _lan()


@app.get("/api/bootstrap")
def api_bootstrap(limit: int = 40):
    vids, total = query_videos(0, limit, "", "", "all", "new", 0)
    return {"ok": True, "videos": vids, "total": total, "scan": scan_state(),
            "state": state(), "reels": state(), "lan": _lan(),
            "ffmpeg": bool(FFPROBE), "fts": FTS, "version": "9.0.1"}


@app.get("/api/health")
def api_health():
    c = cx()
    n = c.execute("SELECT COUNT(*) n FROM videos WHERE deleted=0").fetchone()["n"]
    c.close()
    return {"ok": True, "version": "9.0.1", "videos": n, "media_dir": str(MEDIA), "db": str(DB),
            "ffmpeg": bool(FFMPEG), "ffprobe": bool(FFPROBE), "fts": FTS,
            "thumb_workers": THUMB_WORKERS, "scan": scan_state()}


@app.post("/api/reindex")
def api_reindex():
    return {"ok": True, "total": len(query_videos(0, 1)[0]) if False else api_health()["videos"]}


# ---------------------------------------------------------------- PWA + UI
ICON = ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512" width="512" height="512">'
        '<defs><linearGradient id="g" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="#2CE5C8"/>'
        '<stop offset=".5" stop-color="#4D9FFF"/><stop offset="1" stop-color="#FF3D7F"/></linearGradient></defs>'
        '<rect width="512" height="512" rx="112" fill="#080B12"/>'
        '<rect x="112" y="88" width="288" height="336" rx="56" fill="none" stroke="url(#g)" stroke-width="18"/>'
        '<path d="M212 176v160l140-80z" fill="url(#g)"/><circle cx="368" cy="352" r="26" fill="#FF3D7F"/>'
        '<circle cx="144" cy="160" r="16" fill="#2CE5C8"/></svg>')


@app.get("/icon.svg", include_in_schema=False)
def icon():
    return Response(ICON, media_type="image/svg+xml", headers={"Cache-Control": "public, max-age=86400"})


@app.get("/manifest.webmanifest", include_in_schema=False)
def manifest():
    return JSONResponse({"name": "LocalReels — Cinema & Reels", "short_name": "LocalReels",
                         "start_url": "/", "scope": "/", "display": "standalone",
                         "background_color": "#080B12", "theme_color": "#080B12",
                         "icons": [{"src": "/icon.svg", "sizes": "192x192", "type": "image/svg+xml", "purpose": "any"},
                                   {"src": "/icon.svg", "sizes": "512x512", "type": "image/svg+xml", "purpose": "any"},
                                   {"src": "/icon.svg", "sizes": "any", "type": "image/svg+xml", "purpose": "maskable"}]},
                        media_type="application/manifest+json")


SW = """const C='localreels-9';
self.addEventListener('install',e=>{e.waitUntil(caches.open(C).then(c=>c.addAll(['/','/icon.svg','/manifest.webmanifest','/fonts/fonts.css']).catch(()=>{})).then(()=>self.skipWaiting()))});
self.addEventListener('activate',e=>{e.waitUntil(caches.keys().then(k=>Promise.all(k.filter(x=>x!==C).map(x=>caches.delete(x)))).then(()=>self.clients.claim()))});
self.addEventListener('fetch',e=>{const r=e.request;if(r.method!=='GET')return;const u=new URL(r.url);
if(u.origin!==location.origin)return;
if(u.pathname.startsWith('/api/')||u.pathname.startsWith('/media/'))return;
e.respondWith(fetch(r).then(res=>{if(res&&res.status===200){const cp=res.clone();caches.open(C).then(c=>c.put(r,cp))}return res}).catch(()=>caches.match(r).then(h=>h||caches.match('/'))))});"""


@app.get("/sw.js", include_in_schema=False)
def sw():
    return Response(SW, media_type="application/javascript",
                    headers={"Cache-Control": "no-cache", "Service-Worker-Allowed": "/"})


@app.get("/", include_in_schema=False)
@app.get("/index.html", include_in_schema=False)
def ui():
    f = ROOT / "index.html"
    if not f.is_file():
        raise HTTPException(500, "index.html root mein nahi hai")
    return FileResponse(f, media_type="text/html; charset=utf-8", headers={"Cache-Control": "no-cache"})


@app.get("/favicon.ico", include_in_schema=False)
def fav():
    return Response(status_code=204)


# ---------------------------------------------------------------- startup (non-blocking)
def _migrate_old():
    f = ROOT / "aspects.json"
    if f.is_file():
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
            vals = [(int(v[0]), int(v[1]), k) for k, v in d.items()
                    if isinstance(v, list) and len(v) == 2 and v[0] and v[1]]
            if vals:
                c = cx(False)
                c.executemany("UPDATE videos SET width=?,height=? WHERE id=? AND (width IS NULL OR width=0)", vals)
                c.commit()
                c.close()
            f.rename(f.with_suffix(".json.migrated"))
            print(f"[LocalReels] aspects.json -> DB ({len(vals)} ratios)", flush=True)
        except Exception as e:
            print("[LocalReels] aspects migrate:", e, flush=True)
    try:
        c = cx(False)
        for r in c.execute("SELECT id,path,filename FROM videos WHERE bytes IS NULL OR bytes=0").fetchall():
            p = vpath(dict(r))
            if p:
                c.execute("UPDATE videos SET bytes=? WHERE id=?", (p.stat().st_size, r["id"]))
        c.commit()
        c.close()
    except Exception:
        pass


def _startup():
    fetch_fonts()
    _migrate_old()
    old = os.environ.get("LOCALREELS_OLD_ROOT")
    try:
        c = cx()
        n = c.execute("SELECT COUNT(*) n FROM sources").fetchone()["n"]
        c.close()
    except Exception:
        n = 0
    if n == 0:
        for loc in [str(MEDIA)] + ([old] if old and Path(old).is_dir() else []):
            if Path(loc).is_dir():
                sid = uuid.uuid4().hex[:10]
                try:
                    cc = cx(False)
                    cc.execute("INSERT INTO sources(id,kind,name,location,created_at) VALUES(?,?,?,?,?)",
                               (sid, "folder", Path(loc).name or "Media", loc, now()))
                    cc.commit()
                    cc.close()
                except Exception:
                    continue
                scan_folder(sid, loc)


threading.Thread(target=_startup, daemon=True).start()

try:
    _c0 = cx()
    _n = _c0.execute("SELECT COUNT(*) n FROM videos WHERE deleted=0").fetchone()["n"]
    _c0.close()
except Exception:
    _n = 0
print(f"[LocalReels] READY -> http://127.0.0.1:{PORT} | videos={_n} | fts={FTS} | "
      f"ffprobe={'yes' if FFPROBE else 'NO'}", flush=True)