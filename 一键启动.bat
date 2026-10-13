@echo off
chcp 65001 >nul
title 个人求职助手 - 一键启动
cd /d "%~dp0"

echo ==========================================
echo    个人求职助手（JobSeeker Assistant）
echo ==========================================
echo.

REM ---- 1. 定位 Python（优先 python，其次 py -3）----
set "PY=python"
%PY% --version >nul 2>&1
if errorlevel 1 (
    set "PY=py -3"
    %PY% --version >nul 2>&1
    if errorlevel 1 (
        echo [错误] 未检测到 Python。
        echo        请安装 Python 3.11 及以上版本，并勾选 "Add python.exe to PATH"。
        echo        下载地址: https://www.python.org/downloads/
        echo.
        pause
        exit /b 1
    )
)

echo [1/3] 检查运行依赖...
%PY% -c "import PySide6, playwright, openai, apscheduler, pypdf, docx" >nul 2>&1
if errorlevel 1 (
    echo       缺少依赖，正在安装（首次运行需要几分钟，请耐心等待）...
    %PY% -m pip install -r requirements.txt
    if errorlevel 1 (
        echo.
        echo [错误] 依赖安装失败，请检查网络后重新双击本脚本重试。
        pause
        exit /b 1
    )
) else (
    echo       依赖已就绪
)

echo [2/3] 检查浏览器内核...
if not exist "%LOCALAPPDATA%\ms-playwright\chromium-1208\chrome-win64\chrome.exe" (
    echo       缺少 chromium 内核，正在下载（约 170MB，速度取决于网络）...
    %PY% -m playwright install chromium
    if errorlevel 1 (
        echo.
        echo [提示] chromium 下载失败，可稍后手动执行:  python -m playwright install chromium
        pause
        exit /b 1
    )
) else (
    echo       浏览器内核已就绪
)

echo [3/3] 启动应用...
echo.
%PY% main.py
set "EXIT_CODE=%errorlevel%"

echo.
if "%EXIT_CODE%"=="0" (
    echo 应用已正常退出。
) else (
    echo [提示] 应用异常退出（代码 %EXIT_CODE%）。
    echo        请查看上方错误信息，或打开 data\logs\app.log 查看详细日志。
)
echo.
pause