@echo off
setlocal enabledelayedexpansion
chcp 65001 >nul
title MySVN - Upload to GitHub
cd /d "%~dp0"

echo ========================================
echo   MySVN - Upload to GitHub
echo ========================================
echo.

:: Check git availability
where git >nul 2>&1
if !errorlevel! neq 0 (
    echo [ERROR] Git not found, please install Git first
    pause
    exit /b 1
)

:: Show current status
echo [1/4] Checking repo status...
echo.
git status --short
echo.

:: Check if there are changes
set "has_changes=0"
for /f "tokens=*" %%a in ('git status --porcelain') do set "has_changes=1"
if "!has_changes!"=="0" (
    echo No changes detected, nothing to commit.
    pause
    exit /b 0
)

:: Get default commit message (use PowerShell)
for /f %%i in ('powershell -NoProfile -Command "Get-Date -Format yyyy-MM-dd_HH:mm"') do set "default_msg=update %%i"

:: Ask for commit message
set /p commit_msg=Enter commit message (Enter for default): 
if "!commit_msg!"=="" set "commit_msg=!default_msg!"
echo.

:: Add all files
echo [2/4] Staging files...
git add .
if !errorlevel! neq 0 (
    echo [ERROR] git add failed
    pause
    exit /b 1
)
echo Done
echo.

:: Commit
echo [3/4] Committing...
git commit -m "!commit_msg!"
if !errorlevel! neq 0 (
    echo [ERROR] git commit failed
    pause
    exit /b 1
)
echo Done
echo.

:: Push to remote
echo [4/4] Pushing to GitHub...
echo.
git push origin
if !errorlevel! neq 0 (
    echo [NOTE] Push failed, possible reasons:
    echo   1. Need to pull first
    echo   2. Network issue
    echo   3. No permission
    echo.
    echo Trying pull first...
    echo.
    git pull origin --rebase
    if !errorlevel! equ 0 (
        echo Pull success, pushing again...
        git push origin
    ) else (
        echo [ERROR] Please resolve conflicts manually
        pause
        exit /b 1
    )
)
echo.
echo ========================================
echo   Successfully uploaded to GitHub!
echo   Message: !commit_msg!
echo ========================================
echo.
pause
