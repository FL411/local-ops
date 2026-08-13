@echo off
setlocal
title Build LocalOps Console Launcher
cd /d "%~dp0"

set "CSC=C:\Windows\Microsoft.NET\Framework64\v4.0.30319\csc.exe"
if not exist "%CSC%" set "CSC=C:\Windows\Microsoft.NET\Framework\v4.0.30319\csc.exe"
if not exist "%CSC%" (
  echo [ERROR] .NET Framework 4.x compiler not found.
  pause
  exit /b 1
)

"%CSC%" /nologo /target:winexe /out:"..\LocalOpsConsole.exe" /r:System.Windows.Forms.dll launcher.cs
if errorlevel 1 (
  echo [ERROR] Compile failed.
  pause
  exit /b 1
)

echo OK: LocalOpsConsole.exe created in project root.
echo Double-click it to start the console without a window.
pause
