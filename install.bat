@echo off
:: Instalador rapido para Windows: doble click y listo.
:: Se eleva a administrador solo y ejecuta "yt_stream.py install".

net session >nul 2>&1
if %errorlevel% neq 0 (
  powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
  exit /b
)

cd /d "%~dp0"

where python >nul 2>&1
if %errorlevel% neq 0 (
  echo Python no esta instalado. Instalando con winget...
  winget install -e --id Python.Python.3.12 --scope machine --accept-package-agreements --accept-source-agreements
  echo.
  echo Cerra esta ventana y volve a ejecutar install.bat
  pause
  exit /b
)

python yt_stream.py install
echo.
pause
