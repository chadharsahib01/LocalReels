"""Purane LocalReels DB ke payload JSON se metadata nikaal kar naye DB mein bharo. Files copy nahi hoti."""
from __future__ import annotations

import json
import os
import re
import sqlite3
import sys
import uuid
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

NEW_ROOT = Path(__file__).resolve().parent.parent
NEW_DB = NEW_ROOT / "localreels.db"
VIDEO_EXTS = {".mp4", ".webm", ".mkv", ".mov", ".m4v", ".avi", ".flv", ".ts"}
IMG_EXTS = {".jpg", ".jpeg", ".png", ".webp"}

TITLE_KEYS = ["title", "name", "display_name", "video_title", "caption", "label"]
GROUP_KEYS = ["group", "grp", "group_name", "folder", "folder_name", "category", "collection", "album", "library"]
TAG_KEYS = ["tags", "tag_list", "labels", "keywords", "hashtags"]
H_KEYS = ["height", "resolution", "quality", "res", "height_px"]
DUR_KEYS = ["duration", "dur", "duration_sec", "duration_seconds", "length", "seconds", "runtime", "length_seconds"]
PATH_KEYS = ["path", "file_path", "filepath", "full_path", "abs_path", "local_path", "video_path",
             "imported_path", "media_path", "location", "src", "filename", "file_name", "file", "stored_name"]
POSTER_KEYS = ["poster_path", "poster", "thumbnail", "thumb", "thumbnail_path", "preview", "cover", "poster_file"]
URL_KEYS = ["source_url", "url", "webpage_url", "source", "link", "origin"]
DATE_KEYS = ["created_at", "added_at", "date_added", "imported_at", "created", "timestamp", "added", "downloaded_at"]


def norm(s):
    return re.sub(r"[^a-z0-9]", "", str(s or "").lower())


def safe_id(v):
    s = re.sub(r"[^A-Za-z0-9_-]", "_", str(v or "").strip())
    return s[:60] or uuid.uuid4().hex[:10]


def pretty(stem):
    return re.sub(r"[_\-]+", " ", stem).strip() or stem


def flatten(obj, out=None, depth=0):
    if out is None:
        out = {}
    if depth > 4 or not isinstance(obj, dict):
        return out
    for k, v in obj.items():
        if not isinstance(v, dict):
            out.setdefault(str(k).lower(), v)
    for k, v in obj.items():
        if isinstance(v, dict):
            flatten(v, out, depth + 1)
    return out


def pick(flat, wanted):
    for w in wanted:
        if w in flat and flat[w] not in (None, "", [], {}):
            return flat[w]
    for w in wanted:
        for k, v in flat.items():
            if w in k and v not in (None, "", [], {}):
                return v
    return None


def parse_h(v):
    if v is None:
        return 0
    m = re.search(r"(\d{3,4})", str(v))
    return int(m.group(1)) if m else 0


def parse_dur(v):
    if v is None:
        return 0.0
    s = str(v).strip()
    if ":" in s:
        try:
            nums = [float(p) for p in s.split(":")]
        except ValueError:
            return 0.0
        total = 0.0
        for n in nums:
            total = total * 60 + n
        return round(total, 2)
    m = re.search(r"(\d+(?:\.\d+)?)", s)
    return round(float(m.group(1)), 2) if m else 0.0


def parse_tags(v):
    if v is None:
        return []
    if isinstance(v, (list, tuple)):
        out = []
        for t in v:
            if isinstance(t, dict):
                t = t.get("name") or t.get("tag") or t.get("label") or ""
            t = str(t).strip().lstrip("#")
            if t:
                out.append(t)
        return out[:20]
    s = str(v).strip()
    if not s:
        return []
    if s.startswith("["):
        try:
            return parse_tags(json.loads(s))
        except Exception:
            pass
    return [t.strip().lstrip("#") for t in re.split(r"[,|;]", s) if t.strip()][:20]


def parse_date(v):
    if v in (None, ""):
        return ""
    if isinstance(v, (int, float)):
        ts = float(v)
        if ts > 1e12:
            ts /= 1000.0
        try:
            return datetime.fromtimestamp(ts, timezone.utc).isoformat(timespec="seconds")
        except Exception:
            return ""
    s = str(v).strip()
    if s.isdigit():
        return parse_date(int(s))
    return s if len(s) >= 8 else ""


def resolve(raw, roots):
    if not raw:
        return None
    s = str(raw).strip().strip('"')
    if not s:
        return None
    p = Path(s)
    if p.is_absolute() and p.is_file():
        return p
    for r in roots:
        cand = r / s
        if cand.is_file():
            return cand
    return None


def index_files(dirs, exts):
    by_stem, by_norm, by_name = {}, {}, {}
    for d in dirs:
        if not d.is_dir():
            continue
        for f in d.rglob("*"):
            if f.is_file() and f.suffix.lower() in exts:
                by_name.setdefault(f.name.lower(), f)
                by_stem.setdefault(f.stem.lower(), f)
                by_norm.setdefault(norm(f.stem), f)
    return by_stem, by_norm, by_name


def run(old_root):
    old_root = Path(old_root)
    print("=" * 62)
    print("  LocalReels - Payload Metadata Enrich (IN-PLACE)")
    print("=" * 62)
    print("  Old root :", old_root)
    print("  New DB   :", NEW_DB)

    dbs = sorted(list(old_root.glob("*.sqlite3")) + list(old_root.glob("*.db")),
                 key=lambda p: (0 if "localreels" in p.name.lower() else 1, -p.stat().st_size))
    if not dbs:
        print("  [X] Purana DB nahi mila")
        return
    old_db = dbs[0]
    print("  Old DB   :", old_db)

    oc = sqlite3.connect("file:" + old_db.as_posix() + "?mode=ro", uri=True, timeout=20)
    oc.row_factory = sqlite3.Row
    rows = [dict(r) for r in oc.execute("SELECT * FROM videos")]
    oc.close()
    print("  Rows     :", len(rows))
    if not rows:
        return

    statuses = Counter(str(r.get("status")) for r in rows)
    print("  Status   :", dict(statuses))

    sample = rows[0].get("payload")
    try:
        sample_obj = json.loads(sample) if isinstance(sample, str) else sample
    except Exception:
        sample_obj = None
    if isinstance(sample_obj, dict):
        print("  Payload keys (sample):", ", ".join(list(sample_obj.keys())[:25]))
    else:
        print("  [!] Payload JSON nahi hai - sample:", str(sample)[:160])

    src_dirs = [d for d in old_root.iterdir() if d.is_dir() and
                any(k in d.name.lower() for k in ("media", "import", "video", "download"))]
    poster_dirs = [d for d in old_root.iterdir() if d.is_dir() and
                   any(k in d.name.lower() for k in ("poster", "thumb", "preview", "media", "import"))]
    roots = [old_root] + src_dirs + poster_dirs
    v_stem, v_norm, v_name = index_files(src_dirs, VIDEO_EXTS)
    p_stem, p_norm, p_name = index_files(poster_dirs, IMG_EXTS)

    con = sqlite3.connect(NEW_DB, timeout=30)
    con.row_factory = sqlite3.Row
    base_time = datetime.now(timezone.utc)

    stats = Counter()
    for r in rows:
        oid = str(r.get("id") or "")
        sid = safe_id(oid)
        raw = r.get("payload")
        try:
            obj = json.loads(raw) if isinstance(raw, str) else (raw or {})
        except Exception:
            obj = {}
        flat = flatten(obj if isinstance(obj, dict) else {})

        title = pick(flat, TITLE_KEYS)
        group = pick(flat, GROUP_KEYS)
        tags = parse_tags(pick(flat, TAG_KEYS))
        height = parse_h(pick(flat, H_KEYS))
        dur = parse_dur(pick(flat, DUR_KEYS))
        url = pick(flat, URL_KEYS)
        date = parse_date(pick(flat, DATE_KEYS))
        vraw = pick(flat, PATH_KEYS)
        praw = pick(flat, POSTER_KEYS)

        cur = con.execute("SELECT * FROM videos WHERE id=? OR old_id=?", (sid, sid)).fetchone()

        vpath = resolve(vraw, roots)
        if vpath is None and vraw:
            key = str(vraw).lower()
            vpath = v_name.get(key) or v_stem.get(Path(key).stem.lower()) or v_norm.get(norm(Path(key).stem))
        if vpath is None:
            vpath = v_stem.get(oid.lower()) or v_norm.get(norm(oid))
        if vpath is None and title:
            vpath = v_norm.get(norm(title))

        ppath = resolve(praw, roots)
        if ppath is None and praw:
            key = str(praw).lower()
            ppath = p_name.get(key) or p_stem.get(Path(key).stem.lower()) or p_norm.get(norm(Path(key).stem))
        if ppath is None and vpath is not None:
            ppath = p_stem.get(vpath.stem.lower()) or p_norm.get(norm(vpath.stem))
        if ppath is None:
            ppath = p_stem.get(oid.lower()) or p_norm.get(norm(oid))

        if not date:
            try:
                ordv = int(r.get("ordinal") or 0)
            except Exception:
                ordv = 0
            date = (base_time - timedelta(seconds=ordv)).isoformat(timespec="seconds")

        if cur is None:
            if vpath is None:
                stats["skip_no_file"] += 1
                continue
            con.execute(
                "INSERT OR REPLACE INTO videos(id,title,grp,tags,height,dur,filename,created_at,source_url,path,poster_path,old_id) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (sid, str(title or pretty(vpath.stem))[:200], str(group or "Imported")[:60],
                 json.dumps(tags), height, dur, vpath.name, date, str(url or ""),
                 str(vpath.resolve()), str(ppath.resolve()) if ppath else "", sid))
            stats["recovered"] += 1
            continue

        sets, vals = [], []
        if title:
            sets.append("title=?"); vals.append(str(title)[:200]); stats["title"] += 1
        if group:
            sets.append("grp=?"); vals.append(str(group)[:60]); stats["group"] += 1
        if tags:
            sets.append("tags=?"); vals.append(json.dumps(tags)); stats["tags"] += 1
        if height:
            sets.append("height=?"); vals.append(height); stats["height"] += 1
        if dur:
            sets.append("dur=?"); vals.append(dur); stats["duration"] += 1
        if url:
            sets.append("source_url=?"); vals.append(str(url)); stats["url"] += 1
        if date:
            sets.append("created_at=?"); vals.append(date)
        if ppath:
            sets.append("poster_path=?"); vals.append(str(ppath.resolve())); stats["poster"] += 1
        if vpath:
            sets.append("path=?"); vals.append(str(vpath.resolve()))
            sets.append("filename=?"); vals.append(vpath.name)
        if sets:
            vals.append(cur["id"])
            con.execute("UPDATE videos SET " + ", ".join(sets) + " WHERE id=?", vals)
            stats["updated"] += 1

    filled = 0
    for row in con.execute("SELECT id, path, filename FROM videos WHERE poster_path IS NULL OR poster_path=''").fetchall():
        stem = Path(row["path"] or row["filename"] or row["id"]).stem
        p = p_stem.get(stem.lower()) or p_norm.get(norm(stem))
        if p:
            con.execute("UPDATE videos SET poster_path=? WHERE id=?", (str(p.resolve()), row["id"]))
            filled += 1

    con.commit()
    total = con.execute("SELECT COUNT(*) n FROM videos").fetchone()["n"]
    groups = con.execute("SELECT COUNT(DISTINCT grp) n FROM videos").fetchone()["n"]
    tagged = con.execute("SELECT COUNT(*) n FROM videos WHERE tags<>'[]' AND tags<>''").fetchone()["n"]
    con.close()

    print("-" * 62)
    print("  Rows updated      :", stats["updated"])
    print("  Naye recover hue  :", stats["recovered"])
    print("  Titles mile       :", stats["title"])
    print("  Folders mile      :", stats["group"])
    print("  Tags mile         :", stats["tags"])
    print("  Duration mile     :", stats["duration"])
    print("  Quality mile      :", stats["height"])
    print("  Posters mile      :", stats["poster"] + filled)
    print("  File nahi mili    :", stats["skip_no_file"])
    print("-" * 62)
    print("  TOTAL videos      :", total)
    print("  Folders           :", groups)
    print("  Tagged videos     :", tagged)
    print("=" * 62)

    appjs = NEW_ROOT / "app.js"
    if appjs.is_file():
        txt = appjs.read_text(encoding="utf-8", errors="ignore")
        if "limit=200" in txt:
            appjs.write_text(txt.replace("limit=200", "limit=5000"), encoding="utf-8")
            print("  app.js: limit 200 -> 5000 (poori library load hogi)")
        else:
            print("  app.js: limit pehle se theek hai")


if __name__ == "__main__":
    args = sys.argv[1:]
    root = Path(os.environ.get("LOCALREELS_OLD_ROOT") or r"C:\LocalReels")
    for i, a in enumerate(args):
        if a in ("--old", "--root") and i + 1 < len(args):
            root = Path(args[i + 1])
    if not root.is_dir():
        print("[X] Folder nahi mila:", root)
        sys.exit(1)
    run(root)
