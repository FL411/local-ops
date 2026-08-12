@echo off
setlocal
title local-ops Console
cd /d "%~dp0"

set "PYEXE=C:\Program Files\Python312\python.exe"
if exist "%PYEXE%" (
  "%PYEXE%" -c "import psutil" >nul 2>nul
  if not errorlevel 1 goto :resolve_pythonw_from_exe
)

set "PY="
where py >nul 2>nul
if errorlevel 1 goto :find_python
py -3.12 -c "import psutil" >nul 2>nul
if errorlevel 1 goto :find_python
set "PY=py -3.12"
goto :resolve_pythonw

:find_python
where python >nul 2>nul
if errorlevel 1 goto :no_python
python -c "import psutil" >nul 2>nul
if errorlevel 1 goto :install_psutil
set "PY=python"
goto :resolve_pythonw

:install_psutil
where py >nul 2>nul
if errorlevel 1 goto :install_with_python
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

:launch
start "" "%PYW%" server.py --log-to-file
echo local-ops started in background.
echo Browser will open automatically.
echo See README for log location.
