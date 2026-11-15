"""LocalReels v7 — complete standalone backend.
core.py / main.py / reels_api.py ki zaroorat NAHI. Startup par kuch block nahi hota."""
from __future__ import annotations

import json, mimetypes, os, queue, re, shutil, socket, sqlite3, subprocess, sys, threading, time, uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse

ROOT = Path(__file__).resolve().parent.parent
PORT = int(os.environ.get("LOCALREELS_PORT") or 8000)
MEDIA = Path(os.environ.get("LOCALREELS_MEDIA_DIR") or (ROOT / "media")).resolve()
MEDIA.mkdir(parents=True, exist_ok=True)
DB = ROOT / "localreels.db"
AUTH = os.environ.get("LOCALREELS_AUTH", "nayi_app_secret_123")
VEXT = {".mp4", ".webm", ".mkv", ".mov", ".m4v", ".avi", ".flv", ".ts"}
FFPROBE, FFMPEG = shutil.which("ffprobe"), shutil.which("ffmpeg")

app = FastAPI(title="LocalReels", version="7.0.0")
print(f"[LocalReels v7] boot media={MEDIA}", flush=True)


def now(): return datetime.now(timezone.utc).isoformat(timespec="seconds")
def pretty(s): return re.sub(r"[_\-]+", " ", s).strip() or s


def cx(ro=True):
    if ro:
        c = sqlite3.connect(f"file:{DB.as_posix()}?mode=ro", uri=True, timeout=20)
    else:
        c = sqlite3.connect(DB, timeout=30)
    c.row_factory = sqlite3.Row
    return c


SCHEMA = """
CREATE TABLE IF NOT EXISTS videos(id TEXT PRIMARY KEY,title TEXT,grp TEXT DEFAULT 'Unsorted',
 tags TEXT DEFAULT '[]',width INTEGER DEFAULT 0,height INTEGER DEFAULT 0,dur REAL DEFAULT 0,
 filename TEXT,created_at TEXT,source_url TEXT DEFAULT '',path TEXT DEFAULT '',poster_path TEXT DEFAULT '');
CREATE TABLE IF NOT EXISTS downloads(id TEXT PRIMARY KEY,url TEXT,status TEXT,progress REAL DEFAULT 0,
 error TEXT DEFAULT '',video_id TEXT DEFAULT '',created_at TEXT);
CREATE TABLE IF NOT EXISTS engagement(video_id TEXT PRIMARY KEY,liked INTEGER DEFAULT 0,saved INTEGER DEFAULT 0,
 views INTEGER DEFAULT 0,watch REAL DEFAULT 0,pos REAL DEFAULT 0,reaction TEXT DEFAULT '',updated_at TEXT);
CREATE TABLE IF NOT EXISTS comments(id TEXT PRIMARY KEY,video_id TEXT,author TEXT,body TEXT,
 likes INTEGER DEFAULT 0,liked INTEGER DEFAULT 0,created_at TEXT);
CREATE INDEX IF NOT EXISTS ix_cm ON comments(video_id);
CREATE TABLE IF NOT EXISTS reactions(video_id TEXT,emoji TEXT,count INTEGER DEFAULT 0,PRIMARY KEY(video_id,emoji));
CREATE TABLE IF NOT EXISTS playlists(id TEXT PRIMARY KEY,name TEXT,position INTEGER DEFAULT 0,created_at TEXT);
CREATE TABLE IF NOT EXISTS playlist_items(playlist_id TEXT,video_id TEXT,position INTEGER DEFAULT 0,PRIMARY KEY(playlist_id,video_id));
CREATE TABLE IF NOT EXISTS saved_views(id TEXT PRIMARY KEY,name TEXT,search TEXT,grp TEXT,sort TEXT,aspect TEXT,created_at TEXT);
CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY,value TEXT);
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY,value TEXT);
"""


def init_db():
    c = cx(False)
    try:
        c.executescript(SCHEMA)
        have = {r[1] for r in c.execute("PRAGMA table_info(videos)")}
        for col, typ in (("width", "INTEGER DEFAULT 0"), ("path", "TEXT DEFAULT ''"), ("poster_path", "TEXT DEFAULT ''")):
            if col not in have:
                c.execute(f"ALTER TABLE videos ADD COLUMN {col} {typ}")
        c.commit()
    finally:
        c.close()


init_db()

_I = {"ts": 0.0, "rows": [], "by": {}}
_IL = threading.Lock()


def rows(force=False):
    with _IL:
        if not force and time.time() - _I["ts"] < 20:
            return _I["rows"]
    out = []
    try:
        c = cx()
        out = [dict(r) for r in c.execute("SELECT * FROM videos ORDER BY created_at DESC")]
        c.close()
    except Exception as e:
        print("[LocalReels] db:", e, flush=True)
    with _IL:
        _I["rows"] = out
        _I["by"] = {str(r["id"]): r for r in out}
        _I["ts"] = time.time()
    return out


def one(vid):
    rows()
    with _IL:
        return _I["by"].get(str(vid))


def fmt(r):
    try: tags = json.loads(r.get("tags") or "[]")
    except Exception: tags = []
    return {"id": str(r["id"]), "title": r.get("title") or "Untitled", "group": r.get("grp") or "Unsorted",
            "tags": tags, "width": r.get("width") or 0, "height": r.get("height") or 0,
            "duration": r.get("dur") or 0, "created_at": r.get("created_at"), "source_url": r.get("source_url") or ""}


def vpath(r):
    p = Path(r.get("path") or "")
    if p.is_absolute() and p.is_file(): return p
    if r.get("filename"):
        c = MEDIA / r["filename"]
        if c.is_file(): return c
    for f in MEDIA.glob(f"{r['id']}.*"):
        if f.suffix.lower() in VEXT: return f
    return None


def ppath(r):
    v = r.get("poster_path") or ""
    if v:
        p = Path(v)
        if p.is_file(): return p
        if (MEDIA / v).is_file(): return MEDIA / v
    l = MEDIA / f"{r['id']}.jpg"
    return l if l.is_file() else None


# ---------------------------------------------------------------- media (range + instant poster)
def _it(p, start, ln, ch=1 << 20):
    with p.open("rb") as f:
        f.seek(start); left = ln
        while left > 0:
            b = f.read(min(ch, left))
            if not b: break
            left -= len(b); yield b


def serve(p: Path, rq: Request):
    try: size = p.stat().st_size
    except OSError: raise HTTPException(404)
    e = p.suffix.lower()
    mt = {".mp4": "video/mp4", ".m4v": "video/mp4", ".webm": "video/webm", ".mkv": "video/x-matroska",
          ".mov": "video/quicktime", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
          ".webp": "image/webp", ".vtt": "text/vtt"}.get(e) or mimetypes.guess_type(p.name)[0] or "application/octet-stream"
    h = {"Accept-Ranges": "bytes", "Cache-Control": "public, max-age=604800, immutable"}
    if rq.method == "HEAD":
        return Response(200, media_type=mt, headers={**h, "Content-Length": str(size)})
    m = re.fullmatch(r"bytes=(\d*)-(\d*)", (rq.headers.get("range") or "").strip())
    if m:
        a, b = m.group(1), m.group(2)
        if not a and b:
            ln = min(int(b), size); s0, e0 = max(0, size - ln), size - 1
        else:
            s0 = int(a or 0); e0 = min(int(b), size - 1) if b else size - 1
        if s0 >= size or s0 > e0:
            return Response(416, headers={"Content-Range": f"bytes */{size}"})
        ln = e0 - s0 + 1
        return StreamingResponse(_it(p, s0, ln), 206, media_type=mt,
                                 headers={**h, "Content-Range": f"bytes {s0}-{e0}/{size}", "Content-Length": str(ln)})
    return FileResponse(p, media_type=mt, headers=h)


PH = ('<svg xmlns="http://www.w3.org/2000/svg" width="640" height="360">'
      '<defs><linearGradient id="g" x1="0" y1="0" x2="1" y2="1">'
      '<stop offset="0" stop-color="#151C2F"/><stop offset="1" stop-color="#0A0D15"/></linearGradient></defs>'
      '<rect width="640" height="360" fill="url(#g)"/>'
      '<g opacity=".3"><rect x="272" y="140" width="96" height="80" rx="12" fill="none" stroke="#2CE5C8" stroke-width="6"/>'
      '<path d="M304 162v36l30-18z" fill="#2CE5C8"/></g></svg>')

_PQ: queue.Queue = queue.Queue()
_PS: set = set()
_PL = threading.Lock()


def _poster_worker():
    while True:
        vid, src, out = _PQ.get()
        try:
            if FFMPEG and not out.is_file():
                for ss in ("2", "0.5"):
                    subprocess.run([FFMPEG, "-y", "-v", "error", "-ss", ss, "-i", str(src), "-vframes", "1",
                                    "-vf", "scale=480:-2", str(out)], capture_output=True, timeout=90)
                    if out.is_file(): break
                if out.is_file():
                    c = cx(False); c.execute("UPDATE videos SET poster_path=? WHERE id=?", (str(out), vid)); c.commit(); c.close()
        except Exception:
            pass
        finally:
            with _PL: _PS.discard(vid)
            _PQ.task_done()


threading.Thread(target=_poster_worker, daemon=True).start()


@app.api_route("/media/{name:path}", methods=["GET", "HEAD"], include_in_schema=False)
async def media(name: str, rq: Request):
    rel = Path(name)
    if rel.is_absolute() or ".." in rel.parts: raise HTTPException(404)
    stem, ext = rel.stem, rel.suffix.lower()
    r = one(stem)
    if r:
        if ext in {".jpg", ".jpeg", ".png", ".webp"}:
            p = ppath(r)
            if p: return serve(p, rq)
            src = vpath(r)
            if src:
                with _PL:
                    if stem not in _PS:
                        _PS.add(stem); _PQ.put((stem, src, MEDIA / f"{stem}.jpg"))
            return Response(PH, media_type="image/svg+xml", headers={"Cache-Control": "no-store"})
        if ext == ".vtt":
            for c in (MEDIA / f"{stem}.vtt", Path(r.get("path") or "").with_suffix(".vtt")):
                if c.is_file(): return serve(c, rq)
            raise HTTPException(404)
        p = vpath(r)
        if p: return serve(p, rq)
    d = MEDIA / rel
    if d.is_file(): return serve(d, rq)
    par = MEDIA / rel.parent
    if par.is_dir():
        for f in par.iterdir():
            if f.is_file() and f.stem == stem and ((ext in VEXT and f.suffix.lower() in VEXT) or f.suffix.lower() == ext):
                return serve(f, rq)
    raise HTTPException(404)


# ---------------------------------------------------------------- state
def state():
    c = cx()
    try:
        eng = {r["video_id"]: {"liked": r["liked"], "saved": r["saved"], "views": r["views"],
                               "watch": r["watch"], "position": r["pos"], "reaction": r["reaction"] or ""}
               for r in c.execute("SELECT * FROM engagement")}
        cnt = {r["video_id"]: r["n"] for r in c.execute("SELECT video_id,COUNT(*) n FROM comments GROUP BY video_id")}
        rx: dict = {}
        for r in c.execute("SELECT * FROM reactions"): rx.setdefault(r["video_id"], {})[r["emoji"]] = r["count"]
        pls = []
        for p in c.execute("SELECT * FROM playlists ORDER BY position,created_at"):
            items = [i["video_id"] for i in c.execute("SELECT video_id FROM playlist_items WHERE playlist_id=? ORDER BY position", (p["id"],))]
            pls.append({"id": p["id"], "name": p["name"], "items": items})
        sv = [{"id": r["id"], "name": r["name"], "search": r["search"], "group": r["grp"], "sort": r["sort"], "aspect": r["aspect"]}
              for r in c.execute("SELECT * FROM saved_views ORDER BY created_at DESC")]
        st = {}
        for r in c.execute("SELECT * FROM settings"):
            try: st[r["key"]] = json.loads(r["value"])
            except Exception: pass
    finally:
        c.close()
    return {"engagement": eng, "comment_counts": cnt, "reactions": rx, "playlists": pls, "saved_views": sv, "settings": st}


@app.get("/api/state")
@app.get("/api/reels/state")
def api_state(): return {"ok": True, **state()}


def scan_state():
    data = rows()
    total = len(data)
    done = sum(1 for r in data if (r.get("width") or 0) > 0)
    c = cx()
    running = False
    try:
        m = c.execute("SELECT value FROM meta WHERE key='scan_running'").fetchone()
        running = bool(m and m["value"] == "1")
    except Exception: pass
    c.close()
    return {"running": running, "done": done, "total": total, "cached": done,
            "complete": total > 0 and done >= total, "ffmpeg": bool(FFPROBE)}


def _probe(p: Path):
    if not FFPROBE: return None
    try:
        o = subprocess.run([FFPROBE, "-v", "error", "-select_streams", "v:0",
                            "-show_entries", "stream=width,height:format=duration:stream_side_data=rotation:stream_tags=rotate",
                            "-of", "json", str(p)], capture_output=True, text=True, timeout=20)
        d = json.loads(o.stdout or "{}")
        s = (d.get("streams") or [{}])[0]
        w, h = int(s.get("width") or 0), int(s.get("height") or 0)
        du = float((d.get("format") or {}).get("duration") or 0)
        rot = 0
        for sd in s.get("side_data_list") or []:
            if "rotation" in sd: rot = int(sd["rotation"])
        if not rot: rot = int((s.get("tags") or {}).get("rotate") or 0)
        if abs(rot) % 180 == 90: w, h = h, w
        return (w, h, round(du, 2)) if w and h else None
    except Exception:
        return None


def _scan_worker(todo):
    buf = []

    def work(r):
        time.sleep(0.012)
        p = vpath(r)
        return str(r["id"]), (_probe(p) if p else None)

    try:
        with ThreadPoolExecutor(max_workers=3) as pool:
            for vid, res in pool.map(work, todo):
                if res: buf.append((res[0], res[1], res[2], vid))
                else: buf.append((0, 0, 0, vid))
                if len(buf) >= 40:
                    c = cx(False); c.executemany("UPDATE videos SET width=?,height=?,dur=CASE WHEN ?>0 THEN ? ELSE dur END WHERE id=?",
                                                [(w, h, d, d, i) for w, h, d, i in buf]); c.commit(); c.close(); buf.clear()
    finally:
        if buf:
            try:
                c = cx(False); c.executemany("UPDATE videos SET width=?,height=?,dur=CASE WHEN ?>0 THEN ? ELSE dur END WHERE id=?",
                                            [(w, h, d, d, i) for w, h, d, i in buf]); c.commit(); c.close()
            except Exception: pass
        c = cx(False); c.execute("INSERT OR REPLACE INTO meta(key,value) VALUES('scan_running','0')"); c.commit(); c.close()
        rows(force=True)
        print("[LocalReels] ratio scan DONE (permanent)", flush=True)


@app.post("/api/aspects/scan")
@app.post("/api/reels/aspects/scan")
def api_scan_start(payload: dict | None = None):
    payload = payload or {}
    st = scan_state()
    if st["running"]: return {"ok": True, "already": True, "scan": st}
    if not FFPROBE: return {"ok": False, "error": "ffprobe missing — ffmpeg install karo", "scan": st}
    if st["complete"] and not payload.get("force"): return {"ok": True, "already_complete": True, "scan": st}
    data = rows()
    todo = data if payload.get("force") else [r for r in data if not (r.get("width") or 0)]
    if not todo: return {"ok": True, "already_complete": True, "scan": scan_state()}
    c = cx(False); c.execute("INSERT OR REPLACE INTO meta(key,value) VALUES('scan_running','1')"); c.commit(); c.close()
    threading.Thread(target=_scan_worker, args=(todo,), daemon=True).start()
    return {"ok": True, "scan": scan_state()}


@app.get("/api/aspects/summary")
def api_scan_summary(): return {"ok": True, "scan": scan_state()}


@app.get("/api/aspects")
def api_aspects():
    a = {str(r["id"]): [r["width"], r["height"]] for r in rows() if (r.get("width") or 0) > 0}
    return {"ok": True, "aspects": a, "scan": scan_state(), "ffmpeg": bool(FFPROBE)}


@app.post("/api/aspects")
def api_aspects_put(payload: dict):
    inc = payload.get("aspects") or {}
    vals = []
    for vid, wh in inc.items():
        try:
            w, h = int(wh[0]), int(wh[1])
            if w > 0 and h > 0: vals.append((w, h, str(vid)))
        except Exception: continue
    if vals:
        c = cx(False); c.executemany("UPDATE videos SET width=?,height=? WHERE id=?", vals); c.commit(); c.close(); rows(force=True)
    return {"ok": True, "count": len(vals)}


# ---------------------------------------------------------------- videos
@app.get("/api/videos")
def api_videos(skip: int = 0, limit: int = 12, search: str = "", group: str = ""):
    data = rows()
    s = search.strip().lower()
    if s:
        data = [r for r in data if s in ((r.get("title") or "") + " " + (r.get("grp") or "") + " " + (r.get("tags") or "")).lower()]
    if group and group != "all": data = [r for r in data if (r.get("grp") or "") == group]
    n = len(data)
    page = data[max(0, skip): max(0, skip) + max(1, min(limit, 5000))]
    return {"videos": [fmt(r) for r in page], "total": n}


@app.get("/api/groups")
def api_groups():
    g: dict = {}
    for r in rows(): k = r.get("grp") or "Unsorted"; g[k] = g.get(k, 0) + 1
    return {"groups": [{"name": k, "count": v} for k, v in sorted(g.items())]}


@app.post("/api/videos/bulk-delete")
def api_del(ids: list):
    ids = [str(i) for i in ids]
    c = cx(False)
    try:
        for vid in ids:
            r = one(vid)
            if r:
                for p in (vpath(r), ppath(r), MEDIA / f"{vid}.vtt"):
                    try:
                        if p and p.is_file(): p.unlink()
                    except Exception: pass
            c.execute("DELETE FROM videos WHERE id=?", (vid,))
            c.execute("DELETE FROM engagement WHERE video_id=?", (vid,))
            c.execute("DELETE FROM comments WHERE video_id=?", (vid,))
            c.execute("DELETE FROM playlist_items WHERE video_id=?", (vid,))
        c.commit()
    finally: c.close()
    rows(force=True)
    return {"status": "Success", "deleted": len(ids)}


@app.post("/api/videos/update-group")
def api_grp(p: dict):
    name = str(p.get("new_group", "")).strip()[:60]; ids = [str(i) for i in (p.get("video_ids") or [])]
    if not name or not ids: raise HTTPException(400, "new_group + video_ids")
    c = cx(False); c.executemany("UPDATE videos SET grp=? WHERE id=?", [(name, i) for i in ids]); c.commit(); c.close(); rows(force=True)
    return {"status": "Success"}


@app.post("/api/videos/update-tags")
def api_tags(p: dict):
    tags = [str(t).strip() for t in (p.get("tags") or []) if str(t).strip()]
    ids = [str(i) for i in (p.get("video_ids") or [])]
    if not tags or not ids: raise HTTPException(400, "tags + video_ids")
    c = cx(False)
    try:
        for vid in ids:
            r = c.execute("SELECT tags FROM videos WHERE id=?", (vid,)).fetchone()
            if not r: continue
            try: cur = json.loads(r["tags"] or "[]")
            except Exception: cur = []
            c.execute("UPDATE videos SET tags=? WHERE id=?", (json.dumps(list(dict.fromkeys(cur + tags))[:24]), vid))
        c.commit()
    finally: c.close()
    rows(force=True)
    return {"status": "Success"}


@app.post("/api/videos/rename")
@app.post("/api/reels/rename")
def api_rename(p: dict):
    vid, title = str(p.get("video_id", "")), str(p.get("title", "")).strip()[:200]
    if not vid or not title: raise HTTPException(400, "video_id + title")
    c = cx(False); c.execute("UPDATE videos SET title=? WHERE id=?", (title, vid)); c.commit(); c.close(); rows(force=True)
    return {"status": "Success", "ok": True}


@app.post("/api/reindex")
def api_reindex(): rows(force=True); return {"ok": True, "total": len(_I["rows"])}


def _register():
    try:
        data = rows(force=True)
        names = {(r.get("filename") or "").lower() for r in data}
        paths = {(r.get("path") or "").lower() for r in data}
        ids = {str(r["id"]) for r in data}
        new = []
        for f in sorted(MEDIA.iterdir()):
            if not f.is_file() or f.suffix.lower() not in VEXT: continue
            if f.name.lower() in names or str(f.resolve()).lower() in paths: continue
            vid = f.stem if f.stem not in ids else uuid.uuid4().hex[:10]
            ids.add(vid)
            new.append((vid, pretty(f.stem)[:200], "Library", "[]", 0, 0, 0.0, f.name, now(), "", str(f.resolve()), ""))
        if new:
            c = cx(False)
            c.executemany("INSERT OR REPLACE INTO videos(id,title,grp,tags,width,height,dur,filename,created_at,source_url,path,poster_path)"
                          " VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", new)
            c.commit(); c.close()
        rows(force=True)
        print(f"[LocalReels] media check: {len(new)} new | total {len(_I['rows'])}", flush=True)
    except Exception as e:
        print("[LocalReels] register:", e, flush=True)


@app.post("/api/scan")
def api_scan(): threading.Thread(target=_register, daemon=True).start(); return {"status": "Success", "queued": True}


# ---------------------------------------------------------------- engagement
def _eng_up(vid, field, val):
    c = cx(False)
    c.execute(f"INSERT INTO engagement(video_id,{field},updated_at) VALUES(?,?,?) "
              f"ON CONFLICT(video_id) DO UPDATE SET {field}=excluded.{field},updated_at=excluded.updated_at", (vid, val, now()))
    c.commit(); c.close()


@app.post("/api/like")
@app.post("/api/reels/like")
def api_like(p: dict):
    vid = str(p.get("video_id", "")); v = 1 if p.get("value") else 0
    if not vid: raise HTTPException(400, "video_id")
    _eng_up(vid, "liked", v); return {"ok": True, "liked": v}


@app.post("/api/save")
@app.post("/api/reels/save")
def api_save(p: dict):
    vid = str(p.get("video_id", "")); v = 1 if p.get("value") else 0
    if not vid: raise HTTPException(400, "video_id")
    _eng_up(vid, "saved", v); return {"ok": True, "saved": v}


@app.post("/api/view")
@app.post("/api/reels/view")
def api_view(p: dict):
    vid = str(p.get("video_id", ""))
    if not vid: raise HTTPException(400, "video_id")
    pos = float(p.get("position") or 0); du = float(p.get("duration") or 0)
    delta = max(0.0, min(float(p.get("delta") or 0), 30.0)); first = 1 if p.get("first") else 0
    c = cx(False)
    c.execute("INSERT INTO engagement(video_id,views,watch,pos,updated_at) VALUES(?,?,?,?,?) "
              "ON CONFLICT(video_id) DO UPDATE SET views=engagement.views+excluded.views,"
              "watch=engagement.watch+excluded.watch,pos=excluded.pos,updated_at=excluded.updated_at",
              (vid, first, delta, pos, now()))
    if du > 0: c.execute("UPDATE videos SET dur=? WHERE id=? AND (dur IS NULL OR dur=0)", (du, vid))
    c.commit(); c.close()
    return {"ok": True}


@app.post("/api/react")
@app.post("/api/reels/react")
def api_react(p: dict):
    vid, em = str(p.get("video_id", "")), str(p.get("emoji", ""))[:8]
    if not vid or not em: raise HTTPException(400, "video_id + emoji")
    c = cx(False)
    c.execute("INSERT INTO reactions(video_id,emoji,count) VALUES(?,?,1) ON CONFLICT(video_id,emoji) DO UPDATE SET count=count+1", (vid, em))
    c.execute("INSERT INTO engagement(video_id,reaction,updated_at) VALUES(?,?,?) ON CONFLICT(video_id) DO UPDATE SET reaction=excluded.reaction", (vid, em, now()))
    rs = {r["emoji"]: r["count"] for r in c.execute("SELECT emoji,count FROM reactions WHERE video_id=?", (vid,))}
    c.commit(); c.close()
    return {"ok": True, "reactions": rs}


@app.get("/api/comments")
@app.get("/api/reels/comments")
def api_cm(video_id: str):
    c = cx(); rs = [dict(r) for r in c.execute("SELECT * FROM comments WHERE video_id=? ORDER BY created_at DESC", (video_id,))]; c.close()
    return {"ok": True, "comments": rs}


@app.post("/api/comments")
@app.post("/api/reels/comments")
def api_cm_add(p: dict):
    vid, body = str(p.get("video_id", "")), str(p.get("body", "")).strip()[:400]
    if not vid or not body: raise HTTPException(400, "video_id + body")
    cid = uuid.uuid4().hex[:10]; au = str(p.get("author") or "You")[:40]
    c = cx(False)
    c.execute("INSERT INTO comments(id,video_id,author,body,likes,liked,created_at) VALUES(?,?,?,?,0,0,?)", (cid, vid, au, body, now()))
    r = dict(c.execute("SELECT * FROM comments WHERE id=?", (cid,)).fetchone()); c.commit(); c.close()
    return {"ok": True, "comment": r}


@app.post("/api/comments/like")
@app.post("/api/reels/comments/like")
def api_cm_like(p: dict):
    cid, val = str(p.get("comment_id", "")), bool(p.get("value"))
    c = cx(False); r = c.execute("SELECT * FROM comments WHERE id=?", (cid,)).fetchone()
    if not r: c.close(); raise HTTPException(404)
    n = max(0, r["likes"] + (1 if val else -1))
    c.execute("UPDATE comments SET likes=?,liked=? WHERE id=?", (n, 1 if val else 0, cid)); c.commit(); c.close()
    return {"ok": True, "likes": n, "liked": 1 if val else 0}


@app.delete("/api/comments/{cid}")
@app.delete("/api/reels/comments/{cid}")
def api_cm_del(cid: str):
    c = cx(False); c.execute("DELETE FROM comments WHERE id=?", (cid,)); c.commit(); c.close(); return {"ok": True}


# ---------------------------------------------------------------- playlists / views / settings
@app.post("/api/playlists")
@app.post("/api/reels/playlists")
def api_pl_new(p: dict):
    name = str(p.get("name", "")).strip()[:60]
    if not name: raise HTTPException(400, "name")
    pid = uuid.uuid4().hex[:10]; c = cx(False)
    pos = c.execute("SELECT COALESCE(MAX(position),0)+1 p FROM playlists").fetchone()["p"]
    c.execute("INSERT INTO playlists(id,name,position,created_at) VALUES(?,?,?,?)", (pid, name, pos, now())); c.commit(); c.close()
    return {"ok": True, "playlist": {"id": pid, "name": name, "items": []}}


@app.post("/api/playlists/rename")
@app.post("/api/reels/playlists/rename")
def api_pl_ren(p: dict):
    c = cx(False); c.execute("UPDATE playlists SET name=? WHERE id=?", (str(p.get("name", ""))[:60], str(p.get("id", "")))); c.commit(); c.close()
    return {"ok": True}


@app.delete("/api/playlists/{pid}")
@app.delete("/api/reels/playlists/{pid}")
def api_pl_del(pid: str):
    c = cx(False); c.execute("DELETE FROM playlist_items WHERE playlist_id=?", (pid,)); c.execute("DELETE FROM playlists WHERE id=?", (pid,)); c.commit(); c.close()
    return {"ok": True}


@app.post("/api/playlists/items")
@app.post("/api/reels/playlists/items")
def api_pl_add(p: dict):
    pid = str(p.get("playlist_id", "")); ids = [str(v) for v in (p.get("video_ids") or [])]
    c = cx(False)
    pos = c.execute("SELECT COALESCE(MAX(position),0) p FROM playlist_items WHERE playlist_id=?", (pid,)).fetchone()["p"]
    for vid in ids:
        pos += 1
        c.execute("INSERT OR IGNORE INTO playlist_items(playlist_id,video_id,position) VALUES(?,?,?)", (pid, vid, pos))
    c.commit(); c.close(); return {"ok": True}


@app.post("/api/playlists/remove")
@app.post("/api/reels/playlists/remove")
def api_pl_rm(p: dict):
    c = cx(False); c.execute("DELETE FROM playlist_items WHERE playlist_id=? AND video_id=?", (str(p.get("playlist_id", "")), str(p.get("video_id", "")))); c.commit(); c.close()
    return {"ok": True}


@app.post("/api/playlists/reorder")
@app.post("/api/reels/playlists/reorder")
def api_pl_reorder(p: dict):
    pid = str(p.get("playlist_id", "")); ids = [str(v) for v in (p.get("video_ids") or [])]
    c = cx(False)
    for i, vid in enumerate(ids): c.execute("UPDATE playlist_items SET position=? WHERE playlist_id=? AND video_id=?", (i, pid, vid))
    c.commit(); c.close(); return {"ok": True}


@app.post("/api/views")
@app.post("/api/reels/views")
def api_view_save(p: dict):
    name = str(p.get("name", "")).strip()[:50]
    if not name: raise HTTPException(400, "name")
    vid = uuid.uuid4().hex[:10]; c = cx(False)
    c.execute("INSERT INTO saved_views(id,name,search,grp,sort,aspect,created_at) VALUES(?,?,?,?,?,?,?)",
              (vid, name, str(p.get("search", "")), str(p.get("group", "all")), str(p.get("sort", "new")), str(p.get("aspect", "all")), now()))
    c.commit(); c.close()
    return {"ok": True, "view": {"id": vid, "name": name, "search": p.get("search", ""), "group": p.get("group", "all"),
                                 "sort": p.get("sort", "new"), "aspect": p.get("aspect", "all")}}


@app.delete("/api/views/{vid}")
@app.delete("/api/reels/views/{vid}")
def api_view_del(vid: str):
    c = cx(False); c.execute("DELETE FROM saved_views WHERE id=?", (vid,)); c.commit(); c.close(); return {"ok": True}


@app.post("/api/settings")
@app.post("/api/reels/settings")
def api_set(p: dict):
    c = cx(False); c.execute("INSERT OR REPLACE INTO settings(key,value) VALUES(?,?)", (str(p.get("key", ""))[:40], json.dumps(p.get("value")))); c.commit(); c.close()
    return {"ok": True}


@app.post("/api/history/clear")
@app.post("/api/reels/history/clear")
def api_hist():
    c = cx(False); c.execute("UPDATE engagement SET pos=0,watch=0,views=0"); c.commit(); c.close(); return {"ok": True}


@app.get("/api/export")
@app.get("/api/reels/export")
def api_export():
    s = state(); c = cx()
    s["comments"] = [dict(r) for r in c.execute("SELECT * FROM comments")]
    s["videos"] = [fmt(r) for r in rows()]; c.close(); s["exported_at"] = now()
    return JSONResponse(s, headers={"Content-Disposition": 'attachment; filename="localreels-backup.json"'})


@app.get("/api/stats")
@app.get("/api/reels/stats")
def api_stats():
    files = [p for p in MEDIA.rglob("*") if p.suffix.lower() in VEXT and p.is_file()]
    disk = sum(p.stat().st_size for p in files)
    c = cx()
    a = c.execute("SELECT COALESCE(SUM(views),0) v,COALESCE(SUM(watch),0) w,COALESCE(SUM(liked),0) l,COALESCE(SUM(saved),0) s FROM engagement").fetchone()
    cm = c.execute("SELECT COUNT(*) n FROM comments").fetchone()["n"]; c.close()
    return {"ok": True, "files": len(files), "disk_bytes": disk, "views": a["v"], "watch_seconds": a["w"],
            "liked": a["l"], "saved": a["s"], "comments": cm, "media_dir": str(MEDIA), "ffmpeg": bool(FFMPEG)}


# ---------------------------------------------------------------- upload / download
@app.post("/api/upload")
@app.post("/api/reels/upload")
async def api_upload(file: UploadFile = File(...), title: str = Form(""), group: str = Form("Uploads"), tags: str = Form("")):
    ext = Path(file.filename or "v.mp4").suffix.lower()
    if ext not in VEXT: raise HTTPException(400, f"{ext} supported nahi")
    vid = "up_" + uuid.uuid4().hex[:8]; dest = MEDIA / f"{vid}{ext}"; size = 0
    with dest.open("wb") as out:
        while True:
            ch = await file.read(1 << 20)
            if not ch: break
            size += len(ch)
            if size > 4 * 1024 ** 3:
                out.close(); dest.unlink(missing_ok=True); raise HTTPException(413, "4GB se badi file")
            out.write(ch)
    await file.close()
    pr = _probe(dest) or (0, 0, 0.0)
    tl = [t.strip() for t in tags.split(",") if t.strip()][:12]
    c = cx(False)
    c.execute("INSERT OR REPLACE INTO videos(id,title,grp,tags,width,height,dur,filename,created_at,source_url,path,poster_path)"
              " VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
              (vid, (title or Path(file.filename or vid).stem)[:200], group or "Uploads", json.dumps(tl),
               pr[0], pr[1], pr[2], dest.name, now(), "", str(dest.resolve()), ""))
    r = dict(c.execute("SELECT * FROM videos WHERE id=?", (vid,)).fetchone()); c.commit(); c.close(); rows(force=True)
    return {"ok": True, "video": fmt(r)}


def _dl_set(i, **kw):
    if not kw: return
    c = cx(False); c.execute(f"UPDATE downloads SET {','.join(k+'=?' for k in kw)} WHERE id=?", (*kw.values(), i)); c.commit(); c.close()


def _dl_run(i, url, grp, tags):
    vid = uuid.uuid4().hex[:10]
    try: import yt_dlp  # noqa
    except Exception: _dl_set(i, status="error", error="pip install yt-dlp"); return
    args = ["--no-playlist", "--restrict-filenames", "--newline", "--no-colors", "--write-info-json",
            "-f", "bv*+ba/b", "--merge-output-format", "mp4", "-o", str(MEDIA / f"{vid}.%(ext)s")]
    if FFMPEG: args += ["--ffmpeg-location", str(Path(FFMPEG).parent)]
    _dl_set(i, status="downloading", progress=0)
    try:
        pr = subprocess.Popen([sys.executable, "-m", "yt_dlp"] + args + [url], stdout=subprocess.PIPE,
                              stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="ignore")
    except Exception as e: _dl_set(i, status="error", error=str(e)[:200]); return
    last = -1
    for line in pr.stdout or []:
        m = re.search(r"(\d+(?:\.\d+)?)%", line)
        if m:
            p = int(float(m.group(1)))
            if p != last: last = p; _dl_set(i, progress=p)
    pr.wait()
    if pr.returncode != 0:
        for f in MEDIA.glob(f"{vid}.*"):
            try: f.unlink()
            except Exception: pass
        _dl_set(i, status="error", error="yt-dlp fail"); return
    f = next((x for x in MEDIA.glob(f"{vid}.*") if x.suffix.lower() in VEXT), None)
    if not f: _dl_set(i, status="error", error="file missing"); return
    ttl = pretty(vid); info = MEDIA / f"{vid}.info.json"
    if info.is_file():
        try: ttl = str(json.loads(info.read_text(encoding="utf-8", errors="ignore")).get("title") or ttl)[:200]
        except Exception: pass
        try: info.unlink()
        except Exception: pass
    w, h, du = _probe(f) or (0, 0, 0.0)
    c = cx(False)
    c.execute("INSERT OR REPLACE INTO videos(id,title,grp,tags,width,height,dur,filename,created_at,source_url,path,poster_path)"
              " VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
              (vid, ttl, grp or "Downloads", json.dumps(tags or ["download"]), w, h, du, f.name, now(), url, str(f.resolve()), ""))
    c.commit(); c.close(); rows(force=True); _dl_set(i, status="done", progress=100, video_id=vid)


@app.post("/api/download")
def api_dl(p: dict, rq: Request):
    url = str(p.get("url", "")).strip()
    if not re.match(r"^https?://", url, re.I): raise HTTPException(400, "Sahi URL chahiye")
    sent = rq.headers.get("X-LocalReels-Auth")
    if sent and sent != AUTH: raise HTTPException(403, "auth galat")
    i = uuid.uuid4().hex[:10]; c = cx(False)
    c.execute("INSERT INTO downloads(id,url,status,progress,created_at) VALUES(?,?,?,0,?)", (i, url, "queued", now())); c.commit(); c.close()
    threading.Thread(target=_dl_run, args=(i, url, str(p.get("group") or "Downloads"), [str(t) for t in (p.get("tags") or [])]), daemon=True).start()
    return {"status": "Success", "ok": True, "download_id": i}


@app.get("/api/downloads")
def api_dls():
    c = cx(); r = [dict(x) for x in c.execute("SELECT * FROM downloads ORDER BY created_at DESC LIMIT 30")]; c.close()
    return {"downloads": r}


# ---------------------------------------------------------------- lan / bootstrap / health / UI
def _lan():
    ips = {"127.0.0.1"}
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM); s.connect(("8.8.8.8", 80)); ips.add(s.getsockname()[0]); s.close()
    except Exception: pass
    try:
        for ip in socket.gethostbyname_ex(socket.gethostname())[2]:
            if not ip.startswith("127."): ips.add(ip)
    except Exception: pass
    return {"ok": True, "port": PORT, "urls": [f"http://{i}:{PORT}" for i in sorted(ips, key=lambda x: x.startswith("127."))]}


@app.get("/api/lan")
def api_lan(): return _lan()


@app.get("/api/bootstrap")
def api_bootstrap(limit: int = 60):
    data = rows()
    return {"ok": True, "videos": [fmt(r) for r in data[:max(1, min(limit, 300))]], "total": len(data),
            "scan": scan_state(), "state": state(), "reels": state(), "lan": _lan(),
            "ffmpeg": bool(FFPROBE), "version": "7.0.0"}


@app.get("/api/health")
def api_health():
    return {"ok": True, "version": "7.0.0", "videos": len(rows()), "media_dir": str(MEDIA), "db": str(DB),
            "ffmpeg": bool(FFMPEG), "ffprobe": bool(FFPROBE), "scan": scan_state()}


@app.get("/", include_in_schema=False)
@app.get("/index.html", include_in_schema=False)
def ui():
    f = ROOT / "index.html"
    if not f.is_file(): raise HTTPException(500, "index.html root mein nahi hai")
    return FileResponse(f, media_type="text/html; charset=utf-8", headers={"Cache-Control": "no-cache"})


@app.get("/favicon.ico", include_in_schema=False)
def fav(): return Response(status_code=204)


def _migrate_aspects():
    """Purane aspects.json ko ek baar DB mein daal do."""
    f = ROOT / "aspects.json"
    if not f.is_file(): return
    try:
        d = json.loads(f.read_text(encoding="utf-8"))
        vals = [(int(v[0]), int(v[1]), k) for k, v in d.items() if isinstance(v, list) and len(v) == 2 and v[0] and v[1]]
        if vals:
            c = cx(False); c.executemany("UPDATE videos SET width=?,height=? WHERE id=? AND (width IS NULL OR width=0)", vals); c.commit(); c.close()
        f.rename(f.with_suffix(".json.migrated"))
        rows(force=True)
        print(f"[LocalReels] aspects.json -> DB ({len(vals)} ratios)", flush=True)
    except Exception as e:
        print("[LocalReels] aspects migrate:", e, flush=True)


threading.Thread(target=lambda: (_migrate_aspects(), _register()), daemon=True).start()
print(f"[LocalReels v7] READY -> http://127.0.0.1:{PORT} | videos={len(rows())} | ffprobe={'yes' if FFPROBE else 'NO'}", flush=True)