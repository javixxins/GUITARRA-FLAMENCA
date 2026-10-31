@echo off
rem Detiene Guitarra Flamenca (Windows).
cd /d "%~dp0"
if exist .port (
  set /p PORT=<.port
) else (
  echo ==^> No estaba en marcha.
  timeout /t 3 >nul
  exit /b 0
)
curl -sf -m 2 -X POST http://127.0.0.1:%PORT%/api/quit >nul 2>&1
if errorlevel 1 if exist .pid (
  set /p GPID=<.pid
  call taskkill /PID %%GPID%% /F >nul 2>&1
)
del /q .port .pid 2>nul
echo ==^> Guitarra Flamenca parada.
timeout /t 3 >nul
