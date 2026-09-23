@echo off
chcp 65001 > nul
set PYTHONUTF8=1
title Multi-Agent Trading System (Gemma 3 + XM MT5)

echo ============================================================
echo   Multi-Agent Trading System (XM MT5 + Gemma 3 4B)
echo ============================================================
echo.

cd /d "%~dp0"

echo [*] ตรวจสอบโปรแกรม Ollama...
tasklist /fi "imagename eq ollama.exe" 2>nul | find /i "ollama.exe" >nul
if errorlevel 1 (
    echo [!] กำลังเปิด Ollama...
    start "" "ollama" app
    timeout /t 3 >nul
) else (
    echo [+] Ollama กำลังทำงานอยู่แล้ว
)

echo.
echo [*] กรุณาตรวจสอบว่าได้เปิดโปรแกรม XM MT5 และเปิดปุ่ม Algo Trading แล้ว
echo [*] กำลังเริ่มระบบ Multi-Agent Trading...
echo ============================================================
echo.

python main.py

echo.
echo ระบบหยุดทำงาน
pause
