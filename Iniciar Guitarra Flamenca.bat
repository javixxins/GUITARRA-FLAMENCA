@echo off
rem Arranca Guitarra Flamenca y la abre en el navegador (Windows).
rem Usa el primer puerto libre entre 8850 y 8859.
setlocal
cd /d "%~dp0"
title Guitarra Flamenca

if not exist ".venv\Scripts\python.exe" (
  echo ==^> Primera vez: instalando...
  call instalar.bat
  exit /b
)

rem ya esta en marcha?
for /L %%P in (8850,1,8859) do (
  curl -s -m 1 http://127.0.0.1:%%P/api/config 2>nul | findstr /C:"guitarra-flamenca" >nul && (
    echo ==^> Guitarra Flamenca ya estaba en marcha en el puerto %%P.
    start "" "http://127.0.0.1:%%P"
    exit /b 0
  )
)

if not exist logs mkdir logs
echo ==^> Arrancando Guitarra Flamenca... (se abrira el navegador)
echo     Deja esta ventana abierta mientras uses la app. Cierrala para pararla.
set PYTHONIOENCODING=utf-8
set PYTHONUTF8=1
".venv\Scripts\python.exe" -W ignore server.py
echo.
echo La app se ha detenido. Si ha sido por un error, el motivo aparece arriba.
pause
