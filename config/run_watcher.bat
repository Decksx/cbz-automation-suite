@echo off
cd /d "%~dp0.."

:: Install watchdog if not present
pip show watchdog >nul 2>&1 || pip install watchdog

:: Run the watcher from the repo root so relative paths resolve correctly
python scripts\cbz_watcher.py
pause
