@echo off
setlocal
set "ROOT_DIR=%~dp0"
set "PYTHON_EXE=%~dp0.venv\Scripts\python.exe"
set "PID_FILE=%~dp0.streamlit.pid"
set "OUT_LOG=%~dp0streamlit_out.log"
set "ERR_LOG=%~dp0streamlit_err.log"

if not exist "%PYTHON_EXE%" (
  echo ERROR: .venv\Scripts\python.exe was not found.
  echo See README.md for setup instructions.
  pause
  exit /b 1
)

powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$connection = Get-NetTCPConnection -LocalPort 8501 -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1;" ^
  "if ($connection) {" ^
  "  $running = Get-CimInstance Win32_Process -Filter ('ProcessId=' + $connection.OwningProcess) -ErrorAction SilentlyContinue;" ^
  "  if ($running -and $running.CommandLine -match '(?i)streamlit.*app\.py') {" ^
  "    [System.IO.File]::WriteAllText('%PID_FILE%', [string]$connection.OwningProcess);" ^
  "    Write-Output ('Michi is already running. PID ' + $connection.OwningProcess);" ^
  "    exit 0;" ^
  "  }" ^
  "  Write-Error 'Port 8501 is already used by another application.'; exit 2;" ^
  "}" ^
  "$arguments = @('-m','streamlit','run','%ROOT_DIR%app.py','--server.address','localhost','--server.port','8501','--server.headless','true');" ^
  "$process = Start-Process -FilePath '%PYTHON_EXE%' -ArgumentList $arguments -WorkingDirectory '%ROOT_DIR%' -WindowStyle Hidden -RedirectStandardOutput '%OUT_LOG%' -RedirectStandardError '%ERR_LOG%' -PassThru;" ^
  "[System.IO.File]::WriteAllText('%PID_FILE%', [string]$process.Id);" ^
  "Write-Output ('Started Michi. PID ' + $process.Id)"

if errorlevel 1 (
  echo Failed to start Michi.
  pause
  exit /b 1
)

timeout /t 3 /nobreak > nul
start "" "http://localhost:8501"
echo Michi: http://localhost:8501
echo Run stop_app.bat to stop the app.
endlocal
