@echo off
cd /d "%~dp0"
title GIAM SAT BAI NO 24/24

set "PLAYWRIGHT_BROWSERS_PATH=%~dp0ms-playwright"

if not exist "%~dp0ms-playwright" (
    echo [INFO] Dang kiem tra va cai dat Chromium cho Playwright...
    if exist "%~dp0python_runtime\python.exe" (
        "%~dp0python_runtime\python.exe" -m playwright install chromium
    ) else (
        python -m playwright install chromium
    )
)

if exist "%~dp0python_runtime\python.exe" (
    "%~dp0python_runtime\python.exe" webui_giam_sat.py
) else (
    python webui_giam_sat.py
)
pause
