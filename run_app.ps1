$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $Root

if (-not (Test-Path ".venv\Scripts\python.exe")) {
    throw ".venv がありません。README.md のセットアップ手順を実行してください。"
}

& ".venv\Scripts\python.exe" -m streamlit run app.py --server.address localhost --server.port 8501
