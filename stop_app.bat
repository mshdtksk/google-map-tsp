@echo off
setlocal
set "PID_FILE=%~dp0.streamlit.pid"

powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$processIds = @();" ^
  "if (Test-Path '%PID_FILE%') {" ^
  "  $savedId = (Get-Content '%PID_FILE%' -Raw).Trim();" ^
  "  if ($savedId -match '^\d+$') { $processIds += [int]$savedId }" ^
  "}" ^
  "$connection = Get-NetTCPConnection -LocalPort 8501 -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1;" ^
  "if ($connection) { $processIds += [int]$connection.OwningProcess }" ^
  "$stopped = 0;" ^
  "foreach ($processId in ($processIds | Select-Object -Unique)) {" ^
  "  $process = Get-CimInstance Win32_Process -Filter ('ProcessId=' + $processId) -ErrorAction SilentlyContinue;" ^
  "  if ($process -and $process.CommandLine -match '(?i)streamlit.*app\.py') {" ^
  "    Stop-Process -Id $processId -Force;" ^
  "    Write-Output ('Stopped Michi. PID ' + $processId);" ^
  "    $stopped++;" ^
  "  }" ^
  "}" ^
  "Remove-Item '%PID_FILE%' -Force -ErrorAction SilentlyContinue;" ^
  "if ($stopped -eq 0) { Write-Output 'Michi is not running.' }"

endlocal
