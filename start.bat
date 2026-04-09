@echo off
cd /d "%~dp0"
python run.py
if %errorlevel% neq 0 (
    echo.
    echo Failed to start. Press any key to close...
    pause >nul
)
