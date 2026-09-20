# Michi: Google Maps x Mathematical Optimization

自然文から1日の旅行プランを作成するStreamlitアプリです。Geminiで検索条件を構造化し、Google Maps Platformで実在地点・移動時間・交通状況・標高を取得し、Gurobiで巡回順を最適化します。

## Features

- Google Places API (New)で候補地点、評価、カテゴリ、Google Mapsリンクを取得
- Google Routes APIで地点間の移動時間、距離、車移動時の予測渋滞を取得
- Google Elevation APIで上り高低差を計算
- Gurobiで移動、渋滞、推定待ち時間、上り高低差を含むSelective TSPを求解
- 15地点を優先し、時間内に収まる最大訪問数を採用（最低10地点）
- 数理最適化案と、同じ訪問地点を使うGemini参考案を比較
- Google Maps JavaScript APIで両案の順路を表示

## Architecture

1. Gemini API: 自然文をエリア、テーマ、検索語、移動手段へ変換
2. Places API (New):候補地点を検索・重複排除
3. Routes API: 移動時間行列と渋滞遅延を作成
4. Elevation API: 地点間の上り高低差を作成
5. Gurobi: 時間制約付きSelective TSPを求解
6. Maps JavaScript API: マーカーと訪問順を可視化

## Requirements

- Python 3.12
- Google Cloud APIs: Places API (New), Routes API, Elevation API, Maps JavaScript API
- Gemini API key
- Google Maps API key
- Gurobi license（WLSまたはローカルライセンス。利用できない場合はヒューリスティックへフォールバック）

## Local setup

```powershell
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
Copy-Item .env.example .env
```

`.env`へ値を設定するか、PowerShellで環境変数を設定してください。`.env`はGitへコミットしないでください。アプリは環境変数を読み、ローカル開発時のみ同じディレクトリのキー・ライセンスファイルも利用できます。

```powershell
$env:GEMINI_API_KEY = "..."
$env:GOOGLE_MAPS_API_KEY = "..."
$env:GRB_WLSACCESSID = "..."
$env:GRB_WLSSECRET = "..."
$env:GRB_LICENSEID = "..."
.\run_app.ps1
```

ブラウザーで http://localhost:8501 を開きます。BATの場合は`start_app.bat`、停止は`stop_app.bat`です。

## Test

```powershell
.venv\Scripts\python.exe -m pytest tests -q
```

テストは外部APIをモックし、APIエラー処理、Route Matrix分割、到達不能経路、高低差、待ち時間、TSP制約、Gemini参考案の入力制限を確認します。

## Cloud Run

Cloud Runでは、キーやGurobi情報をSecret Managerから環境変数として注入してください。プロジェクトID、リージョン、サービス名、サービスアカウント、Secret名は環境に合わせて変更します。

```powershell
gcloud run deploy <SERVICE_NAME> --source . `
  --project <PROJECT_ID> --region <REGION> `
  --allow-unauthenticated `
  --service-account <SERVICE_ACCOUNT_EMAIL> `
  --cpu 1 --memory 1Gi --min-instances 0 --max-instances 3 --timeout 300 `
  --set-env-vars STREAMLIT_BROWSER_GATHER_USAGE_STATS=false `
  --set-secrets GEMINI_API_KEY=<GEMINI_SECRET>:latest,GOOGLE_MAPS_API_KEY=<MAPS_SECRET>:latest,GRB_LICENSEID=<GRB_LICENSE_ID_SECRET>:latest,GRB_WLSACCESSID=<GRB_ACCESS_ID_SECRET>:latest,GRB_WLSSECRET=<GRB_SECRET_SECRET>:latest
```

必要なSecret Manager権限は、Cloud Run実行サービスアカウントへの`roles/secretmanager.secretAccessor`です。実在のプロジェクトID、Secret名、キー、ライセンス値はこのリポジトリへ記載しないでください。

## Optimization model

辺`(i,j)`の選択を`x_ij`、地点の訪問を`y_i`とし、次のコストを最小化します。

```text
移動時間 + 渋滞遅延 + 推定待ち時間 + 0.1 * 上り標高差(m)
```

出発地への帰着、地点ごとの入出次数、MTZ部分巡回路除去、時間枠、訪問数を制約にします。

## Security

- `.env`、APIキー、ライセンス、ログはコミットしない
- Google APIキーにはAPI制限と適切なアプリケーション制限を設定する
- Cloud RunではSecret Managerを使う
- 公開前にGit履歴とGitHub Secret Scanningを確認する
- 過去にキーが表示された場合はキーをローテーションする
