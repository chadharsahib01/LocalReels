@echo off
setlocal EnableExtensions
cd /d "%~dp0"
title LocalReels v7

set "LOCALREELS_MEDIA_DIR=C:\LocalReels\media"
set "LOCALREELS_OLD_ROOT=C:\LocalReels"
set "LOCALREELS_AUTH=nayi_app_secret_123"
set "LOCALREELS_PORT=8000"

echo ===============================================
echo   LocalReels v7
echo   Media : %LOCALREELS_MEDIA_DIR%   (in-place)
echo ===============================================

if not exist "%LOCALREELS_MEDIA_DIR%" mkdir "%LOCALREELS_MEDIA_DIR%"
if exist "venv\Scripts\activate.bat" call "venv\Scripts\activate.bat"

python -m pip install -q -r requirements.txt

set "LANIP=127.0.0.1"
for /f "delims=" %%i in ('python -c "import socket;s=socket.socket(socket.AF_INET,socket.SOCK_DGRAM);s.connect(('8.8.8.8',80));print(s.getsockname()[0]);s.close()" 2^>nul') do set "LANIP=%%i"

netsh advfirewall firewall add rule name="LocalReels 8000" dir=in action=allow protocol=TCP localport=%LOCALREELS_PORT% >nul 2>&1

echo.
echo   PC    : http://127.0.0.1:%LOCALREELS_PORT%
echo   Phone : http://%LANIP%:%LOCALREELS_PORT%
echo.

start "" /min cmd /c "timeout /t 4 /nobreak >nul & start "" http://127.0.0.1:%LOCALREELS_PORT%"

python -m uvicorn backend.server:app --host 0.0.0.0 --port %LOCALREELS_PORT% --reload --reload-dir backend
pause