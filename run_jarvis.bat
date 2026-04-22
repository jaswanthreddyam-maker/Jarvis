@echo off
:: Jarvis Desktop Launcher
:: Runs from source using the project virtual environment.
:: For packaged builds, use dist/Jarvis.exe directly.

cd /d "%~dp0"

:: Check for compiled exe first
if exist "dist\Jarvis.exe" (
    echo [BOOT] Launching Jarvis.exe...
    start "" "dist\Jarvis.exe" %*
    exit /b 0
)

:: Repair or create the project runtime before launch
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

if exist ".venv\Scripts\pythonw.exe" (
    echo [BOOT] Launching from repaired .venv (background)...
    start "" ".venv\Scripts\pythonw.exe" ui/app.py %*
) else if exist ".venv\Scripts\python.exe" (
    echo [BOOT] Launching from repaired .venv (console)...
    start "" ".venv\Scripts\python.exe" ui/app.py %*
) else (
    echo [ERROR] Jarvis repaired the environment, but no launcher was created.
    pause
)
