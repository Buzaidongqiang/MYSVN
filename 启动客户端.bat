@echo off
chcp 65001 >nul
title MySVN Client
cd /d "%~dp0"

echo ================================================
echo   Starting My-SVN Client (Python mode)...
echo ================================================
echo.

echo [START] python client.py
echo.
python -c "import flask, requests, PyQt5" 2>nul
if %errorlevel% neq 0 (
    echo [INSTALL] Dependencies missing, installing...
    pip install -r requirements.txt -q
)
python client.py

pause
