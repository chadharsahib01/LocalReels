@echo off
setlocal EnableExtensions
title LocalReels v2 - Auto Setup
cd /d "%~dp0"
cls
echo.
echo  ==========================================
echo    LocalReels v2  -  Auto File Organizer
echo  ==========================================
echo    Folder: %CD%
echo.

set "PYEXE="
if exist "%~dp0venv\Scripts\python.exe" set "PYEXE=%~dp0venv\Scripts\python.exe"
if not defined PYEXE ( where py >nul 2>&1 && set "PYEXE=py" )
if not defined PYEXE ( where python >nul 2>&1 && set "PYEXE=python" )
if not defined PYEXE (
    echo  [X] Python nahi mila. Pehle Python install karo, phir dobara chalao.
    echo.
    pause
    exit /b 1
)

set "TMPPY=%TEMP%\lr_setup_%RANDOM%%RANDOM%.py"

powershell -NoProfile -ExecutionPolicy Bypass -Command ^
 "$l=Get-Content -LiteralPath '%~f0' -Encoding UTF8; $i=[array]::IndexOf($l,'### PYTHON_PAYLOAD ###'); if($i -lt 0){exit 1}; $l[($i+1)..($l.Count-1)] | Set-Content -LiteralPath '%TMPPY%' -Encoding UTF8"

if not exist "%TMPPY%" (
    echo  [X] Setup payload extract nahi hua. PowerShell available hai?
    pause
    exit /b 1
)

"%PYEXE%" "%TMPPY%" "%CD%"
set "RC=%ERRORLEVEL%"
del "%TMPPY%" >nul 2>&1
echo.
pause
exit /b %RC%

### PYTHON_PAYLOAD ###
import os
import sys
import time
import shutil
from pathlib import Path

ROOT = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else Path.cwd()
STAMP = time.strftime("%Y%m%d-%H%M%S")
BK = ROOT / "_backup" / STAMP
SKIP = {"venv", "env", ".venv", "node_modules", ".git", "__pycache__",
        "site-packages", "_backup", ".idea", ".vscode", "dist", "build",
        ".pytest_cache", ".mypy_cache"}

stats = {"move": 0, "new": 0, "fix": 0, "bak": 0, "ok": 0}
missing = []
warns = []


def say(tag, msg):
    print("  [%-4s] %s" % (tag, msg))


def head(t):
    print("\n" + t)
    print("  " + "-" * (len(t) + 2))


def backup(p):
    try:
        rel = p.relative_to(ROOT)
    except ValueError:
        rel = Path(p.name)
    dest = BK / rel
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(str(p), str(dest))
    stats["bak"] += 1
    say("BAK", str(rel))


def walk_files(name):
    found = []
    for dirpath, dirnames, filenames in os.walk(str(ROOT)):
        dirnames[:] = [d for d in dirnames if d.lower() not in SKIP]
        for f in filenames:
            if f.lower() == name.lower():
                found.append(Path(dirpath) / f)
    return found


def same(a, b):
    try:
        return a.read_bytes() == b.read_bytes()
    except Exception:
        return False


def place(filename, rel):
    """File ko kahin se bhi dhundh ke uske sahi path pe rakho."""
    target = ROOT / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    cands = [p for p in walk_files(filename) if p.resolve() != target.resolve()]

    if target.exists():
        if not cands:
            stats["ok"] += 1
            say("OK", rel)
            return True
        best = max(cands, key=lambda p: p.stat().st_mtime)
        if same(best, target):
            for p in cands:
                backup(p)
                p.unlink()
            stats["ok"] += 1
            say("OK", rel + "  (duplicate hataya)")
            return True
        backup(target)
        shutil.move(str(best), str(target))
        stats["move"] += 1
        say("MOVE", rel + "  <-  " + str(best.relative_to(ROOT)))
        for p in cands:
            if p.exists():
                backup(p)
                p.unlink()
        return True

    if cands:
        best = max(cands, key=lambda p: p.stat().st_mtime)
        shutil.move(str(best), str(target))
        stats["move"] += 1
        say("MOVE", rel + "  <-  " + str(best.relative_to(ROOT)))
        for p in cands:
            if p.exists():
                backup(p)
                p.unlink()
        return True

    missing.append(rel)
    say("X", rel + "   (file kahin nahi mili)")
    return False


def write_file(rel, content, nl="\n"):
    p = ROOT / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(str(p), "w", encoding="utf-8", newline=nl) as fh:
        fh.write(content)


def ensure(rel, content, nl="\n", keep_markers=None):
    """Missing ho to banao; purana ho to patch karo; theek ho to chhod do."""
    p = ROOT / rel
    if p.exists():
        cur = p.read_text(encoding="utf-8", errors="ignore")
        if keep_markers is None or all(m in cur for m in keep_markers):
            stats["ok"] += 1
            say("OK", rel)
            return
        backup(p)
        write_file(rel, content, nl)
        stats["fix"] += 1
        say("FIX", rel)
        return
    write_file(rel, content, nl)
    stats["new"] += 1
    say("NEW", rel)


REQUIREMENTS = """fastapi
uvicorn[standard]
sqlalchemy
pydantic-settings
slowapi
httpx
aiofiles
python-multipart
yt-dlp
"""

RUN_BAT = r"""@echo off
setlocal
cd /d "%~dp0"

echo ===============================================
echo   LocalReels v2 - Server Start Ho Raha Hai...
echo ===============================================

if not exist venv (
    echo [setup] venv ban raha hai...
    py -m venv venv 2>nul || python -m venv venv
)

call venv\Scripts\activate

echo [setup] dependencies check...
python -m pip install --upgrade pip >nul 2>&1
python -m pip install -q -r requirements.txt

if not exist media mkdir media

start "" /min cmd /c "timeout /t 4 /nobreak >nul & start "" http://127.0.0.1:8000"

echo [ok] UI: http://127.0.0.1:8000  (Ctrl+C to stop)
uvicorn backend.server:app --reload --host 127.0.0.1 --port 8000
pause
"""

RUN_SH = """#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

echo "LocalReels v2 - Server Start Ho Raha Hai..."

PY=python3
command -v python3 >/dev/null 2>&1 || PY=python

if [ ! -d "venv" ]; then
  "$PY" -m venv venv
fi

source venv/bin/activate

python -m pip install --upgrade pip >/dev/null 2>&1 || true
python -m pip install -q -r requirements.txt

mkdir -p media

(
  sleep 3
  URL="http://127.0.0.1:8000"
  if command -v xdg-open >/dev/null 2>&1; then xdg-open "$URL" >/dev/null 2>&1 || true
  elif command -v open >/dev/null 2>&1; then open "$URL" >/dev/null 2>&1 || true
  fi
) &

echo "[ok] UI: http://127.0.0.1:8000"
exec uvicorn backend.server:app --reload --host 127.0.0.1 --port 8000
"""

GITIGNORE = """venv/
env/
__pycache__/
*.py[cod]
.env
.env.local
reels.db
reels.db-wal
reels.db-shm
media/
downloads/
*.log
.DS_Store
Thumbs.db
.vscode/
.idea/
_backup/
.lr_write_test
"""

MANIFEST = """{
  "name": "LocalReels - Apna Feed",
  "short_name": "LocalReels",
  "description": "Apni local videos, TikTok-style reels feed mein.",
  "start_url": "/",
  "scope": "/",
  "display": "standalone",
  "orientation": "portrait",
  "background_color": "#0A0D15",
  "theme_color": "#0A0D15",
  "icons": [
    { "src": "icon.svg", "sizes": "192x192", "type": "image/svg+xml", "purpose": "any" },
    { "src": "icon.svg", "sizes": "512x512", "type": "image/svg+xml", "purpose": "any" },
    { "src": "icon.svg", "sizes": "any", "type": "image/svg+xml", "purpose": "maskable" }
  ]
}
"""

SW_JS = """const CACHE = 'localreels-v2';
const SHELL = ['/', '/index.html', '/styles.css', '/app.js', '/icon.svg', '/manifest.webmanifest'];

self.addEventListener('install', e => {
  e.waitUntil(caches.open(CACHE).then(c => c.addAll(SHELL).catch(() => {})).then(() => self.skipWaiting()));
});

self.addEventListener('activate', e => {
  e.waitUntil(caches.keys().then(keys => Promise.all(keys.filter(k => k !== CACHE).map(k => caches.delete(k)))).then(() => self.clients.claim()));
});

self.addEventListener('fetch', e => {
  const req = e.request;
  if (req.method !== 'GET') return;
  const url = new URL(req.url);
  if (url.origin !== self.location.origin) return;
  if (url.pathname.startsWith('/api/') || url.pathname.startsWith('/media/') || url.pathname.startsWith('/reels-media/')) return;
  e.respondWith(
    fetch(req).then(res => {
      if (res && res.status === 200) { const copy = res.clone(); caches.open(CACHE).then(c => c.put(req, copy)); }
      return res;
    }).catch(() => caches.match(req).then(hit => hit || caches.match('/index.html')))
  );
});
"""

ICON_SVG = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512" width="512" height="512">
  <defs>
    <linearGradient id="g" x1="0" y1="0" x2="1" y2="1">
      <stop offset="0" stop-color="#2CE5C8"/><stop offset=".5" stop-color="#4D9FFF"/><stop offset="1" stop-color="#FF3D7F"/>
    </linearGradient>
  </defs>
  <rect width="512" height="512" rx="112" fill="#0A0D15"/>
  <rect x="112" y="88" width="288" height="336" rx="56" fill="none" stroke="url(#g)" stroke-width="18"/>
  <path d="M212 176v160l140-80z" fill="url(#g)"/>
  <circle cx="368" cy="352" r="26" fill="#FF3D7F"/>
  <circle cx="144" cy="160" r="16" fill="#2CE5C8"/>
</svg>
"""

print("  Root: %s" % ROOT)

head("1. Folders")
for d in ("backend", "media"):
    p = ROOT / d
    if p.is_dir():
        say("OK", d + "/")
    else:
        p.mkdir(parents=True, exist_ok=True)
        say("NEW", d + "/")

head("2. Core files ko sahi path pe move karna")
place("index.html", "index.html")
place("styles.css", "styles.css")
place("app.js", "app.js")
place("server.py", "backend/server.py")
place("reels_api.py", "backend/reels_api.py")
place("verify_setup.py", "verify_setup.py")

head("3. Support files (missing = auto-create)")
ensure("manifest.webmanifest", MANIFEST)
ensure("sw.js", SW_JS)
ensure("icon.svg", ICON_SVG)
ensure(".gitignore", GITIGNORE)
ensure("backend/__init__.py", "")
ensure("requirements.txt", REQUIREMENTS,
       keep_markers=["python-multipart", "yt-dlp", "aiofiles"])
ensure("run.bat", RUN_BAT, nl="\r\n", keep_markers=["backend.server:app"])
ensure("run.sh", RUN_SH, nl="\n", keep_markers=["backend.server:app"])

head("4. Health check")
main_py = ROOT / "backend" / "main.py"
if main_py.is_file():
    say("OK", "backend/main.py  (chhua nahi gaya)")
else:
    warns.append("backend/main.py nahi mila - tumhara original backend zaroori hai")
    say("!!", "backend/main.py MISSING")

appjs = ROOT / "app.js"
if appjs.is_file():
    txt = appjs.read_text(encoding="utf-8", errors="ignore")
    if "video-grid" in txt or "startLivePolling" in txt:
        warns.append("app.js abhi PURANA version hai - naya v2 app.js paste karo")
        say("!!", "app.js = old version")
    else:
        say("OK", "app.js = v2 engine")

idx = ROOT / "index.html"
if idx.is_file():
    txt = idx.read_text(encoding="utf-8", errors="ignore")
    need = [n for n in ("styles.css", "app.js", "manifest.webmanifest") if n not in txt]
    if need:
        warns.append("index.html mein ye links missing: " + ", ".join(need))
        say("!!", "index.html links: " + ", ".join(need))
    else:
        say("OK", "index.html links wired")

for tool in ("ffmpeg", "ffprobe"):
    say("OK" if shutil.which(tool) else "!!",
        tool + ("" if shutil.which(tool) else "  (nahi hai - thumbnails off rahenge)"))

head("5. Final structure")
TREE = [
    ("index.html", "root"), ("styles.css", "root"), ("app.js", "root"),
    ("manifest.webmanifest", "root"), ("sw.js", "root"), ("icon.svg", "root"),
    ("requirements.txt", "root"), ("run.bat", "root"), ("run.sh", "root"),
    ("backend/__init__.py", "root"), ("backend/main.py", "root"),
    ("backend/server.py", "root"), ("backend/reels_api.py", "root"),
]
for rel, _ in TREE:
    mark = "OK " if (ROOT / rel).is_file() else "X  "
    print("    [%s] %s" % (mark, rel))
print("    [OK ] media/")
print("    [--] reels.db  (server chalne par auto-banega)")

head("Summary")
print("    moved=%d  created=%d  patched=%d  backed-up=%d  untouched=%d"
      % (stats["move"], stats["new"], stats["fix"], stats["bak"], stats["ok"]))
if stats["bak"]:
    print("    Purani files yahan safe hain: _backup/%s/" % STAMP)

core_missing = [m for m in missing if m in
                ("index.html", "styles.css", "app.js",
                 "backend/server.py", "backend/reels_api.py")]
if core_missing:
    print("\n  [!!] Ye code files nahi mili - inka content chat se paste karo:")
    for m in core_missing:
        print("       %s" % (ROOT / m))
    print("       (script khali file nahi banata, taaki app toota hua na chale)")

if warns:
    print("\n  Warnings:")
    for w in warns:
        print("    - " + w)

if not core_missing and not warns:
    print("\n  Sab ready. Ab chalao:  run.bat   ->   http://127.0.0.1:8000")
else:
    print("\n  Upar wali cheezein fix karke setup dobara chala sakte ho (idempotent hai).")

sys.exit(1 if core_missing else 0)