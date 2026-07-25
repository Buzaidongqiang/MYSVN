@echo off
chcp 65001 >nul
title MySVN Pack Tool
cd /d "%~dp0"

echo ================================================
echo   My-SVN Pack Tool
echo ================================================
echo.

echo [1/2] Running pack.py (installs deps + packs exe)...
echo      This will take 8-12 minutes total.
echo.

python pack.py

if %errorlevel% neq 0 (
    echo.
    echo [ERROR] Pack failed! Please try manually:
    echo   1. cd /d D:\Mysvn
    echo   2. pip install -r requirements.txt
    echo   3. pip install pyinstaller
    echo   4. pyinstaller --onefile --console --distpath . server.py -n MySVN-Server.exe
    echo   5. pyinstaller --onefile --windowed --distpath . client.py -n MySVN-Client.exe
    pause
    exit /b 1
)

echo.
echo [OK] All done! You can now run:
echo   启动服务端.bat  or  MySVN-Server.exe
echo   启动客户端.bat  or  MySVN-Client.exe
echo.
pause
