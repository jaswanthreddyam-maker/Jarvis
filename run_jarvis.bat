@echo off
:: Jarvis Desktop Launcher
:: Runs from source using the project virtual environment.
:: For packaged builds, use dist/Jarvis.exe directly.

cd /d "%~dp0"
set "PYTHON_EXE=.venv\Scripts\python.exe"

:: Check for compiled exe first
if exist "dist\Jarvis.exe" (
    if exist "%PYTHON_EXE%" (
        echo [BOOT] Stopping existing Jarvis processes...
        "%PYTHON_EXE%" scripts\stop_existing_jarvis.py
        if errorlevel 1 (
            echo [ERROR] Could not stop existing Jarvis processes.
            pause
            exit /b 1
        )
        echo [BOOT] Launching backend service...
        start "Jarvis Backend" "%PYTHON_EXE%" -m jarvis.api
        "%PYTHON_EXE%" scripts\wait_for_backend.py --timeout 60
        if errorlevel 1 (
            echo [ERROR] Backend startup failed. Check the Jarvis Backend window and logs\jarvis.error.log.
            pause
            exit /b 1
        )
    )
    echo [BOOT] Launching Jarvis.exe...
    start "" "dist\Jarvis.exe" %*
    exit /b 0
)

:: Repair or create the project runtime before launch
if exist "%PYTHON_EXE%" (
    "%PYTHON_EXE%" scripts\bootstrap_env.py
) else (
    py -3 scripts\bootstrap_env.py 2>nul || python scripts\bootstrap_env.py
)

if errorlevel 1 (
    echo [ERROR] Jarvis could not repair the Python environment.
    pause
    exit /b 1
)

echo [BOOT] Stopping existing Jarvis processes...
"%PYTHON_EXE%" scripts\stop_existing_jarvis.py
if errorlevel 1 (
    echo [ERROR] Could not stop existing Jarvis processes.
    pause
    exit /b 1
)
echo [BOOT] Launching backend service...
start "Jarvis Backend" "%PYTHON_EXE%" -m jarvis.api
"%PYTHON_EXE%" scripts\wait_for_backend.py --timeout 60
if errorlevel 1 (
    echo [ERROR] Backend startup failed. Check the Jarvis Backend window and logs\jarvis.error.log.
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
