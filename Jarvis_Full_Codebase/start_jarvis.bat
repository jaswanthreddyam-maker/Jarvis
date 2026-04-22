@echo off
set JARVIS_FORCE_MOCK_TTS=1
cd /d "%~dp0"
if exist ".venv\Scripts\python.exe" (
    ".venv\Scripts\python.exe" scripts\bootstrap_env.py
) else (
    py -3 scripts\bootstrap_env.py 2>nul || python scripts\bootstrap_env.py
)
if errorlevel 1 (
    echo [ERROR] Jarvis could not repair the Python environment.
    pause
    exit /b 1
)
".venv\Scripts\python.exe" ui\app.py
pause
