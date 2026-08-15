@echo off
setlocal
title local-ops Console
cd /d "%~dp0"

REM Probe order: py launcher (latest) -> python on PATH.
REM Each candidate must satisfy Python>=3.12 AND have psutil, else try next.
set "PY="

:try_py
where py >nul 2>nul
if errorlevel 1 goto :try_python
py -3 -c "import sys,psutil;raise SystemExit(0 if sys.version_info >= (3,12) else 1)" >nul 2>nul
if errorlevel 1 goto :try_python
set "PY=py -3"
goto :resolve_pythonw

:try_python
where python >nul 2>nul
if errorlevel 1 goto :install_psutil
python -c "import sys,psutil;raise SystemExit(0 if sys.version_info >= (3,12) else 1)" >nul 2>nul
if errorlevel 1 goto :install_psutil
set "PY=python"
goto :resolve_pythonw

:install_psutil
REM Here: no usable interpreter found, or version too old, or psutil missing.
REM Distinguish old-version from missing-psutil: give a clear hint for old version.
where py >nul 2>nul
if not errorlevel 1 goto :check_py_version
where python >nul 2>nul
if errorlevel 1 goto :no_python
python -c "import sys;raise SystemExit(0 if sys.version_info >= (3,12) else 1)" >nul 2>nul
if errorlevel 1 goto :old_python
goto :install_with_python

:check_py_version
py -3 -c "import sys;raise SystemExit(0 if sys.version_info >= (3,12) else 1)" >nul 2>nul
if errorlevel 1 goto :old_python

:install_psutil_with_py
echo [INFO] Installing psutil with py launcher ...
py -3 -m pip install "psutil>=7.2"
if errorlevel 1 goto :install_failed
set "PY=py -3"
goto :resolve_pythonw

:install_with_python
echo [INFO] Installing psutil ...
python -m pip install "psutil>=7.2"
if errorlevel 1 goto :install_failed
set "PY=python"
goto :resolve_pythonw

:old_python
echo [ERROR] Python 3.12 or newer is required, but found an older version.
echo Please install Python 3.12+ from https://www.python.org/downloads/
pause
exit /b 1

:install_failed
echo [ERROR] psutil install failed.
echo Run manually: python -m pip install "psutil>=7.2"
pause
exit /b 1

:no_python
echo [ERROR] Python 3.12+ not found.
echo Please install Python from https://www.python.org/downloads/
echo and make sure "Add python.exe to PATH" is checked.
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

:probe
"%PYEXE%" "%~dp0launcher_check.py" status > "%TEMP%\localops_status.txt" 2>nul
set /p LSTATUS=<"%TEMP%\localops_status.txt"
del "%TEMP%\localops_status.txt" >nul 2>nul
if not defined LSTATUS set "LSTATUS=STOPPED"
if "%LSTATUS%"=="STOPPED" goto :launch
for /f "tokens=2" %%p in ("%LSTATUS%") do set "LPORT=%%p"
if not defined LPORT goto :launch
REM Already running: open browser directly (tray owns open/restart/stop)
"%PYEXE%" "%~dp0launcher_check.py" open %LPORT%
exit /b 0

:launch
"%PYW%" server.py --log-to-file
echo local-ops started in background.
echo Browser will open automatically.
echo See README for log location.
