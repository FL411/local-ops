@echo off
setlocal
title local-ops Console
cd /d "%~dp0"

REM Probe order: py launcher (latest) -> python on PATH.
REM Version check only (do not import psutil here): python.exe may see
REM user-site copies that pythonw cannot. ensure-runtime installs into
REM THIS interpreter so pythonw can import psutil.
set "PY="

:try_py
where py >nul 2>nul
if errorlevel 1 goto :try_python
py -3 -c "import sys;raise SystemExit(0 if sys.version_info >= (3,12) else 1)" >nul 2>nul
if errorlevel 1 goto :try_python
set "PY=py -3"
goto :resolve_pythonw

:try_python
where python >nul 2>nul
if errorlevel 1 goto :maybe_old
python -c "import sys;raise SystemExit(0 if sys.version_info >= (3,12) else 1)" >nul 2>nul
if errorlevel 1 goto :old_python
set "PY=python"
goto :resolve_pythonw

:maybe_old
where py >nul 2>nul
if not errorlevel 1 goto :old_python
goto :no_python

:old_python
echo [ERROR] Python 3.12 or newer is required, but found an older version.
echo Please install Python 3.12+ from https://www.python.org/downloads/
pause
exit /b 1

:no_python
echo [ERROR] Python 3.12+ not found.
echo Please install Python from https://www.python.org/downloads/
echo and make sure "Add python.exe to PATH" is checked.
pause
exit /b 1

:install_failed
echo [ERROR] psutil install failed.
echo Run manually: python -m pip install "psutil>=7.2"
pause
exit /b 1

:resolve_pythonw
%PY% -c "import sys;print(sys.executable)" > "%TEMP%\localops_pyexe.txt" 2>nul
set /p PYEXE=<"%TEMP%\localops_pyexe.txt"
del "%TEMP%\localops_pyexe.txt" >nul 2>nul
if not defined PYEXE goto :no_python

:resolve_pythonw_from_exe
set "PYW=%PYEXE:\python.exe=\pythonw.exe%"
if not exist "%PYW%" set "PYW=%PYEXE%"

:ensure_runtime
"%PYEXE%" "%~dp0launcher_check.py" ensure-runtime
if errorlevel 1 goto :install_failed

:probe
set "LSTATE="
set "LPORT="
"%PYEXE%" "%~dp0launcher_check.py" status > "%TEMP%\localops_status.txt" 2>nul
set /p LSTATUS=<"%TEMP%\localops_status.txt"
del "%TEMP%\localops_status.txt" >nul 2>nul
if not defined LSTATUS set "LSTATUS=STOPPED"
if "%LSTATUS%"=="STOPPED" goto :launch
for /f "tokens=1,2" %%s in ("%LSTATUS%") do (
  set "LSTATE=%%s"
  set "LPORT=%%t"
)
if "%LSTATE%"=="STALE" goto :launch
if not "%LSTATE%"=="RUNNING" goto :launch
if not defined LPORT goto :launch
REM Already running: open browser directly (tray owns open/restart/stop)
"%PYEXE%" "%~dp0launcher_check.py" open %LPORT%
exit /b 0

:launch
"%PYW%" server.py --log-to-file
echo local-ops started in background.
echo Browser will open automatically.
echo See README for log location.
