@echo off
chcp 65001 >nul
title MySVN - 一键上传 GitHub
cd /d "%~dp0"

echo ========================================
echo   MySVN - 一键上传 GitHub
echo ========================================
echo.

:: 检查 git 是否可用
where git >nul 2>&1
if %errorlevel% neq 0 (
    echo [错误] 未找到 git 命令，请先安装 Git
    pause
    exit /b 1
)

:: 显示当前状态
echo [1/4] 正在检查仓库状态...
echo.
git status --short
echo.

:: 检查是否有文件变动
set "has_changes=0"
for /f "tokens=*" %%a in ('git status --porcelain') do set "has_changes=1"
if "%has_changes%"=="0" (
    echo 没有检测到任何文件变动，无需提交。
    pause
    exit /b 0
)

:: 获取默认提交信息（日期+时间）
for /f "tokens=2 delims==" %%i in ('wmic os get localdatetime /value ^| find "="') do set dt=%%i
set default_msg=update %dt:~0,4%-%dt:~4,2%-%dt:~6,2% %dt:~8,2%:%dt:~10,2%

:: 输入提交信息
set /p commit_msg=请输入提交信息（直接回车使用默认）: 
if "%commit_msg%"=="" set commit_msg=%default_msg%
echo.

:: 添加所有文件
echo [2/4] 正在添加文件到暂存区...
git add .
if %errorlevel% neq 0 (
    echo [错误] git add 失败
    pause
    exit /b 1
)
echo 完成
echo.

:: 提交
echo [3/4] 正在提交...
git commit -m "%commit_msg%"
if %errorlevel% neq 0 (
    echo [错误] git commit 失败
    pause
    exit /b 1
)
echo 完成
echo.

:: 推送到远程
echo [4/4] 正在推送到 GitHub（可能需要输入账号密码）...
echo.
git push origin
if %errorlevel% neq 0 (
    echo [注意] 推送失败，可能原因：
    echo   1. 需要先 git pull 更新本地分支
    echo   2. 网络连接不稳定
    echo   3. 没有推送权限
    echo.
    echo 尝试先拉取再推送...
    echo.
    git pull origin --rebase
    if %errorlevel% equ 0 (
        echo 拉取成功，重新推送中...
        git push origin
    ) else (
        echo [错误] 请手动处理冲突
        pause
        exit /b 1
    )
)
echo.
echo ========================================
echo  ✓ 已成功上传到 GitHub！
echo  提交信息: %commit_msg%
echo ========================================
echo.
pause
