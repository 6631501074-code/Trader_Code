@echo off
chcp 65001 >nul
set PYTHONUTF8=1

echo ============================================================
echo   AI Trading Dashboard - Real-Time Web Server
echo ============================================================
echo.
echo Starting server at: http://localhost:8000
echo Press Ctrl+C to stop
echo.

cd /d "%~dp0"
python -m uvicorn web.server:app --host 0.0.0.0 --port 8000 --log-level info

pause
