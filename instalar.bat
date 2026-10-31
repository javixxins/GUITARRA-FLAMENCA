@echo off
rem Instalador de Guitarra Flamenca para Windows (solo hace falta una vez).
setlocal
cd /d "%~dp0"
title Guitarra Flamenca - instalacion
echo ==^> Guitarra Flamenca - instalacion

rem Python 3.10 (tambien vale 3.9 o 3.11; con 3.12+ aun no funciona la deteccion de notas)
set "PY="
call :find_python
if not defined PY (
  where winget >nul 2>&1
  if not errorlevel 1 (
    echo ==^> No encuentro Python 3.10. Lo instalo con winget...
    winget install -e --id Python.Python.3.10 --scope user --accept-source-agreements --accept-package-agreements
    call :find_python
  )
)
if not defined PY (
  echo.
  echo [X] Necesitas Python 3.10. Se abrira la pagina de descarga:
  echo     instala "Windows installer (64-bit)" y vuelve a abrir este archivo.
  start "" "https://www.python.org/downloads/release/python-31011/"
  if not defined GF_NO_START pause
  exit /b 1
)
echo ==^> Usando %PY%

if not exist ".venv\Scripts\python.exe" (
  %PY% -m venv .venv
  if errorlevel 1 ( echo [X] No se pudo crear el entorno de Python. & (if not defined GF_NO_START pause) & exit /b 1 )
)
".venv\Scripts\python.exe" -m pip install -q --upgrade pip
echo ==^> Instalando dependencias (unos minutos, solo la primera vez)...
".venv\Scripts\python.exe" -m pip install --prefer-binary -r requirements.txt
if errorlevel 1 ( echo [X] Fallo la instalacion de dependencias. Revisa el mensaje de arriba. & (if not defined GF_NO_START pause) & exit /b 1 )

echo ==^> Creando el acceso directo "Guitarra Flamenca" en el Escritorio...
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$d=[Environment]::GetFolderPath('Desktop'); $s=(New-Object -ComObject WScript.Shell).CreateShortcut((Join-Path $d 'Guitarra Flamenca.lnk'));" ^
  "$s.TargetPath='%~dp0Iniciar Guitarra Flamenca.bat'; $s.WorkingDirectory='%~dp0'; $s.IconLocation='%~dp0icon.ico'; $s.WindowStyle=7; $s.Save()"

echo.
echo [OK] Instalacion terminada. Desde ahora abre la app con el icono "Guitarra Flamenca" del Escritorio.
if not defined GF_NO_START call "%~dp0Iniciar Guitarra Flamenca.bat"
exit /b 0

:find_python
for %%V in (3.10 3.11 3.9) do (
  if not defined PY (
    py -%%V -c "import sys" >nul 2>&1 && set "PY=py -%%V"
  )
)
if not defined PY (
  for %%P in ("%LOCALAPPDATA%\Programs\Python\Python310\python.exe" "%LOCALAPPDATA%\Programs\Python\Python311\python.exe" "%ProgramFiles%\Python310\python.exe" "%ProgramFiles%\Python311\python.exe") do (
    if not defined PY if exist %%P set "PY=%%~P"
  )
)
if not defined PY (
  python -c "import sys; sys.exit(0 if (3,9) <= sys.version_info[:2] <= (3,11) else 1)" >nul 2>&1 && set "PY=python"
)
if defined PY if exist "%PY%" set "PY="%PY%""
exit /b 0
