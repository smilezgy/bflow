@echo off
setlocal enabledelayedexpansion
cd /d "%~dp0"

rem ===============================================================
rem  Locate a working Python interpreter.
rem
rem  Why this is not simply "python":
rem  Windows ships a 0-byte stub at "WindowsApps\python.exe". It only
rem  prints "Python was not found" and exits with code 9009, so calling
rem  it blindly looks exactly like "Python is not installed". We must
rem  actually execute a candidate and confirm it can run code.
rem
rem  Search order:
rem    0. PYTHON env var            (explicit override)
rem    1. <project>\python\python.exe
rem    2. py -3 launcher
rem    3. python / python3 on PATH  (each candidate is executed to verify)
rem    4. common install locations
rem ===============================================================

set "PYEXE="

rem 0) explicit override
if defined PYTHON (
  if exist "%PYTHON%" (
    for %%I in ("%PYTHON%") do set "PYEXE=%%~fI"
  ) else (
    echo [X] PYTHON points to a missing file: "%PYTHON%"
    pause
    exit /b 1
  )
)

rem 1) bundled interpreter inside the project folder
if not defined PYEXE (
  if exist "%~dp0python\python.exe" set "PYEXE=%~dp0python\python.exe"
)

rem 2) py launcher
if not defined PYEXE (
  for /f "usebackq delims=" %%I in (`py -3 -c "import sys;print(sys.executable)" 2^>nul`) do (
    if not defined PYEXE set "PYEXE=%%I"
  )
)

rem 3) python on PATH -- verify each candidate by running it
if not defined PYEXE (
  for %%C in (python python3) do (
    if not defined PYEXE (
      for /f "usebackq delims=" %%I in (`where %%C 2^>nul`) do (
        if not defined PYEXE (
          "%%I" -c "import sys" >nul 2>&1 && set "PYEXE=%%I"
        )
      )
    )
  )
)

rem 4) common install locations
if not defined PYEXE (
  for /f "usebackq delims=" %%I in (`dir /b /s "%LOCALAPPDATA%\Programs\Python\Python3*\python.exe" 2^>nul`) do (
    if not defined PYEXE set "PYEXE=%%I"
  )
)
if not defined PYEXE (
  for /f "usebackq delims=" %%I in (`dir /b /s "C:/Program Files/Python3*/python.exe" 2^>nul`) do (
    if not defined PYEXE set "PYEXE=%%I"
  )
)

if not defined PYEXE (
  echo.
  echo [X] No usable Python interpreter was found.
  echo.
  echo     The "Microsoft Store" python is only a 0-byte stub and cannot run.
  echo     Pick one of these:
  echo.
  echo       1^) Install real Python ^(recommended^)
  echo            https://www.python.org/downloads/
  echo            Tick "Add python.exe to PATH" during setup.
  echo.
  echo       2^) Or drop a Python into the project folder, it is auto-detected:
  echo            ^<project^>\python\python.exe
  echo.
  echo       3^) Or point to an interpreter explicitly, then run this script:
  echo            set PYTHON=C:/path/to/python.exe
  echo            run.bat
  echo.
  pause
  exit /b 1
)

echo Using Python: %PYEXE%
"%PYEXE%" -c "import sys;print('Version:', sys.version.split()[0])"

rem ===============================================================
rem  Install dependencies
rem ===============================================================
echo.
echo Installing dependencies...
if not exist "requirements.txt" (
  echo [!] requirements.txt not found next to this script.
  echo     Keep run.bat inside the project folder.
  pause
  exit /b 1
)
"%PYEXE%" -m pip install -r requirements.txt --quiet
if errorlevel 1 (
  echo.
  echo [!] Dependency installation failed.
  echo     If this is a network issue, run it manually:
  echo         "%PYEXE%" -m pip install -r requirements.txt
  echo     Or skip installation and start with what is already installed:
  echo         "%PYEXE%" main.py
  pause
  exit /b 1
)

rem ===============================================================
rem  Start
rem ===============================================================
rem  Parse --host / --port for the display line only.
rem  The real arguments are passed through to main.py untouched.
set "SHOWHOST=127.0.0.1"
set "SHOWPORT=8000"
if not "%BFLOW_HOST%"=="" set "SHOWHOST=%BFLOW_HOST%"
if not "%BFLOW_PORT%"=="" set "SHOWPORT=%BFLOW_PORT%"
set "PREV="
for %%A in (%*) do (
  if "!PREV!"=="--host" set "SHOWHOST=%%A"
  if "!PREV!"=="--port" set "SHOWPORT=%%A"
  call :set_show "%%A"
  set "PREV=%%A"
)

echo.
echo Starting Bflow...
echo Open: http://%SHOWHOST%:%SHOWPORT%/
echo Close this window to stop the server.
echo.

"%PYEXE%" main.py %*

echo.
echo Server stopped.
pause

rem  Parse "--host=xxx" / "--port=xxx" into display variables.
: set_show
set "ARG=%~1"
if not "%ARG:~0,7%"=="--host=" goto :chk_port
set "SHOWHOST=%ARG:~7%"
goto :eof
:chk_port
if not "%ARG:~0,7%"=="--port=" goto :eof
set "SHOWPORT=%ARG:~7%"
goto :eof
