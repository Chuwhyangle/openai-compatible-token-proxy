@echo off
setlocal

cd /d "%~dp0"

set "PYTHON_EXE=%~dp0.venv\Scripts\python.exe"
if not exist "%PYTHON_EXE%" (
  echo [token-proxy] Python virtual environment was not found.
  echo [token-proxy] Expected: "%PYTHON_EXE%"
  echo [token-proxy] Please create the virtual environment first.
  pause
  exit /b 1
)

if not defined OPENAI_API_KEY if not defined OPENAI_UPSTREAM_API_KEY (
  echo.
  echo [token-proxy] OPENAI_API_KEY is not set.
  set /p OPENAI_API_KEY=Please enter your API key:
)

if not defined OPENAI_UPSTREAM_API_KEY if defined OPENAI_API_KEY (
  set "OPENAI_UPSTREAM_API_KEY=%OPENAI_API_KEY%"
)

for /f "tokens=5" %%P in ('netstat -ano ^| findstr /r /c:":8787 .*LISTENING"') do set "EXISTING_PID=%%P"
if defined EXISTING_PID (
  echo [token-proxy] Port 8787 is already in use by PID %EXISTING_PID%.
  echo [token-proxy] If this is your running proxy, open:
  echo [token-proxy] http://127.0.0.1:8787/dashboard
  pause
  exit /b 0
)

echo.
echo [token-proxy] Starting local proxy on http://127.0.0.1:8787
echo [token-proxy] Dashboard: http://127.0.0.1:8787/dashboard
echo.

"%PYTHON_EXE%" -m uvicorn app.main:app --env-file .env --host 127.0.0.1 --port 8787

set "EXIT_CODE=%ERRORLEVEL%"
echo.
echo [token-proxy] Server stopped with exit code %EXIT_CODE%.
pause
exit /b %EXIT_CODE%
