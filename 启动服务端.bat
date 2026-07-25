@echo off
chcp 65001 >nul
title MySVN Server
cd /d "%~dp0"

echo ================================================
echo   Starting My-SVN Server...
echo ================================================
echo.

if exist "MySVN-Server.exe" (
    echo [TRY] MySVN-Server.exe
    MySVN-Server.exe
    if %errorlevel% equ 0 goto :end
    echo [FAIL] exe failed, falling back to python...
    echo.
)

echo [START] python server.py
echo.
python -c "import flask, requests" 2>nul
if %errorlevel% neq 0 (
    echo [INSTALL] Dependencies missing, installing...
    pip install -r requirements.txt -q
)
python server.py

:end
pause
