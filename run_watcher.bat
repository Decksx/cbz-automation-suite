@echo off
echo ============================================
echo  CBZ Watcher - Setup and Launch
echo ============================================

:: Change to the directory where this .bat file lives
cd /d "%~dp0"

:: Check Python is installed
python --version >nul 2>&1
if errorlevel 1 (
    echo ERROR: Python not found. Please install Python from https://python.org
    pause
    exit /b 1
)

:: Install required dependency
echo Installing dependencies...
pip install watchdog --quiet

echo.
echo Starting CBZ Watcher...
echo Press Ctrl+C to stop.
echo.

python cbz_watcher.py

pause