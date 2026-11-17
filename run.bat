@echo off
cd /d "%~dp0"

set "PYEXE=%~dp0python\python.exe"
if not exist "%PYEXE%" set "PYEXE=python"

echo Installing dependencies...
"%PYEXE%" -m pip install -r requirements.txt --quiet
if errorlevel 1 (
  echo Failed to install dependencies.
  pause
  exit /b 1
)

echo.
echo Starting Bflow...
echo Visit: http://127.0.0.1:8000/
echo.

"%PYEXE%" main.py

echo.
echo Server stopped.
pause
