@echo off
set HF_HUB_OFFLINE=1
set JARVIS_WHISPER_MODEL=base.en
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
set "PYTHON_EXE=.venv\Scripts\python.exe"
if not exist "%PYTHON_EXE%" (
    echo [ERROR] Jarvis runtime is missing %PYTHON_EXE%.
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
"%PYTHON_EXE%" ui\app.py
pause
