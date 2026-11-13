@echo off
rem Double-click to start. First run: creates a private Python environment and installs everything.
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
    where py >nul 2>nul && (set PY=py -3) || (set PY=python)
    echo First start: setting up, this takes about a minute...
    %PY% -m venv .venv || (echo Python 3.9+ is required: https://www.python.org/downloads/ & pause & exit /b 1)
    ".venv\Scripts\python.exe" -m pip install -q -r requirements.txt || (echo Install failed. & pause & exit /b 1)
)
".venv\Scripts\python.exe" led_visualizer.py %*
echo.
pause
