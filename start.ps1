# 一条命令起服务（Windows）。
#   .\start.ps1            默认 8000 端口
#   .\start.ps1 -Port 8080
param([int]$Port = 8000)

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

if (-not (Test-Path ".env")) {
    Copy-Item ".env.example" ".env"
    Write-Host "[!] 已生成 .env，请先填入 LLM_API_KEY 再重跑" -ForegroundColor Yellow
    exit 1
}

# 索引目录名要和 app 读到的 TAG 一致，否则脚本查一个目录、服务开另一个，起来就报索引不存在。
$tag = $env:TAG
if (-not $tag) {
    $line = Select-String -Path ".env" -Pattern "^TAG=" -ErrorAction SilentlyContinue | Select-Object -Last 1
    if ($line) { $tag = ($line.Line -split "=", 2)[1].Trim() }
}
if (-not $tag) { $tag = "prod" }
$env:TAG = $tag

if (-not (Test-Path "rag_storage/$tag")) {
    Write-Host "[*] 没有索引，先建（约 3 分钟，¥1 左右）..." -ForegroundColor Cyan
    python run_index.py --input seed --pages 0 --tag $tag --gleaning 0
}

Write-Host "[*] http://127.0.0.1:$Port （索引 tag=$tag）" -ForegroundColor Green
python -m uvicorn app.main:app --host 127.0.0.1 --port $Port
