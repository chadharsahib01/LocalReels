"""Purani LocalReels library ko naye backend mein register karo. Files COPY nahi hoti."""
from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

NEW_ROOT = Path(__file__).resolve().parent.parent
NEW_DB = NEW_ROOT / "localreels.db"
NEW_MEDIA = Path(os.environ.get("LOCALREELS_MEDIA_DIR") or (NEW_ROOT / "media"))
VIDEO_EXTS = {".mp4", ".webm", ".mkv", ".mov", ".m4v", ".avi", ".flv", ".ts"}
IMG_EXTS = {".jpg", ".jpeg", ".png", ".webp"}

ID_COLS = ["id", "video_id", "uuid", "vid", "slug", "key"]
TITLE_COLS = ["title", "name", "display_name", "video_title", "caption"]
GROUP_COLS = ["grp", "group", "group_name", "folder", "folder_name", "category", "collection"]
TAG_COLS = ["tags", "tag_list", "labels", "keywords"]
H_COLS = ["height", "resolution", "quality", "res", "height_px"]
DUR_COLS = ["dur", "duration", "duration_sec", "duration_seconds", "length", "seconds", "runtime"]
PATH_COLS = ["path", "file_path", "filepath", "full_path", "abs_path", "location", "video_path", "src"]
FILE_COLS = ["filename", "file_name", "file", "original_name", "fname", "stored_name"]
POSTER_COLS = ["poster_path", "poster", "thumbnail", "thumb", "thumbnail_path", "preview", "cover"]
URL_COLS = ["source_url", "url", "source", "link", "webpage_url", "origin"]
DATE_COLS = ["created_at", "added_at", "date_added", "imported_at", "created", "timestamp", "added"]


def norm(s):
    return re.sub(r"[^a-z0-9]", "", str(s or "").lower())


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def pick(cols, wanted):
    low = {c.lower(): c for c in cols}
    for w in wanted:
        if w in low:
            return low[w]
    for w in wanted:
        for lc, real in low.items():
            if w in lc:
                return real
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
        return [str(t).strip() for t in v if str(t).strip()][:20]
    s = str(v).strip()
    if not s:
        return []
    if s.startswith("["):
        try:
            data = json.loads(s)
            if isinstance(data, list):
                return [str(t).strip() for t in data if str(t).strip()][:20]
        except Exception:
            pass
    return [t.strip() for t in re.split(r"[,|;#]", s) if t.strip()][:20]


def parse_date(v):
    if v is None or v == "":
        return now()
    if isinstance(v, (int, float)):
        ts = float(v)
        if ts > 1e12:
            ts /= 1000.0
        try:
            return datetime.fromtimestamp(ts, timezone.utc).isoformat(timespec="seconds")
        except Exception:
            return now()
    s = str(v).strip()
    if s.isdigit():
        return parse_date(int(s))
    return s


def safe_id(v):
    s = re.sub(r"[^A-Za-z0-9_-]", "_", str(v or "").strip())
    return s[:60] or uuid.uuid4().hex[:10]


def pretty(stem):
    return re.sub(r"[_\-]+", " ", stem).strip() or stem


def ensure_schema():
    NEW_MEDIA.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(NEW_DB, timeout=30)
    con.executescript("""
CREATE TABLE IF NOT EXISTS videos(
  id TEXT PRIMARY KEY, title TEXT NOT NULL, grp TEXT DEFAULT 'Unsorted',
  tags TEXT DEFAULT '[]', height INTEGER DEFAULT 0, dur REAL DEFAULT 0,
  filename TEXT, created_at TEXT, source_url TEXT DEFAULT '');
CREATE TABLE IF NOT EXISTS downloads(
  id TEXT PRIMARY KEY, url TEXT, status TEXT DEFAULT 'queued', progress REAL DEFAULT 0,
  error TEXT DEFAULT '', video_id TEXT DEFAULT '', created_at TEXT);
""")
    have = {r[1] for r in con.execute("PRAGMA table_info(videos)")}
    for col in ("path", "poster_path", "old_id"):
        if col not in have:
            con.execute("ALTER TABLE videos ADD COLUMN " + col + " TEXT DEFAULT ''")
    con.commit()
    con.close()


def index_files(dirs, exts):
    files = []
    for d in dirs:
        if not d.is_dir():
            continue
        for f in d.rglob("*"):
            if f.is_file() and f.suffix.lower() in exts:
                files.append(f)
    by_name, by_stem, by_norm = {}, {}, {}
    for f in files:
        by_name.setdefault(f.name.lower(), f)
        by_stem.setdefault(f.stem.lower(), f)
        by_norm.setdefault(norm(f.stem), f)
    return files, by_name, by_stem, by_norm


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


def read_old_rows(db_file):
    if not db_file or not db_file.is_file():
        return [], "", []
    con = sqlite3.connect("file:" + db_file.as_posix() + "?mode=ro", uri=True, timeout=20)
    con.row_factory = sqlite3.Row
    tables = [r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")]
    best, best_score, best_cols = None, -1, []
    for t in tables:
        if t.startswith("sqlite_"):
            continue
        try:
            cols = [r[1] for r in con.execute('PRAGMA table_info("' + t + '")')]
        except Exception:
            continue
        score = 0
        if pick(cols, ID_COLS):
            score += 2
        if pick(cols, TITLE_COLS):
            score += 2
        if pick(cols, PATH_COLS) or pick(cols, FILE_COLS):
            score += 3
        if pick(cols, DUR_COLS):
            score += 1
        if pick(cols, H_COLS):
            score += 1
        if pick(cols, GROUP_COLS):
            score += 1
        if norm(t) in ("videos", "media", "library", "items"):
            score += 4
        if "video" in norm(t) or "media" in norm(t):
            score += 2
        if score > best_score:
            best, best_score, best_cols = t, score, cols
    if not best or best_score < 4:
        con.close()
        return [], "", tables
    rows = [dict(r) for r in con.execute('SELECT * FROM "' + best + '"')]
    con.close()
    return rows, best, best_cols


def run_import(old_root, copy=False, verbose=True):
    old_root = Path(old_root)
    NEW_MEDIA.mkdir(parents=True, exist_ok=True)
    ensure_schema()

    def say(*a):
        if verbose:
            print(*a)

    say("=" * 60)
    say("  LocalReels - Purani Library Register (IN-PLACE)")
    say("=" * 60)
    say("  Old root :", old_root)
    say("  New DB   :", NEW_DB)

    dbs = sorted(list(old_root.glob("*.sqlite3")) + list(old_root.glob("*.db")),
                 key=lambda p: (0 if "localreels" in p.name.lower() else 1, -p.stat().st_size))
    db_file = dbs[0] if dbs else None
    say("  Old DB   :", db_file or "(nahi mila - sirf folders scan honge)")

    src_dirs = [d for d in old_root.iterdir() if d.is_dir() and
                any(k in d.name.lower() for k in ("media", "import", "video", "download"))]
    if (old_root / "media").is_dir() and (old_root / "media") not in src_dirs:
        src_dirs.insert(0, old_root / "media")
    if not src_dirs:
        src_dirs = [old_root]
    poster_dirs = [d for d in old_root.iterdir() if d.is_dir() and
                   any(k in d.name.lower() for k in ("poster", "thumb", "preview", "media", "import"))]
    say("  Video dirs :", ", ".join(d.name for d in src_dirs))
    say("  Poster dirs:", ", ".join(d.name for d in poster_dirs) or "-")

    files, by_name, by_stem, by_norm = index_files(src_dirs, VIDEO_EXTS)
    pfiles, pby_name, pby_stem, pby_norm = index_files(poster_dirs, IMG_EXTS)
    say("  Mile: " + str(len(files)) + " video files, " + str(len(pfiles)) + " posters")

    rows, table, cols = read_old_rows(db_file)
    if table:
        say("  Table    : '" + table + "' (" + str(len(rows)) + " rows)")
        say("  Columns  : " + ", ".join(cols))
    elif db_file:
        say("  [!] Koi video table detect nahi hui - folders se import hoga")

    c_id, c_title = pick(cols, ID_COLS), pick(cols, TITLE_COLS)
    c_grp, c_tags = pick(cols, GROUP_COLS), pick(cols, TAG_COLS)
    c_h, c_dur = pick(cols, H_COLS), pick(cols, DUR_COLS)
    c_path, c_file = pick(cols, PATH_COLS), pick(cols, FILE_COLS)
    c_post, c_url, c_date = pick(cols, POSTER_COLS), pick(cols, URL_COLS), pick(cols, DATE_COLS)

    roots = [old_root] + src_dirs + poster_dirs
    con = sqlite3.connect(NEW_DB, timeout=30)
    con.row_factory = sqlite3.Row
    used = set()
    imported = orphan = missing = posters = copied = 0

    def store(vid, title, grp, tags, height, dur, vpath, ppath, url, created):
        nonlocal posters, copied
        vid = safe_id(vid)
        while con.execute("SELECT 1 FROM videos WHERE id=?", (vid,)).fetchone():
            ex = con.execute("SELECT path FROM videos WHERE id=?", (vid,)).fetchone()
            if ex and ex["path"] and Path(ex["path"]).resolve() == vpath.resolve():
                return False
            vid = vid[:50] + "_" + uuid.uuid4().hex[:4]
        fname = vpath.name
        if copy:
            dest = NEW_MEDIA / (vid + vpath.suffix.lower())
            if not dest.is_file():
                shutil.copy2(vpath, dest)
                copied += 1
            vpath, fname = dest, dest.name
            if ppath and ppath.is_file():
                pdest = NEW_MEDIA / (vid + ".jpg")
                if not pdest.is_file():
                    shutil.copy2(ppath, pdest)
                ppath = pdest
        con.execute(
            "INSERT OR REPLACE INTO videos(id,title,grp,tags,height,dur,filename,created_at,source_url,path,poster_path,old_id) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (vid, str(title or pretty(vpath.stem))[:200], str(grp or "Imported")[:60],
             json.dumps(tags or []), int(height or 0), float(dur or 0), fname,
             parse_date(created), str(url or ""), str(vpath.resolve()),
             str(ppath.resolve()) if ppath else "", str(vid)))
        if ppath:
            posters += 1
        used.add(vpath.resolve())
        return True

    for r in rows:
        vpath = resolve(r.get(c_path) if c_path else None, roots)
        if vpath is None and c_file:
            key = str(r.get(c_file) or "").lower()
            vpath = by_name.get(key) or by_stem.get(Path(key).stem.lower()) or by_norm.get(norm(Path(key).stem))
        rid = str(r.get(c_id) or "") if c_id else ""
        if vpath is None and rid:
            vpath = by_stem.get(rid.lower()) or by_norm.get(norm(rid))
        title = str(r.get(c_title) or "") if c_title else ""
        if vpath is None and title:
            vpath = by_norm.get(norm(title))
        if vpath is None:
            missing += 1
            continue
        ppath = resolve(r.get(c_post) if c_post else None, roots)
        if ppath is None:
            ppath = (pby_stem.get(vpath.stem.lower()) or pby_name.get(vpath.stem.lower() + ".jpg")
                     or pby_stem.get(rid.lower()) or pby_norm.get(norm(rid)) or pby_norm.get(norm(title)))
        if store(rid or vpath.stem, title or pretty(vpath.stem),
                 r.get(c_grp) if c_grp else "Imported",
                 parse_tags(r.get(c_tags)) if c_tags else [],
                 parse_h(r.get(c_h)) if c_h else 0,
                 parse_dur(r.get(c_dur)) if c_dur else 0,
                 vpath, ppath, r.get(c_url) if c_url else "", r.get(c_date) if c_date else None):
            imported += 1

    for f in files:
        if f.resolve() in used:
            continue
        ppath = pby_stem.get(f.stem.lower()) or pby_norm.get(norm(f.stem))
        if store(f.stem, pretty(f.stem), "Imported", [], 0, 0, f, ppath, "", None):
            orphan += 1

    con.commit()
    total = con.execute("SELECT COUNT(*) n FROM videos").fetchone()["n"]
    con.close()

    say("-" * 60)
    say("  DB se register    : " + str(imported))
    say("  Extra files       : " + str(orphan))
    say("  Posters mile      : " + str(posters))
    if copy:
        say("  Copy ki gayi files: " + str(copied))
    if missing:
        say("  Skip (file missing): " + str(missing))
    say("  TOTAL library mein: " + str(total))
    say("=" * 60)
    return {"imported": imported, "orphan": orphan, "posters": posters,
            "missing": missing, "total": total, "table": table}


if __name__ == "__main__":
    args = sys.argv[1:]
    root = Path(os.environ.get("LOCALREELS_OLD_ROOT") or r"C:\LocalReels")
    do_copy = "--copy" in args
    for i, a in enumerate(args):
        if a in ("--old", "--root") and i + 1 < len(args):
            root = Path(args[i + 1])
    if not root.is_dir():
        print("[X] Folder nahi mila: " + str(root))
        sys.exit(1)
    run_import(root, copy=do_copy)
